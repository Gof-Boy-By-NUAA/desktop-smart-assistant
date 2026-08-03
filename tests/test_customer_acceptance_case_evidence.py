"""Adversarial tests for externally signed customer skills/performance evidence."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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


def _write_external_inputs(tmp_path: Path) -> tuple[Path, Path, str, Path, Path]:
    private_key = Ed25519PrivateKey.from_private_bytes(b"\x71" * 32)
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    trust_root = {
        "schema_version": 1,
        "kind": "smart-assistant-customer-acceptance-trust-root",
        "keys": [{
            "key_id": "target-customer-acceptance-2026",
            "ed25519_public_key": public_key,
        }],
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
            {"status": "completed", "passed": True},
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    evidence = {
        "schema_version": 1,
        "kind": "smart-assistant-customer-acceptance-evidence",
        "acceptance_id": "customer-skills-acceptance-001",
        "customer_id": "target-customer",
        "execution_id": "customer-skills-run-001",
        "git_commit": _git_state(ROOT)["commit"],
        "source_fingerprint_sha256": source_fingerprint(ROOT),
        "artifact_sha256": "a" * 64,
        "environment_sha256": "b" * 64,
        "customer_package_sha256": sha256_file(package_manifest),
        "customer_report_sha256": sha256_file(report_path),
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
    evidence_path, trust_path, trust_sha256, package_root, report_path = (
        _write_external_inputs(tmp_path)
    )
    verified_package = object()
    monkeypatch.setattr(
        evidence_module,
        "load_customer_package",
        lambda root, manifest_sha256: verified_package,
    )
    monkeypatch.setattr(
        evidence_module,
        "verify_customer_report",
        lambda report, package: () if package is verified_package else ("bad",),
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
    evidence_path, trust_path, trust_sha256, package_root, report_path = (
        _write_external_inputs(tmp_path)
    )
    monkeypatch.setattr(
        evidence_module, "load_customer_package", lambda root, manifest_sha256: object()
    )
    monkeypatch.setattr(
        evidence_module, "verify_customer_report", lambda report, package: ()
    )
    report_path.write_text(
        json.dumps(
            {"status": "completed", "passed": False},
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
            {"status": "completed", "passed": True},
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
