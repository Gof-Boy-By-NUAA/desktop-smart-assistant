"""Generate and independently verify the release evidence manifest.

The manifest is deliberately fail-closed.  It records what is available in the
working tree, but it never turns a missing customer/deployment artifact into a
PASS.  ``verify`` re-hashes every referenced file instead of trusting the
values written by ``generate``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2
REPORTS = {
    "retrieval_comparison": "benchmarks/results/cmrc2018-comparison.json",
    "retrieval_verification": "benchmarks/results/cmrc2018-comparison-verification.json",
    "knowledge_comparison": "benchmarks/results/cmrc2018-knowledge-comparison.json",
    "knowledge_verification": "benchmarks/results/cmrc2018-knowledge-comparison-verification.json",
    "memory_outbox": "benchmarks/results/cmrc2018-memory-outbox.json",
    "skills_selection": "benchmarks/results/cmrc2018-skills-selection.json",
    "customer_acceptance": "benchmarks/results/customer-skill-acceptance.json",
    "web_boundary_security": "benchmarks/results/web-boundary-security.json",
    "web_boundary_verification": "benchmarks/results/web-boundary-security-verification.json",
}
DATASETS = {
    "retrieval_cmrc2018": "benchmarks/.cache/cmrc2018-source/data/cmrc2018_dev.json",
    "skills_selection": "benchmarks/skills/github_issue_skill_selection.json",
}
# The digest covers the full deliverable source tree, rather than a hand-picked
# subset of "interesting" modules.  The digest is not a security signature;
# it is a stale-report detector and must be checked by a separately protected
# CI job before release.
SOURCE_PATHS = (".",)
EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    ".wrangler",
    "__pycache__",
    ".cache",
    "node_modules",
    "dist",
    "tmp",
    "workspace",
    "logs",
    "local",
    "results",
}
EXCLUDED_ROOT_PARTS = {"build", "ref"}
EXCLUDED_PATH_PREFIXES = {
    "desktop/build/build-work",
    "desktop/build/dist",
}
EXCLUDED_FILENAMES = {
    ".DS_Store",
    ".preview_secret",
    "config.json",
    "config.yaml",
    "client_config.json",
    "nohup.out",
    "plugins.json",
    "user_datas.pkl",
}


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_source_files(root: Path) -> Iterable[Path]:
    def is_excluded(relative: Path, *, directory: bool = False) -> bool:
        parts = relative.parts
        relative_text = relative.as_posix()
        if any(part in EXCLUDED_PARTS for part in parts):
            return True
        if parts and parts[0] in EXCLUDED_ROOT_PARTS:
            return True
        if any(
            relative_text == prefix or relative_text.startswith(prefix + "/")
            for prefix in EXCLUDED_PATH_PREFIXES
        ):
            return True
        if directory:
            return False
        return (
            relative.name in EXCLUDED_FILENAMES
            or relative.name.startswith("audit_")
            or relative.suffix in {".log", ".pyc"}
            or ".egg-info" in parts
        )

    seen: set[Path] = set()
    for raw in SOURCE_PATHS:
        path = root / raw
        if path.is_file():
            relative = path.relative_to(root)
            if not is_excluded(relative) and path not in seen:
                seen.add(path)
                yield path
            continue
        if not path.is_dir():
            continue
        for directory_text, directory_names, file_names in os.walk(
            path,
            topdown=True,
            followlinks=False,
        ):
            directory = Path(directory_text)
            relative_directory = directory.relative_to(root)
            directory_names[:] = [
                name
                for name in directory_names
                if not is_excluded(relative_directory / name, directory=True)
            ]
            for name in file_names:
                candidate = directory / name
                relative = candidate.relative_to(root)
                if is_excluded(relative) or candidate in seen:
                    continue
                seen.add(candidate)
                yield candidate


def source_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(_iter_source_files(root), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content_hash = sha256_file(path).encode("ascii")
        digest.update(relative + b"\0" + content_hash + b"\n")
    return digest.hexdigest()


def _git_state(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        return result.stdout.strip()

    def run_nul(*args: str) -> set[str] | None:
        """Read Git path output without C-style quoting non-ASCII filenames."""

        try:
            result = subprocess.run(
                ["git", *args],
                cwd=root,
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        return {
            os.fsdecode(value)
            for value in result.stdout.split(b"\0")
            if value
        }

    commit = run("rev-parse", "HEAD")
    prefix = run("rev-parse", "--show-prefix")
    status = run("status", "--porcelain", "--", ".")
    tracked = run_nul("ls-files", "-z", "--full-name", "--", ".") or set()
    source_files = list(_iter_source_files(root))
    if prefix is not None:
        normalized_prefix = prefix.replace("\\", "/")
        expected_tracked = {
            normalized_prefix + path.relative_to(root).as_posix()
            for path in source_files
        }
    else:
        expected_tracked = set()
    tracked_source_count = len(expected_tracked & tracked)
    all_sources_tracked = bool(expected_tracked) and expected_tracked <= tracked
    clean = status == ""
    commit_bound = bool(commit) and clean and all_sources_tracked
    return {
        "commit": commit or "ABSENT",
        "clean": clean,
        "commit_bound": commit_bound,
        "source_file_count": len(expected_tracked),
        "tracked_source_count": tracked_source_count,
        "all_source_paths_tracked": all_sources_tracked,
        "status_sha256": _sha256_bytes((status or "").encode("utf-8")),
    }


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _is_git_tracked_path(root: Path, path: Path) -> bool:
    """Return whether ``path`` is currently tracked by the enclosing Git tree.

    The release manifest is a generated, timestamped attestation artifact.  It
    cannot be committed to the same tree whose cleanliness it records: writing
    it would necessarily dirty that tree.  Source and formal input reports are
    still checked independently by :func:`_git_state` and by report hashes.
    """

    try:
        relative = path.resolve().relative_to(root)
    except ValueError:
        return False
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative.as_posix()],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    """Write a generated evidence artifact without exposing a partial JSON file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _report_record(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    record: dict[str, Any] = {
        "path": relative,
        "exists": path.is_file(),
        "sha256": sha256_file(path) if path.is_file() else None,
        "status": "ABSENT",
        "passed": False,
    }
    if not path.is_file():
        return record
    payload = _load_json(path)
    if payload is None:
        record["status"] = "INVALID_JSON"
        return record
    record["status"] = str(payload.get("status", "completed"))
    record["passed"] = payload.get("passed") is True
    record["schema_version"] = payload.get("schema_version")
    record["limitations"] = payload.get("limitations")
    if relative == REPORTS["skills_selection"]:
        from benchmarks.skills.runner import verify_skill_selection_report

        contract = verify_skill_selection_report(payload)
        record["declared_status"] = payload.get("status")
        record["contract_valid"] = contract["valid"] is True
        record["contract_errors"] = contract["errors"]
        if not record["contract_valid"]:
            record["passed"] = False
            record["status"] = "INVALID_REPORT_CONTRACT"
    return record


def _dataset_record(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    return {
        "path": relative,
        "exists": path.is_file(),
        "sha256": sha256_file(path) if path.is_file() else None,
    }


def _skills_dataset_record(root: Path) -> dict[str, Any]:
    """Record the immutable local silver fixture without calling it gold data."""

    from benchmarks.skills.dataset import EXPECTED_DATASET_SHA256

    record = _dataset_record(root, DATASETS["skills_selection"])
    record["expected_sha256"] = EXPECTED_DATASET_SHA256
    record["pinned"] = record["sha256"] == EXPECTED_DATASET_SHA256
    return record


def _current_implementation_fingerprints(root: Path) -> dict[str, tuple[str, str]]:
    from benchmarks.knowledge.compare import _comparison_fingerprint
    from benchmarks.memory.outbox import _implementation_fingerprint as memory_fingerprint
    from benchmarks.retrieval.compare import comparison_implementation_fingerprint
    from benchmarks.security.web_boundary import source_fingerprint as web_fingerprint
    from benchmarks.skills.runner import implementation_fingerprint as skills_fingerprint

    retrieval = comparison_implementation_fingerprint()
    web = web_fingerprint(root)
    return {
        "retrieval_comparison": ("comparison_implementation_sha256", retrieval),
        "retrieval_verification": ("comparison_implementation_sha256", retrieval),
        "knowledge_comparison": (
            "comparison_implementation_sha256", _comparison_fingerprint(root)
        ),
        "knowledge_verification": (
            "comparison_implementation_sha256", _comparison_fingerprint(root)
        ),
        "memory_outbox": ("implementation_sha256", memory_fingerprint()),
        "skills_selection": ("implementation_sha256", skills_fingerprint()),
        "web_boundary_security": ("source_fingerprint_sha256", web),
        "web_boundary_verification": ("source_fingerprint_sha256", web),
    }


def _apply_report_freshness(
    root: Path, reports: dict[str, dict[str, Any]]
) -> None:
    fingerprints = _current_implementation_fingerprints(root)
    for name, (field, current) in fingerprints.items():
        record = reports[name]
        payload = _load_json(root / record["path"]) if record["exists"] else None
        declared = payload.get(field) if payload is not None else None
        fresh = declared == current
        record["implementation_fingerprint_field"] = field
        record["declared_implementation_sha256"] = declared
        record["current_implementation_sha256"] = current
        record["fresh"] = fresh
        if record["passed"] and not fresh:
            record["passed"] = False
            record["status"] = "STALE_SOURCE"

    verification_sources = {
        "retrieval_verification": "retrieval_comparison",
        "knowledge_verification": "knowledge_comparison",
        "web_boundary_verification": "web_boundary_security",
    }
    for verifier_name, source_name in verification_sources.items():
        record = reports[verifier_name]
        payload = _load_json(root / record["path"]) if record["exists"] else None
        declared_report_sha256 = (
            payload.get("report_sha256") if payload is not None else None
        )
        current_report_sha256 = reports[source_name].get("sha256")
        linked = (
            isinstance(declared_report_sha256, str)
            and declared_report_sha256 == current_report_sha256
        )
        record["verified_report"] = source_name
        record["declared_report_sha256"] = declared_report_sha256
        record["current_report_sha256"] = current_report_sha256
        record["source_report_linked"] = linked
        if record["passed"] and not linked:
            record["passed"] = False
            record["status"] = "STALE_SOURCE_REPORT"


def _customer_acceptance_diagnostic(
    root: Path,
    *,
    expected_source_fingerprint: str,
    git: dict[str, Any],
) -> dict[str, Any]:
    """Preserve local crypto checks as diagnostics, never as release authority.

    Environment-selected keys and evidence files share the local checkout's
    trust domain.  They can help diagnose a package, but cannot establish the
    protected CI/registry/customer evidence required to lift customer or Skills
    production hard denials.
    """

    from benchmarks.evidence.customer_acceptance_case import (
        verify_configured_customer_acceptance_evidence,
    )

    diagnostic = verify_configured_customer_acceptance_evidence(
        root,
        expected_source_fingerprint=expected_source_fingerprint,
        expected_git_commit=str(git["commit"]),
    )
    return {
        **diagnostic,
        "local_diagnostic_passed": bool(diagnostic.get("passed") is True),
        "release_checkout_bound": bool(git["commit_bound"]),
        "external_protected_evidence": False,
        "release_passed": False,
        "release_status": "EXTERNAL_PROTECTED_EVIDENCE_ABSENT",
    }


def generate_manifest(
    root: Path | None = None,
    *,
    precomputed_source_fingerprint: str | None = None,
    precomputed_git_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = (root or _root()).resolve()
    current_source_fingerprint = (
        precomputed_source_fingerprint
        if precomputed_source_fingerprint is not None
        else source_fingerprint(root)
    )
    reports = {name: _report_record(root, path) for name, path in REPORTS.items()}
    _apply_report_freshness(root, reports)
    datasets = {
        name: _dataset_record(root, path) for name, path in DATASETS.items()
    }
    datasets["skills_selection"] = _skills_dataset_record(root)
    customer = reports["customer_acceptance"]
    skills_contract_valid = reports["skills_selection"].get("contract_valid") is True
    web_boundary_closed = (
        reports["web_boundary_security"]["passed"]
        and reports["web_boundary_verification"]["passed"]
    )
    git = precomputed_git_state or _git_state(root)
    from benchmarks.evidence.fde_case import (
        verify_configured_fde_case_evidence,
    )

    fde_case = verify_configured_fde_case_evidence(
        root,
        expected_source_fingerprint=current_source_fingerprint,
        expected_git_commit=str(git["commit"]),
    )
    # A customer signature over an uncommitted checkout is still evidence
    # about those bytes, but it is not release evidence.  Keep the raw
    # verifier result visible while refusing to lift the FDE hard denial
    # until the full source tree is clean and commit-bound.
    fde_case = {
        **fde_case,
        "release_checkout_bound": bool(git["commit_bound"]),
        "release_passed": bool(fde_case["passed"] and git["commit_bound"]),
    }
    customer_acceptance_case = _customer_acceptance_diagnostic(
        root,
        expected_source_fingerprint=current_source_fingerprint,
        git=git,
    )
    customer_accepted = False

    hard_denials = {
        "FDE_CASE_EVIDENCE": "YES" if fde_case["release_passed"] else "ABSENT",
        "TARGET_CUSTOMER_ACCEPTANCE": "YES" if customer_accepted else "NO",
        "CUSTOMER_ATTESTATION": "YES" if customer_accepted else "ABSENT",
        "CUSTOMER_TEST_EXECUTION": "NOT_RUN",
        # The legacy GitHub-title selector report is silver-label local
        # diagnostics, not customer gold data.  Only a verified external
        # customer package/report can establish the skills production gate.
        "SKILLS_GOLD_DATASET_VALID": "YES" if customer_accepted else "NO",
        "SKILLS_PRODUCTION_GATE_ELIGIBLE": (
            "YES" if customer_accepted else "NO"
        ),
        "SKILLS_LOCAL_REPORT_CONTRACT": "YES" if skills_contract_valid else "NO",
        "GIT_COMMIT_BOUND_EVIDENCE": "YES" if git["commit_bound"] else "ABSENT",
        "REMOTE_CI_REQUIRED_CHECKS": "NOT_RUN",
        "BRANCH_PROTECTION": "ABSENT",
        "SIGNED_RELEASE_ARTIFACT": "ABSENT",
        "REPRODUCIBLE_BUILD": "NOT_RUN",
        "DOCKER_BUILD": "NOT_RUN",
        "INSTALLER_SMOKE_TEST": "NOT_RUN",
        "MIGRATION_ROLLBACK_TEST": "NOT_RUN",
        "72H_SOAK": "NOT_RUN",
        "PRODUCTION_ALERT_FIRE_TEST": "NOT_RUN",
        "SESSION_CITATION_UI_CLOSED_LOOP": (
            "YES" if web_boundary_closed else "NO"
        ),
        # The in-repository verifier is valuable tamper detection, but it is
        # not an independent trust domain because the same PR can modify the
        # producer, verifier and workflow. Never label it independent evidence.
        "KNOWLEDGE_LOCAL_RECOMPUTATION_VERIFIED": (
            "YES" if reports["knowledge_verification"]["passed"] else "NO"
        ),
        "KNOWLEDGE_INDEPENDENT_VERIFICATION": "NO",
        "EXTERNAL_VERIFIER_ATTESTATION": "ABSENT",
        "SESSION_CITATION_PRODUCTION_VERIFIED": "NOT_RUN",
    }
    required_conditions = {
        "all_formal_reports_present": all(item["exists"] for item in reports.values()),
        "retrieval_formal_gate": reports["retrieval_comparison"]["passed"] and reports["retrieval_verification"]["passed"],
        "knowledge_formal_gate": (
            reports["knowledge_comparison"]["passed"]
            and reports["knowledge_verification"]["passed"]
        ),
        "memory_formal_gate": reports["memory_outbox"]["passed"],
        "skills_local_report_contract": skills_contract_valid,
        "skills_pinned_dataset": datasets["skills_selection"]["pinned"],
        "skills_formal_gate": skills_contract_valid and customer_accepted,
        "fde_case_evidence": fde_case["release_passed"],
        "customer_acceptance": customer_accepted,
        "git_commit_bound_evidence": git["commit_bound"],
        "clean_release_tree": git["clean"],
        "reproducible_build": False,
        "docker_build": False,
        "installer_smoke_test": False,
        "migration_rollback_test": False,
        "soak_and_alert_test": False,
        "session_citation_closed_loop": web_boundary_closed,
        "external_verifier_attestation": False,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository_root": str(root),
        "source_fingerprint_sha256": current_source_fingerprint,
        "git": git,
        "reports": reports,
        "datasets": datasets,
        "fde_case": fde_case,
        "customer_acceptance_case": customer_acceptance_case,
        "hard_denials": hard_denials,
        "required_conditions": required_conditions,
        "passed": all(required_conditions.values()),
    }


def verify_manifest(manifest: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    root = (root or _root()).resolve()
    checks: list[dict[str, Any]] = []

    if not isinstance(manifest, dict):
        manifest = {}

    def check(name: str, actual: Any, expected: Any, passed: bool) -> None:
        checks.append({"name": name, "actual": actual, "expected": expected, "passed": bool(passed)})

    required_manifest_fields = {
        "schema_version",
        "generated_at",
        "repository_root",
        "source_fingerprint_sha256",
        "git",
        "reports",
        "datasets",
        "fde_case",
        "customer_acceptance_case",
        "hard_denials",
        "required_conditions",
        "passed",
    }
    check(
        "manifest.schema",
        sorted(manifest.keys()),
        sorted(required_manifest_fields),
        set(manifest) == required_manifest_fields,
    )
    check(
        "schema_version",
        manifest.get("schema_version"),
        SCHEMA_VERSION,
        manifest.get("schema_version") == SCHEMA_VERSION,
    )
    check(
        "repository_root",
        manifest.get("repository_root"),
        str(root),
        manifest.get("repository_root") == str(root),
    )
    check(
        "generated_at",
        manifest.get("generated_at"),
        "non-empty string",
        isinstance(manifest.get("generated_at"), str)
        and bool(manifest["generated_at"]),
    )
    current_source_fingerprint = source_fingerprint(root)
    check(
        "source_fingerprint",
        manifest.get("source_fingerprint_sha256"),
        current_source_fingerprint,
        manifest.get("source_fingerprint_sha256") == current_source_fingerprint,
    )
    current_git = _git_state(root)
    declared_git = manifest.get("git")
    declared_git = declared_git if isinstance(declared_git, dict) else {}
    for field, expected in current_git.items():
        check(
            f"git.{field}",
            declared_git.get(field),
            expected,
            declared_git.get(field) == expected,
        )

    declared_reports = manifest.get("reports")
    declared_reports = declared_reports if isinstance(declared_reports, dict) else {}
    check(
        "reports.schema",
        sorted(declared_reports.keys()),
        sorted(REPORTS),
        set(declared_reports) == set(REPORTS),
    )
    current_reports = {
        name: _report_record(root, relative) for name, relative in REPORTS.items()
    }
    _apply_report_freshness(root, current_reports)
    for name in REPORTS:
        expected = declared_reports.get(name, {})
        expected = expected if isinstance(expected, dict) else {}
        current = current_reports[name]
        check(f"report.{name}.record", expected, current, expected == current)
        check(f"report.{name}.sha256", expected.get("sha256"), current.get("sha256"), expected.get("sha256") == current.get("sha256"))
        check(f"report.{name}.status", expected.get("status"), current.get("status"), expected.get("status") == current.get("status"))
        check(f"report.{name}.passed", expected.get("passed"), current.get("passed"), expected.get("passed") == current.get("passed"))
        check(f"report.{name}.fresh", expected.get("fresh"), current.get("fresh"), expected.get("fresh") == current.get("fresh"))
        for linkage_field in (
            "verified_report",
            "declared_report_sha256",
            "current_report_sha256",
            "source_report_linked",
        ):
            if linkage_field in current:
                check(
                    f"report.{name}.{linkage_field}",
                    expected.get(linkage_field),
                    current.get(linkage_field),
                    expected.get(linkage_field) == current.get(linkage_field),
                )
        if name == "skills_selection":
            for field in (
                "schema_version",
                "declared_status",
                "limitations",
                "contract_valid",
                "contract_errors",
            ):
                check(
                    f"report.skills_selection.{field}",
                    expected.get(field),
                    current.get(field),
                    expected.get(field) == current.get(field),
                )

    from benchmarks.evidence.fde_case import (
        verify_configured_fde_case_evidence,
    )
    current_fde_case = verify_configured_fde_case_evidence(
        root,
        expected_source_fingerprint=current_source_fingerprint,
        expected_git_commit=str(current_git["commit"]),
    )
    current_fde_case = {
        **current_fde_case,
        "release_checkout_bound": bool(current_git["commit_bound"]),
        "release_passed": bool(
            current_fde_case["passed"] and current_git["commit_bound"]
        ),
    }
    check(
        "fde_case",
        manifest.get("fde_case"),
        current_fde_case,
        manifest.get("fde_case") == current_fde_case,
    )
    current_customer_acceptance_case = _customer_acceptance_diagnostic(
        root,
        expected_source_fingerprint=current_source_fingerprint,
        git=current_git,
    )
    check(
        "customer_acceptance_case",
        manifest.get("customer_acceptance_case"),
        current_customer_acceptance_case,
        manifest.get("customer_acceptance_case")
        == current_customer_acceptance_case,
    )

    declared_datasets = manifest.get("datasets")
    declared_datasets = (
        declared_datasets if isinstance(declared_datasets, dict) else {}
    )
    check(
        "datasets.schema",
        sorted(declared_datasets.keys()),
        sorted(DATASETS),
        set(declared_datasets) == set(DATASETS),
    )
    for name, relative in DATASETS.items():
        expected = declared_datasets.get(name, {})
        expected = expected if isinstance(expected, dict) else {}
        current = (
            _skills_dataset_record(root)
            if name == "skills_selection"
            else _dataset_record(root, relative)
        )
        check(f"dataset.{name}.record", expected, current, expected == current)
        check(f"dataset.{name}.sha256", expected.get("sha256"), current.get("sha256"), expected.get("sha256") == current.get("sha256"))

    recomputed = generate_manifest(
        root,
        precomputed_source_fingerprint=current_source_fingerprint,
        precomputed_git_state=current_git,
    )
    required = manifest.get("required_conditions")
    recomputed_required = recomputed["required_conditions"]
    check("required_conditions", required, recomputed_required, required == recomputed_required)
    declared_hard_denials = manifest.get("hard_denials")
    recomputed_hard_denials = recomputed["hard_denials"]
    check(
        "hard_denials",
        declared_hard_denials,
        recomputed_hard_denials,
        isinstance(declared_hard_denials, dict)
        and declared_hard_denials == recomputed_hard_denials,
    )
    expected_passed = all(bool(value) for value in recomputed_required.values())
    check("passed", manifest.get("passed"), expected_passed, manifest.get("passed") is expected_passed)
    integrity_passed = all(item["passed"] for item in checks)
    return {
        "schema_version": 1,
        "passed": integrity_passed and expected_passed,
        "integrity_passed": integrity_passed,
        "manifest_sha256": _sha256_bytes(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")),
        "checks": checks,
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or verify release evidence")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help=(
            "generated manifest artifact path; generation refuses a Git-tracked "
            "path so the attested checkout remains clean"
        ),
    )
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(arguments)
    root = _root()
    output = args.output.resolve()
    if args.verify:
        manifest = _load_json(output)
        if manifest is None:
            print(json.dumps({"passed": False, "error": "invalid manifest"}, ensure_ascii=False, indent=2))
            return 1
        result = verify_manifest(manifest, root)
    else:
        if _is_git_tracked_path(root, output):
            result = {
                "passed": False,
                "error": (
                    "refusing to write a release manifest to a Git-tracked path; "
                    "generate it as an external release artifact instead"
                ),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 1
        manifest = generate_manifest(root)
        _write_json_atomically(output, manifest)
        result = {"schema_version": 1, "passed": manifest["passed"], "manifest": manifest}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
