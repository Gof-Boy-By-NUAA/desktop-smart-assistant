"""Adversarial verification tests for external FDE customer case evidence."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from benchmarks.customer.json_utils import canonical_json_bytes
from benchmarks.evidence.fde_case import (
    EVIDENCE_ENV,
    REQUIRED_JOURNEYS,
    TRUST_ROOT_ENV,
    TRUST_ROOT_SHA256_ENV,
    fde_attestation_payload,
    sha256_file,
    verify_configured_fde_case_evidence,
    verify_fde_case_evidence,
)
from benchmarks.evidence.release_manifest import (
    _git_state,
    generate_manifest,
    source_fingerprint,
    verify_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def _external_signed_evidence(tmp_path: Path) -> tuple[Path, Path, str]:
    private_key = Ed25519PrivateKey.from_private_bytes(b"\x51" * 32)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    trust_root = {
        "schema_version": 1,
        "kind": "smart-assistant-fde-trust-root",
        "keys": [{
            "key_id": "customer-release-2026",
            "ed25519_public_key": public_key,
        }],
    }
    trust_path = tmp_path / "customer-trust-root.json"
    trust_path.write_text(
        json.dumps(trust_root, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    evidence = {
        "schema_version": 1,
        "kind": "smart-assistant-fde-case-evidence",
        "case_id": "customer-fde-case-001",
        "customer_id": "target-customer",
        "execution_id": "customer-run-001",
        "git_commit": _git_state(ROOT)["commit"],
        "source_fingerprint_sha256": source_fingerprint(ROOT),
        "artifact_sha256": "a" * 64,
        "environment_sha256": "b" * 64,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "journeys": [{
            "journey_id": journey,
            "outcome": "passed",
            "evidence_sha256": ("%x" % (index + 1)) * 64,
        } for index, journey in enumerate(REQUIRED_JOURNEYS)],
        "attestation": {
            "key_id": "customer-release-2026",
            "signature": "",
        },
    }
    evidence["attestation"]["signature"] = private_key.sign(
        canonical_json_bytes(fde_attestation_payload(evidence))
    ).hex()
    evidence_path = tmp_path / "customer-fde-evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return evidence_path, trust_path, sha256_file(trust_path)


def _verify(evidence_path: Path, trust_path: Path, trust_sha256: str):
    return verify_fde_case_evidence(
        ROOT,
        expected_source_fingerprint=source_fingerprint(ROOT),
        expected_git_commit=_git_state(ROOT)["commit"],
        evidence_path=evidence_path,
        trust_root_path=trust_path,
        expected_trust_root_sha256=trust_sha256,
    )


def test_absent_external_fde_inputs_fail_closed(monkeypatch):
    monkeypatch.delenv(EVIDENCE_ENV, raising=False)
    monkeypatch.delenv(TRUST_ROOT_ENV, raising=False)
    monkeypatch.delenv(TRUST_ROOT_SHA256_ENV, raising=False)

    result = verify_configured_fde_case_evidence(
        ROOT,
        expected_source_fingerprint=source_fingerprint(ROOT),
        expected_git_commit=_git_state(ROOT)["commit"],
    )

    assert result == {
        "schema_version": 1,
        "status": "ABSENT",
        "passed": False,
        "evidence_present": False,
        "trust_root_present": False,
        "trust_root_pinned": False,
        "evidence_sha256": None,
        "trust_root_sha256": None,
        "case_id": None,
        "execution_id": None,
        "errors": [],
    }


def test_external_signed_fde_case_is_bound_to_current_commit_and_source(tmp_path):
    evidence_path, trust_path, trust_sha256 = _external_signed_evidence(tmp_path)

    result = _verify(evidence_path, trust_path, trust_sha256)

    assert result["status"] == "VERIFIED"
    assert result["passed"] is True
    assert result["case_id"] == "customer-fde-case-001"
    assert result["execution_id"] == "customer-run-001"


def test_fde_case_rejects_same_pr_paths_tampering_and_wrong_trust_pin(tmp_path):
    evidence_path, trust_path, trust_sha256 = _external_signed_evidence(tmp_path)
    original = json.loads(evidence_path.read_text(encoding="utf-8"))
    tampered = copy.deepcopy(original)
    tampered["artifact_sha256"] = "c" * 64
    evidence_path.write_text(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    signature_failure = _verify(evidence_path, trust_path, trust_sha256)
    assert signature_failure["status"] == "INVALID"
    assert signature_failure["passed"] is False
    assert "signature" in signature_failure["errors"][0]

    in_repository = verify_fde_case_evidence(
        ROOT,
        expected_source_fingerprint=source_fingerprint(ROOT),
        expected_git_commit=_git_state(ROOT)["commit"],
        evidence_path=ROOT / "benchmarks" / "evidence" / "fde_case.py",
        trust_root_path=trust_path,
        expected_trust_root_sha256=trust_sha256,
    )
    assert in_repository["status"] == "INVALID"
    assert "outside the release checkout" in in_repository["errors"][0]

    evidence_path.write_text(
        json.dumps(original, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    wrong_pin = _verify(evidence_path, trust_path, "0" * 64)
    assert wrong_pin["status"] == "INVALID"
    assert "does not match external pin" in wrong_pin["errors"][0]


def test_release_manifest_recomputes_fde_case_and_keeps_customer_gate_closed(
    monkeypatch,
    tmp_path,
):
    evidence_path, trust_path, trust_sha256 = _external_signed_evidence(tmp_path)
    monkeypatch.setenv(EVIDENCE_ENV, str(evidence_path))
    monkeypatch.setenv(TRUST_ROOT_ENV, str(trust_path))
    monkeypatch.setenv(TRUST_ROOT_SHA256_ENV, trust_sha256)

    manifest = generate_manifest(ROOT)
    assert manifest["fde_case"]["passed"] is True
    assert manifest["fde_case"]["release_checkout_bound"] is False
    assert manifest["fde_case"]["release_passed"] is False
    assert manifest["hard_denials"]["FDE_CASE_EVIDENCE"] == "ABSENT"
    # The currently absent customer skills acceptance cannot be replaced by
    # this separately signed product journey evidence.
    assert manifest["hard_denials"]["TARGET_CUSTOMER_ACCEPTANCE"] == "NO"
    assert manifest["required_conditions"]["fde_case_evidence"] is False
    assert manifest["required_conditions"]["customer_acceptance"] is False
    assert verify_manifest(manifest, ROOT)["integrity_passed"] is True

    tampered = copy.deepcopy(manifest)
    tampered["fde_case"]["status"] = "ABSENT"
    result = verify_manifest(tampered, ROOT)
    assert result["integrity_passed"] is False
    assert any(
        item["name"] == "fde_case" and not item["passed"]
        for item in result["checks"]
    )
