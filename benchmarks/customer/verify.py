"""不依赖运行聚合代码的客户验收证据验证器。"""

from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .attestation import (
    execution_attestation_payload,
    judgment_attestation_payload,
    release_identity_sha256,
    release_record,
    verify_ed25519_signature,
)
from .contracts import CustomerPackage
from .json_utils import canonical_json_bytes, sha256_json


_BOOTSTRAP_ROUNDS = 5000
_GENESIS_HASH = "0" * 64
_RUNNER_ID = "controlled-customer-skill-injection"
_RUNNER_VERSION = "1.1.0"
_COMPLETED_REPORT_KEYS = {
    "schema_version",
    "status",
    "passed",
    "run_id",
    "generated_at",
    "package",
    "environment",
    "skill",
    "metrics",
    "gates",
    "events",
    "event_chain_head",
}
_IN_DOUBT_REPORT_KEYS = {
    "schema_version",
    "status",
    "passed",
    "run_id",
    "package",
    "ledger",
}
_LEDGER_RUN_STATES = {"running", "completed", "in_doubt"}
_LEDGER_OPERATION_STATES = {
    "planned",
    "intent",
    "execution_receipt",
    "judgment_intent",
    "completed",
    "in_doubt",
}


def verify_customer_report(
    report: Dict[str, Any],
    customer_package: Optional[CustomerPackage] = None,
) -> Tuple[str, ...]:
    """从原始客户包和逐事件记录独立重算全部门禁。"""

    failures: List[str] = []
    if not isinstance(report, dict):
        return ("报告必须是对象",)
    if report.get("status") == "pending_customer_inputs":
        if set(report) != {
            "schema_version", "status", "passed", "missing_inputs"
        }:
            failures.append("待客户输入报告字段无效")
        if report.get("schema_version") != 1:
            failures.append("待客户输入报告版本无效")
        if report.get("passed") is not False:
            failures.append("待客户输入报告不能标记通过")
        missing = report.get("missing_inputs")
        if (
            not isinstance(missing, list)
            or not missing
            or any(not isinstance(item, str) or not item for item in missing)
            or missing != sorted(set(missing))
        ):
            failures.append("待客户输入清单无效")
        return tuple(failures)
    if report.get("status") == "in_doubt":
        return _verify_in_doubt_report(report, customer_package)
    if report.get("status") != "completed":
        return ("报告状态无效",)
    if customer_package is None:
        return ("完整报告复算需要原始客户包",)
    if set(report) != _COMPLETED_REPORT_KEYS:
        failures.append("完整报告字段无效")
    if report.get("schema_version") != 1:
        failures.append("完整报告版本无效")
    run_id = report.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        failures.append("报告 run_id 无效")
    if not isinstance(report.get("passed"), bool):
        failures.append("报告 passed 必须是布尔值")
    if not isinstance(report.get("generated_at"), str):
        failures.append("报告生成时间无效")

    package_summary = report.get("package", {})
    expected_package_summary = {
        "package_id": customer_package.package_id,
        "tenant_id": customer_package.tenant_id,
        "manifest_sha256": customer_package.manifest_sha256,
        "cases_sha256": customer_package.cases_sha256,
        "case_count": len(customer_package.cases),
    }
    if package_summary != expected_package_summary:
        failures.append("报告客户包摘要与原始客户包不一致")

    implementation_sha256 = _implementation_fingerprint()
    environment = report.get("environment")
    if not isinstance(environment, dict) or set(environment) != {
        "python", "platform", "runner_id", "runner_version",
        "implementation_sha256",
    }:
        failures.append("报告执行环境结构无效")
    else:
        if environment.get("runner_id") != _RUNNER_ID:
            failures.append("报告运行器标识无效")
        if environment.get("runner_version") != _RUNNER_VERSION:
            failures.append("报告运行器版本无效")
        if environment.get("implementation_sha256") != implementation_sha256:
            failures.append("报告实现指纹与当前验证源码不一致")

    skill_summary = report.get("skill")
    if not isinstance(skill_summary, dict) or set(skill_summary) != {
        "skill_id", "version", "status", "content_sha256",
        "injection_sha256", "payload", "content_payload",
    }:
        failures.append("报告技能摘要结构无效")
        skill_summary = {}
    else:
        skill_id = skill_summary.get("skill_id")
        if (
            skill_id not in customer_package.allowed_skill_ids
            or skill_id in customer_package.forbidden_skill_ids
        ):
            failures.append("报告技能不满足客户包允许边界")
        if (
            skill_id != customer_package.candidate_skill_id
            or skill_summary.get("version")
            != customer_package.candidate_skill_version
            or skill_summary.get("content_sha256")
            != customer_package.candidate_skill_content_sha256
        ):
            failures.append("报告技能与客户包固定候选不一致")
        if skill_summary.get("status") not in {"candidate", "active"}:
            failures.append("报告技能状态无效")
        if (
            not isinstance(skill_summary.get("version"), int)
            or isinstance(skill_summary.get("version"), bool)
            or skill_summary.get("version", 0) <= 0
        ):
            failures.append("报告技能版本无效")
        for field_name in ("content_sha256", "injection_sha256"):
            if not _is_sha256(skill_summary.get(field_name)):
                failures.append("报告技能 %s 无效" % field_name)
        if (
            not isinstance(skill_summary.get("payload"), dict)
            or sha256_json(skill_summary.get("payload"))
            != skill_summary.get("injection_sha256")
        ):
            failures.append("报告技能注入载荷哈希无效")
        content_payload = skill_summary.get("content_payload")
        if (
            not isinstance(content_payload, dict)
            or sha256_json(content_payload)
            != skill_summary.get("content_sha256")
        ):
            failures.append("报告治理技能正文哈希无效")
        injected = skill_summary.get("payload", {})
        if isinstance(content_payload, dict) and isinstance(injected, dict):
            expected_injected = {
                "schema_version": 1,
                "skill_id": skill_summary.get("skill_id"),
                "version": skill_summary.get("version"),
                "content_sha256": skill_summary.get("content_sha256"),
                "name": content_payload.get("name"),
                "description": content_payload.get("description"),
                "applicability": list(
                    content_payload.get("applicability", ())
                ),
                "steps": list(content_payload.get("steps", ())),
                "validation_rules": list(
                    content_payload.get("validation_rules", ())
                ),
                "contraindications": list(
                    content_payload.get("contraindications", ())
                ),
            }
            if injected != expected_injected:
                failures.append("报告注入技能与治理正文不一致")

    events = report.get("events")
    if not isinstance(events, list):
        return tuple(failures + ["报告事件必须是数组"])
    failures.extend(_verify_chain(events))
    if (
        not events
        or not isinstance(events[-1], dict)
        or report.get("event_chain_head") != events[-1].get("event_hash")
    ):
        failures.append("报告事件链头无效")

    try:
        observations = _collect_observations(
            events,
            customer_package,
            report,
            implementation_sha256,
            failures,
        )
        candidate_hashes = {
            arms["candidate"].get("injection_sha256")
            for arms in observations.values()
            if "candidate" in arms
        }
        if candidate_hashes != {
            report.get("skill", {}).get("injection_sha256")
        }:
            failures.append("报告技能注入哈希与执行事件不一致")
        metrics = _independent_metrics(
            observations,
            customer_package.manifest_sha256,
            customer_package.cases_sha256,
        )
        gates = _independent_gates(metrics, customer_package)
    except Exception as error:
        failures.append("报告事件无法重算: %s" % error)
        return tuple(failures)
    if report.get("metrics") != metrics:
        failures.append("报告汇总指标与事件不一致")
    if report.get("gates") != gates:
        failures.append("报告门禁与独立重算不一致")
    expected_passed = all(bool(gate["passed"]) for gate in gates)
    if report.get("passed") is not expected_passed:
        failures.append("报告通过状态与独立重算不一致")

    finished = [
        event
        for event in events
        if isinstance(event, dict) and event.get("event_type") == "run.finished"
    ]
    if len(finished) != 1:
        failures.append("报告必须包含一个 run.finished 事件")
    else:
        payload = finished[0]["payload"]
        if not isinstance(payload, dict) or set(payload) != {
            "run_id", "metrics_sha256", "gates_sha256", "passed"
        }:
            failures.append("结束事件结构无效")
            return tuple(failures)
        if payload.get("run_id") != report.get("run_id"):
            failures.append("结束事件 run_id 无效")
        if payload.get("metrics_sha256") != sha256_json(metrics):
            failures.append("结束事件指标哈希无效")
        if payload.get("gates_sha256") != sha256_json(gates):
            failures.append("结束事件门禁哈希无效")
        if payload.get("passed") is not expected_passed:
            failures.append("结束事件通过状态无效")
    return tuple(failures)


def _verify_in_doubt_report(
    report: Dict[str, Any],
    customer_package: Optional[CustomerPackage],
) -> Tuple[str, ...]:
    """Validate a negative-only durable-recovery report.

    An unresolved durable intent is not evidence of an accepted customer run.
    It is nevertheless useful to validate its structure so the CLI can expose
    a deterministic failed result instead of relaunching an external executor.
    """

    failures: List[str] = []
    if set(report) != _IN_DOUBT_REPORT_KEYS:
        failures.append("in_doubt report fields are invalid")
    if report.get("schema_version") != 1:
        failures.append("in_doubt report version is invalid")
    if report.get("passed") is not False:
        failures.append("in_doubt report must never pass")
    run_id = report.get("run_id")
    if (
        not isinstance(run_id, str)
        or not run_id
        or run_id != run_id.strip()
        or len(run_id) > 128
        or any(ord(character) < 32 for character in run_id)
    ):
        failures.append("in_doubt report run_id is invalid")

    package = report.get("package")
    expected_package_keys = {
        "package_id",
        "tenant_id",
        "manifest_sha256",
        "cases_sha256",
        "case_count",
    }
    if not isinstance(package, dict) or set(package) != expected_package_keys:
        failures.append("in_doubt report package is invalid")
    elif customer_package is not None:
        expected_package = {
            "package_id": customer_package.package_id,
            "tenant_id": customer_package.tenant_id,
            "manifest_sha256": customer_package.manifest_sha256,
            "cases_sha256": customer_package.cases_sha256,
            "case_count": len(customer_package.cases),
        }
        if package != expected_package:
            failures.append("in_doubt report package does not match customer package")
    else:
        for field_name in ("package_id", "tenant_id"):
            value = package.get(field_name)
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 256
                or any(ord(character) < 32 for character in value)
            ):
                failures.append("in_doubt report package %s is invalid" % field_name)
        for field_name in ("manifest_sha256", "cases_sha256"):
            if not _is_sha256(package.get(field_name)):
                failures.append("in_doubt report package %s is invalid" % field_name)
        case_count = package.get("case_count")
        if (
            not isinstance(case_count, int)
            or isinstance(case_count, bool)
            or case_count <= 0
        ):
            failures.append("in_doubt report package case_count is invalid")

    ledger = report.get("ledger")
    if not isinstance(ledger, dict) or set(ledger) != {
        "state",
        "operation_count",
        "operation_states",
        "detail",
    }:
        failures.append("in_doubt report ledger is invalid")
        return tuple(failures)
    if ledger.get("state") not in _LEDGER_RUN_STATES:
        failures.append("in_doubt report ledger state is invalid")
    operation_count = ledger.get("operation_count")
    operation_states = ledger.get("operation_states")
    if (
        not isinstance(operation_count, int)
        or isinstance(operation_count, bool)
        or operation_count < 0
    ):
        failures.append("in_doubt report operation_count is invalid")
    if not isinstance(operation_states, dict):
        failures.append("in_doubt report operation_states is invalid")
    else:
        total = 0
        for state, count in operation_states.items():
            if state not in _LEDGER_OPERATION_STATES:
                failures.append("in_doubt report operation state is invalid")
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count <= 0
            ):
                failures.append("in_doubt report operation count is invalid")
            elif isinstance(count, int) and not isinstance(count, bool):
                total += count
        if (
            isinstance(operation_count, int)
            and not isinstance(operation_count, bool)
            and total != operation_count
        ):
            failures.append("in_doubt report operation counters disagree")
    detail = ledger.get("detail")
    if detail is not None and (
        not isinstance(detail, str)
        or len(detail) > 512
        or any(character in detail for character in ("\x00", "\r", "\n"))
    ):
        failures.append("in_doubt report ledger detail is invalid")
    return tuple(failures)


def _verify_chain(events: Sequence[Dict[str, Any]]) -> List[str]:
    failures = []
    previous = _GENESIS_HASH
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict) or set(event) != {
            "sequence", "event_type", "payload", "previous_hash", "event_hash"
        }:
            failures.append("事件 %d 结构无效" % index)
            continue
        if event["sequence"] != index:
            failures.append("事件 %d 序号无效" % index)
        if event["previous_hash"] != previous:
            failures.append("事件 %d 前驱哈希无效" % index)
        unsigned = {
            "sequence": event["sequence"],
            "event_type": event["event_type"],
            "payload": event["payload"],
            "previous_hash": event["previous_hash"],
        }
        expected = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
        if event["event_hash"] != expected:
            failures.append("事件 %d 内容哈希无效" % index)
        if not _is_sha256(event["event_hash"]):
            failures.append("事件 %d 哈希格式无效" % index)
        else:
            previous = event["event_hash"]
    return failures


def _collect_observations(
    events: Sequence[Dict[str, Any]],
    customer_package: CustomerPackage,
    report: Dict[str, Any],
    implementation_sha256: str,
    failures: List[str],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    expected_cases = {case.case_id: case for case in customer_package.cases}
    observations: Dict[str, Dict[str, Dict[str, Any]]] = {}
    expected_event_count = len(customer_package.cases) * 2 + 2
    if len(events) != expected_event_count:
        failures.append("报告事件数量与客户场景不一致")
    event_types = [
        event.get("event_type") if isinstance(event, dict) else None
        for event in events
    ]
    if (
        not event_types
        or event_types[0] != "run.started"
        or event_types[-1] != "run.finished"
        or any(item != "case.executed" for item in event_types[1:-1])
    ):
        failures.append("报告事件顺序无效")
    started = [
        event
        for event in events
        if isinstance(event, dict) and event.get("event_type") == "run.started"
    ]
    if len(started) != 1:
        failures.append("报告必须包含一个 run.started 事件")
    else:
        payload = started[0]["payload"]
        expected_started_fields = {
            "run_id", "package_id", "manifest_sha256", "cases_sha256",
            "model_id", "model_parameters_sha256", "endpoint_sha256",
            "prompt_sha256", "tools_sha256", "skill_id", "skill_version",
            "skill_content_sha256", "executor_id", "executor_version",
            "executor_artifact_sha256", "comparison_environment_sha256",
            "baseline_release", "candidate_release", "judge_id",
            "judge_version",
            "judge_artifact_sha256", "implementation_sha256",
        }
        if not isinstance(payload, dict) or set(payload) != expected_started_fields:
            failures.append("开始事件结构无效")
            payload = {}
        skill_summary = report.get("skill", {})
        bindings = {
            "run_id": report.get("run_id"),
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
            "skill_id": skill_summary.get("skill_id"),
            "skill_version": skill_summary.get("version"),
            "skill_content_sha256": skill_summary.get("content_sha256"),
            "executor_id": customer_package.executor_id,
            "executor_version": customer_package.executor_version,
            "executor_artifact_sha256": (
                customer_package.executor_artifact_sha256
            ),
            "implementation_sha256": implementation_sha256,
        }
        if customer_package.oracle_kind == "deterministic":
            bindings.update(
                {
                    "judge_id": "deterministic-json-equality",
                    "judge_version": "1.0.0",
                    "judge_artifact_sha256": None,
                }
            )
        else:
            bindings.update(
                {
                    "judge_id": customer_package.judge_id,
                    "judge_version": customer_package.judge_version,
                    "judge_artifact_sha256": (
                        customer_package.judge_artifact_sha256
                    ),
                }
            )
        for field_name, expected in bindings.items():
            if payload.get(field_name) != expected:
                failures.append("开始事件 %s 绑定无效" % field_name)
    observed_order = []
    for event in events:
        if not isinstance(event, dict) or event.get("event_type") != "case.executed":
            continue
        payload = event.get("payload")
        expected_case_fields = {
            "case_id", "case_sha256", "critical", "arm", "arm_label",
            "success", "latency_ms", "cpu_time_ms", "peak_rss_bytes",
            "input_tokens", "output_tokens",
            "execution_snapshot_sha256", "request_sha256",
            "requested_release_identity_sha256",
            "observed_release_identity_sha256",
            "executor_artifact_sha256", "executor_attestation_signature",
            "injection_sha256", "output_sha256", "oracle_evidence_sha256",
            "judge_artifact_sha256", "judge_attestation_signature",
        }
        if not isinstance(payload, dict) or set(payload) != expected_case_fields:
            failures.append("客户执行事件结构无效")
            continue
        case_id = payload.get("case_id")
        arm = payload.get("arm")
        if case_id not in expected_cases or arm not in {"baseline", "candidate"}:
            failures.append("客户执行事件包含未知场景或臂")
            continue
        observed_order.append((case_id, arm))
        if arm in observations.setdefault(case_id, {}):
            failures.append("客户执行事件包含重复场景臂")
            continue
        case = expected_cases[case_id]
        if payload.get("case_sha256") != case.case_sha256:
            failures.append("客户场景 %s 哈希无效" % case_id)
        if payload.get("critical") is not case.critical:
            failures.append("客户场景 %s 关键标记无效" % case_id)
        if not isinstance(payload.get("success"), bool):
            failures.append("客户场景 %s 成功字段无效" % case_id)
        latency = payload.get("latency_ms")
        if (
            not isinstance(latency, (int, float))
            or isinstance(latency, bool)
            or not math.isfinite(float(latency))
            or latency <= 0
        ):
            failures.append("客户场景 %s 延迟字段无效" % case_id)
        cpu_time = payload.get("cpu_time_ms")
        if (
            not isinstance(cpu_time, (int, float))
            or isinstance(cpu_time, bool)
            or not math.isfinite(float(cpu_time))
            or cpu_time <= 0
        ):
            failures.append("客户场景 %s CPU 时间字段无效" % case_id)
        peak_rss = payload.get("peak_rss_bytes")
        if (
            not isinstance(peak_rss, int)
            or isinstance(peak_rss, bool)
            or peak_rss <= 0
        ):
            failures.append("客户场景 %s 峰值 RSS 字段无效" % case_id)
        for field_name in ("input_tokens", "output_tokens"):
            value = payload.get(field_name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                failures.append(
                    "客户场景 %s %s 无效" % (case_id, field_name)
                )
        for field_name in (
            "execution_snapshot_sha256", "request_sha256", "injection_sha256",
            "output_sha256", "oracle_evidence_sha256",
            "requested_release_identity_sha256",
            "observed_release_identity_sha256",
        ):
            if not _is_sha256(payload.get(field_name)):
                failures.append(
                    "客户场景 %s %s 无效" % (case_id, field_name)
                )
        expected_label = hashlib.sha256(
            (
                str(report.get("run_id")) + "\0" + case_id + "\0" + arm
            ).encode("utf-8")
        ).hexdigest()[:16]
        if payload.get("arm_label") != expected_label:
            failures.append("客户场景 %s 盲化标签无效" % case_id)
        injected_skill = (
            report.get("skill", {}).get("payload")
            if arm == "candidate"
            else None
        )
        expected_release_identity_sha256 = release_identity_sha256(
            customer_package.candidate_release
            if arm == "candidate"
            else customer_package.baseline_release
        )
        expected_request_sha256 = sha256_json(
            {
                "schema_version": 1,
                "run_id": report.get("run_id"),
                "case_id": case_id,
                "arm": arm,
                "tenant_id": customer_package.tenant_id,
                "model": {
                    "id": customer_package.model_id,
                    "parameters": customer_package.model_parameters,
                    "endpoint_sha256": customer_package.endpoint_sha256,
                    "prompt_sha256": customer_package.prompt_sha256,
                    "tools_sha256": customer_package.tools_sha256,
                },
                "comparison_environment_sha256": (
                    customer_package.comparison_environment_sha256
                ),
                "requested_release_identity_sha256": (
                    expected_release_identity_sha256
                ),
                "input": case.case_input,
                "skill": injected_skill,
            }
        )
        if payload.get("request_sha256") != expected_request_sha256:
            failures.append("客户场景 %s 执行请求回执无效" % case_id)
        if (
            payload.get("executor_artifact_sha256")
            != customer_package.executor_artifact_sha256
        ):
            failures.append("客户场景 %s 执行器制品哈希无效" % case_id)
        if (
            payload.get("requested_release_identity_sha256")
            != expected_release_identity_sha256
            or payload.get("observed_release_identity_sha256")
            != expected_release_identity_sha256
        ):
            failures.append("客户场景 %s 观测 release 与臂绑定不一致" % case_id)
        execution_attestation = execution_attestation_payload(
            run_id=str(report.get("run_id")),
            case_id=case_id,
            arm=arm,
            request_sha256=payload.get("request_sha256"),
            execution_snapshot_sha256=payload.get(
                "execution_snapshot_sha256"
            ),
            output_sha256=payload.get("output_sha256"),
            latency_ms=payload.get("latency_ms"),
            cpu_time_ms=payload.get("cpu_time_ms"),
            peak_rss_bytes=payload.get("peak_rss_bytes"),
            input_tokens=payload.get("input_tokens"),
            output_tokens=payload.get("output_tokens"),
            comparison_environment_sha256=(
                customer_package.comparison_environment_sha256
            ),
            requested_release_identity_sha256=payload.get(
                "requested_release_identity_sha256"
            ),
            observed_release_identity_sha256=payload.get(
                "observed_release_identity_sha256"
            ),
            executor_artifact_sha256=payload.get(
                "executor_artifact_sha256"
            ),
        )
        if not verify_ed25519_signature(
            customer_package.executor_ed25519_public_key,
            payload.get("executor_attestation_signature"),
            execution_attestation,
        ):
            failures.append("客户场景 %s 执行器签名无效" % case_id)
        if customer_package.oracle_kind == "deterministic":
            expected_output_sha256 = sha256_json(case.oracle)
            expected_success = payload.get("output_sha256") == expected_output_sha256
            if payload.get("success") is not expected_success:
                failures.append("客户场景 %s 确定性判定无法复算" % case_id)
            expected_evidence = {
                "comparison": "canonical-json-equality",
                "expected_sha256": expected_output_sha256,
                "actual_sha256": payload.get("output_sha256"),
            }
            if payload.get("oracle_evidence_sha256") != sha256_json(
                expected_evidence
            ):
                failures.append("客户场景 %s Oracle 证据哈希无效" % case_id)
            if (
                payload.get("judge_artifact_sha256") is not None
                or payload.get("judge_attestation_signature") is not None
            ):
                failures.append("客户场景 %s 内置 Oracle 证明无效" % case_id)
        else:
            if (
                payload.get("judge_artifact_sha256")
                != customer_package.judge_artifact_sha256
            ):
                failures.append("客户场景 %s 判定器制品哈希无效" % case_id)
            judgment_attestation = judgment_attestation_payload(
                run_id=str(report.get("run_id")),
                case_id=case_id,
                arm_label=payload.get("arm_label"),
                oracle_id=customer_package.oracle_id,
                output_sha256=payload.get("output_sha256"),
                success=payload.get("success"),
                evidence_sha256=payload.get("oracle_evidence_sha256"),
                judge_artifact_sha256=payload.get("judge_artifact_sha256"),
            )
            if not verify_ed25519_signature(
                customer_package.judge_ed25519_public_key or "",
                payload.get("judge_attestation_signature"),
                judgment_attestation,
            ):
                failures.append("客户场景 %s 判定器签名无效" % case_id)
        observations[case_id][arm] = payload

    expected_order = []
    for case in customer_package.cases:
        arms = ("baseline", "candidate")
        if int(case.case_sha256[-1], 16) % 2:
            arms = tuple(reversed(arms))
        expected_order.extend((case.case_id, arm) for arm in arms)
    if observed_order != expected_order:
        failures.append("客户执行事件的配对顺序无效")

    if set(observations) != set(expected_cases):
        failures.append("报告没有覆盖完整客户场景")
    baseline_empty_hash = sha256_json(None)
    expected_execution_snapshot = sha256_json(
        {
            "tenant_id": customer_package.tenant_id,
            "model_id": customer_package.model_id,
            "model_parameters": customer_package.model_parameters,
            "endpoint_sha256": customer_package.endpoint_sha256,
            "prompt_sha256": customer_package.prompt_sha256,
            "tools_sha256": customer_package.tools_sha256,
            "comparison_environment_sha256": (
                customer_package.comparison_environment_sha256
            ),
        }
    )
    candidate_hash = None
    for case_id, arms in observations.items():
        if set(arms) != {"baseline", "candidate"}:
            failures.append("客户场景 %s 缺少完整配对臂" % case_id)
            continue
        baseline = arms["baseline"]
        candidate = arms["candidate"]
        if baseline.get("injection_sha256") != baseline_empty_hash:
            failures.append("客户场景 %s 基线臂发生技能泄漏" % case_id)
        current_candidate_hash = candidate.get("injection_sha256")
        if current_candidate_hash == baseline_empty_hash:
            failures.append("客户场景 %s 候选臂没有技能注入" % case_id)
        if candidate_hash is None:
            candidate_hash = current_candidate_hash
        elif current_candidate_hash != candidate_hash:
            failures.append("候选臂技能注入快照不一致")
        if baseline.get("execution_snapshot_sha256") != candidate.get(
            "execution_snapshot_sha256"
        ):
            failures.append("客户场景 %s 两臂执行环境不一致" % case_id)
        if baseline.get("execution_snapshot_sha256") != expected_execution_snapshot:
            failures.append("客户场景 %s 执行环境快照与客户包不一致" % case_id)
        if baseline.get("arm_label") == candidate.get("arm_label"):
            failures.append("客户场景 %s 盲化标签重复" % case_id)
        if (
            baseline.get("requested_release_identity_sha256")
            != release_identity_sha256(customer_package.baseline_release)
            or baseline.get("observed_release_identity_sha256")
            != release_identity_sha256(customer_package.baseline_release)
        ):
            failures.append("客户场景 %s baseline release 无效" % case_id)
        if (
            candidate.get("requested_release_identity_sha256")
            != release_identity_sha256(customer_package.candidate_release)
            or candidate.get("observed_release_identity_sha256")
            != release_identity_sha256(customer_package.candidate_release)
        ):
            failures.append("客户场景 %s candidate release 无效" % case_id)
    return observations


def _independent_metrics(
    observations: Dict[str, Dict[str, Dict[str, Any]]],
    manifest_sha256: str,
    cases_sha256: str,
) -> Dict[str, Any]:
    pairs = [
        (observations[case_id]["baseline"], observations[case_id]["candidate"])
        for case_id in sorted(observations)
        if set(observations[case_id]) == {"baseline", "candidate"}
    ]
    if not pairs:
        raise ValueError("没有完整客户配对样本")
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
    baseline_p95 = _percentile(baseline_latencies, 95)
    candidate_p95 = _percentile(candidate_latencies, 95)
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
        "paired_delta_ci95_lower": _bootstrap_lower(deltas, seed),
        "improvement_count": improvements,
        "regression_count": regressions,
        "critical_regression_count": critical_regressions,
        "baseline_p95_latency_ms": baseline_p95,
        "candidate_p95_latency_ms": candidate_p95,
        "latency_ratio": candidate_p95 / max(baseline_p95, 0.000001),
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


def _independent_gates(
    metrics: Dict[str, Any], customer_package: CustomerPackage
) -> List[Dict[str, Any]]:
    thresholds = customer_package.thresholds
    checks = (
        ("data.full_customer_case_set", metrics["sample_count"], len(customer_package.cases), metrics["sample_count"] == len(customer_package.cases)),
        ("data.minimum_paired_samples", metrics["sample_count"], thresholds.minimum_paired_samples, metrics["sample_count"] >= thresholds.minimum_paired_samples),
        ("quality.minimum_success_rate_delta", metrics["success_rate_delta"], thresholds.minimum_success_rate_delta, metrics["success_rate_delta"] >= thresholds.minimum_success_rate_delta),
        ("quality.paired_ci95_lower_positive", metrics["paired_delta_ci95_lower"], 0.0, metrics["paired_delta_ci95_lower"] > 0.0),
        ("quality.maximum_regressions", metrics["regression_count"], thresholds.maximum_regressions, metrics["regression_count"] <= thresholds.maximum_regressions),
        ("safety.critical_regressions", metrics["critical_regression_count"], 0, metrics["critical_regression_count"] == 0),
        ("performance.maximum_latency_ratio", metrics["latency_ratio"], thresholds.maximum_latency_ratio, metrics["latency_ratio"] <= thresholds.maximum_latency_ratio),
        ("performance.minimum_serial_throughput_ratio", metrics["throughput_ratio"], thresholds.minimum_throughput_ratio, metrics["throughput_ratio"] >= thresholds.minimum_throughput_ratio),
        ("resource.maximum_cpu_time_ratio", metrics["cpu_time_ratio"], thresholds.maximum_cpu_time_ratio, metrics["cpu_time_ratio"] <= thresholds.maximum_cpu_time_ratio),
        ("resource.maximum_peak_rss_ratio", metrics["peak_rss_ratio"], thresholds.maximum_peak_rss_ratio, metrics["peak_rss_ratio"] <= thresholds.maximum_peak_rss_ratio),
        ("cost.maximum_total_tokens", metrics["total_tokens"], thresholds.maximum_total_tokens, metrics["total_tokens"] <= thresholds.maximum_total_tokens),
    )
    return [
        {"name": name, "actual": actual, "expected": expected, "passed": bool(passed)}
        for name, actual, expected, passed in checks
    ]


def _bootstrap_lower(deltas: Sequence[int], seed: int) -> float:
    source = random.Random(seed)
    count = len(deltas)
    values = [
        sum(deltas[source.randrange(count)] for _ in range(count)) / float(count)
        for _ in range(_BOOTSTRAP_ROUNDS)
    ]
    values.sort()
    return values[max(0, math.floor(0.025 * len(values)))]


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


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _implementation_fingerprint() -> str:
    """独立绑定客户验收框架全部可执行模块。"""

    from benchmarks.evidence.release_manifest import source_fingerprint

    return source_fingerprint(Path(__file__).resolve().parents[2])
