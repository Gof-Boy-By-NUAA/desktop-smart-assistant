"""Adversarial tests for externally signed customer skills/performance evidence."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from benchmarks.customer.attestation import release_attestation_payload
from benchmarks.customer.contracts import CustomerRelease
from benchmarks.customer.json_utils import canonical_json_bytes
from benchmarks.evidence import customer_acceptance_case as evidence_module
from benchmarks.evidence.customer_acceptance_case import (
    EVIDENCE_ENV,
    PACKAGE_ROOT_ENV,
    REPORT_ENV,
    TRUST_ROOT_ENV,
    TRUST_ROOT_SHA256_ENV,
    customer_acceptance_attestation_payload,
    sha256_file,
    verify_configured_customer_acceptance_evidence,
    verify_customer_acceptance_evidence,
)
from benchmarks.evidence.release_manifest import _git_state, source_fingerprint


ROOT = Path(__file__).resolve().parents[1]


def _public_key_hex(private_key: Ed25519PrivateKey) -> str:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


def _signed_release(
    private_key: Ed25519PrivateKey,
    *,
    release_id: str,
    git_commit: str,
    source_fingerprint_sha256: str,
    artifact_sha256: str,
    sbom_sha256: str,
) -> CustomerRelease:
    unsigned = CustomerRelease(
        release_id=release_id,
        git_commit=git_commit,
        source_fingerprint_sha256=source_fingerprint_sha256,
        artifact_sha256=artifact_sha256,
        sbom_sha256=sbom_sha256,
        signer_key_id="smart-assistant-release-2026",
        signature="",
    )
    return CustomerRelease(
        **{
            **unsigned.__dict__,
            "signature": private_key.sign(
                canonical_json_bytes(release_attestation_payload(unsigned))
            ).hex(),
        }
    )


def _write_external_inputs(
    tmp_path: Path,
) -> tuple[Path, Path, str, Path, Path, object]:
    private_key = Ed25519PrivateKey.from_private_bytes(b"\x71" * 32)
    release_private_key = Ed25519PrivateKey.from_private_bytes(b"\x72" * 32)
    trust_root = {
        "schema_version": 2,
        "kind": "smart-assistant-customer-acceptance-trust-root",
        "keys": [
            {
                "key_id": "target-customer-acceptance-2026",
                "purpose": "customer_acceptance",
                "ed25519_public_key": _public_key_hex(private_key),
            },
            {
                "key_id": "smart-assistant-release-2026",
                "purpose": "smart_assistant_release",
                "ed25519_public_key": _public_key_hex(release_private_key),
            },
        ],
    }
    trust_path = tmp_path / "customer-acceptance-trust-root.json"
    trust_path.write_text(
        json.dumps(trust_root, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    package_root = tmp_path / "customer-package"
    package_root.mkdir()
    package_manifest = package_root / "manifest.json"
    package_manifest.write_text(
        json.dumps({"fixture": "package"}, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    report_path = tmp_path / "customer-report.json"
    report_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "passed": True,
                "run_id": "customer-skills-run-001",
                "event_chain_head": "d" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    current_commit = _git_state(ROOT)["commit"]
    current_source = source_fingerprint(ROOT)
    package = SimpleNamespace(
        baseline_release=_signed_release(
            release_private_key,
            release_id="smart-assistant-baseline-v1",
            git_commit="1" * 40,
            source_fingerprint_sha256="2" * 64,
            artifact_sha256="3" * 64,
            sbom_sha256="4" * 64,
        ),
        candidate_release=_signed_release(
            release_private_key,
            release_id="smart-assistant-candidate-v2",
            git_commit=current_commit,
            source_fingerprint_sha256=current_source,
            artifact_sha256="a" * 64,
            sbom_sha256="c" * 64,
        ),
        comparison_environment_sha256="b" * 64,
    )
    evidence = {
        "schema_version": 3,
        "kind": "smart-assistant-customer-acceptance-evidence",
        "acceptance_id": "customer-skills-acceptance-001",
        "customer_id": "target-customer",
        "execution_id": "customer-skills-run-001",
        "git_commit": current_commit,
        "source_fingerprint_sha256": current_source,
        "artifact_sha256": "a" * 64,
        "environment_sha256": "b" * 64,
        "customer_package_sha256": sha256_file(package_manifest),
        "customer_report_sha256": sha256_file(report_path),
        "customer_report_run_id": "customer-skills-run-001",
        "customer_report_event_chain_head": "d" * 64,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "attestation": {
            "key_id": "target-customer-acceptance-2026",
            "signature": "",
        },
    }
    evidence["attestation"]["signature"] = private_key.sign(
        canonical_json_bytes(customer_acceptance_attestation_payload(evidence))
    ).hex()
    evidence_path = tmp_path / "customer-acceptance-evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return (
        evidence_path,
        trust_path,
        sha256_file(trust_path),
        package_root,
        report_path,
        package,
    )


def _verify(
    evidence_path: Path,
    trust_path: Path,
    trust_sha256: str,
    package_root: Path,
    report_path: Path,
):
    return verify_customer_acceptance_evidence(
        ROOT,
        expected_source_fingerprint=source_fingerprint(ROOT),
        expected_git_commit=_git_state(ROOT)["commit"],
        evidence_path=evidence_path,
        trust_root_path=trust_path,
        expected_trust_root_sha256=trust_sha256,
        package_root=package_root,
        report_path=report_path,
    )


def test_absent_customer_acceptance_evidence_fails_closed(monkeypatch):
    for name in (
        EVIDENCE_ENV,
        TRUST_ROOT_ENV,
        TRUST_ROOT_SHA256_ENV,
        PACKAGE_ROOT_ENV,
        REPORT_ENV,
    ):
        monkeypatch.delenv(name, raising=False)

    result = verify_configured_customer_acceptance_evidence(
        ROOT,
        expected_source_fingerprint=source_fingerprint(ROOT),
        expected_git_commit=_git_state(ROOT)["commit"],
    )

    assert result["status"] == "ABSENT"
    assert result["passed"] is False
    assert result["errors"] == []


def test_signed_external_customer_evidence_binds_package_and_report(
    monkeypatch, tmp_path
):
    evidence_path, trust_path, trust_sha256, package_root, report_path, package = (
        _write_external_inputs(tmp_path)
    )
    monkeypatch.setattr(
        evidence_module,
        "load_customer_package",
        lambda root, manifest_sha256: package,
    )
    monkeypatch.setattr(
        evidence_module,
        "verify_customer_report",
        lambda report, loaded: () if loaded is package else ("bad",),
    )

    result = _verify(
        evidence_path, trust_path, trust_sha256, package_root, report_path
    )

    assert result["status"] == "VERIFIED"
    assert result["passed"] is True
    assert result["customer_package_sha256"] == sha256_file(
        package_root / "manifest.json"
    )
    assert result["customer_report_sha256"] == sha256_file(report_path)


def test_customer_evidence_rejects_report_or_signature_tampering(
    monkeypatch, tmp_path
):
    evidence_path, trust_path, trust_sha256, package_root, report_path, package = (
        _write_external_inputs(tmp_path)
    )
    monkeypatch.setattr(
        evidence_module, "load_customer_package", lambda root, manifest_sha256: package
    )
    monkeypatch.setattr(
        evidence_module, "verify_customer_report", lambda report, package: ()
    )
    report_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "passed": False,
                "run_id": "customer-skills-run-001",
                "event_chain_head": "d" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    result = _verify(
        evidence_path, trust_path, trust_sha256, package_root, report_path
    )
    assert result["status"] == "INVALID"
    assert result["passed"] is False
    assert "report hash" in result["errors"][0]

    original = json.loads(evidence_path.read_text(encoding="utf-8"))
    tampered = copy.deepcopy(original)
    tampered["artifact_sha256"] = "c" * 64
    evidence_path.write_text(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "passed": True,
                "run_id": "customer-skills-run-001",
                "event_chain_head": "d" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    result = _verify(
        evidence_path, trust_path, trust_sha256, package_root, report_path
    )
    assert result["status"] == "INVALID"
    assert "signature" in result["errors"][0]


def test_customer_evidence_rejects_reused_or_wrong_purpose_trust_keys(
    monkeypatch, tmp_path
):
    evidence_path, trust_path, trust_sha256, package_root, report_path, package = (
        _write_external_inputs(tmp_path)
    )
    monkeypatch.setattr(
        evidence_module, "load_customer_package", lambda root, manifest_sha256: package
    )
    monkeypatch.setattr(
        evidence_module, "verify_customer_report", lambda report, loaded: ()
    )
    trust_root = json.loads(trust_path.read_text(encoding="utf-8"))
    trust_root["keys"][1]["ed25519_public_key"] = trust_root["keys"][0][
        "ed25519_public_key"
    ]
    trust_path.write_text(
        json.dumps(trust_root, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    result = _verify(
        evidence_path,
        trust_path,
        sha256_file(trust_path),
        package_root,
        report_path,
    )
    assert result["status"] == "INVALID"
    assert "cannot reuse a key" in result["errors"][0]

    trust_root = json.loads(trust_path.read_text(encoding="utf-8"))
    trust_root["keys"][1]["ed25519_public_key"] = _public_key_hex(
        Ed25519PrivateKey.from_private_bytes(b"\x72" * 32)
    )
    trust_root["keys"][1]["purpose"] = "customer_acceptance"
    trust_path.write_text(
        json.dumps(trust_root, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    result = _verify(
        evidence_path,
        trust_path,
        sha256_file(trust_path),
        package_root,
        report_path,
    )
    assert result["status"] == "INVALID"
    assert "not trusted for SmartAssistant releases" in result["errors"][0]


def test_customer_evidence_rejects_validly_signed_baseline_artifact_claim(
    monkeypatch, tmp_path
):
    evidence_path, trust_path, trust_sha256, package_root, report_path, package = (
        _write_external_inputs(tmp_path)
    )
    monkeypatch.setattr(
        evidence_module, "load_customer_package", lambda root, manifest_sha256: package
    )
    monkeypatch.setattr(
        evidence_module, "verify_customer_report", lambda report, loaded: ()
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["artifact_sha256"] = package.baseline_release.artifact_sha256
    private_key = Ed25519PrivateKey.from_private_bytes(b"\x71" * 32)
    evidence["attestation"]["signature"] = private_key.sign(
        canonical_json_bytes(customer_acceptance_attestation_payload(evidence))
    ).hex()
    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    result = _verify(
        evidence_path, trust_path, trust_sha256, package_root, report_path
    )

    assert result["status"] == "INVALID"
    assert "candidate release artifact" in result["errors"][0]


def test_customer_evidence_rejects_invalid_release_signature(
    monkeypatch, tmp_path
):
    evidence_path, trust_path, trust_sha256, package_root, report_path, package = (
        _write_external_inputs(tmp_path)
    )
    invalid_package = SimpleNamespace(
        baseline_release=package.baseline_release,
        candidate_release=replace(package.candidate_release, signature="0" * 128),
        comparison_environment_sha256=package.comparison_environment_sha256,
    )
    monkeypatch.setattr(
        evidence_module,
        "load_customer_package",
        lambda root, manifest_sha256: invalid_package,
    )
    monkeypatch.setattr(
        evidence_module, "verify_customer_report", lambda report, loaded: ()
    )

    result = _verify(
        evidence_path, trust_path, trust_sha256, package_root, report_path
    )

    assert result["status"] == "INVALID"
    assert "release signature is invalid" in result["errors"][0]


def test_customer_evidence_rejects_execution_or_event_chain_replay(
    monkeypatch, tmp_path
):
    evidence_path, trust_path, trust_sha256, package_root, report_path, package = (
        _write_external_inputs(tmp_path)
    )
    monkeypatch.setattr(
        evidence_module, "load_customer_package", lambda root, manifest_sha256: package
    )
    monkeypatch.setattr(
        evidence_module, "verify_customer_report", lambda report, loaded: ()
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["execution_id"] = "replayed-execution"
    private_key = Ed25519PrivateKey.from_private_bytes(b"\x71" * 32)
    evidence["attestation"]["signature"] = private_key.sign(
        canonical_json_bytes(customer_acceptance_attestation_payload(evidence))
    ).hex()
    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    result = _verify(
        evidence_path, trust_path, trust_sha256, package_root, report_path
    )

    assert result["status"] == "INVALID"
    assert "execution id does not match report run" in result["errors"][0]
