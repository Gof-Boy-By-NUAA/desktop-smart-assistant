"""Independent recomputation verifier for the formal Knowledge comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

from .compare import (
    REQUIRED_REPETITIONS,
    _ABSOLUTE_LATENCY_LIMITS_MS,
    _BOOTSTRAP_REPETITIONS,
    _MAX_INDEX_MEDIAN_RATIO,
    _MAX_ONE_SIDED_P_VALUE,
    _MAX_PAIRED_CI95_RATIO,
    _QUALITY_CEILINGS,
    _QUALITY_FLOORS,
    _RELATIVE_LATENCY_LIMITS,
    _aggregate_security_reports,
    _assert_measurement_protocol_stable,
    _build_gates,
    _build_paired_query_benchmark,
    _comparison_fingerprint,
    _recompute_index_benchmark,
    comparison_paths,
)


SCHEMA_VERSION = 1


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_report(report_path: Path, root: Path | None = None) -> Dict[str, Any]:
    root = (root or _root()).resolve()
    checks: List[Dict[str, Any]] = []

    def add(name: str, actual: Any, expected: Any, passed: bool) -> None:
        checks.append({
            "name": name,
            "actual": actual,
            "expected": expected,
            "passed": bool(passed),
        })

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "invalid_report",
            "report_path": str(report_path),
            "checks": [{"name": "valid_json", "actual": type(exc).__name__, "expected": "valid JSON", "passed": False}],
            "passed": False,
        }

    current_fingerprint = _comparison_fingerprint(root)
    add("schema_version", report.get("schema_version"), 4, report.get("schema_version") == 4)
    add(
        "comparison_implementation_sha256",
        report.get("comparison_implementation_sha256"),
        current_fingerprint,
        report.get("comparison_implementation_sha256") == current_fingerprint,
    )
    add(
        "comparison_implementation_paths",
        report.get("comparison_implementation_paths"),
        list(comparison_paths()),
        report.get("comparison_implementation_paths") == list(comparison_paths()),
    )
    add("repetitions", report.get("repetitions"), REQUIRED_REPETITIONS, report.get("repetitions") == REQUIRED_REPETITIONS)

    errors: List[Dict[str, str]] = []

    def recompute(name: str, operation):
        try:
            return operation()
        except Exception as exc:
            errors.append({
                "step": name,
                "error_type": type(exc).__name__,
                "message": str(exc),
            })
            return None

    recomputed_security = recompute(
        "security", lambda: _aggregate_security_reports(report["security"]["runs"])
    )
    recomputed_query = recompute(
        "query_benchmark",
        lambda: _build_paired_query_benchmark(report["legacy"], report["governed"]),
    )
    recomputed_index = recompute(
        "index_benchmark", lambda: _recompute_index_benchmark(report["index_benchmark"])
    )
    recomputed_measurement = recompute(
        "measurement_protocol",
        lambda: _assert_measurement_protocol_stable(
            report["quality_warmup"],
            report["execution_order"],
            report["index_benchmark"],
        ),
    )
    if recomputed_security is not None and recomputed_query is not None:
        recomputed_gates = recompute(
            "gates",
            lambda: _build_gates(
                report["legacy"],
                report["governed"],
                recomputed_security,
                recomputed_query,
                report["index_benchmark"],
                max_queries=None,
            ),
        )
    else:
        recomputed_gates = None
        errors.append({
            "step": "gates",
            "error_type": "PrerequisiteFailure",
            "message": "security or query benchmark recomputation failed",
        })
    add("raw_evidence_recomputable", errors, [], not errors)

    add("security_recomputed", report.get("security"), recomputed_security, report.get("security") == recomputed_security)
    add("query_benchmark_recomputed", report.get("query_benchmark"), recomputed_query, report.get("query_benchmark") == recomputed_query)
    add(
        "index_verified_summary_recomputed",
        report.get("index_benchmark", {}).get("verified_summary"),
        recomputed_index,
        report.get("index_benchmark", {}).get("verified_summary") == recomputed_index,
    )
    add("measurement_protocol_recomputed", report.get("measurement_protocol"), recomputed_measurement, report.get("measurement_protocol") == recomputed_measurement)
    add("gates_recomputed", report.get("gates"), recomputed_gates, report.get("gates") == recomputed_gates)

    thresholds = report.get("thresholds", {})
    expected_thresholds = {
        "quality_floors": _QUALITY_FLOORS,
        "quality_ceilings": _QUALITY_CEILINGS,
        "absolute_latency_limits_ms": _ABSOLUTE_LATENCY_LIMITS_MS,
        "relative_latency_limits": _RELATIVE_LATENCY_LIMITS,
        "max_one_sided_p_value": _MAX_ONE_SIDED_P_VALUE,
        "max_paired_ci95_ratio": _MAX_PAIRED_CI95_RATIO,
        "max_index_median_ratio": _MAX_INDEX_MEDIAN_RATIO,
        "bootstrap_repetitions": _BOOTSTRAP_REPETITIONS,
        "index_protocol": report.get("index_benchmark", {}).get("protocol"),
    }
    add("thresholds_exact", thresholds, expected_thresholds, thresholds == expected_thresholds)

    failed_gates = [
        gate.get("name") for gate in (recomputed_gates or []) if gate.get("passed") is not True
    ]
    add("all_recomputed_gates_pass", failed_gates, [], failed_gates == [] and len(recomputed_gates or []) == 69)
    expected_passed = bool(recomputed_gates) and all(gate.get("passed") is True for gate in recomputed_gates)
    add("report_passed_claim", report.get("passed"), expected_passed, report.get("passed") is expected_passed)
    full_gate = next(
        (gate for gate in (recomputed_gates or []) if gate.get("name") == "data.full_query_set"),
        None,
    )
    expected_full = bool(full_gate and full_gate.get("passed") is True)
    add(
        "official_full_dataset_gate",
        report.get("official_full_dataset_gate"),
        expected_full,
        report.get("official_full_dataset_gate") is expected_full,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "completed",
        "report_path": str(report_path),
        "report_sha256": _sha256(report_path),
        "comparison_implementation_sha256": current_fingerprint,
        "checks": checks,
        "errors": errors,
        "passed": all(item["passed"] for item in checks),
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(arguments)
    result = verify_report(Path(args.report))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(
        (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
