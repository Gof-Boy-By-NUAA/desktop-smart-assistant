"""Independently replay and verify the formal Web boundary report."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

from .web_boundary import (
    REQUIRED_CHECKS,
    SCHEMA_VERSION,
    run_checks,
    source_fingerprint,
)


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _report_sha256(path: Path) -> str:
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
            "schema_version": 1,
            "status": "invalid_report",
            "report_path": str(report_path),
            "report_sha256": _report_sha256(report_path) if report_path.is_file() else None,
            "checks": [{"name": "valid_json", "actual": type(exc).__name__, "expected": "valid JSON object", "passed": False}],
            "passed": False,
        }

    add("schema_version", report.get("schema_version"), SCHEMA_VERSION, report.get("schema_version") == SCHEMA_VERSION)
    add(
        "required_checks_exact",
        report.get("required_checks"),
        list(REQUIRED_CHECKS),
        report.get("required_checks") == list(REQUIRED_CHECKS),
    )
    current_fingerprint = source_fingerprint(root)
    add(
        "source_fingerprint",
        report.get("source_fingerprint_sha256"),
        current_fingerprint,
        report.get("source_fingerprint_sha256") == current_fingerprint,
    )

    reported_checks = report.get("checks") if isinstance(report.get("checks"), list) else []
    reported_by_name = {
        item.get("name"): item
        for item in reported_checks
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    add(
        "reported_check_names_unique",
        len(reported_by_name),
        len(REQUIRED_CHECKS),
        len(reported_by_name) == len(reported_checks) == len(REQUIRED_CHECKS),
    )
    add(
        "reported_checks_passed",
        [name for name in REQUIRED_CHECKS if not reported_by_name.get(name, {}).get("passed")],
        [],
        all(reported_by_name.get(name, {}).get("passed") is True for name in REQUIRED_CHECKS),
    )

    # Re-run the attacks rather than trusting PASS fields in the report.
    replay = run_checks(root)
    replay_by_name = {item["name"]: item for item in replay}
    replay_failures = [
        name for name in REQUIRED_CHECKS
        if replay_by_name.get(name, {}).get("passed") is not True
    ]
    add("independent_attack_replay", replay_failures, [], replay_failures == [])
    add(
        "report_passed_claim",
        report.get("passed"),
        True,
        report.get("passed") is True,
    )

    return {
        "schema_version": 1,
        "status": "completed",
        "report_path": str(report_path),
        "report_sha256": _report_sha256(report_path),
        "source_fingerprint_sha256": current_fingerprint,
        "checks": checks,
        "replay_checks": replay,
        "passed": all(item["passed"] for item in checks),
        "limitations": {
            "local_execution_only": True,
            "remote_ci_protected": False,
            "production_deployment_verified": False,
        },
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
