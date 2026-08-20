"""对比全量技能元数据与受治理 Top-K 生产选择链。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Sequence, Tuple

from agent.memory.governance import IdentityContext
from agent.skills.governance import (
    ControlledPairedSuiteRunner,
    EvaluationPolicy,
    GovernedSkillRepository,
    GovernedSkillService,
    PairedCaseExecutor,
    SkillEvaluationCommand,
    SkillProposal,
    SourceEvidence,
)
from agent.skills.manager import SkillManager

from .dataset import (
    DEFAULT_DATASET_PATH,
    EXPECTED_DATASET_SHA256,
    AnnotationRule,
    SkillSelectionDataset,
    SkillSelectionDatasetError,
    load_skill_selection_dataset,
)
from .metrics import SelectionObservation, calculate_selection_metrics


_TENANT_ID = "benchmark-skill-selection"
_MODEL_ID = "benchmark-selector-model@1"
_IMPLEMENTATION_PATHS = (
    "agent/skills/governance/contracts.py",
    "agent/skills/governance/repository.py",
    "agent/skills/governance/service.py",
    "agent/skills/locks.py",
    "agent/skills/manager.py",
    "agent/skills/formatter.py",
    "agent/skills/retrieval/contracts.py",
    "agent/skills/retrieval/runtime.py",
    "agent/retrieval/lexical.py",
    "benchmarks/skills/dataset.py",
    "benchmarks/skills/metrics.py",
    "benchmarks/skills/runner.py",
)
_REPORT_SCHEMA_VERSION = 1
_BLOCKED_DATASET_STATUS = "blocked_invalid_dataset"
_COMPLETED_SILVER_STATUS = "completed_silver_label_local"
_SILVER_LIMITATIONS = {
    "public_github_issue_titles_are_silver_labels": True,
    "label_rule_leakage": True,
    "production_gate_eligible": False,
    "gold_dataset_valid": False,
    "governed_subset_only": True,
    "body_or_comments_included": False,
    "customer_tasks_included": False,
    "customer_success_rate_measured": False,
}
_BASELINE_ARM_DESCRIPTION = "frozen full-catalog metadata selection"
_CONTROLLED_ARM_DESCRIPTION = "governed verified Top-K selection"


class _CatalogPublicationExecutor(PairedCaseExecutor):
    """仅用于把冻结基准目录通过现有发布门禁。"""

    @property
    def executor_id(self) -> str:
        return "skill-selection-benchmark-catalog"

    @property
    def executor_version(self) -> str:
        return "1.0.0"

    def execute_baseline(self, *, model_id, case_input):
        time.sleep(0.001)
        return {"catalog_ready": False}

    def execute_candidate(self, *, model_id, candidate, case_input):
        return {"catalog_ready": True}


def run_skill_selection_benchmark(
    dataset: SkillSelectionDataset,
    top_k: int = 2,
) -> Dict[str, object]:
    """使用产品选择器处理评测分割，不调用模型生成答案。"""

    if not isinstance(dataset, SkillSelectionDataset):
        raise TypeError("dataset 必须是严格加载的 SkillSelectionDataset")
    if dataset.provenance_complete is not True:
        raise ValueError("数据集缺少完整抓取来源证明")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError("top_k 必须是正整数")

    with tempfile.TemporaryDirectory(prefix="smart-assistant-skill-benchmark-") as root:
        workspace = Path(root)
        manager = _build_catalog_manager(dataset.rules, workspace)
        try:
            observations, case_rows = _evaluate_arms(
                dataset, manager, top_k=top_k
            )
        finally:
            manager.close()

    implementation_sha256 = implementation_fingerprint()
    baseline_metrics = calculate_selection_metrics(observations["all_skills_metadata"])
    top_k_metrics = calculate_selection_metrics(observations["controlled_top_k"])
    return {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # This benchmark may complete as local silver-label diagnostics, but
        # it can never establish a Skills production or gold-data gate.
        "status": _COMPLETED_SILVER_STATUS,
        "passed": False,
        "dataset": _dataset_metadata(dataset),
        "implementation_sha256": implementation_sha256,
        "implementation_files": list(_IMPLEMENTATION_PATHS),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "arms": {
            "all_skills_metadata": {
                "description": _BASELINE_ARM_DESCRIPTION,
                "metrics": baseline_metrics,
            },
            "controlled_top_k": {
                "description": _CONTROLLED_ARM_DESCRIPTION,
                "top_k": top_k,
                "production_candidate_verification": True,
                "metrics": top_k_metrics,
            },
        },
        "cases": case_rows,
        "limitations": {
            **_SILVER_LIMITATIONS,
            "claim": (
                "本报告只测量公开 GitHub issue 标题对确定性路由规则的匹配；"
                "银标不代表人工金标、真实任务完成率或客户成功率。"
            ),
        },
    }


def _dataset_metadata(dataset: SkillSelectionDataset) -> Dict[str, object]:
    return {
        "id": dataset.dataset_id,
        "sha256": dataset.sha256,
        "design_split_sha256": dataset.design_split_sha256,
        "evaluation_split_sha256": dataset.evaluation_split_sha256,
        "snapshot_generated_at": dataset.snapshot_generated_at,
        "label_tier": dataset.label_tier,
        "normalization": dataset.normalization,
        "design_case_count": len(dataset.design_cases),
        "evaluation_case_count": len(dataset.evaluation_cases),
        "negative_case_count": sum(
            int(not case.expected_skill_names) for case in dataset.evaluation_cases
        ),
        "source": dataset.source,
        "frozen_labels_recomputed": True,
        "provenance_complete": dataset.provenance_complete,
    }


def run_skill_selection_benchmark_from_path(
    dataset_path: Path = DEFAULT_DATASET_PATH,
    expected_sha256: str = EXPECTED_DATASET_SHA256,
    top_k: int = 2,
) -> Dict[str, object]:
    """先执行冻结哈希、严格结构、银标和快照时序门禁。"""

    dataset = load_skill_selection_dataset(dataset_path, expected_sha256)
    return run_skill_selection_benchmark(dataset, top_k=top_k)


def _build_catalog_manager(
    rules: Sequence[AnnotationRule], workspace: Path
) -> SkillManager:
    skills_dir = workspace / "skills"
    builtin_dir = workspace / "builtin-empty"
    skills_dir.mkdir(parents=True, exist_ok=True)
    builtin_dir.mkdir(parents=True, exist_ok=True)
    repository = GovernedSkillRepository(
        skills_dir / ".system" / "governed-skills.db"
    )
    suite_path = workspace / "catalog-publication-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "input": {"operation": "publish-frozen-catalog"},
                        "expected": {"catalog_ready": True},
                    }
                ]
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    service = GovernedSkillService(
        repository,
        skills_dir,
        _TENANT_ID,
        EvaluationPolicy(
            minimum_sample_count=1,
            max_candidate_p95_latency_ms=1000.0,
            max_latency_regression_ratio=1.0,
        ),
        ControlledPairedSuiteRunner(workspace, _CatalogPublicationExecutor()),
    )
    suite_sha256 = hashlib.sha256(suite_path.read_bytes()).hexdigest()
    try:
        for rule in rules:
            proposal = service.propose(
                _identity("catalog-proposer", "skill:propose"),
                _catalog_proposal(rule),
            )
            evaluation = service.evaluate(
                _identity("catalog-validator", "skill:validate"),
                SkillEvaluationCommand(
                    skill_id=proposal.skill_id,
                    version=proposal.version,
                    suite_path=str(suite_path),
                    suite_sha256=suite_sha256,
                    model_id=_MODEL_ID,
                    idempotency_key="evaluate-%s" % rule.skill_name,
                ),
            )
            service.publish(
                _identity("catalog-publisher", "skill:publish"),
                proposal.skill_id,
                proposal.version,
                evaluation.evaluation_id,
                "publish-%s" % rule.skill_name,
            )
    finally:
        repository.close()
    return SkillManager(
        builtin_dir=str(builtin_dir),
        custom_dir=str(skills_dir),
        config={},
        tenant_id=_TENANT_ID,
        identity_context=_identity("benchmark-reader", "skill:read"),
    )


def _catalog_proposal(rule: AnnotationRule) -> SkillProposal:
    source_payload = json.dumps(
        {"skill_name": rule.skill_name, "markers": rule.markers},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    marker_text = "、".join(rule.markers)
    return SkillProposal(
        name=rule.skill_name,
        description="处理以下需求标记：%s" % marker_text,
        applicability=tuple("标题包含 %s" % marker for marker in rule.markers),
        steps=("核对任务与技能适用性", "执行技能步骤", "验证结果"),
        validation_rules=("只对匹配的需求使用本技能",),
        contraindications=("标题不匹配时不得注入",),
        model_compatibility=(_MODEL_ID,),
        sources=(
            SourceEvidence(
                source_type="benchmark-annotation-policy",
                source_ref="benchmark://skill-selection/%s" % rule.skill_name,
                payload=source_payload,
                sha256=hashlib.sha256(source_payload).hexdigest(),
            ),
        ),
        idempotency_key="propose-%s" % rule.skill_name,
    )


def _evaluate_arms(
    dataset: SkillSelectionDataset,
    manager: SkillManager,
    *,
    top_k: int,
) -> Tuple[Dict[str, Tuple[SelectionObservation, ...]], list[Dict[str, object]]]:
    reader = _identity("benchmark-reader", "skill:read")
    runtime = manager.get_shadow_runtime()
    if runtime is None:
        raise RuntimeError("产品技能选择运行时不可用")
    baseline_rows = []
    top_k_rows = []
    case_rows = []
    expected_catalog = set(dataset.skill_names)

    for case in dataset.evaluation_cases:
        baseline_started = time.perf_counter_ns()
        baseline_prompt = manager.build_skills_prompt(skill_filter=None)
        baseline_names = tuple(
            sorted(
                entry.skill.name
                for entry in manager.filter_skills(
                    skill_filter=None, include_disabled=False
                )
                if entry.skill.name in expected_catalog
            )
        )
        baseline_latency_ms = (
            time.perf_counter_ns() - baseline_started
        ) / 1_000_000.0
        baseline_rows.append(
            SelectionObservation(
                case=case,
                selected_skill_names=baseline_names,
                prompt=baseline_prompt,
                latency_ms=baseline_latency_ms,
            )
        )

        selection_started = time.perf_counter_ns()
        with manager.production_injection_lock():
            run = runtime.start_run(
                reader,
                case.title,
                _MODEL_ID,
                "github-issue-%d" % case.number,
                top_k=top_k,
            )
            verified_candidates, verified_names = (
                manager.verify_production_candidates(
                    reader, run.candidates, _MODEL_ID
                )
            )
            skill_filter = manager.production_skill_filter(verified_names)
            selected_prompt = manager.build_skills_prompt(
                skill_filter=skill_filter
            )
        selection_latency_ms = (
            time.perf_counter_ns() - selection_started
        ) / 1_000_000.0
        selected_names = tuple(sorted(set(verified_names)))
        injection_status = (
            "injected"
            if verified_candidates
            else "no_eligible_candidate"
            if run.candidates
            else "no_match"
        )
        runtime.record_injection(run, injection_status, verified_candidates)
        runtime.finish_run(
            run,
            "completed",
            {"benchmark_case_number": case.number},
        )
        top_k_rows.append(
            SelectionObservation(
                case=case,
                selected_skill_names=selected_names,
                prompt=selected_prompt,
                latency_ms=selection_latency_ms,
            )
        )
        case_rows.append(
            {
                "number": case.number,
                "html_url": case.html_url,
                "expected_skill_names": list(case.expected_skill_names),
                "all_skills_metadata": list(baseline_names),
                "all_skills_metadata_prompt": baseline_prompt,
                "all_skills_metadata_latency_ms": baseline_latency_ms,
                "controlled_top_k": list(selected_names),
                "controlled_top_k_prompt": selected_prompt,
                "injection_status": injection_status,
                "controlled_top_k_latency_ms": selection_latency_ms,
            }
        )
    return (
        {
            "all_skills_metadata": tuple(baseline_rows),
            "controlled_top_k": tuple(top_k_rows),
        },
        case_rows,
    )


def _identity(actor_user_id: str, *roles: str) -> IdentityContext:
    return IdentityContext(
        tenant_id=_TENANT_ID,
        actor_user_id=actor_user_id,
        roles=frozenset(roles),
        trace_id="trace-%s" % actor_user_id,
        auth_source="skill-selection-benchmark",
    )


def implementation_fingerprint() -> str:
    """绑定基准适配器与实际生产选择链实现。"""

    repository_root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for relative_path in _IMPLEMENTATION_PATHS:
        path = repository_root / relative_path
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    _configure_utf8_stdout()
    parser = argparse.ArgumentParser(description="运行技能选择银标基准")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument(
        "--dataset-sha256", default=EXPECTED_DATASET_SHA256
    )
    parser.add_argument("--top-k", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = run_skill_selection_benchmark_from_path(
            args.dataset,
            expected_sha256=args.dataset_sha256,
            top_k=args.top_k,
        )
    except SkillSelectionDatasetError as error:
        report = _blocked_dataset_report(
            args.dataset, args.dataset_sha256, error
        )
        rendered = json.dumps(
            report, ensure_ascii=False, indent=2, sort_keys=True
        )
        print(rendered)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes((rendered + "\n").encode("utf-8"))
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes((rendered + "\n").encode("utf-8"))
    return 0


def _configure_utf8_stdout() -> None:
    """在 Windows 管道和控制台中统一输出 UTF-8。"""

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8")
    except (OSError, ValueError):
        return


def _blocked_dataset_report(
    dataset_path: Path,
    expected_sha256: str,
    error: SkillSelectionDatasetError,
) -> Dict[str, object]:
    """为 CI 保留不含任何质量指标的数据阻断证据。"""

    path = Path(dataset_path)
    try:
        actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        actual_sha256 = None
    return {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": _BLOCKED_DATASET_STATUS,
        "passed": False,
        "dataset": {
            "path": str(path),
            "expected_sha256": expected_sha256,
            "actual_sha256": actual_sha256,
            "provenance_complete": False,
            "label_tier": "deterministic_silver",
        },
        "implementation_sha256": implementation_fingerprint(),
        "implementation_files": list(_IMPLEMENTATION_PATHS),
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
        "metrics": None,
        "limitations": {
            "public_github_issue_titles_are_silver_labels": True,
            "label_rule_leakage": True,
            "production_gate_eligible": False,
            "gold_dataset_valid": False,
            "governed_subset_only": True,
            "body_or_comments_included": False,
            "customer_tasks_included": False,
            "customer_success_rate_measured": False,
        },
    }


def verify_skill_selection_report(
    report: object,
    *,
    dataset_path: Path = DEFAULT_DATASET_PATH,
    expected_dataset_sha256: str = EXPECTED_DATASET_SHA256,
) -> dict[str, object]:
    """Validate the untrusted local Skills report against its fixed contract.

    The GitHub-title snapshot is explicitly a pinned silver-label fixture.  A
    report produced from it must never claim ``passed`` or production/gold
    eligibility, including on the invalid-dataset path.  This is intentionally
    a local tamper detector, not an external attestation.
    """

    errors: list[str] = []
    if not isinstance(report, dict):
        return {"valid": False, "errors": ["report must be an object"]}

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    require(
        report.get("schema_version") == _REPORT_SCHEMA_VERSION,
        "schema_version does not match",
    )
    status = report.get("status")
    require(
        status in {_BLOCKED_DATASET_STATUS, _COMPLETED_SILVER_STATUS},
        "status is not an allowed local Skills status",
    )
    expected_fields = {
        "schema_version",
        "generated_at",
        "status",
        "passed",
        "dataset",
        "implementation_sha256",
        "implementation_files",
        "limitations",
    }
    if status == _BLOCKED_DATASET_STATUS:
        expected_fields |= {"error", "metrics"}
    elif status == _COMPLETED_SILVER_STATUS:
        expected_fields |= {"environment", "arms", "cases"}
    require(
        set(report) == expected_fields,
        "report fields do not match the strict status schema",
    )
    generated_at = report.get("generated_at")
    try:
        generated_time = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        if generated_time.tzinfo is None:
            raise ValueError("timezone is required")
        generated_time = generated_time.astimezone(timezone.utc)
    except (TypeError, ValueError):
        errors.append("generated_at must be an ISO-8601 timestamp with timezone")
    else:
        require(
            generated_time <= datetime.now(timezone.utc) + timedelta(minutes=5),
            "generated_at is implausibly in the future",
        )
    require(report.get("passed") is False, "local silver report must have passed=false")
    require(
        report.get("implementation_sha256") == implementation_fingerprint(),
        "implementation_sha256 does not match current implementation",
    )
    require(
        report.get("implementation_files") == list(_IMPLEMENTATION_PATHS),
        "implementation_files do not match fixed implementation paths",
    )

    limitations = report.get("limitations")
    require(isinstance(limitations, dict), "limitations must be an object")
    if isinstance(limitations, dict):
        expected_limitation_fields = set(_SILVER_LIMITATIONS)
        if status == _COMPLETED_SILVER_STATUS:
            expected_limitation_fields.add("claim")
        require(
            set(limitations) == expected_limitation_fields,
            "limitations fields do not match the strict status schema",
        )
        for name, expected in _SILVER_LIMITATIONS.items():
            require(
                limitations.get(name) is expected,
                "limitations.%s must be %r" % (name, expected),
            )

    dataset = report.get("dataset")
    require(isinstance(dataset, dict), "dataset must be an object")
    if not isinstance(dataset, dict):
        dataset = {}
    require(
        dataset.get("label_tier") == "deterministic_silver",
        "dataset.label_tier must be deterministic_silver",
    )

    if status == _BLOCKED_DATASET_STATUS:
        require(
            set(dataset)
            == {
                "path",
                "expected_sha256",
                "actual_sha256",
                "provenance_complete",
                "label_tier",
            },
            "blocked dataset fields do not match the strict schema",
        )
        require(report.get("metrics") is None, "blocked report metrics must be null")
        require(
            dataset.get("expected_sha256") == expected_dataset_sha256,
            "blocked report expected_sha256 does not match fixed dataset pin",
        )
        require(
            dataset.get("actual_sha256") == expected_dataset_sha256,
            "blocked report actual_sha256 does not match fixed dataset pin",
        )
        require(
            dataset.get("provenance_complete") is False,
            "blocked report provenance_complete must be false",
        )
        error = report.get("error")
        require(isinstance(error, dict), "blocked report error must be an object")
        if isinstance(error, dict):
            require(
                error.get("type") == "SkillSelectionDatasetError",
                "blocked report error.type is invalid",
            )
            require(isinstance(error.get("message"), str), "blocked report error.message is invalid")
    elif status == _COMPLETED_SILVER_STATUS:
        errors.extend(
            _completed_report_errors(
                report,
                dataset_path=dataset_path,
                expected_dataset_sha256=expected_dataset_sha256,
            )
        )

    return {"valid": not errors, "errors": errors}


def _completed_report_errors(
    report: dict[str, object],
    *,
    dataset_path: Path,
    expected_dataset_sha256: str,
) -> list[str]:
    """Recompute every serialised case and arm from the immutable dataset."""

    try:
        dataset = load_skill_selection_dataset(
            dataset_path, expected_dataset_sha256
        )
    except SkillSelectionDatasetError as exc:
        return ["fixed dataset could not be verified: %s" % exc]

    errors: list[str] = []
    dataset_record = report.get("dataset")
    if not isinstance(dataset_record, dict) or not _json_equivalent(
        dataset_record, _dataset_metadata(dataset)
    ):
        errors.append("completed report dataset metadata does not match fixed dataset")

    environment = report.get("environment")
    if not isinstance(environment, dict) or set(environment) != {"python", "platform"}:
        errors.append("completed report environment schema is invalid")
    elif not all(isinstance(value, str) and value for value in environment.values()):
        errors.append("completed report environment values are invalid")

    arms = report.get("arms")
    if not isinstance(arms, dict) or set(arms) != {
        "all_skills_metadata",
        "controlled_top_k",
    }:
        return errors + ["completed report arms schema is invalid"]
    baseline = arms["all_skills_metadata"]
    controlled = arms["controlled_top_k"]
    if not isinstance(baseline, dict) or set(baseline) != {"description", "metrics"}:
        errors.append("baseline arm schema is invalid")
    elif baseline.get("description") != _BASELINE_ARM_DESCRIPTION:
        errors.append("baseline arm description is invalid")
    if not isinstance(controlled, dict) or set(controlled) != {
        "description",
        "top_k",
        "production_candidate_verification",
        "metrics",
    }:
        errors.append("controlled arm schema is invalid")
        top_k = None
    else:
        top_k = controlled.get("top_k")
        if controlled.get("description") != _CONTROLLED_ARM_DESCRIPTION:
            errors.append("controlled arm description is invalid")
        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
            errors.append("controlled arm top_k is invalid")
        if controlled.get("production_candidate_verification") is not True:
            errors.append("controlled arm verification flag is invalid")

    rows = report.get("cases")
    if not isinstance(rows, list) or not rows:
        return errors + ["completed report cases must be a non-empty array"]
    expected_cases = {case.number: case for case in dataset.evaluation_cases}
    if len(rows) != len(expected_cases):
        errors.append("completed report case count does not match fixed dataset")
    seen_numbers: set[int] = set()
    baseline_observations: list[SelectionObservation] = []
    controlled_observations: list[SelectionObservation] = []
    expected_catalog = tuple(sorted(dataset.skill_names))
    known_skills = set(dataset.skill_names)
    required_case_fields = {
        "number",
        "html_url",
        "expected_skill_names",
        "all_skills_metadata",
        "all_skills_metadata_prompt",
        "all_skills_metadata_latency_ms",
        "controlled_top_k",
        "controlled_top_k_prompt",
        "injection_status",
        "controlled_top_k_latency_ms",
    }
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or set(row) != required_case_fields:
            errors.append("case %d schema is invalid" % index)
            continue
        number = row.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            errors.append("case %d number is invalid" % index)
            continue
        if number in seen_numbers:
            errors.append("case %d is duplicated" % number)
            continue
        seen_numbers.add(number)
        fixed_case = expected_cases.get(number)
        if fixed_case is None:
            errors.append("case %d is absent from fixed dataset" % number)
            continue
        if row.get("html_url") != fixed_case.html_url:
            errors.append("case %d html_url does not match fixed dataset" % number)
        if row.get("expected_skill_names") != list(fixed_case.expected_skill_names):
            errors.append("case %d labels do not match fixed dataset" % number)

        baseline_names = _reported_skill_names(
            row.get("all_skills_metadata"), "case %d baseline" % number, errors
        )
        controlled_names = _reported_skill_names(
            row.get("controlled_top_k"), "case %d controlled" % number, errors
        )
        if baseline_names is None or controlled_names is None:
            continue
        if tuple(baseline_names) != expected_catalog:
            errors.append("case %d baseline catalog does not match fixed dataset" % number)
        if not set(controlled_names).issubset(known_skills):
            errors.append("case %d controlled selection has an unknown skill" % number)
        if tuple(controlled_names) != fixed_case.expected_skill_names:
            errors.append("case %d controlled selection does not match fixed dataset" % number)
        if isinstance(top_k, int) and len(controlled_names) > top_k:
            errors.append("case %d controlled selection exceeds top_k" % number)
        injection_status = row.get("injection_status")
        if injection_status not in {"injected", "no_eligible_candidate", "no_match"}:
            errors.append("case %d injection_status is invalid" % number)
        elif (injection_status == "injected") != bool(controlled_names):
            errors.append("case %d injection_status contradicts selection" % number)
        elif not controlled_names and injection_status != "no_match":
            errors.append("case %d injection_status must be no_match" % number)

        baseline_prompt = row.get("all_skills_metadata_prompt")
        controlled_prompt = row.get("controlled_top_k_prompt")
        baseline_latency = row.get("all_skills_metadata_latency_ms")
        controlled_latency = row.get("controlled_top_k_latency_ms")
        if not isinstance(baseline_prompt, str) or not isinstance(controlled_prompt, str):
            errors.append("case %d prompt evidence is invalid" % number)
            continue
        if not _finite_nonnegative(baseline_latency) or not _finite_nonnegative(controlled_latency):
            errors.append("case %d latency evidence is invalid" % number)
            continue
        baseline_observations.append(
            SelectionObservation(
                fixed_case, tuple(baseline_names), baseline_prompt, float(baseline_latency)
            )
        )
        controlled_observations.append(
            SelectionObservation(
                fixed_case, tuple(controlled_names), controlled_prompt, float(controlled_latency)
            )
        )

    if seen_numbers != set(expected_cases):
        errors.append("completed report cases do not cover the fixed dataset exactly")
    if len(baseline_observations) != len(expected_cases) or len(controlled_observations) != len(expected_cases):
        return errors
    try:
        recomputed_baseline = calculate_selection_metrics(baseline_observations)
        recomputed_controlled = calculate_selection_metrics(controlled_observations)
    except (TypeError, ValueError) as exc:
        return errors + ["completed report metrics cannot be recomputed: %s" % exc]
    if not isinstance(baseline, dict) or not _json_equivalent(
        baseline.get("metrics"), recomputed_baseline
    ):
        errors.append("baseline arm metrics do not match case evidence")
    if not isinstance(controlled, dict) or not _json_equivalent(
        controlled.get("metrics"), recomputed_controlled
    ):
        errors.append("controlled arm metrics do not match case evidence")
    if errors or not isinstance(top_k, int):
        return errors
    try:
        executed = run_skill_selection_benchmark(dataset, top_k=top_k)
    except (OSError, RuntimeError, ValueError) as exc:
        return [*errors, "local selection chain could not be replayed: %s" % exc]
    executed_rows = {
        row["number"]: row for row in executed["cases"] if isinstance(row, dict)
    }
    for row in rows:
        number = row["number"]
        expected_row = executed_rows.get(number)
        if expected_row is None:
            errors.append("case %d is absent from local selection replay" % number)
            continue
        for field in (
            "expected_skill_names",
            "all_skills_metadata",
            "controlled_top_k",
            "injection_status",
        ):
            if row[field] != expected_row[field]:
                errors.append(
                    "case %d %s does not match local selection replay"
                    % (number, field)
                )
    return errors


def _reported_skill_names(
    value: object, context: str, errors: list[str]
) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        errors.append("%s selection must be an array of strings" % context)
        return None
    if value != sorted(set(value)):
        errors.append("%s selection must be sorted and unique" % context)
        return None
    return value


def _finite_nonnegative(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _json_equivalent(left: object, right: object) -> bool:
    try:
        return json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
            right, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
