from __future__ import annotations

import copy
import json
from pathlib import Path

from benchmarks.knowledge.verify import verify_report


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "benchmarks/results/cmrc2018-knowledge-comparison.json"


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "knowledge-tampered.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def test_knowledge_verifier_accepts_current_formal_report():
    result = verify_report(REPORT, ROOT)
    assert result["passed"] is True
    assert result["errors"] == []
    assert all(check["passed"] for check in result["checks"])


def test_knowledge_verifier_rejects_forged_pass_claim(tmp_path):
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    report["passed"] = False
    result = verify_report(_write(tmp_path, report), ROOT)
    assert result["passed"] is False
    assert any(
        check["name"] == "report_passed_claim" and not check["passed"]
        for check in result["checks"]
    )


def test_knowledge_verifier_rejects_tampered_raw_index_ratio(tmp_path):
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    report["index_benchmark"]["blocks"][0]["trials"][0]["latency_ms"] *= 1.25
    result = verify_report(_write(tmp_path, report), ROOT)
    assert result["passed"] is False
    assert any(error["step"] in {"index_benchmark", "gates"} for error in result["errors"])


def test_knowledge_verifier_rejects_tampered_bootstrap_ci(tmp_path):
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    forged = copy.deepcopy(report)
    forged["index_benchmark"]["block_statistics"][
        "bootstrap_median_ratio_ci95"
    ][1] = 0.01
    result = verify_report(_write(tmp_path, forged), ROOT)
    assert result["passed"] is False
    assert any(error["step"] == "gates" for error in result["errors"])


def test_knowledge_verifier_rejects_implementation_fingerprint_tamper(tmp_path):
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    report["comparison_implementation_sha256"] = "0" * 64
    result = verify_report(_write(tmp_path, report), ROOT)
    assert result["passed"] is False
    assert any(
        check["name"] == "comparison_implementation_sha256"
        and not check["passed"]
        for check in result["checks"]
    )
