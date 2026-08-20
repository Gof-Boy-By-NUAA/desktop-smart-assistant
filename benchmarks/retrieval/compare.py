"""在同一真实数据集上执行检索性能门禁。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import random
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .dataset import ensure_cmrc2018_dev
from .evaluate import run_baseline, run_improved


_HIGHER_IS_BETTER = (
    "recall_at_1",
    "recall_at_5",
    "recall_at_10",
    "mrr_at_10",
)
_LOWER_QUALITY_IS_BETTER = (
    "empty_result_rate",
)
_TIMING_METRICS = (
    "latency_ms_mean",
    "latency_ms_p50",
    "latency_ms_p95",
    "index_latency_ms",
)
_MINIMUM_EFFECT_RATIO = 0.95
_SIGN_TEST_ALPHA = 0.05
_BOOTSTRAP_SEED = 20260731
_BOOTSTRAP_REPETITIONS = 10_000
_COMPARISON_IMPLEMENTATION_PATHS = (
    "benchmarks/retrieval/compare.py",
    "benchmarks/retrieval/verify.py",
    "benchmarks/retrieval/evaluate.py",
    "benchmarks/retrieval/smart_assistant_baseline.py",
    "benchmarks/retrieval/improved_engine.py",
    "benchmarks/retrieval/dataset.py",
    "benchmarks/retrieval/metrics.py",
    "benchmarks/retrieval/data_sources.json",
)


def compare_engines(
    dataset_path: Path,
    max_queries: Optional[int] = None,
    candidate_limit: int = 40,
    repetitions: int = 11,
) -> Dict[str, object]:
    """交错运行两个引擎，并使用中位数生成逐指标硬门禁。"""

    if repetitions <= 0:
        raise ValueError("repetitions 必须大于零")
    implementation_sha256 = comparison_implementation_fingerprint()
    baseline_runs = []
    improved_runs = []
    for repetition in range(repetitions):
        if repetition % 2 == 0:
            baseline_runs.append(run_baseline(dataset_path, max_queries=max_queries))
            improved_runs.append(
                run_improved(
                    dataset_path,
                    max_queries=max_queries,
                    candidate_limit=candidate_limit,
                )
            )
        else:
            improved_runs.append(
                run_improved(
                    dataset_path,
                    max_queries=max_queries,
                    candidate_limit=candidate_limit,
                )
            )
            baseline_runs.append(run_baseline(dataset_path, max_queries=max_queries))

    baseline = _aggregate_reports(baseline_runs)
    improved = _aggregate_reports(improved_runs)
    _assert_comparable(baseline, improved)
    baseline_metrics = baseline["metrics"]
    improved_metrics = improved["metrics"]
    gates = []

    for metric in _HIGHER_IS_BETTER:
        before = float(baseline_metrics[metric])
        after = float(improved_metrics[metric])
        gates.append(
            {
                "metric": metric,
                "direction": "higher",
                "baseline": before,
                "improved": after,
                "delta": after - before,
                "passed": after > before,
            }
        )

    for metric in _LOWER_QUALITY_IS_BETTER:
        before = float(baseline_metrics[metric])
        after = float(improved_metrics[metric])
        gates.append(
            {
                "metric": metric,
                "direction": "lower",
                "baseline": before,
                "improved": after,
                "delta": after - before,
                "passed": after < before,
            }
        )

    paired_statistics = {}
    for metric_index, metric in enumerate(_TIMING_METRICS):
        baseline_values = _timing_values(baseline_runs, metric)
        improved_values = _timing_values(improved_runs, metric)
        ratios = _paired_ratios(baseline_values, improved_values)
        statistics_report = _paired_ratio_statistics(
            ratios,
            seed=_BOOTSTRAP_SEED + metric_index,
        )
        paired_statistics[metric] = statistics_report
        ci95 = statistics_report["bootstrap_median_ratio_ci95"]
        median_ratio = float(statistics_report["median_paired_ratio"])
        sign_test_p_value = float(
            statistics_report["one_sided_sign_test_p_value"]
        )
        passed = (
            median_ratio <= _MINIMUM_EFFECT_RATIO
            and sign_test_p_value <= _SIGN_TEST_ALPHA
            and float(ci95[1]) <= _MINIMUM_EFFECT_RATIO
        )
        baseline_median = statistics.median(baseline_values)
        improved_median = statistics.median(improved_values)
        gates.append(
            {
                "metric": metric,
                "direction": "paired_ratio_lower",
                "baseline": baseline_median,
                "improved": improved_median,
                "delta": improved_median - baseline_median,
                "minimum_effect_ratio": _MINIMUM_EFFECT_RATIO,
                "sign_test_alpha": _SIGN_TEST_ALPHA,
                "details": statistics_report,
                "passed": passed,
            }
        )
    passed = all(bool(gate["passed"]) for gate in gates)
    if comparison_implementation_fingerprint() != implementation_sha256:
        raise RuntimeError("对比运行期间基准实现指纹发生变化")
    return {
        "schema_version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "comparison_implementation_sha256": implementation_sha256,
        "passed": passed,
        "repetitions": repetitions,
        "baseline": baseline,
        "improved": improved,
        "paired_statistics": paired_statistics,
        "gates": gates,
    }


def _assert_comparable(
    baseline: Dict[str, object], improved: Dict[str, object]
) -> None:
    """拒绝比较数据源或问题集合不同的报告。"""

    baseline_dataset = baseline["dataset"]
    improved_dataset = improved["dataset"]
    for field_name in ("id", "sha256", "query_selection_sha256"):
        if baseline_dataset[field_name] != improved_dataset[field_name]:
            raise ValueError("报告不可比较，字段不一致: %s" % field_name)


def _aggregate_reports(reports: list) -> Dict[str, object]:
    """校验质量指标稳定性，并对计时指标取中位数。"""

    if not reports:
        raise ValueError("报告列表不能为空")
    reference_engine_id, reference_implementation = _report_identity(
        reports[0], 1
    )
    for index, report in enumerate(reports[1:], start=2):
        engine_id, implementation_sha256 = _report_identity(report, index)
        if engine_id != reference_engine_id:
            raise ValueError("重复运行的 engine.id 发生漂移")
        if implementation_sha256 != reference_implementation:
            raise ValueError("重复运行的 engine.implementation_sha256 发生漂移")
    aggregate = copy.deepcopy(reports[0])
    quality_metrics = _HIGHER_IS_BETTER + ("empty_result_rate", "query_count")
    for metric in quality_metrics:
        values = [report["metrics"][metric] for report in reports]
        if any(value != values[0] for value in values[1:]):
            raise ValueError("重复运行的质量指标不一致: %s" % metric)
        aggregate["metrics"][metric] = values[0]

    timing_metrics = ("latency_ms_mean", "latency_ms_p50", "latency_ms_p95")
    timing_samples = {}
    for metric in timing_metrics:
        values = [float(report["metrics"][metric]) for report in reports]
        aggregate["metrics"][metric] = statistics.median(values)
        timing_samples[metric] = values

    index_values = [float(report["index_latency_ms"]) for report in reports]
    aggregate["index_latency_ms"] = statistics.median(index_values)
    timing_samples["index_latency_ms"] = index_values
    aggregate["timing_samples"] = timing_samples
    aggregate["repetitions"] = len(reports)
    return aggregate


def _timing_values(reports: list, metric: str) -> list[float]:
    """Extract a timing metric from paired single-run reports."""

    if metric == "index_latency_ms":
        return [float(report["index_latency_ms"]) for report in reports]
    return [float(report["metrics"][metric]) for report in reports]


def _paired_ratios(
    baseline_values: list[float], improved_values: list[float]
) -> list[float]:
    """Return improved/baseline ratios without destroying run pairing."""

    if len(baseline_values) != len(improved_values) or not baseline_values:
        raise ValueError("paired timing samples must have equal non-zero length")
    ratios = []
    for baseline_value, improved_value in zip(baseline_values, improved_values):
        if baseline_value <= 0.0 or improved_value < 0.0:
            raise ValueError("timing samples must be positive")
        ratios.append(improved_value / baseline_value)
    return ratios


def _paired_ratio_statistics(ratios: list[float], seed: int) -> Dict[str, object]:
    """Compute deterministic paired effect, sign-test and bootstrap evidence."""

    if not ratios or any(
        not math.isfinite(value) or value < 0.0 for value in ratios
    ):
        raise ValueError("paired ratios must be finite non-negative values")
    strict_wins = sum(int(value < 1.0) for value in ratios)
    sample_count = len(ratios)
    return {
        "paired_ratios": ratios,
        "pair_count": sample_count,
        "strict_win_count": strict_wins,
        "strict_win_rate": strict_wins / sample_count,
        "median_paired_ratio": statistics.median(ratios),
        "one_sided_sign_test_p_value": _one_sided_sign_test_p_value(
            strict_wins, sample_count
        ),
        "bootstrap_median_ratio_ci95": _bootstrap_median_ratio_ci95(
            ratios, seed=seed
        ),
        "bootstrap_seed": seed,
        "bootstrap_repetitions": _BOOTSTRAP_REPETITIONS,
    }


def _one_sided_sign_test_p_value(wins: int, sample_count: int) -> float:
    """Exact P(X >= wins) for X~Binomial(sample_count, 0.5); ties are losses."""

    if not 0 <= wins <= sample_count:
        raise ValueError("invalid sign-test counts")
    numerator = sum(
        math.comb(sample_count, value)
        for value in range(wins, sample_count + 1)
    )
    return numerator / float(2**sample_count)


def _bootstrap_median_ratio_ci95(
    ratios: list[float], seed: int
) -> tuple[float, float]:
    """Return a deterministic percentile bootstrap CI for the paired median."""

    generator = random.Random(seed)
    sample_count = len(ratios)
    medians = []
    for _ in range(_BOOTSTRAP_REPETITIONS):
        sample = [
            ratios[generator.randrange(sample_count)]
            for _ in range(sample_count)
        ]
        medians.append(statistics.median(sample))
    medians.sort()
    lower_index = int(0.025 * (_BOOTSTRAP_REPETITIONS - 1))
    upper_index = int(0.975 * (_BOOTSTRAP_REPETITIONS - 1))
    return medians[lower_index], medians[upper_index]


def _report_identity(report: Dict[str, object], index: int) -> tuple[str, str]:
    """校验单轮报告版本及引擎身份字段。"""

    if report.get("schema_version") != 2:
        raise ValueError("第 %d 轮报告 schema_version 必须为 2" % index)
    engine = report.get("engine")
    if not isinstance(engine, dict):
        raise ValueError("第 %d 轮报告缺少 engine 对象" % index)
    engine_id = engine.get("id")
    if not isinstance(engine_id, str) or not engine_id.strip():
        raise ValueError("第 %d 轮报告 engine.id 无效" % index)
    implementation_sha256 = engine.get("implementation_sha256")
    if (
        not isinstance(implementation_sha256, str)
        or len(implementation_sha256) != 64
        or implementation_sha256 != implementation_sha256.lower()
        or any(
            character not in "0123456789abcdef"
            for character in implementation_sha256
        )
    ):
        raise ValueError(
            "第 %d 轮报告 engine.implementation_sha256 无效" % index
        )
    return engine_id, implementation_sha256


def comparison_implementation_fingerprint() -> str:
    """绑定数据加载、引擎适配、指标计算和比较门禁实现。"""

    repository_root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for relative_path in _COMPARISON_IMPLEMENTATION_PATHS:
        path = repository_root / relative_path
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="运行检索基线对比门禁")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--max-queries", type=int)
    parser.add_argument("--candidate-limit", type=int, default=40)
    parser.add_argument("--repetitions", type=int, default=11)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    dataset_path = args.dataset or ensure_cmrc2018_dev()
    report = compare_engines(
        dataset_path,
        max_queries=args.max_queries,
        candidate_limit=args.candidate_limit,
        repetitions=args.repetitions,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes((rendered + "\n").encode("utf-8"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
