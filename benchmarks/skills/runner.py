"""对比全量技能元数据与受治理 Top-K 生产选择链。"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Sequence, Tuple

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
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
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
                int(not case.expected_skill_names)
                for case in dataset.evaluation_cases
            ),
            "source": dataset.source,
            "frozen_labels_recomputed": True,
            "provenance_complete": dataset.provenance_complete,
        },
        "implementation_sha256": implementation_sha256,
        "implementation_files": list(_IMPLEMENTATION_PATHS),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "arms": {
            "all_skills_metadata": {
                "description": "每个样本注入冻结目录的全部技能元数据",
                "metrics": baseline_metrics,
            },
            "controlled_top_k": {
                "description": "产品检索、治理事实复核和投影字节复核后的 Top-K",
                "top_k": top_k,
                "production_candidate_verification": True,
                "metrics": top_k_metrics,
            },
        },
        "cases": case_rows,
        "limitations": {
            "public_github_issue_titles_are_silver_labels": True,
            "label_rule_leakage": True,
            "production_gate_eligible": False,
            "governed_subset_only": True,
            "body_or_comments_included": False,
            "customer_tasks_included": False,
            "customer_success_rate_measured": False,
            "claim": (
                "本报告只测量公开 GitHub issue 标题对确定性路由规则的匹配；"
                "银标不代表人工金标、真实任务完成率或客户成功率。"
            ),
        },
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
                "controlled_top_k": list(selected_names),
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
            args.output.write_text(rendered + "\n", encoding="utf-8")
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
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
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "blocked_invalid_dataset",
        "passed": False,
        "dataset": {
            "path": str(path),
            "expected_sha256": expected_sha256,
            "actual_sha256": actual_sha256,
            "provenance_complete": False,
        },
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
        "metrics": None,
        "limitations": {
            "label_rule_leakage": True,
            "production_gate_eligible": False,
            "governed_subset_only": True,
            "customer_success_rate_measured": False,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
