from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

from benchmarks.evidence import customer_acceptance_case as customer_case_module
from benchmarks.evidence import release_manifest as release_manifest_module
from benchmarks.evidence.release_manifest import (
    _git_state,
    _is_git_tracked_path,
    _iter_source_files,
    generate_manifest,
    main,
    verify_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def test_release_source_fingerprint_covers_full_delivery_control_plane():
    source_paths = {
        path.relative_to(ROOT).as_posix() for path in _iter_source_files(ROOT)
    }
    assert {
        ".gitignore",
        ".github/workflows/release.yml",
        "agent/tools/scheduler/scheduler_tool.py",
        "bridge/agent_bridge.py",
        "channel/web/web_channel.py",
        "desktop/build/requirements-desktop.txt",
        "desktop/src/renderer/src/api/client.ts",
        "tests/test_release_evidence_manifest.py",
    } <= source_paths
    assert "benchmarks/results/release-evidence-manifest.json" not in source_paths
    assert ".preview_secret" not in source_paths
    assert "web_sse_journal.sqlite3" not in source_paths
    assert not any(path.startswith("tmp/") for path in source_paths)


def test_git_binding_uses_unquoted_non_ascii_paths(tmp_path: Path):
    # This must test Git/path handling independently of the current SmartAssistant
    # checkout, which is intentionally dirty during implementation and should
    # truthfully fail its release gate. A small committed Unicode-path repo
    # proves _git_state reads NUL-delimited paths without C-style quoting.
    repository = tmp_path / "unicode-仓库"
    repository.mkdir()
    (repository / "源文件.txt").write_text("evidence", encoding="utf-8")
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "release-test@example.invalid"],
        ["git", "config", "user.name", "Release Test"],
        ["git", "add", "--", "源文件.txt"],
        ["git", "commit", "-m", "unicode source fixture"],
    ):
        subprocess.run(command, cwd=repository, check=True, capture_output=True)

    state = _git_state(repository)
    assert state["all_source_paths_tracked"] is True
    assert state["source_file_count"] == state["tracked_source_count"] == 1
    assert state["commit_bound"] is True


def test_git_binding_excludes_known_runtime_sse_database(tmp_path: Path):
    repository = tmp_path / "runtime-state-repository"
    repository.mkdir()
    (repository / ".gitignore").write_text(
        "web_sse_journal.sqlite3\n", encoding="utf-8"
    )
    (repository / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "web_sse_journal.sqlite3").write_bytes(b"runtime state")
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "release-test@example.invalid"],
        ["git", "config", "user.name", "Release Test"],
        ["git", "add", "--", ".gitignore", "source.py"],
        ["git", "commit", "-m", "runtime state fixture"],
    ):
        subprocess.run(command, cwd=repository, check=True, capture_output=True)

    state = _git_state(repository)
    assert state["source_file_count"] == state["tracked_source_count"] == 2
    assert state["all_source_paths_tracked"] is True
    assert state["commit_bound"] is True

def test_release_manifest_is_an_ignored_generated_artifact_not_tracked_source(
    tmp_path: Path,
):
    output = ROOT / "benchmarks/results/release-evidence-manifest.json"
    assert _is_git_tracked_path(ROOT, output) is False
    ignored = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={ROOT.resolve().as_posix()}",
            "check-ignore",
            "--quiet",
            "--",
            output.relative_to(ROOT).as_posix(),
        ],
        cwd=ROOT,
        check=False,
    )
    assert ignored.returncode == 0
    tracked_output = ROOT / ".gitignore"
    original = tracked_output.read_bytes()
    assert _is_git_tracked_path(ROOT, tracked_output) is True
    assert main(["--output", str(tracked_output)]) == 1
    assert tracked_output.read_bytes() == original

    generated_output = tmp_path / "release-evidence-manifest.json"
    assert main(["--output", str(generated_output)]) == 1
    generated = json.loads(generated_output.read_text(encoding="utf-8"))
    result = verify_manifest(generated, ROOT)
    assert result["integrity_passed"] is True
    assert result["passed"] is False
    assert not list(tmp_path.glob(".*.tmp"))


def test_release_manifest_is_explicitly_fail_closed_on_missing_external_gates():
    manifest = generate_manifest(ROOT)
    assert manifest["passed"] is False
    assert manifest["hard_denials"]["TARGET_CUSTOMER_ACCEPTANCE"] == "NO"
    assert manifest["hard_denials"]["CUSTOMER_ATTESTATION"] == "ABSENT"
    assert manifest["hard_denials"]["FDE_CASE_EVIDENCE"] == "ABSENT"
    assert manifest["hard_denials"]["DOCKER_BUILD"] == "NOT_RUN"
    assert manifest["hard_denials"]["SESSION_CITATION_UI_CLOSED_LOOP"] == "YES"
    assert manifest["required_conditions"]["session_citation_closed_loop"] is True
    assert manifest["required_conditions"]["retrieval_formal_gate"] is True
    assert manifest["required_conditions"]["knowledge_formal_gate"] is True
    assert manifest["hard_denials"]["KNOWLEDGE_LOCAL_RECOMPUTATION_VERIFIED"] == "YES"
    assert manifest["hard_denials"]["KNOWLEDGE_INDEPENDENT_VERIFICATION"] == "NO"
    assert manifest["hard_denials"]["EXTERNAL_VERIFIER_ATTESTATION"] == "ABSENT"
    assert manifest["required_conditions"]["external_verifier_attestation"] is False
    assert manifest["required_conditions"]["fde_case_evidence"] is False
    assert manifest["required_conditions"]["skills_formal_gate"] is False
    assert manifest["required_conditions"]["skills_local_report_contract"] is True
    assert manifest["required_conditions"]["skills_pinned_dataset"] is True
    assert manifest["hard_denials"]["SKILLS_LOCAL_REPORT_CONTRACT"] == "YES"
    assert manifest["reports"]["skills_selection"]["contract_valid"] is True
    assert (
        manifest["reports"]["skills_selection"]["status"]
        == "blocked_invalid_dataset"
    )


def test_local_customer_crypto_diagnostic_cannot_lift_customer_or_skills_gates(
    monkeypatch,
):
    simulated_git = {
        **_git_state(ROOT),
        "clean": True,
        "commit_bound": True,
    }
    monkeypatch.setattr(
        release_manifest_module, "_git_state", lambda root: simulated_git
    )
    monkeypatch.setattr(
        customer_case_module,
        "verify_configured_customer_acceptance_evidence",
        lambda *args, **kwargs: {"passed": True, "status": "LOCAL_CRYPTO_VALID"},
    )

    manifest = release_manifest_module.generate_manifest(ROOT)
    diagnostic = manifest["customer_acceptance_case"]
    assert diagnostic["local_diagnostic_passed"] is True
    assert diagnostic["external_protected_evidence"] is False
    assert diagnostic["release_passed"] is False
    assert manifest["hard_denials"]["TARGET_CUSTOMER_ACCEPTANCE"] == "NO"
    assert manifest["hard_denials"]["CUSTOMER_ATTESTATION"] == "ABSENT"
    assert manifest["hard_denials"]["CUSTOMER_TEST_EXECUTION"] == "NOT_RUN"
    assert manifest["hard_denials"]["SKILLS_GOLD_DATASET_VALID"] == "NO"
    assert manifest["hard_denials"]["SKILLS_PRODUCTION_GATE_ELIGIBLE"] == "NO"
    assert manifest["required_conditions"]["customer_acceptance"] is False
    assert manifest["required_conditions"]["skills_formal_gate"] is False


def test_release_manifest_verifier_accepts_currently_generated_manifest():
    manifest = generate_manifest(ROOT)
    result = verify_manifest(manifest, ROOT)
    assert result["passed"] is False
    assert all(
        check["passed"]
        for check in result["checks"]
        if check["name"] != "passed"
    )
    assert all(check["passed"] for check in result["checks"])


def test_release_manifest_verifier_rejects_tampered_report_metadata():
    manifest = json.loads(
        (ROOT / "benchmarks/results/release-evidence-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    tampered = copy.deepcopy(manifest)
    tampered["reports"]["retrieval_comparison"]["sha256"] = "0" * 64
    result = verify_manifest(tampered, ROOT)
    assert result["passed"] is False
    assert any(
        check["name"] == "report.retrieval_comparison.sha256"
        and not check["passed"]
        for check in result["checks"]
    )


def test_release_manifest_verifier_rejects_tampered_verifier_linkage():
    manifest = generate_manifest(ROOT)
    tampered = copy.deepcopy(manifest)
    tampered["reports"]["knowledge_verification"][
        "declared_report_sha256"
    ] = "0" * 64
    tampered["reports"]["knowledge_verification"][
        "source_report_linked"
    ] = True
    result = verify_manifest(tampered, ROOT)
    assert result["passed"] is False
    assert any(
        check["name"]
        == "report.knowledge_verification.declared_report_sha256"
        and not check["passed"]
        for check in result["checks"]
    )


def test_release_manifest_verifier_rejects_every_hard_denial_mutation():
    manifest = generate_manifest(ROOT)
    for name, value in manifest["hard_denials"].items():
        tampered = copy.deepcopy(manifest)
        tampered["hard_denials"][name] = "YES" if value != "YES" else "NO"
        result = verify_manifest(tampered, ROOT)
        assert result["integrity_passed"] is False, name
        assert result["passed"] is False, name
        assert any(
            check["name"] == "hard_denials" and not check["passed"]
            for check in result["checks"]
        ), name


def test_release_manifest_verifier_rejects_forged_skills_contract_or_dataset_pin():
    manifest = generate_manifest(ROOT)
    contract_flip = copy.deepcopy(manifest)
    contract_flip["reports"]["skills_selection"]["contract_valid"] = not bool(
        manifest["reports"]["skills_selection"]["contract_valid"]
    )
    pin_flip = copy.deepcopy(manifest)
    pin_flip["datasets"]["skills_selection"]["expected_sha256"] = "0" * 64

    for tampered, check_name in (
        (contract_flip, "report.skills_selection.contract_valid"),
        (pin_flip, "dataset.skills_selection.record"),
    ):
        result = verify_manifest(tampered, ROOT)
        assert result["integrity_passed"] is False
        assert any(
            check["name"] == check_name and not check["passed"]
            for check in result["checks"]
        )


def test_release_manifest_verifier_rejects_every_git_binding_mutation():
    manifest = generate_manifest(ROOT)
    for name, value in manifest["git"].items():
        tampered = copy.deepcopy(manifest)
        if isinstance(value, bool):
            tampered["git"][name] = not value
        elif isinstance(value, int):
            tampered["git"][name] = value + 1
        else:
            tampered["git"][name] = "FORGED"
        result = verify_manifest(tampered, ROOT)
        assert result["integrity_passed"] is False, name
        assert any(
            check["name"] == f"git.{name}" and not check["passed"]
            for check in result["checks"]
        ), name

    malformed = copy.deepcopy(manifest)
    malformed["git"] = "FORGED"
    result = verify_manifest(malformed, ROOT)
    assert result["integrity_passed"] is False
    assert any(
        check["name"] == "git.commit" and not check["passed"]
        for check in result["checks"]
    )


def test_release_manifest_verifier_rejects_missing_or_unknown_hard_denial_fields():
    manifest = generate_manifest(ROOT)

    missing = copy.deepcopy(manifest)
    missing["hard_denials"].pop("FDE_CASE_EVIDENCE")
    unknown = copy.deepcopy(manifest)
    unknown["hard_denials"]["FORGED_GATE"] = "YES"

    for tampered in (missing, unknown):
        result = verify_manifest(tampered, ROOT)
        assert result["integrity_passed"] is False
        assert any(
            check["name"] == "hard_denials" and not check["passed"]
            for check in result["checks"]
        )
