import copy
import json
from pathlib import Path

from benchmarks.security import web_boundary
from benchmarks.security.verify import verify_report
from benchmarks.security.web_boundary import REQUIRED_CHECKS, generate_report


ROOT = Path(__file__).resolve().parents[1]


def test_web_boundary_generator_runs_every_required_attack():
    report = generate_report(ROOT)
    assert report["passed"] is True
    assert [item["name"] for item in report["checks"]] == list(REQUIRED_CHECKS)
    failed = [item for item in report["checks"] if not item["passed"]]
    assert not failed, json.dumps(failed, ensure_ascii=False, indent=2)
    assert report["limitations"]["production_deployment_verified"] is False
    assert report["limitations"]["customer_execution_verified"] is False


def test_web_boundary_verifier_accepts_current_report():
    result = verify_report(
        ROOT / "benchmarks/results/web-boundary-security.json", ROOT
    )
    failed = [item for item in result["checks"] if not item["passed"]]
    assert result["passed"] is True, json.dumps(failed, ensure_ascii=False, indent=2)
    assert not failed


def test_web_boundary_verifier_rejects_tampered_pass_claim(tmp_path):
    report = json.loads(
        (ROOT / "benchmarks/results/web-boundary-security.json").read_text(
            encoding="utf-8"
        )
    )
    report["checks"][0]["passed"] = False
    report["passed"] = True
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(report), encoding="utf-8")

    result = verify_report(tampered, ROOT)
    assert result["passed"] is False
    failed_names = {
        item["name"] for item in result["checks"] if not item["passed"]
    }
    assert "reported_checks_passed" in failed_names


def test_web_boundary_temp_cleanup_retries_but_does_not_ignore_lock(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "attack-workspace"
    workspace.mkdir()
    (workspace / "legacy.db").write_bytes(b"sqlite")
    real_rmtree = web_boundary.shutil.rmtree
    calls = []

    monkeypatch.setattr(web_boundary.tempfile, "mkdtemp", lambda **_: str(workspace))

    def transient_rmtree(path):
        calls.append(Path(path))
        if len(calls) == 1:
            raise PermissionError(13, "transient sqlite handle", str(path))
        real_rmtree(path)

    monkeypatch.setattr(web_boundary.shutil, "rmtree", transient_rmtree)
    monkeypatch.setattr(web_boundary.time, "sleep", lambda _: None)

    with web_boundary._temporary_workspace() as actual:
        assert actual == workspace

    assert calls == [workspace, workspace]
    assert not workspace.exists()
