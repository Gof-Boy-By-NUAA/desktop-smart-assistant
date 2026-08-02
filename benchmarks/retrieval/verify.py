"""Independently verify a schema-v3 retrieval comparison report."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
from pathlib import Path
from typing import Any, Dict, Sequence

from .compare import comparison_implementation_fingerprint


_TIMING_METRICS = (
    "latency_ms_mean",
    "latency_ms_p50",
 "latency_ms_p95",
    "index_latency_ms",
)
_HIGHER_IS_BETTER = (
    "recall_at_1",
    "recall_at_5",
    "recall_at_10",
    "mrr_at_10",
)
_BOOTSTRAP_REPETITIONS = 10_000
_BOOTSTRAP_SEED = 20260731
_MINIMUM_EFFECT_RATIO = 0.95
_SIGN_TEST_ALPHA = 0.05


def verify_report(report_path: Path, dataset_path: Path) -> Dict[str, Any]:
    """Recompute every release gate from raw samples and current source files."""

    report_bytes = Path(report_path).read_bytes()
    report = json.loads(report_bytes.decode("utf-8"))
    checks = []

    def check(name: str, actual: Any, expected: Any, passed: bool) -> None:
        checks.append(
            {"name": name, "actual": actual, "expected": expected, "passed": bool(passed)}
        )

    check("schema_version", report.get("schema_version"), 3, report.get("schema_version") == 3)
    current_fingerprint = comparison_implementation_fingerprint()
    check(
        "comparison_implementation_sha256",
        report.get("comparison_implementation_sha256"),
        current_fingerprint,
        report.get("comparison_implementation_sha256") == current_fingerprint,
    )
    dataset_sha256 = hashlib.sha256(Path(dataset_path).read_bytes()).hexdigest()
    for engine_name in ("baseline", "improved"):
        engine_dataset = report.get(engine_name, {}).get("dataset", {})
        check(
            f"{engine_name}.dataset.sha256",
            engine_dataset.get("sha256"),
            dataset_sha256,
            engine_dataset.get("sha256") == dataset_sha256,
        )

    baseline = report.get("baseline", {})
    improved = report.get("improved", {})
    baseline_metrics = baseline.get("metrics", {})
    improved_metrics = improved.get("metrics", {})
    gates = {gate.get("metric"): gate for gate in report.get("gates", [])}

    for metric in _HIGHER_IS_BETTER:
        before = float(baseline_metrics[metric])
        after = float(improved_metrics[metric])
        gate = gates.get(metric, {})
        expected_passed = after > before
        check(
            f"gate.{metric}",
            gate.get("passed"),
            expected_passed,
            gate.get("direction") == "higher"
            and _same_number(gate.get("baseline"), before)
            and _same_number(gate.get("improved"), after)
            and _same_number(gate.get("delta"), after - before)
            and gate.get("passed") is expected_passed,
        )

    before_empty = float(baseline_metrics["empty_result_rate"])
    after_empty = float(improved_metrics["empty_result_rate"])
    empty_gate = gates.get("empty_result_rate", {})
    expected_empty_passed = after_empty < before_empty
    check(
        "gate.empty_result_rate",
        empty_gate.get("passed"),
        expected_empty_passed,
        empty_gate.get("direction") == "lower"
        and _same_number(empty_gate.get("baseline"), before_empty)
        and _same_number(empty_gate.get("improved"), after_empty)
        and empty_gate.get("passed") is expected_empty_passed,
    )

    paired_statistics = report.get("paired_statistics", {})
    for index, metric in enumerate(_TIMING_METRICS):
        baseline_values = _timing_samples(baseline, metric)
        improved_values = _timing_samples(improved, metric)
        ratios = [after / before for before, after in zip(baseline_values, improved_values)]
        expected_stats = _statistics(ratios, seed=_BOOTSTRAP_SEED + index)
        actual_stats = paired_statistics.get(metric, {})
        stats_match = _same_structure(actual_stats, expected_stats)
        expected_passed = (
            expected_stats["median_paired_ratio"] <= _MINIMUM_EFFECT_RATIO
            and expected_stats["one_sided_sign_test_p_value"] <= _SIGN_TEST_ALPHA
            and expected_stats["bootstrap_median_ratio_ci95"][1] <= _MINIMUM_EFFECT_RATIO
        )
        gate = gates.get(metric, {})
        gate_match = (
            gate.get("direction") == "paired_ratio_lower"
            and _same_number(gate.get("minimum_effect_ratio"), _MINIMUM_EFFECT_RATIO)
            and _same_number(gate.get("sign_test_alpha"), _SIGN_TEST_ALPHA)
            and _same_structure(gate.get("details", {}), expected_stats)
            and gate.get("passed") is expected_passed
        )
        check(
            f"paired.{metric}",
            {"statistics_match": stats_match, "gate_passed": gate.get("passed")},
            {"statistics_match": True, "gate_passed": expected_passed},
            stats_match and gate_match,
        )

    expected_gate_count = len(_HIGHER_IS_BETTER) + 1 + len(_TIMING_METRICS)
    check("gate_count", len(report.get("gates", [])), expected_gate_count, len(report.get("gates", [])) == expected_gate_count)
    recomputed_passed = all(bool(gate.get("passed")) for gate in report.get("gates", []))
    check(
        "report.passed",
        report.get("passed"),
        recomputed_passed,
        report.get("passed") is recomputed_passed,
    )
    passed = all(item["passed"] for item in checks) and recomputed_passed
    return {
        "schema_version": 1,
        "passed": passed,
        "report_path": str(Path(report_path)),
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "dataset_path": str(Path(dataset_path)),
        "dataset_sha256": dataset_sha256,
        "comparison_implementation_sha256": current_fingerprint,
        "checks": checks,
    }


def _timing_samples(report: Dict[str, Any], metric: str) -> list[float]:
    values = report.get("timing_samples", {}).get(metric, [])
    if not values:
        raise ValueError(f"missing timing samples: {metric}")
    result = [float(value) for value in values]
    if any(not math.isfinite(value) or value <= 0.0 for value in result):
        raise ValueError(f"invalid timing samples: {metric}")
    return result


def _statistics(ratios: list[float], seed: int) -> Dict[str, Any]:
    wins = sum(int(value < 1.0) for value in ratios)
    sample_count = len(ratios)
    return {
        "paired_ratios": ratios,
        "pair_count": sample_count,
        "strict_win_count": wins,
        "strict_win_rate": wins / sample_count,
        "median_paired_ratio": statistics.median(ratios),
        "one_sided_sign_test_p_value": sum(
            math.comb(sample_count, value) for value in range(wins, sample_count + 1)
        ) / float(2**sample_count),
        "bootstrap_median_ratio_ci95": _bootstrap_ci(ratios, seed),
        "bootstrap_seed": seed,
        "bootstrap_repetitions": _BOOTSTRAP_REPETITIONS,
    }


def _bootstrap_ci(ratios: list[float], seed: int) -> tuple[float, float]:
    generator = random.Random(seed)
    count = len(ratios)
    medians = []
    for _ in range(_BOOTSTRAP_REPETITIONS):
        medians.append(
            statistics.median([ratios[generator.randrange(count)] for _ in range(count)])
        )
    medians.sort()
    return (
        medians[int(0.025 * (_BOOTSTRAP_REPETITIONS - 1))],
        medians[int(0.975 * (_BOOTSTRAP_REPETITIONS - 1))],
    )


def _same_number(actual: Any, expected: float) -> bool:
    try:
        return math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-12)
    except (TypeError, ValueError):
        return False


def _same_structure(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and set(actual) == set(expected) and all(
            _same_structure(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, (tuple, list)):
        return isinstance(actual, (tuple, list)) and len(actual) == len(expected) and all(
            _same_structure(left, right) for left, right in zip(actual, expected)
        )
    if isinstance(expected, float):
        return _same_number(actual, expected)
    return actual == expected


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify retrieval comparison evidence")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    verification = verify_report(args.report, args.dataset)
    rendered = json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if verification["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
