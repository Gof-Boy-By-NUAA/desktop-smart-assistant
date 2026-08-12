"""评测专用技能注入、配对运行和独立证据复算。"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from agent.skills.governance import (
    GovernedSkillRepository,
    SkillStatus,
    SkillVersion,
)

from .attestation import (
    execution_attestation_payload,
    judgment_attestation_payload,
    release_identity_sha256,
    release_record,
    verify_ed25519_signature,
)
from .contracts import (
    CustomerExecutionResult,
    CustomerExecutionRequest,
    CustomerJudgment,
    CustomerPackage,
    CustomerPackageError,
    CustomerJudgmentRequest,
)
from .evidence import EventChain
from .executor import (
    CustomerCaseExecutor,
    execution_request_sha256,
    execution_snapshot_sha256,
)
from .json_utils import canonical_json_bytes, clean_sha256, clean_text, sha256_json
from .judge import CustomerCaseJudge, DeterministicCustomerCaseJudge
from .ledger import CustomerExecutionLedger
from .verify import verify_customer_report


_BOOTSTRAP_ROUNDS = 5000


class ControlledCustomerAcceptanceRunner:
    """只在客户验收环境内执行无技能与单技能配对测试。"""

    _RUNNER_ID = "controlled-customer-skill-injection"
    _RUNNER_VERSION = "1.1.0"

    def __init__(
        self,
        repository: GovernedSkillRepository,
        tenant_id: str,
        executor: CustomerCaseExecutor,
        judge: Optional[CustomerCaseJudge] = None,
        *,
        execution_ledger_path: Path,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.tenant_id = clean_text(tenant_id, "tenant_id")
        if not isinstance(executor, CustomerCaseExecutor):
            raise CustomerPackageError("executor 必须实现 CustomerCaseExecutor")
        self.executor = executor
        self.judge = judge
        self.execution_ledger_path = Path(execution_ledger_path)
        self._fault_injector = fault_injector

    def _inject_fault(self, point: str) -> None:
        """Invoke deterministic test-only crash injection at a durable boundary."""

        if self._fault_injector is not None:
            self._fault_injector(point)

    def run(
        self,
        customer_package: CustomerPackage,
        *,
        skill_id: str,
        skill_version: int,
        expected_skill_content_sha256: str,
    ) -> Dict[str, Any]:
        """Run one content-addressed customer package without replaying effects."""

        if customer_package.tenant_id != self.tenant_id:
            raise CustomerPackageError("customer package tenant does not match runner")
        skill = self._load_skill(
            customer_package,
            skill_id,
            skill_version,
            expected_skill_content_sha256,
        )
        judge = self._select_judge(customer_package)
        self._assert_attestation_identities(customer_package, judge)
        implementation_sha256 = _implementation_fingerprint()
        skill_payload = _skill_payload(skill)
        run_binding_sha256 = _run_binding_sha256(
            customer_package,
            skill,
            judge,
            implementation_sha256,
            self.executor,
            self._RUNNER_ID,
            self._RUNNER_VERSION,
        )
        run_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "smart-assistant-customer-acceptance:" + run_binding_sha256,
            )
        )
        self._assert_skill_unchanged(skill)
        operation_plan = _execution_operation_plan(
            customer_package,
            self.tenant_id,
            run_id,
            skill_payload,
        )
        ledger = CustomerExecutionLedger(self.execution_ledger_path)
        run_claim = ledger.claim_run(
            customer_package.manifest_sha256,
            customer_package.cases_sha256,
            run_id,
            run_binding_sha256,
            implementation_sha256,
            operation_plan,
        )
        if run_claim["claim_status"] == "completed":
            # The report was committed before the previous caller was told that
            # the run completed. Returning the byte-equivalent report is safe;
            # re-invoking either external dependency is not.
            completed_report = dict(run_claim["report"])
            if verify_customer_report(completed_report, customer_package):
                raise CustomerPackageError(
                    "completed customer report failed independent verification"
                )
            return completed_report
        if run_claim["claim_status"] not in {"claimed", "resumable"}:
            ledger_record = ledger.describe_run(
                customer_package.manifest_sha256,
                customer_package.cases_sha256,
            )
            if ledger_record is None:
                raise CustomerPackageError(
                    "customer execution ledger lost its claimed run"
                )
            return _in_doubt_customer_report(
                customer_package,
                run_id,
                ledger_record,
            )

        chain = EventChain()
        chain.append(
            "run.started",
            {
                "run_id": run_id,
                "package_id": customer_package.package_id,
                "manifest_sha256": customer_package.manifest_sha256,
                "cases_sha256": customer_package.cases_sha256,
                "model_id": customer_package.model_id,
                "model_parameters_sha256": sha256_json(
                    customer_package.model_parameters
                ),
                "endpoint_sha256": customer_package.endpoint_sha256,
                "prompt_sha256": customer_package.prompt_sha256,
                "tools_sha256": customer_package.tools_sha256,
                "comparison_environment_sha256": (
                    customer_package.comparison_environment_sha256
                ),
                "baseline_release": release_record(
                    customer_package.baseline_release
                ),
                "candidate_release": release_record(
                    customer_package.candidate_release
                ),
                "skill_id": skill.skill_id,
                "skill_version": skill.version,
                "skill_content_sha256": skill.content_hash,
                "executor_id": clean_text(
                    self.executor.executor_id, "executor_id", 128
                ),
                "executor_version": clean_text(
                    self.executor.executor_version, "executor_version", 128
                ),
                "executor_artifact_sha256": (
                    customer_package.executor_artifact_sha256
                ),
                "judge_id": clean_text(judge.judge_id, "judge_id", 128),
                "judge_version": clean_text(
                    judge.judge_version, "judge_version", 128
                ),
                "judge_artifact_sha256": customer_package.judge_artifact_sha256,
                "implementation_sha256": implementation_sha256,
            },
        )
        try:
            for case in customer_package.cases:
                arms = ("baseline", "candidate")
                if int(case.case_sha256[-1], 16) % 2:
                    arms = tuple(reversed(arms))
                blind_labels = _blind_labels(run_id, case.case_id, arms)
                for arm in arms:
                    if arm == "candidate":
                        self._assert_skill_unchanged(skill)
                    request = CustomerExecutionRequest(
                        run_id=run_id,
                        case_id=case.case_id,
                        arm=arm,
                        tenant_id=self.tenant_id,
                        model_id=customer_package.model_id,
                        model_parameters=customer_package.model_parameters,
                        endpoint_sha256=customer_package.endpoint_sha256,
                        prompt_sha256=customer_package.prompt_sha256,
                        tools_sha256=customer_package.tools_sha256,
                        comparison_environment_sha256=(
                            customer_package.comparison_environment_sha256
                        ),
                        requested_release_identity_sha256=(
                            release_identity_sha256(
                                customer_package.candidate_release
                                if arm == "candidate"
                                else customer_package.baseline_release
                            )
                        ),
                        case_input=case.case_input,
                        skill=skill_payload if arm == "candidate" else None,
                    )
                    request_sha256 = execution_request_sha256(request)
                    operation_claim = ledger.claim_case_operation(
                        customer_package.manifest_sha256,
                        customer_package.cases_sha256,
                        run_id,
                        case.case_id,
                        arm,
                        request_sha256,
                    )
                    operation_state = operation_claim["state"]
                    if operation_claim["claim_status"] != "claimed" and (
                        operation_state
                        not in {"execution_receipt", "completed"}
                    ):
                        ledger.mark_run_in_doubt(
                            customer_package.manifest_sha256,
                            customer_package.cases_sha256,
                            run_id,
                            "a prior case-arm intent already exists",
                        )
                        return _in_doubt_customer_report(
                            customer_package,
                            run_id,
                            ledger.describe_run(
                                customer_package.manifest_sha256,
                                customer_package.cases_sha256,
                            ),
                        )
                    recovered_receipts = None
                    if operation_claim["claim_status"] != "claimed":
                        recovered_receipts = ledger.load_operation_receipts(
                            customer_package.manifest_sha256,
                            customer_package.cases_sha256,
                            run_id,
                            case.case_id,
                            arm,
                            request_sha256,
                        )
                    try:
                        if recovered_receipts is None:
                            self._inject_fault("before_effect")
                            result = self.executor.execute(request)
                            self._inject_fault("after_effect_before_receipt")
                            _validate_execution_result(
                                result, request, customer_package
                            )
                            ledger.record_execution_receipt(
                                customer_package.manifest_sha256,
                                customer_package.cases_sha256,
                                run_id,
                                case.case_id,
                                arm,
                                request_sha256,
                                receipt=_execution_receipt(result),
                            )
                        else:
                            result = _execution_result_from_receipt(
                                recovered_receipts["execution_receipt"]
                            )
                            _validate_execution_result(
                                result, request, customer_package
                            )

                        if recovered_receipts is not None and operation_state == "completed":
                            judgment = _judgment_from_receipt(
                                recovered_receipts["judgment_receipt"]
                            )
                            _validate_judgment(
                                judgment,
                                request,
                                blind_labels[arm],
                                customer_package,
                                result,
                            )
                        else:
                            if arm == "candidate":
                                self._assert_skill_unchanged(skill)
                            ledger.begin_judgment(
                                customer_package.manifest_sha256,
                                customer_package.cases_sha256,
                                run_id,
                                case.case_id,
                                arm,
                                request_sha256,
                            )
                            judgment = judge.judge(
                                CustomerJudgmentRequest(
                                    run_id=run_id,
                                    case_id=case.case_id,
                                    arm_label=blind_labels[arm],
                                    oracle_id=customer_package.oracle_id,
                                    oracle=case.oracle,
                                    output=result.output,
                                )
                            )
                            if not isinstance(judgment, CustomerJudgment):
                                raise CustomerPackageError(
                                    "independent judge must return CustomerJudgment"
                                )
                            if not isinstance(judgment.success, bool):
                                raise CustomerPackageError(
                                    "independent judge success must be boolean"
                                )
                            canonical_json_bytes(judgment.evidence)
                            _validate_judgment(
                                judgment,
                                request,
                                blind_labels[arm],
                                customer_package,
                                result,
                            )
                            ledger.record_completed_operation(
                                customer_package.manifest_sha256,
                                customer_package.cases_sha256,
                                run_id,
                                case.case_id,
                                arm,
                                request_sha256,
                                judgment_receipt=_judgment_receipt(judgment),
                            )
                        self._inject_fault("after_receipt_before_chain")
                    except BaseException as error:
                        try:
                            ledger.mark_case_in_doubt(
                                customer_package.manifest_sha256,
                                customer_package.cases_sha256,
                                run_id,
                                case.case_id,
                                arm,
                                request_sha256,
                                type(error).__name__,
                            )
                        except CustomerPackageError:
                            # The original crash is more informative; the durable
                            # intent already prevents a future automatic replay.
                            pass
                        raise
                    chain.append(
                        "case.executed",
                        _case_event_payload(
                            case,
                            arm,
                            blind_labels[arm],
                            request,
                            result,
                            judgment,
                        ),
                    )

            self._assert_skill_unchanged(skill)
            metrics = _metrics_from_events(
                chain.events,
                customer_package.manifest_sha256,
                customer_package.cases_sha256,
            )
            gates = _build_gates(metrics, customer_package)
            passed = all(bool(gate["passed"]) for gate in gates)
            chain.append(
                "run.finished",
                {
                    "run_id": run_id,
                    "metrics_sha256": sha256_json(metrics),
                    "gates_sha256": sha256_json(gates),
                    "passed": passed,
                },
            )
            report = _completed_customer_report(
                customer_package,
                skill,
                skill_payload,
                implementation_sha256,
                metrics,
                gates,
                passed,
                run_id,
                chain,
                self._RUNNER_ID,
                self._RUNNER_VERSION,
            )
            if verify_customer_report(report, customer_package):
                raise CustomerPackageError(
                    "generated customer report failed independent verification"
                )
            self._inject_fault("before_final_report_commit")
            ledger.complete_run(
                customer_package.manifest_sha256,
                customer_package.cases_sha256,
                run_id,
                report,
            )
            return report
        except BaseException as error:
            try:
                if not ledger.is_recoverable_run(
                    customer_package.manifest_sha256,
                    customer_package.cases_sha256,
                    run_id,
                ):
                    ledger.mark_run_in_doubt(
                        customer_package.manifest_sha256,
                        customer_package.cases_sha256,
                        run_id,
                        type(error).__name__,
                    )
            except CustomerPackageError:
                pass
            raise


    def _load_skill(
        self,
        customer_package: CustomerPackage,
        skill_id: str,
        skill_version: int,
        expected_content_sha256: str,
    ) -> SkillVersion:
        skill_id = clean_text(skill_id, "skill_id", 128)
        expected_content_sha256 = clean_sha256(
            expected_content_sha256, "expected_skill_content_sha256"
        )
        if (
            skill_id != customer_package.candidate_skill_id
            or skill_version != customer_package.candidate_skill_version
            or expected_content_sha256
            != customer_package.candidate_skill_content_sha256
        ):
            raise CustomerPackageError("运行候选与客户包固定候选不一致")
        if skill_id not in customer_package.allowed_skill_ids:
            raise CustomerPackageError("技能不在客户包允许列表中")
        if skill_id in customer_package.forbidden_skill_ids:
            raise CustomerPackageError("技能位于客户包禁止列表中")
        if (
            not isinstance(skill_version, int)
            or isinstance(skill_version, bool)
            or skill_version <= 0
        ):
            raise CustomerPackageError("skill_version 必须大于零")
        record = self.repository.read_version(
            self.tenant_id, skill_id, skill_version
        )
        if record.tenant_id != self.tenant_id:
            raise CustomerPackageError("技能事实租户不一致")
        if record.status not in {SkillStatus.CANDIDATE, SkillStatus.ACTIVE}:
            raise CustomerPackageError("只有候选或有效技能可进入受控评测")
        if record.content_hash != expected_content_sha256:
            raise CustomerPackageError("技能正文 SHA-256 与运行请求不一致")
        if customer_package.model_id not in record.model_compatibility:
            raise CustomerPackageError("技能与客户包模型不兼容")
        return record

    def _select_judge(
        self, customer_package: CustomerPackage
    ) -> CustomerCaseJudge:
        if customer_package.oracle_kind == "deterministic":
            if self.judge is not None and not isinstance(
                self.judge, DeterministicCustomerCaseJudge
            ):
                raise CustomerPackageError("确定性客户包必须使用内置判定器")
            return self.judge or DeterministicCustomerCaseJudge()
        if self.judge is None:
            raise CustomerPackageError("客户盲评包缺少独立判定器")
        return self.judge

    def _assert_attestation_identities(
        self,
        customer_package: CustomerPackage,
        judge: CustomerCaseJudge,
    ) -> None:
        if (
            self.executor.executor_id != customer_package.executor_id
            or self.executor.executor_version
            != customer_package.executor_version
        ):
            raise CustomerPackageError("执行器身份与客户包证明不一致")
        if customer_package.oracle_kind == "customer_blind_review" and (
            judge.judge_id != customer_package.judge_id
            or judge.judge_version != customer_package.judge_version
        ):
            raise CustomerPackageError("判定器身份与客户包证明不一致")

    def _assert_skill_unchanged(self, expected: SkillVersion) -> None:
        """在候选臂执行前后回查治理状态和正文哈希。"""

        current = self.repository.read_version(
            self.tenant_id, expected.skill_id, expected.version
        )
        if (
            current.status is not expected.status
            or current.content_hash != expected.content_hash
        ):
            raise CustomerPackageError("技能状态或正文在评测期间发生变化")


def pending_customer_report(missing_inputs: Sequence[str]) -> Dict[str, Any]:
    """缺少外部客户输入时生成不可误判为通过的报告。"""

    return {
        "schema_version": 1,
        "status": "pending_customer_inputs",
        "passed": False,
        "missing_inputs": sorted(set(missing_inputs)),
    }


def _package_summary(customer_package: CustomerPackage) -> Dict[str, Any]:
    return {
        "package_id": customer_package.package_id,
        "tenant_id": customer_package.tenant_id,
        "manifest_sha256": customer_package.manifest_sha256,
        "cases_sha256": customer_package.cases_sha256,
        "case_count": len(customer_package.cases),
    }


def _run_binding_sha256(
    customer_package: CustomerPackage,
    skill: SkillVersion,
    judge: CustomerCaseJudge,
    implementation_sha256: str,
    executor: CustomerCaseExecutor,
    runner_id: str,
    runner_version: str,
) -> str:
    """Hash every immutable identity that makes an execution replay-safe.

    The package hashes are necessary but not sufficient: a completed report
    must not be returned as if it belonged to a changed governed skill, runner,
    executor identity, or judge identity.  Conversely, a different binding is
    deliberately not a request to re-run the old customer package; the ledger
    will return an explicit in_doubt result.
    """

    return sha256_json(
        {
            "schema_version": 1,
            "package": {
                "manifest_sha256": customer_package.manifest_sha256,
                "cases_sha256": customer_package.cases_sha256,
                "tenant_id": customer_package.tenant_id,
                "baseline_release_identity_sha256": release_identity_sha256(
                    customer_package.baseline_release
                ),
                "candidate_release_identity_sha256": release_identity_sha256(
                    customer_package.candidate_release
                ),
                "comparison_environment_sha256": (
                    customer_package.comparison_environment_sha256
                ),
                "oracle_id": customer_package.oracle_id,
                "oracle_kind": customer_package.oracle_kind,
            },
            "skill": {
                "skill_id": skill.skill_id,
                "version": skill.version,
                "status": skill.status.value,
                "content_sha256": skill.content_hash,
            },
            "executor": {
                "executor_id": clean_text(executor.executor_id, "executor_id", 128),
                "executor_version": clean_text(
                    executor.executor_version, "executor_version", 128
                ),
                "executor_artifact_sha256": customer_package.executor_artifact_sha256,
            },
            "judge": {
                "judge_id": clean_text(judge.judge_id, "judge_id", 128),
                "judge_version": clean_text(judge.judge_version, "judge_version", 128),
                "judge_artifact_sha256": customer_package.judge_artifact_sha256,
            },
            "runner": {
                "runner_id": clean_text(runner_id, "runner_id", 128),
                "runner_version": clean_text(runner_version, "runner_version", 128),
                "implementation_sha256": clean_sha256(
                    implementation_sha256, "implementation_sha256"
                ),
            },
        }
    )


def _execution_operation_plan(
    customer_package: CustomerPackage,
    tenant_id: str,
    run_id: str,
    skill_payload: Dict[str, Any],
) -> List[Dict[str, str]]:
    """Materialize every permitted external operation before the first call.

    The durable ledger receives this exact plan in its initial transaction, so
    a caller cannot later close a run with a partial or invented case-arm set.
    Planning is local and has no externally visible side effect.
    """

    plan: List[Dict[str, str]] = []
    for case in customer_package.cases:
        arms = ("baseline", "candidate")
        if int(case.case_sha256[-1], 16) % 2:
            arms = tuple(reversed(arms))
        for arm in arms:
            request = CustomerExecutionRequest(
                run_id=run_id,
                case_id=case.case_id,
                arm=arm,
                tenant_id=tenant_id,
                model_id=customer_package.model_id,
                model_parameters=customer_package.model_parameters,
                endpoint_sha256=customer_package.endpoint_sha256,
                prompt_sha256=customer_package.prompt_sha256,
                tools_sha256=customer_package.tools_sha256,
                comparison_environment_sha256=(
                    customer_package.comparison_environment_sha256
                ),
                requested_release_identity_sha256=release_identity_sha256(
                    customer_package.candidate_release
                    if arm == "candidate"
                    else customer_package.baseline_release
                ),
                case_input=case.case_input,
                skill=skill_payload if arm == "candidate" else None,
            )
            plan.append(
                {
                    "case_id": case.case_id,
                    "arm": arm,
                    "request_sha256": execution_request_sha256(request),
                }
            )
    return plan


def _in_doubt_customer_report(
    customer_package: CustomerPackage,
    run_id: str,
    ledger_record: Dict[str, Any],
) -> Dict[str, Any]:
    """Render a negative-only, structurally verifiable recovery result."""

    if not isinstance(ledger_record, dict):
        raise CustomerPackageError("customer ledger recovery record is invalid")
    state = ledger_record.get("state")
    if state not in {"running", "completed", "in_doubt"}:
        raise CustomerPackageError("customer ledger recovery state is invalid")
    operation_count = ledger_record.get("operation_count")
    operation_states = ledger_record.get("operation_states")
    if (
        not isinstance(operation_count, int)
        or isinstance(operation_count, bool)
        or operation_count < 0
        or not isinstance(operation_states, dict)
    ):
        raise CustomerPackageError("customer ledger recovery counters are invalid")
    normalized_states: Dict[str, int] = {}
    for name, count in operation_states.items():
        if name not in {
            "planned",
            "intent",
            "execution_receipt",
            "judgment_intent",
            "completed",
            "in_doubt",
        }:
            raise CustomerPackageError("customer ledger operation state is invalid")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
        ):
            raise CustomerPackageError("customer ledger operation count is invalid")
        normalized_states[name] = count
    if sum(normalized_states.values()) != operation_count:
        raise CustomerPackageError("customer ledger recovery counters disagree")
    detail = ledger_record.get("detail")
    if detail is not None and (
        not isinstance(detail, str)
        or len(detail) > 512
        or any(character in detail for character in ("\x00", "\r", "\n"))
    ):
        raise CustomerPackageError("customer ledger recovery detail is invalid")
    return {
        "schema_version": 1,
        "status": "in_doubt",
        "passed": False,
        "run_id": clean_text(run_id, "run_id", 128),
        "package": _package_summary(customer_package),
        "ledger": {
            "state": state,
            "operation_count": operation_count,
            "operation_states": normalized_states,
            "detail": detail,
        },
    }


def _execution_receipt(result: CustomerExecutionResult) -> Dict[str, Any]:
    """Persist the verified result required to recover without an executor call."""

    return {
        "schema_version": 2,
        "output": result.output,
        "latency_ms": float(result.latency_ms),
        "cpu_time_ms": float(result.cpu_time_ms),
        "peak_rss_bytes": result.peak_rss_bytes,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "execution_snapshot_sha256": result.execution_snapshot_sha256,
        "request_sha256": result.request_sha256,
        "requested_release_identity_sha256": (
            result.requested_release_identity_sha256
        ),
        "observed_release_identity_sha256": result.observed_release_identity_sha256,
        "executor_artifact_sha256": result.executor_artifact_sha256,
        "attestation_signature": result.attestation_signature,
    }


def _judgment_receipt(judgment: CustomerJudgment) -> Dict[str, Any]:
    """Persist the verified judgment required to rebuild the event chain."""

    return {
        "schema_version": 2,
        "success": judgment.success,
        "evidence": judgment.evidence,
        "judge_artifact_sha256": judgment.judge_artifact_sha256,
        "attestation_signature": judgment.attestation_signature,
    }


def _execution_result_from_receipt(receipt: Any) -> CustomerExecutionResult:
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "output",
        "latency_ms",
        "cpu_time_ms",
        "peak_rss_bytes",
        "input_tokens",
        "output_tokens",
        "execution_snapshot_sha256",
        "request_sha256",
        "requested_release_identity_sha256",
        "observed_release_identity_sha256",
        "executor_artifact_sha256",
        "attestation_signature",
    } or receipt["schema_version"] != 2:
        raise CustomerPackageError("customer execution receipt is invalid")
    return CustomerExecutionResult(
        output=receipt["output"],
        latency_ms=receipt["latency_ms"],
        cpu_time_ms=receipt["cpu_time_ms"],
        peak_rss_bytes=receipt["peak_rss_bytes"],
        input_tokens=receipt["input_tokens"],
        output_tokens=receipt["output_tokens"],
        execution_snapshot_sha256=receipt["execution_snapshot_sha256"],
        request_sha256=receipt["request_sha256"],
        requested_release_identity_sha256=receipt[
            "requested_release_identity_sha256"
        ],
        observed_release_identity_sha256=receipt[
            "observed_release_identity_sha256"
        ],
        executor_artifact_sha256=receipt["executor_artifact_sha256"],
        attestation_signature=receipt["attestation_signature"],
    )


def _judgment_from_receipt(receipt: Any) -> CustomerJudgment:
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "success",
        "evidence",
        "judge_artifact_sha256",
        "attestation_signature",
    } or receipt["schema_version"] != 2:
        raise CustomerPackageError("customer judgment receipt is invalid")
    return CustomerJudgment(
        success=receipt["success"],
        evidence=receipt["evidence"],
        judge_artifact_sha256=receipt["judge_artifact_sha256"],
        attestation_signature=receipt["attestation_signature"],
    )


def _case_event_payload(
    case: Any,
    arm: str,
    arm_label: str,
    request: CustomerExecutionRequest,
    result: CustomerExecutionResult,
    judgment: CustomerJudgment,
) -> Dict[str, Any]:
    """Keep the independently verified completed-report event schema stable."""

    return {
        "case_id": case.case_id,
        "case_sha256": case.case_sha256,
        "critical": case.critical,
        "arm": arm,
        "arm_label": arm_label,
        "success": judgment.success,
        "latency_ms": result.latency_ms,
        "cpu_time_ms": result.cpu_time_ms,
        "peak_rss_bytes": result.peak_rss_bytes,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "execution_snapshot_sha256": result.execution_snapshot_sha256,
        "request_sha256": result.request_sha256,
        "requested_release_identity_sha256": (
            result.requested_release_identity_sha256
        ),
        "observed_release_identity_sha256": (
            result.observed_release_identity_sha256
        ),
        "executor_artifact_sha256": result.executor_artifact_sha256,
        "executor_attestation_signature": result.attestation_signature,
        "injection_sha256": sha256_json(request.skill),
        "output_sha256": sha256_json(result.output),
        "oracle_evidence_sha256": sha256_json(judgment.evidence),
        "judge_artifact_sha256": judgment.judge_artifact_sha256,
        "judge_attestation_signature": judgment.attestation_signature,
    }


def _completed_customer_report(
    customer_package: CustomerPackage,
    skill: SkillVersion,
    skill_payload: Dict[str, Any],
    implementation_sha256: str,
    metrics: Dict[str, Any],
    gates: List[Dict[str, Any]],
    passed: bool,
    run_id: str,
    chain: EventChain,
    runner_id: str,
    runner_version: str,
) -> Dict[str, Any]:
    report = {
        "schema_version": 1,
        "status": "completed",
        "passed": passed,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "package": _package_summary(customer_package),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "runner_id": runner_id,
            "runner_version": runner_version,
            "implementation_sha256": implementation_sha256,
        },
        "skill": {
            "skill_id": skill.skill_id,
            "version": skill.version,
            "status": skill.status.value,
            "content_sha256": skill.content_hash,
            "injection_sha256": sha256_json(skill_payload),
            "payload": skill_payload,
            "content_payload": GovernedSkillRepository.content_payload(skill),
        },
        "metrics": metrics,
        "gates": gates,
        "events": list(chain.events),
        "event_chain_head": chain.head_hash,
    }
    # The first caller and a post-commit replay must observe the same JSON
    # value.  Governance payloads can contain tuples internally, whereas the
    # durable report is intentionally JSON-only and will deserialize as lists.
    return json.loads(canonical_json_bytes(report).decode("utf-8"))


def _skill_payload(skill: SkillVersion) -> Dict[str, Any]:
    """生成只供评测适配器注入的结构化单技能。"""

    return {
        "schema_version": 1,
        "skill_id": skill.skill_id,
        "version": skill.version,
        "content_sha256": skill.content_hash,
        "name": skill.name,
        "description": skill.description,
        "applicability": list(skill.applicability),
        "steps": list(skill.steps),
        "validation_rules": list(skill.validation_rules),
        "contraindications": list(skill.contraindications),
    }


def _validate_execution_result(
    result: CustomerExecutionResult,
    request: CustomerExecutionRequest,
    customer_package: CustomerPackage,
) -> None:
    if not isinstance(result, CustomerExecutionResult):
        raise CustomerPackageError(
            "执行适配器必须返回 CustomerExecutionResult"
        )
    if result.execution_snapshot_sha256 != execution_snapshot_sha256(request):
        raise CustomerPackageError("执行适配器实际环境快照与客户包不一致")
    if result.request_sha256 != execution_request_sha256(request):
        raise CustomerPackageError("执行适配器请求回执与实际请求不一致")
    if (
        result.executor_artifact_sha256
        != customer_package.executor_artifact_sha256
    ):
        raise CustomerPackageError("执行器制品哈希与客户包不一致")
    if (
        result.requested_release_identity_sha256
        != request.requested_release_identity_sha256
        or result.observed_release_identity_sha256
        != request.requested_release_identity_sha256
    ):
        raise CustomerPackageError("执行适配器观测 release 与请求固定 release 不一致")
    for value in (result.input_tokens, result.output_tokens):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise CustomerPackageError("执行适配器令牌数量无效")
    if (
        not isinstance(result.latency_ms, (int, float))
        or isinstance(result.latency_ms, bool)
        or not math.isfinite(float(result.latency_ms))
        or result.latency_ms <= 0
    ):
        raise CustomerPackageError("执行适配器延迟必须是正有限值")
    if (
        not isinstance(result.cpu_time_ms, (int, float))
        or isinstance(result.cpu_time_ms, bool)
        or not math.isfinite(float(result.cpu_time_ms))
        or result.cpu_time_ms <= 0
    ):
        raise CustomerPackageError("执行适配器 CPU 时间必须是正有限值")
    if (
        not isinstance(result.peak_rss_bytes, int)
        or isinstance(result.peak_rss_bytes, bool)
        or result.peak_rss_bytes <= 0
    ):
        raise CustomerPackageError("执行适配器峰值 RSS 必须是正整数")
    canonical_json_bytes(result.output)
    attestation = execution_attestation_payload(
        run_id=request.run_id,
        case_id=request.case_id,
        arm=request.arm,
        request_sha256=result.request_sha256,
        execution_snapshot_sha256=result.execution_snapshot_sha256,
        output_sha256=sha256_json(result.output),
        latency_ms=float(result.latency_ms),
        cpu_time_ms=float(result.cpu_time_ms),
        peak_rss_bytes=result.peak_rss_bytes,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        comparison_environment_sha256=(
            request.comparison_environment_sha256
        ),
        requested_release_identity_sha256=(
            result.requested_release_identity_sha256
        ),
        observed_release_identity_sha256=(
            result.observed_release_identity_sha256
        ),
        executor_artifact_sha256=result.executor_artifact_sha256,
    )
    if not verify_ed25519_signature(
        customer_package.executor_ed25519_public_key,
        result.attestation_signature,
        attestation,
    ):
        raise CustomerPackageError("执行器 Ed25519 证明无效")


def _validate_judgment(
    judgment: CustomerJudgment,
    request: CustomerExecutionRequest,
    arm_label: str,
    customer_package: CustomerPackage,
    result: CustomerExecutionResult,
) -> None:
    if customer_package.oracle_kind == "deterministic":
        if (
            judgment.judge_artifact_sha256 is not None
            or judgment.attestation_signature is not None
        ):
            raise CustomerPackageError("内置确定性判定器不能自报外部证明")
        return
    if (
        judgment.judge_artifact_sha256
        != customer_package.judge_artifact_sha256
        or not isinstance(judgment.attestation_signature, str)
    ):
        raise CustomerPackageError("判定器制品或签名缺失")
    attestation = judgment_attestation_payload(
        run_id=request.run_id,
        case_id=request.case_id,
        arm_label=arm_label,
        oracle_id=customer_package.oracle_id,
        output_sha256=sha256_json(result.output),
        success=judgment.success,
        evidence_sha256=sha256_json(judgment.evidence),
        judge_artifact_sha256=judgment.judge_artifact_sha256,
    )
    if not verify_ed25519_signature(
        customer_package.judge_ed25519_public_key or "",
        judgment.attestation_signature,
        attestation,
    ):
        raise CustomerPackageError("判定器 Ed25519 证明无效")


def _blind_labels(
    run_id: str, case_id: str, arms: Iterable[str]
) -> Dict[str, str]:
    labels = {}
    for arm in arms:
        labels[arm] = hashlib.sha256(
            (run_id + "\0" + case_id + "\0" + arm).encode("utf-8")
        ).hexdigest()[:16]
    return labels


def _metrics_from_events(
    events: Sequence[Dict[str, Any]],
    manifest_sha256: str,
    cases_sha256: str,
) -> Dict[str, Any]:
    by_case: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for event in events:
        if event.get("event_type") != "case.executed":
            continue
        payload = event["payload"]
        by_case.setdefault(payload["case_id"], {})[payload["arm"]] = payload
    pairs = []
    for case_id in sorted(by_case):
        arms = by_case[case_id]
        if set(arms) != {"baseline", "candidate"}:
            raise ValueError("客户场景缺少完整配对臂: %s" % case_id)
        pairs.append((arms["baseline"], arms["candidate"]))
    if not pairs:
        raise ValueError("报告没有客户配对样本")
    baseline_passed = sum(int(pair[0]["success"]) for pair in pairs)
    candidate_passed = sum(int(pair[1]["success"]) for pair in pairs)
    regressions = sum(
        int(pair[0]["success"] and not pair[1]["success"])
        for pair in pairs
    )
    improvements = sum(
        int(not pair[0]["success"] and pair[1]["success"])
        for pair in pairs
    )
    critical_regressions = sum(
        int(
            pair[0]["critical"]
            and pair[0]["success"]
            and not pair[1]["success"]
        )
        for pair in pairs
    )
    baseline_latencies = [float(pair[0]["latency_ms"]) for pair in pairs]
    candidate_latencies = [float(pair[1]["latency_ms"]) for pair in pairs]
    baseline_cpu_times = [float(pair[0]["cpu_time_ms"]) for pair in pairs]
    candidate_cpu_times = [float(pair[1]["cpu_time_ms"]) for pair in pairs]
    baseline_peak_rss = [int(pair[0]["peak_rss_bytes"]) for pair in pairs]
    candidate_peak_rss = [int(pair[1]["peak_rss_bytes"]) for pair in pairs]
    deltas = [
        int(pair[1]["success"]) - int(pair[0]["success"])
        for pair in pairs
    ]
    seed = int(
        hashlib.sha256(
            (manifest_sha256 + cases_sha256).encode("ascii")
        ).hexdigest()[:16],
        16,
    )
    baseline_total_latency_ms = sum(baseline_latencies)
    candidate_total_latency_ms = sum(candidate_latencies)
    baseline_total_cpu_time_ms = sum(baseline_cpu_times)
    candidate_total_cpu_time_ms = sum(candidate_cpu_times)
    baseline_p95_peak_rss_bytes = _percentile(baseline_peak_rss, 95)
    candidate_p95_peak_rss_bytes = _percentile(candidate_peak_rss, 95)
    return {
        "sample_count": len(pairs),
        "baseline_passed": baseline_passed,
        "candidate_passed": candidate_passed,
        "baseline_success_rate": baseline_passed / float(len(pairs)),
        "candidate_success_rate": candidate_passed / float(len(pairs)),
        "success_rate_delta": (candidate_passed - baseline_passed)
        / float(len(pairs)),
        "paired_delta_ci95_lower": _bootstrap_lower_bound(deltas, seed),
        "improvement_count": improvements,
        "regression_count": regressions,
        "critical_regression_count": critical_regressions,
        "baseline_p95_latency_ms": _percentile(baseline_latencies, 95),
        "candidate_p95_latency_ms": _percentile(candidate_latencies, 95),
        "latency_ratio": _percentile(candidate_latencies, 95)
        / max(_percentile(baseline_latencies, 95), 0.000001),
        "baseline_serial_throughput_rps": (
            len(pairs) * 1000.0 / baseline_total_latency_ms
        ),
        "candidate_serial_throughput_rps": (
            len(pairs) * 1000.0 / candidate_total_latency_ms
        ),
        "throughput_ratio": (
            baseline_total_latency_ms / candidate_total_latency_ms
        ),
        "baseline_total_cpu_time_ms": baseline_total_cpu_time_ms,
        "candidate_total_cpu_time_ms": candidate_total_cpu_time_ms,
        "cpu_time_ratio": (
            candidate_total_cpu_time_ms / baseline_total_cpu_time_ms
        ),
        "baseline_p95_peak_rss_bytes": baseline_p95_peak_rss_bytes,
        "candidate_p95_peak_rss_bytes": candidate_p95_peak_rss_bytes,
        "peak_rss_ratio": (
            candidate_p95_peak_rss_bytes / baseline_p95_peak_rss_bytes
        ),
        "total_tokens": sum(
            int(arm["input_tokens"]) + int(arm["output_tokens"])
            for pair in pairs
            for arm in pair
        ),
    }


def _build_gates(
    metrics: Dict[str, Any], customer_package: CustomerPackage
) -> List[Dict[str, Any]]:
    thresholds = customer_package.thresholds
    checks = (
        (
            "data.full_customer_case_set",
            metrics["sample_count"],
            len(customer_package.cases),
            metrics["sample_count"] == len(customer_package.cases),
        ),
        (
            "data.minimum_paired_samples",
            metrics["sample_count"],
            thresholds.minimum_paired_samples,
            metrics["sample_count"] >= thresholds.minimum_paired_samples,
        ),
        (
            "quality.minimum_success_rate_delta",
            metrics["success_rate_delta"],
            thresholds.minimum_success_rate_delta,
            metrics["success_rate_delta"]
            >= thresholds.minimum_success_rate_delta,
        ),
        (
            "quality.paired_ci95_lower_positive",
            metrics["paired_delta_ci95_lower"],
            0.0,
            metrics["paired_delta_ci95_lower"] > 0.0,
        ),
        (
            "quality.maximum_regressions",
            metrics["regression_count"],
            thresholds.maximum_regressions,
            metrics["regression_count"] <= thresholds.maximum_regressions,
        ),
        (
            "safety.critical_regressions",
            metrics["critical_regression_count"],
            0,
            metrics["critical_regression_count"] == 0,
        ),
        (
            "performance.maximum_latency_ratio",
            metrics["latency_ratio"],
            thresholds.maximum_latency_ratio,
            metrics["latency_ratio"] <= thresholds.maximum_latency_ratio,
        ),
        (
            "performance.minimum_serial_throughput_ratio",
            metrics["throughput_ratio"],
            thresholds.minimum_throughput_ratio,
            metrics["throughput_ratio"]
            >= thresholds.minimum_throughput_ratio,
        ),
        (
            "resource.maximum_cpu_time_ratio",
            metrics["cpu_time_ratio"],
            thresholds.maximum_cpu_time_ratio,
            metrics["cpu_time_ratio"] <= thresholds.maximum_cpu_time_ratio,
        ),
        (
            "resource.maximum_peak_rss_ratio",
            metrics["peak_rss_ratio"],
            thresholds.maximum_peak_rss_ratio,
            metrics["peak_rss_ratio"] <= thresholds.maximum_peak_rss_ratio,
        ),
        (
            "cost.maximum_total_tokens",
            metrics["total_tokens"],
            thresholds.maximum_total_tokens,
            metrics["total_tokens"] <= thresholds.maximum_total_tokens,
        ),
    )
    return [
        {
            "name": name,
            "actual": actual,
            "expected": expected,
            "passed": bool(passed),
        }
        for name, actual, expected, passed in checks
    ]


def _bootstrap_lower_bound(deltas: Sequence[int], seed: int) -> float:
    random_source = random.Random(seed)
    count = len(deltas)
    means = []
    for _ in range(_BOOTSTRAP_ROUNDS):
        means.append(
            sum(deltas[random_source.randrange(count)] for _ in range(count))
            / float(count)
        )
    means.sort()
    return means[max(0, math.floor(0.025 * len(means)))]


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (
        position - lower
    )


def _implementation_fingerprint() -> str:
    """Bind every release source, including governed-skill dependencies."""

    from benchmarks.evidence.release_manifest import source_fingerprint

    return source_fingerprint(Path(__file__).resolve().parents[2])
