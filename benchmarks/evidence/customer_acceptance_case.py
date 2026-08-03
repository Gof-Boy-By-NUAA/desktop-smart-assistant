"""Verify externally signed customer performance/skills acceptance evidence.

The in-repository customer runner validates a package and its signed executor
receipts, but a same-PR author could otherwise choose both package and keys.
This verifier accepts a completed customer report only when a separately
pinned customer trust root signs the exact package/report hashes and release
object under review.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from benchmarks.customer.attestation import (
    clean_ed25519_public_key,
    clean_ed25519_signature,
    release_attestation_payload,
    verify_ed25519_signature,
)
from benchmarks.customer.json_utils import (
    canonical_json_bytes,
    strict_json_loads,
)
from benchmarks.customer.contracts import CustomerAcceptanceError
from benchmarks.customer.package import load_customer_package
from benchmarks.customer.verify import verify_customer_report
from common.path_safety import has_link_or_reparse_component


SCHEMA_VERSION = 3
TRUST_ROOT_SCHEMA_VERSION = 2
EVIDENCE_ENV = "SMART_ASSISTANT_CUSTOMER_ACCEPTANCE_EVIDENCE_PATH"
TRUST_ROOT_ENV = "SMART_ASSISTANT_CUSTOMER_ACCEPTANCE_TRUST_ROOT_PATH"
TRUST_ROOT_SHA256_ENV = "SMART_ASSISTANT_CUSTOMER_ACCEPTANCE_TRUST_ROOT_SHA256"
PACKAGE_ROOT_ENV = "SMART_ASSISTANT_CUSTOMER_ACCEPTANCE_PACKAGE_ROOT"
REPORT_ENV = "SMART_ASSISTANT_CUSTOMER_ACCEPTANCE_REPORT_PATH"
MAX_INPUT_BYTES = 2 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TRUST_ROOT_KEYS = {"schema_version", "kind", "keys"}
_TRUST_KEY_KEYS = {"key_id", "purpose", "ed25519_public_key"}
_EVIDENCE_KEYS = {
    "schema_version",
    "kind",
    "acceptance_id",
    "customer_id",
    "execution_id",
    "git_commit",
    "source_fingerprint_sha256",
    "artifact_sha256",
    "environment_sha256",
    "customer_package_sha256",
    "customer_report_sha256",
    "customer_report_run_id",
    "customer_report_event_chain_head",
    "executed_at",
    "attestation",
}
_ATTESTATION_KEYS = {"key_id", "signature"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def customer_acceptance_attestation_payload(
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        raise ValueError("customer acceptance evidence must be an object")
    return {key: value for key, value in evidence.items() if key != "attestation"}


def _clean_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("invalid %s" % field)
    return value


def _clean_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("invalid %s" % field)
    return value


def _external_file(
    value: Path | None,
    repository_root: Path,
    label: str,
) -> tuple[Path, bytes]:
    if value is None:
        raise ValueError("%s path is absent" % label)
    supplied = value.expanduser()
    if supplied.is_symlink() or has_link_or_reparse_component(supplied):
        raise ValueError("%s must not contain a symbolic link or reparse point" % label)
    try:
        resolved = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("%s path is unavailable" % label) from exc
    try:
        resolved.relative_to(repository_root)
    except ValueError:
        pass
    else:
        raise ValueError("%s must be outside the release checkout" % label)
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError("%s must be an external regular file" % label)
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ValueError("%s cannot be read" % label) from exc
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("%s size is invalid" % label)
    return resolved, raw


def _external_json(
    value: Path | None,
    repository_root: Path,
    label: str,
) -> tuple[Path, dict[str, Any], str]:
    path, raw = _external_file(value, repository_root, label)
    payload = strict_json_loads(raw, label)
    if not isinstance(payload, dict):
        raise ValueError("%s must be an object" % label)
    return path, payload, hashlib.sha256(raw).hexdigest()


def _external_directory(
    value: Path | None,
    repository_root: Path,
    label: str,
) -> Path:
    if value is None:
        raise ValueError("%s path is absent" % label)
    supplied = value.expanduser()
    if supplied.is_symlink() or has_link_or_reparse_component(supplied):
        raise ValueError("%s must not contain a symbolic link or reparse point" % label)
    try:
        resolved = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("%s path is unavailable" % label) from exc
    try:
        resolved.relative_to(repository_root)
    except ValueError:
        pass
    else:
        raise ValueError("%s must be outside the release checkout" % label)
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("%s must be an external directory" % label)
    return resolved


def _trust_keys(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    if set(payload) != _TRUST_ROOT_KEYS:
        raise ValueError("customer trust root fields are invalid")
    if payload.get("schema_version") != TRUST_ROOT_SCHEMA_VERSION:
        raise ValueError("customer trust root schema version is invalid")
    if payload.get("kind") != "smart-assistant-customer-acceptance-trust-root":
        raise ValueError("customer trust root kind is invalid")
    entries = payload.get("keys")
    if not isinstance(entries, list) or not entries:
        raise ValueError("customer trust root keys are invalid")
    result: dict[str, dict[str, str]] = {}
    seen_public_keys: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _TRUST_KEY_KEYS:
            raise ValueError("customer trust root key entry is invalid")
        key_id = _clean_identifier(entry.get("key_id"), "trust key id")
        if key_id in result:
            raise ValueError("customer trust root contains duplicate key id")
        purpose = entry.get("purpose")
        if purpose not in {"customer_acceptance", "smart_assistant_release"}:
            raise ValueError("customer trust root key purpose is invalid")
        public_key = clean_ed25519_public_key(
            entry.get("ed25519_public_key"), "customer trust root public key"
        )
        if public_key in seen_public_keys:
            raise ValueError(
                "customer trust root cannot reuse a key across trust purposes"
            )
        seen_public_keys.add(public_key)
        result[key_id] = {"purpose": purpose, "public_key": public_key}
    return result


def _record(
    *,
    status: str,
    evidence_present: bool,
    trust_root_present: bool,
    trust_root_pinned: bool,
    package_present: bool,
    report_present: bool,
    errors: list[str],
    evidence_sha256: str | None = None,
    trust_root_sha256: str | None = None,
    customer_package_sha256: str | None = None,
    customer_report_sha256: str | None = None,
    acceptance_id: str | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "passed": status == "VERIFIED",
        "evidence_present": evidence_present,
        "trust_root_present": trust_root_present,
        "trust_root_pinned": trust_root_pinned,
        "package_present": package_present,
        "report_present": report_present,
        "evidence_sha256": evidence_sha256,
        "trust_root_sha256": trust_root_sha256,
        "customer_package_sha256": customer_package_sha256,
        "customer_report_sha256": customer_report_sha256,
        "acceptance_id": acceptance_id,
        "execution_id": execution_id,
        "errors": sorted(set(errors)),
    }


def verify_customer_acceptance_evidence(
    repository_root: Path,
    *,
    expected_source_fingerprint: str,
    expected_git_commit: str,
    evidence_path: Path | None,
    trust_root_path: Path | None,
    expected_trust_root_sha256: str | None,
    package_root: Path | None,
    report_path: Path | None,
) -> dict[str, Any]:
    """Fail closed unless an external customer signed verified evidence."""

    root = repository_root.resolve()
    presence = {
        "evidence_present": evidence_path is not None,
        "trust_root_present": trust_root_path is not None,
        "trust_root_pinned": expected_trust_root_sha256 is not None,
        "package_present": package_root is not None,
        "report_present": report_path is not None,
    }
    if not any(presence.values()):
        return _record(status="ABSENT", errors=[], **presence)
    if not all(presence.values()):
        return _record(
            status="INCOMPLETE_EXTERNAL_INPUT",
            errors=[
                "external customer evidence, trust root, trust-root pin, package, and report are all required"
            ],
            **presence,
        )
    try:
        pinned = _clean_sha256(
            expected_trust_root_sha256, "customer trust-root sha256 pin"
        )
        _trust_path, trust_root, trust_sha256 = _external_json(
            trust_root_path, root, "customer trust root"
        )
        if trust_sha256 != pinned:
            raise ValueError("customer trust root does not match external pin")
        trusted = _trust_keys(trust_root)
        _evidence_path, evidence, evidence_sha256 = _external_json(
            evidence_path, root, "customer acceptance evidence"
        )
        if set(evidence) != _EVIDENCE_KEYS:
            raise ValueError("customer acceptance evidence fields are invalid")
        if evidence.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("customer acceptance evidence schema version is invalid")
        if evidence.get("kind") != "smart-assistant-customer-acceptance-evidence":
            raise ValueError("customer acceptance evidence kind is invalid")
        acceptance_id = _clean_identifier(
            evidence.get("acceptance_id"), "acceptance id"
        )
        _clean_identifier(evidence.get("customer_id"), "customer id")
        execution_id = _clean_identifier(
            evidence.get("execution_id"), "execution id"
        )
        report_run_id = _clean_identifier(
            evidence.get("customer_report_run_id"), "customer report run id"
        )
        report_event_chain_head = _clean_sha256(
            evidence.get("customer_report_event_chain_head"),
            "customer report event-chain head",
        )
        git_commit = evidence.get("git_commit")
        if not isinstance(git_commit, str) or not _COMMIT.fullmatch(git_commit):
            raise ValueError("customer acceptance Git commit is invalid")
        if git_commit != expected_git_commit:
            raise ValueError(
                "customer acceptance evidence is not bound to current Git commit"
            )
        if _clean_sha256(
            evidence.get("source_fingerprint_sha256"), "source fingerprint"
        ) != expected_source_fingerprint:
            raise ValueError(
                "customer acceptance evidence is not bound to current source tree"
            )
        _clean_sha256(evidence.get("artifact_sha256"), "artifact sha256")
        _clean_sha256(evidence.get("environment_sha256"), "environment sha256")
        executed_at = evidence.get("executed_at")
        if not isinstance(executed_at, str) or not executed_at:
            raise ValueError("customer acceptance execution timestamp is invalid")
        try:
            datetime.fromisoformat(executed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                "customer acceptance execution timestamp is invalid"
            ) from exc
        attestation = evidence.get("attestation")
        if not isinstance(attestation, dict) or set(attestation) != _ATTESTATION_KEYS:
            raise ValueError("customer acceptance attestation fields are invalid")
        key_id = _clean_identifier(
            attestation.get("key_id"), "customer acceptance signer"
        )
        signer = trusted.get(key_id)
        if signer is None or signer["purpose"] != "customer_acceptance":
            raise ValueError("customer acceptance signer is not trusted")
        signature = clean_ed25519_signature(
            attestation.get("signature"), "customer acceptance signature"
        )
        if not verify_ed25519_signature(
            signer["public_key"],
            signature,
            customer_acceptance_attestation_payload(evidence),
        ):
            raise ValueError("customer acceptance signature is invalid")

        package_dir = _external_directory(
            package_root, root, "customer acceptance package"
        )
        package_manifest = package_dir / "manifest.json"
        if not package_manifest.is_file() or package_manifest.is_symlink():
            raise ValueError("customer acceptance package manifest is unavailable")
        package_sha256 = sha256_file(package_manifest)
        if package_sha256 != _clean_sha256(
            evidence.get("customer_package_sha256"), "customer package sha256"
        ):
            raise ValueError("customer acceptance package hash is not signed")
        package = load_customer_package(package_dir, package_sha256)
        _validate_release_signature(package.baseline_release, trusted)
        _validate_release_signature(package.candidate_release, trusted)
        if package.candidate_release.git_commit != expected_git_commit:
            raise ValueError(
                "candidate release is not bound to current Git commit"
            )
        if (
            package.candidate_release.source_fingerprint_sha256
            != expected_source_fingerprint
        ):
            raise ValueError(
                "candidate release is not bound to current source tree"
            )
        if package.candidate_release.artifact_sha256 != evidence["artifact_sha256"]:
            raise ValueError(
                "candidate release artifact does not match acceptance evidence"
            )
        if (
            package.comparison_environment_sha256
            != evidence["environment_sha256"]
        ):
            raise ValueError(
                "comparison environment does not match acceptance evidence"
            )

        _report_path, report, report_sha256 = _external_json(
            report_path, root, "customer acceptance report"
        )
        if report_sha256 != _clean_sha256(
            evidence.get("customer_report_sha256"), "customer report sha256"
        ):
            raise ValueError("customer acceptance report hash is not signed")
        if report.get("status") != "completed" or report.get("passed") is not True:
            raise ValueError("customer acceptance report did not pass")
        if report.get("run_id") != report_run_id:
            raise ValueError(
                "customer acceptance evidence execution does not match report run"
            )
        if execution_id != report_run_id:
            raise ValueError(
                "customer acceptance execution id does not match report run"
            )
        if report.get("event_chain_head") != report_event_chain_head:
            raise ValueError(
                "customer acceptance evidence does not match report event chain"
            )
        failures = verify_customer_report(report, package)
        if failures:
            raise ValueError("customer acceptance report verification failed: %s" % failures[0])
    except (CustomerAcceptanceError, ValueError, TypeError, OSError) as exc:
        return _record(status="INVALID", errors=[str(exc)], **presence)
    return _record(
        status="VERIFIED",
        errors=[],
        evidence_sha256=evidence_sha256,
        trust_root_sha256=trust_sha256,
        customer_package_sha256=package_sha256,
        customer_report_sha256=report_sha256,
        acceptance_id=acceptance_id,
        execution_id=execution_id,
        **presence,
    )


def verify_configured_customer_acceptance_evidence(
    repository_root: Path,
    *,
    expected_source_fingerprint: str,
    expected_git_commit: str,
) -> dict[str, Any]:
    return verify_customer_acceptance_evidence(
        repository_root,
        expected_source_fingerprint=expected_source_fingerprint,
        expected_git_commit=expected_git_commit,
        evidence_path=(
            Path(os.environ[EVIDENCE_ENV]) if os.environ.get(EVIDENCE_ENV) else None
        ),
        trust_root_path=(
            Path(os.environ[TRUST_ROOT_ENV])
            if os.environ.get(TRUST_ROOT_ENV)
            else None
        ),
        expected_trust_root_sha256=os.environ.get(TRUST_ROOT_SHA256_ENV),
        package_root=(
            Path(os.environ[PACKAGE_ROOT_ENV])
            if os.environ.get(PACKAGE_ROOT_ENV)
            else None
        ),
        report_path=(
            Path(os.environ[REPORT_ENV]) if os.environ.get(REPORT_ENV) else None
        ),
    )


def _validate_release_signature(
    release: Any, trusted: dict[str, dict[str, str]]
) -> None:
    signer = trusted.get(release.signer_key_id)
    if signer is None or signer["purpose"] != "smart_assistant_release":
        raise ValueError("release signer is not trusted for SmartAssistant releases")
    if not verify_ed25519_signature(
        signer["public_key"],
        release.signature,
        release_attestation_payload(release),
    ):
        raise ValueError("SmartAssistant release signature is invalid")
