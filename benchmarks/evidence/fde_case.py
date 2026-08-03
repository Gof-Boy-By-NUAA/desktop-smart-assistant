"""Fail-closed verifier for externally signed FDE customer journey evidence.

The release checkout deliberately does not contain the customer trust root or
the signed case artifact.  A same-PR change can edit in-repository checks, so
acceptance inputs must be supplied by a protected external runner, pinned by
digest, and bound to the exact commit/source tree under review.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from benchmarks.customer.attestation import (
    clean_ed25519_public_key,
    clean_ed25519_signature,
    verify_ed25519_signature,
)
from benchmarks.customer.json_utils import canonical_json_bytes


SCHEMA_VERSION = 1
EVIDENCE_ENV = "SMART_ASSISTANT_FDE_EVIDENCE_PATH"
TRUST_ROOT_ENV = "SMART_ASSISTANT_FDE_TRUST_ROOT_PATH"
TRUST_ROOT_SHA256_ENV = "SMART_ASSISTANT_FDE_TRUST_ROOT_SHA256"
MAX_INPUT_BYTES = 512 * 1024
REQUIRED_JOURNEYS = (
    "web_authenticated_agent_retry",
    "web_attachment_citation_cancel_reconnect",
    "web_restart_in_doubt_recovery",
    "desktop_authenticated_agent_retry",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TRUST_ROOT_KEYS = {"schema_version", "kind", "keys"}
_TRUST_KEY_KEYS = {"key_id", "ed25519_public_key"}
_EVIDENCE_KEYS = {
    "schema_version",
    "kind",
    "case_id",
    "customer_id",
    "execution_id",
    "git_commit",
    "source_fingerprint_sha256",
    "artifact_sha256",
    "environment_sha256",
    "executed_at",
    "journeys",
    "attestation",
}
_ATTESTATION_KEYS = {"key_id", "signature"}
_JOURNEY_KEYS = {"journey_id", "outcome", "evidence_sha256"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fde_attestation_payload(evidence: dict[str, Any]) -> dict[str, Any]:
    """Return the exact customer-signed payload, excluding the signature."""

    if not isinstance(evidence, dict):
        raise ValueError("FDE evidence must be an object")
    return {key: value for key, value in evidence.items() if key != "attestation"}


def _record(
    *,
    status: str,
    evidence_present: bool,
    trust_root_present: bool,
    trust_root_pinned: bool,
    errors: list[str],
    evidence_sha256: str | None = None,
    trust_root_sha256: str | None = None,
    case_id: str | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "passed": status == "VERIFIED",
        "evidence_present": evidence_present,
        "trust_root_present": trust_root_present,
        "trust_root_pinned": trust_root_pinned,
        "evidence_sha256": evidence_sha256,
        "trust_root_sha256": trust_root_sha256,
        "case_id": case_id,
        "execution_id": execution_id,
        "errors": sorted(set(errors)),
    }


def _clean_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _clean_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _read_external_json(
    path: Path | None,
    repository_root: Path,
    label: str,
) -> tuple[Path, dict[str, Any], str]:
    if path is None:
        raise ValueError(f"{label} path is absent")
    supplied = path.expanduser()
    if supplied.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    try:
        resolved = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} path is unavailable") from exc
    try:
        resolved.relative_to(repository_root)
    except ValueError:
        pass
    else:
        raise ValueError(f"{label} must be outside the release checkout")
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"{label} must be an external regular file")
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} cannot be read") from exc
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError(f"{label} size is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object")
    return resolved, payload, hashlib.sha256(raw).hexdigest()


def _load_trust_root(payload: dict[str, Any]) -> dict[str, str]:
    if set(payload) != _TRUST_ROOT_KEYS:
        raise ValueError("trust root fields are invalid")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("trust root schema version is invalid")
    if payload.get("kind") != "smart-assistant-fde-trust-root":
        raise ValueError("trust root kind is invalid")
    keys = payload.get("keys")
    if not isinstance(keys, list) or not keys:
        raise ValueError("trust root keys are invalid")
    result: dict[str, str] = {}
    for entry in keys:
        if not isinstance(entry, dict) or set(entry) != _TRUST_KEY_KEYS:
            raise ValueError("trust root key entry is invalid")
        key_id = _clean_identifier(entry.get("key_id"), "trust key id")
        if key_id in result:
            raise ValueError("trust root contains duplicate key id")
        result[key_id] = clean_ed25519_public_key(
            entry.get("ed25519_public_key"), "trust root public key"
        )
    return result


def _validate_journeys(value: Any) -> None:
    if not isinstance(value, list) or len(value) != len(REQUIRED_JOURNEYS):
        raise ValueError("FDE journey list is invalid")
    journey_ids: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != _JOURNEY_KEYS:
            raise ValueError("FDE journey fields are invalid")
        journey_id = entry.get("journey_id")
        if journey_id not in REQUIRED_JOURNEYS or journey_id in journey_ids:
            raise ValueError("FDE journey id is invalid")
        journey_ids.add(journey_id)
        if entry.get("outcome") != "passed":
            raise ValueError("FDE journey did not pass")
        _clean_sha256(entry.get("evidence_sha256"), "journey evidence sha256")
    if journey_ids != set(REQUIRED_JOURNEYS):
        raise ValueError("FDE journey coverage is incomplete")


def _validate_evidence(
    evidence: dict[str, Any],
    *,
    trusted_keys: dict[str, str],
    expected_source_fingerprint: str,
    expected_git_commit: str,
) -> tuple[str, str]:
    if set(evidence) != _EVIDENCE_KEYS:
        raise ValueError("FDE evidence fields are invalid")
    if evidence.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("FDE evidence schema version is invalid")
    if evidence.get("kind") != "smart-assistant-fde-case-evidence":
        raise ValueError("FDE evidence kind is invalid")
    case_id = _clean_identifier(evidence.get("case_id"), "case id")
    _clean_identifier(evidence.get("customer_id"), "customer id")
    execution_id = _clean_identifier(evidence.get("execution_id"), "execution id")
    git_commit = evidence.get("git_commit")
    if not isinstance(git_commit, str) or not _COMMIT.fullmatch(git_commit):
        raise ValueError("FDE evidence git commit is invalid")
    if not isinstance(expected_git_commit, str) or git_commit != expected_git_commit:
        raise ValueError("FDE evidence is not bound to current Git commit")
    if (
        _clean_sha256(
            evidence.get("source_fingerprint_sha256"), "source fingerprint"
        )
        != expected_source_fingerprint
    ):
        raise ValueError("FDE evidence is not bound to current source tree")
    _clean_sha256(evidence.get("artifact_sha256"), "artifact sha256")
    _clean_sha256(evidence.get("environment_sha256"), "environment sha256")
    executed_at = evidence.get("executed_at")
    if not isinstance(executed_at, str) or not executed_at:
        raise ValueError("FDE execution timestamp is invalid")
    try:
        datetime.fromisoformat(executed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("FDE execution timestamp is invalid") from exc
    _validate_journeys(evidence.get("journeys"))
    attestation = evidence.get("attestation")
    if not isinstance(attestation, dict) or set(attestation) != _ATTESTATION_KEYS:
        raise ValueError("FDE attestation fields are invalid")
    key_id = _clean_identifier(attestation.get("key_id"), "attestation key id")
    public_key = trusted_keys.get(key_id)
    if public_key is None:
        raise ValueError("FDE signer is not trusted")
    signature = clean_ed25519_signature(
        attestation.get("signature"), "FDE attestation signature"
    )
    if not verify_ed25519_signature(
        public_key, signature, fde_attestation_payload(evidence)
    ):
        raise ValueError("FDE attestation signature is invalid")
    return case_id, execution_id


def verify_fde_case_evidence(
    repository_root: Path,
    *,
    expected_source_fingerprint: str,
    expected_git_commit: str,
    evidence_path: Path | None,
    trust_root_path: Path | None,
    expected_trust_root_sha256: str | None,
) -> dict[str, Any]:
    """Verify external evidence without allowing an in-repository substitute."""

    root = repository_root.resolve()
    evidence_present = evidence_path is not None
    trust_root_present = trust_root_path is not None
    trust_root_pinned = expected_trust_root_sha256 is not None
    if not (evidence_present or trust_root_present or trust_root_pinned):
        return _record(
            status="ABSENT",
            evidence_present=False,
            trust_root_present=False,
            trust_root_pinned=False,
            errors=[],
        )
    if not (evidence_present and trust_root_present and trust_root_pinned):
        return _record(
            status="INCOMPLETE_EXTERNAL_INPUT",
            evidence_present=evidence_present,
            trust_root_present=trust_root_present,
            trust_root_pinned=trust_root_pinned,
            errors=["external FDE evidence, trust root, and trust-root pin are all required"],
        )
    try:
        pinned_sha256 = _clean_sha256(
            expected_trust_root_sha256, "trust-root sha256 pin"
        )
        _trust_path, trust_payload, trust_sha256 = _read_external_json(
            trust_root_path, root, "FDE trust root"
        )
        if trust_sha256 != pinned_sha256:
            raise ValueError("FDE trust root does not match external pin")
        trusted_keys = _load_trust_root(trust_payload)
        _evidence_path, evidence, evidence_sha256 = _read_external_json(
            evidence_path, root, "FDE evidence"
        )
        case_id, execution_id = _validate_evidence(
            evidence,
            trusted_keys=trusted_keys,
            expected_source_fingerprint=expected_source_fingerprint,
            expected_git_commit=expected_git_commit,
        )
    except (ValueError, TypeError) as exc:
        return _record(
            status="INVALID",
            evidence_present=evidence_present,
            trust_root_present=trust_root_present,
            trust_root_pinned=trust_root_pinned,
            errors=[str(exc)],
        )
    return _record(
        status="VERIFIED",
        evidence_present=True,
        trust_root_present=True,
        trust_root_pinned=True,
        evidence_sha256=evidence_sha256,
        trust_root_sha256=trust_sha256,
        case_id=case_id,
        execution_id=execution_id,
        errors=[],
    )


def verify_configured_fde_case_evidence(
    repository_root: Path,
    *,
    expected_source_fingerprint: str,
    expected_git_commit: str,
) -> dict[str, Any]:
    """Load only externally supplied FDE evidence paths and pin."""

    evidence = os.environ.get(EVIDENCE_ENV)
    trust_root = os.environ.get(TRUST_ROOT_ENV)
    trust_root_sha256 = os.environ.get(TRUST_ROOT_SHA256_ENV)
    return verify_fde_case_evidence(
        repository_root,
        expected_source_fingerprint=expected_source_fingerprint,
        expected_git_commit=expected_git_commit,
        evidence_path=Path(evidence) if evidence else None,
        trust_root_path=Path(trust_root) if trust_root else None,
        expected_trust_root_sha256=trust_root_sha256,
    )
