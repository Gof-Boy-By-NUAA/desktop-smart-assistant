from __future__ import annotations

import copy
import json
from pathlib import Path

from benchmarks.evidence.release_manifest import (
    generate_manifest,
    verify_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def test_release_manifest_is_explicitly_fail_closed_on_missing_external_gates():
    manifest = generate_manifest(ROOT)
    assert manifest["passed"] is False
    assert manifest["hard_denials"]["TARGET_CUSTOMER_ACCEPTANCE"] == "NO"
    assert manifest["hard_denials"]["CUSTOMER_ATTESTATION"] == "ABSENT"
    assert manifest["hard_denials"]["DOCKER_BUILD"] == "NOT_RUN"
    assert manifest["hard_denials"]["SESSION_CITATION_UI_CLOSED_LOOP"] == "YES"
    assert manifest["required_conditions"]["session_citation_closed_loop"] is True
    assert manifest["required_conditions"]["retrieval_formal_gate"] is True
    assert manifest["required_conditions"]["knowledge_formal_gate"] is True
    assert manifest["hard_denials"]["KNOWLEDGE_LOCAL_RECOMPUTATION_VERIFIED"] == "YES"
    assert manifest["hard_denials"]["KNOWLEDGE_INDEPENDENT_VERIFICATION"] == "NO"
    assert manifest["hard_denials"]["EXTERNAL_VERIFIER_ATTESTATION"] == "ABSENT"
    assert manifest["required_conditions"]["external_verifier_attestation"] is False
    assert manifest["required_conditions"]["skills_formal_gate"] is False


def test_release_manifest_verifier_accepts_current_manifest():
    manifest = json.loads(
        (ROOT / "benchmarks/results/release-evidence-manifest.json").read_text(
            encoding="utf-8"
        )
    )
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
