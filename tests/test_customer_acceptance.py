"""客户包、受控技能注入和独立证据复算测试。"""

from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.memory.governance import IdentityContext
from agent.skills.governance import (
    GovernedSkillRepository,
    GovernedSkillService,
    SkillProposal,
    SourceEvidence,
)
from benchmarks.customer import (
    ControlledCustomerAcceptanceRunner,
    CustomerCaseExecutor,
    CustomerExecutionRequest,
    CustomerExecutionResult,
    SubprocessCustomerCaseExecutor,
    load_customer_package,
)
from benchmarks.customer import __main__ as customer_main_module
from benchmarks.customer.__main__ import main as customer_main
from benchmarks.customer import executor as customer_executor_module
from benchmarks.customer import package as customer_package_module
from benchmarks.customer.attestation import (
    execution_attestation_payload,
    release_identity_sha256,
    release_attestation_payload,
)
from benchmarks.customer.contracts import (
    CustomerExecutionError,
    CustomerPackageError,
    CustomerRelease,
)
from benchmarks.customer.json_utils import canonical_json_bytes, sha256_json
from benchmarks.customer.ledger import CustomerExecutionLedger
from benchmarks.customer.runner import (
    _implementation_fingerprint as runner_implementation_fingerprint,
    pending_customer_report,
)
from benchmarks.customer.verify import (
    _implementation_fingerprint as verifier_implementation_fingerprint,
    verify_customer_report,
)
from benchmarks.customer.executor import (
    execution_request_sha256,
    execution_snapshot_sha256,
)


_TENANT_ID = "customer-tenant"
_MODEL_ID = "customer-model@2026-07"
_EXECUTOR_ARTIFACT_SHA256 = "d" * 64
_TEST_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(b"\x11" * 32)
_TEST_PUBLIC_KEY_HEX = _TEST_PRIVATE_KEY.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
).hex()
_RELEASE_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(b"\x12" * 32)


def _signed_release(
    release_id: str,
    commit_fill: str,
    source_fill: str,
    artifact_fill: str,
    sbom_fill: str,
) -> CustomerRelease:
    unsigned = CustomerRelease(
        release_id=release_id,
        git_commit=commit_fill * 40,
        source_fingerprint_sha256=source_fill * 64,
        artifact_sha256=artifact_fill * 64,
        sbom_sha256=sbom_fill * 64,
        signer_key_id="fixture-smart-assistant-release",
        signature="",
    )
    return CustomerRelease(
        **{
            **unsigned.__dict__,
            "signature": _RELEASE_PRIVATE_KEY.sign(
                canonical_json_bytes(release_attestation_payload(unsigned))
            ).hex(),
        }
    )


_BASELINE_RELEASE = _signed_release(
    "smart-assistant-baseline-v1", "a", "b", "c", "d"
)
_CANDIDATE_RELEASE = _signed_release(
    "smart-assistant-candidate-v2", "e", "f", "1", "2"
)
_COMPARISON_ENVIRONMENT = {
    "agent_runtime_sha256": "3" * 64,
    "container_image_sha256": "4" * 64,
    "machine_profile_sha256": "5" * 64,
    "network_policy_sha256": "6" * 64,
    "os_image_sha256": "7" * 64,
    "provider_model_revision_sha256": "8" * 64,
}


def _release_manifest(release: CustomerRelease):
    return {
        "release_id": release.release_id,
        "git_commit": release.git_commit,
        "source_fingerprint_sha256": release.source_fingerprint_sha256,
        "artifact_sha256": release.artifact_sha256,
        "sbom_sha256": release.sbom_sha256,
        "signer_key_id": release.signer_key_id,
        "signature": release.signature,
    }


def _comparison_manifest():
    return {
        "execution_environment": dict(_COMPARISON_ENVIRONMENT),
        "execution_environment_sha256": sha256_json(
            {
                "schema_version": 1,
                **_COMPARISON_ENVIRONMENT,
            }
        ),
    }


class FixtureCustomerExecutor(CustomerCaseExecutor):
    """记录两臂请求，并让候选臂在测试数据上稳定改进。"""

    def __init__(self):
        self.requests = []

    @property
    def executor_id(self) -> str:
        return "fixture-customer-executor"

    @property
    def executor_version(self) -> str:
        return "1.0.0"

    def execute(self, request):
        self.requests.append(request)
        output = (
            request.case_input["candidate_output"]
            if request.skill is not None
            else {"answer": "wrong"}
        )
        latency_ms = 0.8 if request.skill is not None else 1.0
        cpu_time_ms = 0.6 if request.skill is not None else 1.0
        peak_rss_bytes = 800 if request.skill is not None else 1000
        snapshot_sha256 = execution_snapshot_sha256(request)
        request_sha256 = execution_request_sha256(request)
        attestation = execution_attestation_payload(
            run_id=request.run_id,
            case_id=request.case_id,
            arm=request.arm,
            request_sha256=request_sha256,
            execution_snapshot_sha256=snapshot_sha256,
            output_sha256=sha256_json(output),
            latency_ms=latency_ms,
            cpu_time_ms=cpu_time_ms,
            peak_rss_bytes=peak_rss_bytes,
            input_tokens=10,
            output_tokens=2,
            comparison_environment_sha256=(
                request.comparison_environment_sha256
            ),
            requested_release_identity_sha256=(
                request.requested_release_identity_sha256
            ),
            observed_release_identity_sha256=(
                request.requested_release_identity_sha256
            ),
            executor_artifact_sha256=_EXECUTOR_ARTIFACT_SHA256,
        )
        return CustomerExecutionResult(
            output=output,
            latency_ms=latency_ms,
            cpu_time_ms=cpu_time_ms,
            peak_rss_bytes=peak_rss_bytes,
            input_tokens=10,
            output_tokens=2,
            execution_snapshot_sha256=snapshot_sha256,
            request_sha256=request_sha256,
            requested_release_identity_sha256=(
                request.requested_release_identity_sha256
            ),
            observed_release_identity_sha256=(
                request.requested_release_identity_sha256
            ),
            executor_artifact_sha256=_EXECUTOR_ARTIFACT_SHA256,
            attestation_signature=_TEST_PRIVATE_KEY.sign(
                canonical_json_bytes(attestation)
            ).hex(),
        )


class MismatchedSnapshotExecutor(FixtureCustomerExecutor):
    """证明通用执行器也不能伪造两臂环境快照。"""

    def execute(self, request):
        result = super().execute(request)
        return CustomerExecutionResult(
            output=result.output,
            latency_ms=result.latency_ms,
            cpu_time_ms=result.cpu_time_ms,
            peak_rss_bytes=result.peak_rss_bytes,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            execution_snapshot_sha256="0" * 64,
            request_sha256=result.request_sha256,
            requested_release_identity_sha256=(
                result.requested_release_identity_sha256
            ),
            observed_release_identity_sha256=(
                result.observed_release_identity_sha256
            ),
            executor_artifact_sha256=result.executor_artifact_sha256,
            attestation_signature=result.attestation_signature,
        )


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _candidate(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    repository = GovernedSkillRepository(
        skills_dir / ".system" / "governed-skills.db"
    )
    service = GovernedSkillService(repository, skills_dir, _TENANT_ID)
    identity = IdentityContext(
        tenant_id=_TENANT_ID,
        actor_user_id="proposer",
        roles=frozenset({"skill:propose"}),
        trace_id="trace-customer-proposal",
        auth_source="customer-test",
    )
    source = b'{"run":"verified-customer-trace"}'
    record = service.propose(
        identity,
        SkillProposal(
            name="customer-data-check",
            description="在提交答案前核对客户数据。",
            applicability=("客户数据任务",),
            steps=("读取数据", "核对结果"),
            validation_rules=("结果必须通过客户 Oracle",),
            contraindications=("缺少客户数据时禁止使用",),
            model_compatibility=(_MODEL_ID,),
            sources=(
                SourceEvidence(
                    source_type="execution-trace",
                    source_ref="trace://customer/1",
                    payload=source,
                    sha256=_sha(source),
                ),
            ),
            idempotency_key="customer-proposal",
        ),
    )
    return repository, skills_dir, record


def _package(tmp_path: Path, record, case_count: int = 30):
    skill_id = record.skill_id
    root = tmp_path / "customer-package"
    root.mkdir()
    cases = {
        "cases": [
            {
                "case_id": "case-%03d" % index,
                "input": {
                    "candidate_output": {"answer": "expected-%03d" % index},
                },
                "oracle": {"answer": "expected-%03d" % index},
                "critical": index <= 5,
            }
            for index in range(1, case_count + 1)
        ]
    }
    cases_payload = json.dumps(
        cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    (root / "cases.json").write_bytes(cases_payload)
    manifest = {
        "schema_version": 2,
        "package_id": "customer-package-v1",
        "tenant_id": _TENANT_ID,
        "model": {
            "id": _MODEL_ID,
            "parameters": {"temperature": 0, "seed": 42},
            "endpoint_sha256": "c" * 64,
            "prompt_sha256": "a" * 64,
            "tools_sha256": "b" * 64,
        },
        "comparison": _comparison_manifest(),
        "releases": {
            "baseline": _release_manifest(_BASELINE_RELEASE),
            "candidate": _release_manifest(_CANDIDATE_RELEASE),
        },
        "attestation": {
            "executor": {
                "id": "fixture-customer-executor",
                "version": "1.0.0",
                "artifact_sha256": _EXECUTOR_ARTIFACT_SHA256,
                "ed25519_public_key": _TEST_PUBLIC_KEY_HEX,
            },
            "judge": None,
        },
        "oracle": {"id": "customer-oracle-v1", "kind": "deterministic"},
        "cases": {
            "path": "cases.json",
            "sha256": _sha(cases_payload),
            "count": case_count,
        },
        "skills": {
            "allowed": [skill_id],
            "forbidden": [],
            "candidate": {
                "skill_id": skill_id,
                "version": record.version,
                "content_sha256": record.content_hash,
            },
        },
        "thresholds": {
            "minimum_success_rate_delta": 0.50,
            "maximum_regressions": 0,
            "maximum_latency_ratio": 1.0,
            "minimum_throughput_ratio": 1.1,
            "maximum_cpu_time_ratio": 1.0,
            "maximum_peak_rss_ratio": 1.0,
            "maximum_total_tokens": case_count * 30,
            "minimum_paired_samples": 30,
        },
    }
    manifest_payload = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    (root / "manifest.json").write_bytes(manifest_payload)
    return root, _sha(manifest_payload)


def _tree_hashes(root: Path):
    return {
        path.relative_to(root).as_posix(): _sha(path.read_bytes())
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith("-shm")
    }


def _claim_customer_ledger_process(path: str, start, results, run_id: str) -> None:
    """Spawn-safe helper exercising the database claim across real processes."""

    try:
        if not start.wait(timeout=15):
            results.put(("error", "start_timeout"))
            return
        claim = CustomerExecutionLedger(Path(path)).claim_run(
            "a" * 64,
            "b" * 64,
            run_id,
            "c" * 64,
            "d" * 64,
            [
                {
                    "case_id": "case-1",
                    "arm": "baseline",
                    "request_sha256": "e" * 64,
                }
            ],
        )
        results.put(("ok", claim["claim_status"]))
    except BaseException as error:
        results.put(("error", type(error).__name__))


def _rehash_events(report):
    previous = "0" * 64
    for event in report["events"]:
        event["previous_hash"] = previous
        unsigned = {
            "sequence": event["sequence"],
            "event_type": event["event_type"],
            "payload": event["payload"],
            "previous_hash": event["previous_hash"],
        }
        event["event_hash"] = _sha(canonical_json_bytes(unsigned))
        previous = event["event_hash"]
    report["event_chain_head"] = previous


def _resign_execution_event(report, event):
    payload = event["payload"]
    attestation = execution_attestation_payload(
        run_id=report["run_id"],
        case_id=payload["case_id"],
        arm=payload["arm"],
        request_sha256=payload["request_sha256"],
        execution_snapshot_sha256=payload["execution_snapshot_sha256"],
        output_sha256=payload["output_sha256"],
        latency_ms=payload["latency_ms"],
        cpu_time_ms=payload["cpu_time_ms"],
        peak_rss_bytes=payload["peak_rss_bytes"],
        input_tokens=payload["input_tokens"],
        output_tokens=payload["output_tokens"],
        comparison_environment_sha256=_comparison_manifest()[
            "execution_environment_sha256"
        ],
        requested_release_identity_sha256=payload[
            "requested_release_identity_sha256"
        ],
        observed_release_identity_sha256=payload[
            "observed_release_identity_sha256"
        ],
        executor_artifact_sha256=payload["executor_artifact_sha256"],
    )
    payload["executor_attestation_signature"] = _TEST_PRIVATE_KEY.sign(
        canonical_json_bytes(attestation)
    ).hex()


def test_missing_customer_inputs_can_never_be_reported_as_passed():
    report = pending_customer_report(["package_root", "manifest_sha256"])

    assert report["status"] == "pending_customer_inputs"
    assert report["passed"] is False
    assert verify_customer_report(report) == ()


def test_customer_package_rejects_case_tampering(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        (root / "cases.json").write_text("{\"cases\":[]}", encoding="utf-8")

        with pytest.raises(CustomerPackageError, match="客户场景 SHA-256"):
            load_customer_package(root, manifest_sha256)
    finally:
        repository.close()


def test_customer_package_rejects_reparse_component(monkeypatch, tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        monkeypatch.setattr(
            customer_package_module,
            "has_link_or_reparse_component",
            lambda path: True,
        )

        with pytest.raises(CustomerPackageError, match="重解析点"):
            load_customer_package(root, manifest_sha256)
    finally:
        repository.close()


def test_customer_package_rejects_infinite_threshold(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, _ = _package(tmp_path, record)
        manifest_path = root / "manifest.json"
        payload = manifest_path.read_bytes().replace(
            b'"maximum_latency_ratio":1.0',
            b'"maximum_latency_ratio":1e309',
        )
        manifest_path.write_bytes(payload)

        with pytest.raises(
            CustomerPackageError, match="maximum_latency_ratio"
        ):
            load_customer_package(root, _sha(payload))
    finally:
        repository.close()


def test_customer_package_rejects_cloned_case_inputs(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, _ = _package(tmp_path, record)
        manifest_path = root / "manifest.json"
        cases_path = root / "cases.json"
        cases = json.loads(cases_path.read_text(encoding="utf-8"))
        cases["cases"][1]["input"] = copy.deepcopy(cases["cases"][0]["input"])
        cases["cases"][1]["oracle"] = copy.deepcopy(cases["cases"][0]["oracle"])
        cases_payload = json.dumps(
            cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        cases_path.write_bytes(cases_payload)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["cases"]["sha256"] = _sha(cases_payload)
        manifest_payload = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        manifest_path.write_bytes(manifest_payload)

        with pytest.raises(CustomerPackageError, match="重复任务输入"):
            load_customer_package(root, _sha(manifest_payload))
    finally:
        repository.close()


@pytest.mark.parametrize(
    "field_name",
    (
        "release_id",
        "git_commit",
        "source_fingerprint_sha256",
        "artifact_sha256",
        "sbom_sha256",
    ),
)
def test_customer_package_rejects_non_distinct_release_identity(
    tmp_path, field_name
):
    repository, _, record = _candidate(tmp_path)
    try:
        root, _ = _package(tmp_path, record)
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["releases"]["candidate"][field_name] = manifest["releases"][
            "baseline"
        ][field_name]
        payload = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        manifest_path.write_bytes(payload)

        with pytest.raises(
            CustomerPackageError, match="baseline.*candidate"
        ):
            load_customer_package(root, _sha(payload))
    finally:
        repository.close()


def test_customer_package_rejects_environment_descriptor_hash_mismatch(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, _ = _package(tmp_path, record)
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["comparison"]["execution_environment"]["os_image_sha256"] = (
            "0" * 64
        )
        payload = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        manifest_path.write_bytes(payload)

        with pytest.raises(
            CustomerPackageError, match="execution_environment_sha256"
        ):
            load_customer_package(root, _sha(payload))
    finally:
        repository.close()


def test_customer_package_requires_at_least_thirty_paired_samples(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record, case_count=29)

        with pytest.raises(
            CustomerPackageError, match="minimum_paired_samples"
        ):
            load_customer_package(root, manifest_sha256)
    finally:
        repository.close()


def test_controlled_candidate_arm_passes_and_does_not_mutate_skill_store(tmp_path):
    repository, skills_dir, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        executor = FixtureCustomerExecutor()
        runner = ControlledCustomerAcceptanceRunner(
            repository, _TENANT_ID, executor,
            execution_ledger_path=tmp_path / "customer-execution-ledger.sqlite3",
        )
        before = _tree_hashes(skills_dir)

        report = runner.run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )

        assert report["status"] == "completed"
        assert report["passed"] is True
        assert report["metrics"]["baseline_passed"] == 0
        assert report["metrics"]["candidate_passed"] == 30
        assert report["metrics"]["paired_delta_ci95_lower"] > 0
        assert report["metrics"]["latency_ratio"] == pytest.approx(0.8)
        assert report["metrics"]["throughput_ratio"] == pytest.approx(1.25)
        assert report["metrics"]["cpu_time_ratio"] == pytest.approx(0.6)
        assert report["metrics"]["peak_rss_ratio"] == pytest.approx(0.8)
        assert {
            gate["name"] for gate in report["gates"]
        } >= {
            "performance.minimum_serial_throughput_ratio",
            "resource.maximum_cpu_time_ratio",
            "resource.maximum_peak_rss_ratio",
        }
        assert verify_customer_report(report, package) == ()
        round_tripped = json.loads(
            json.dumps(report, ensure_ascii=False, allow_nan=False)
        )
        assert verify_customer_report(round_tripped, package) == ()
        assert _tree_hashes(skills_dir) == before
        baseline = [item for item in executor.requests if item.arm == "baseline"]
        candidate = [item for item in executor.requests if item.arm == "candidate"]
        assert all(item.skill is None for item in baseline)
        assert all(item.skill["skill_id"] == record.skill_id for item in candidate)
        assert all(item.model_id == _MODEL_ID for item in executor.requests)
        assert all(
            item.model_parameters == {"temperature": 0, "seed": 42}
            for item in executor.requests
        )
    finally:
        repository.close()


def test_customer_execution_ledger_allows_only_one_process_claim(tmp_path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    ledger_path = tmp_path / "customer-execution-ledger.sqlite3"
    processes = [
        context.Process(
            target=_claim_customer_ledger_process,
            args=(str(ledger_path), start, results, "run-%d" % index),
        )
        for index in range(2)
    ]
    try:
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=30)
        assert all(process.exitcode == 0 for process in processes)
        outcomes = [results.get(timeout=10) for _ in processes]
        assert sorted(outcomes) == [("ok", "claimed"), ("ok", "in_doubt")]
        summary = CustomerExecutionLedger(ledger_path).describe_run(
            "a" * 64,
            "b" * 64,
        )
        assert summary is not None
        assert summary["state"] == "running"
        assert summary["operation_count"] == 1
        assert summary["operation_states"] == {"planned": 1}
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        results.close()


def test_completed_customer_run_is_returned_without_reexecution(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        ledger_path = tmp_path / "customer-execution-ledger.sqlite3"
        first_executor = FixtureCustomerExecutor()
        first_runner = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            first_executor,
            execution_ledger_path=ledger_path,
        )

        first_report = first_runner.run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )

        replay_executor = FixtureCustomerExecutor()
        replay_runner = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            replay_executor,
            execution_ledger_path=ledger_path,
        )
        replay_report = replay_runner.run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )

        assert len(first_executor.requests) == len(package.cases) * 2
        assert replay_executor.requests == []
        assert replay_report == first_report
        assert verify_customer_report(replay_report, package) == ()

        tampered = copy.deepcopy(first_report)
        tampered["metrics"]["candidate_passed"] = 0
        with sqlite3.connect(ledger_path) as connection:
            connection.execute(
                """
                UPDATE customer_acceptance_runs
                SET report_json = ?, report_sha256 = ?
                WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                """,
                (
                    canonical_json_bytes(tampered).decode("utf-8"),
                    sha256_json(tampered),
                    package.manifest_sha256,
                    package.cases_sha256,
                ),
            )
        tampered_executor = FixtureCustomerExecutor()
        with pytest.raises(
            CustomerPackageError,
            match="independent verification",
        ):
            ControlledCustomerAcceptanceRunner(
                repository,
                _TENANT_ID,
                tampered_executor,
                execution_ledger_path=ledger_path,
            ).run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256=record.content_hash,
            )
        assert tampered_executor.requests == []
    finally:
        repository.close()


def test_customer_execution_ledger_rejects_manual_completion_of_unsettled_plan(
    tmp_path,
):
    ledger = CustomerExecutionLedger(
        tmp_path / "customer-execution-ledger.sqlite3"
    )
    manifest_sha256 = "a" * 64
    cases_sha256 = "b" * 64
    run_id = "manual-completion-test"
    request_sha256 = "e" * 64
    assert ledger.claim_run(
        manifest_sha256,
        cases_sha256,
        run_id,
        "c" * 64,
        "d" * 64,
        [
            {
                "case_id": "case-1",
                "arm": "baseline",
                "request_sha256": request_sha256,
            }
        ],
    )["claim_status"] == "claimed"
    with pytest.raises(CustomerPackageError, match="does not match"):
        ledger.claim_case_operation(
            manifest_sha256,
            cases_sha256,
            run_id,
            "case-1",
            "baseline",
            "0" * 64,
        )
    with pytest.raises(CustomerPackageError, match="not completely settled"):
        ledger.complete_run(
            manifest_sha256,
            cases_sha256,
            run_id,
            {
                "status": "completed",
                "passed": True,
                "run_id": run_id,
                "package": {
                    "manifest_sha256": manifest_sha256,
                    "cases_sha256": cases_sha256,
                },
                "events": [
                    {
                        "event_type": "case.executed",
                        "payload": {
                            "case_id": "case-1",
                            "arm": "baseline",
                            "request_sha256": request_sha256,
                        },
                    }
                ],
            },
        )
    summary = ledger.describe_run(manifest_sha256, cases_sha256)
    assert summary is not None
    assert summary["state"] == "running"
    assert summary["operation_states"] == {"planned": 1}


def test_commit_success_followed_by_caller_crash_never_reexecutes(
    monkeypatch,
    tmp_path,
):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        ledger_path = tmp_path / "customer-execution-ledger.sqlite3"
        executor = FixtureCustomerExecutor()
        runner = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            executor,
            execution_ledger_path=ledger_path,
        )
        original = CustomerExecutionLedger.complete_run

        def crash_after_commit(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise SystemExit("simulated disconnect after final commit")

        monkeypatch.setattr(
            CustomerExecutionLedger,
            "complete_run",
            crash_after_commit,
        )
        with pytest.raises(SystemExit, match="disconnect after final commit"):
            runner.run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256=record.content_hash,
            )
        assert len(executor.requests) == len(package.cases) * 2
        monkeypatch.setattr(CustomerExecutionLedger, "complete_run", original)

        replay_executor = FixtureCustomerExecutor()
        replay_report = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            replay_executor,
            execution_ledger_path=ledger_path,
        ).run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )

        assert replay_report["status"] == "completed"
        assert replay_report["passed"] is True
        assert replay_executor.requests == []
        assert verify_customer_report(replay_report, package) == ()
    finally:
        repository.close()


def test_customer_execution_ledger_rejects_out_of_order_transitions(tmp_path):
    ledger = CustomerExecutionLedger(
        tmp_path / "customer-execution-ledger.sqlite3"
    )
    manifest_sha256 = "a" * 64
    cases_sha256 = "b" * 64
    run_id = "state-machine-test"
    request_sha256 = "e" * 64
    operation = (
        manifest_sha256,
        cases_sha256,
        run_id,
        "case-1",
        "baseline",
        request_sha256,
    )
    ledger.claim_run(
        manifest_sha256,
        cases_sha256,
        run_id,
        "c" * 64,
        "d" * 64,
        [
            {
                "case_id": "case-1",
                "arm": "baseline",
                "request_sha256": request_sha256,
            }
        ],
    )
    with pytest.raises(CustomerPackageError, match="transition was rejected"):
        ledger.record_execution_receipt(*operation, receipt={"phase": "early"})
    with pytest.raises(CustomerPackageError, match="transition was rejected"):
        ledger.begin_judgment(*operation)
    with pytest.raises(CustomerPackageError, match="transition was rejected"):
        ledger.record_completed_operation(
            *operation,
            judgment_receipt={"phase": "early"},
        )

    assert ledger.claim_case_operation(*operation)["claim_status"] == "claimed"
    ledger.record_execution_receipt(*operation, receipt={"phase": "executed"})
    with pytest.raises(CustomerPackageError, match="transition was rejected"):
        ledger.record_completed_operation(
            *operation,
            judgment_receipt={"phase": "too-early"},
        )
    ledger.begin_judgment(*operation)
    ledger.record_completed_operation(
        *operation,
        judgment_receipt={"phase": "settled"},
    )
    with pytest.raises(CustomerPackageError, match="transition was rejected"):
        ledger.begin_judgment(*operation)
    assert CustomerExecutionLedger(
        tmp_path / "customer-execution-ledger.sqlite3"
    ).describe_run(manifest_sha256, cases_sha256)["operation_states"] == {
        "completed": 1
    }


def test_executor_crash_after_external_side_effect_is_never_replayed(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        ledger_path = tmp_path / "customer-execution-ledger.sqlite3"

        class CrashAfterExternalEffectExecutor(FixtureCustomerExecutor):
            def execute(self, request):
                super().execute(request)
                raise SystemExit("simulated crash after external execution")

        crashing_executor = CrashAfterExternalEffectExecutor()
        crashing_runner = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            crashing_executor,
            execution_ledger_path=ledger_path,
        )
        with pytest.raises(SystemExit, match="simulated crash"):
            crashing_runner.run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256=record.content_hash,
            )
        assert len(crashing_executor.requests) == 1

        retry_executor = FixtureCustomerExecutor()
        retry_runner = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            retry_executor,
            execution_ledger_path=ledger_path,
        )
        report = retry_runner.run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )

        assert report["status"] == "in_doubt"
        assert report["passed"] is False
        assert report["ledger"]["state"] == "in_doubt"
        assert retry_executor.requests == []
        assert verify_customer_report(report, package) == ()
        with pytest.raises(CustomerPackageError):
            CustomerExecutionLedger(ledger_path).complete_run(
                package.manifest_sha256,
                package.cases_sha256,
                report["run_id"],
                {"status": "completed", "passed": True},
            )
        assert CustomerExecutionLedger(ledger_path).describe_run(
            package.manifest_sha256,
            package.cases_sha256,
        )["state"] == "in_doubt"
        forged = copy.deepcopy(report)
        forged["passed"] = True
        assert verify_customer_report(forged, package)
    finally:
        repository.close()


def test_crash_between_executor_result_and_receipt_is_never_replayed(
    monkeypatch,
    tmp_path,
):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        ledger_path = tmp_path / "customer-execution-ledger.sqlite3"
        executor = FixtureCustomerExecutor()
        runner = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            executor,
            execution_ledger_path=ledger_path,
        )
        original = CustomerExecutionLedger.record_execution_receipt
        fired = {"value": False}

        def fail_after_executor_result(self, *args, **kwargs):
            if not fired["value"]:
                fired["value"] = True
                raise SystemExit("simulated receipt commit crash")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(
            CustomerExecutionLedger,
            "record_execution_receipt",
            fail_after_executor_result,
        )
        with pytest.raises(SystemExit, match="receipt commit crash"):
            runner.run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256=record.content_hash,
            )
        assert len(executor.requests) == 1
        monkeypatch.setattr(
            CustomerExecutionLedger,
            "record_execution_receipt",
            original,
        )

        retry_executor = FixtureCustomerExecutor()
        report = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            retry_executor,
            execution_ledger_path=ledger_path,
        ).run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )

        assert report["status"] == "in_doubt"
        assert report["passed"] is False
        assert retry_executor.requests == []
        assert verify_customer_report(report, package) == ()
    finally:
        repository.close()


def test_crash_after_judgment_before_receipt_is_never_replayed(
    monkeypatch,
    tmp_path,
):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        ledger_path = tmp_path / "customer-execution-ledger.sqlite3"
        executor = FixtureCustomerExecutor()
        runner = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            executor,
            execution_ledger_path=ledger_path,
        )

        def fail_after_judgment(self, *args, **kwargs):
            raise SystemExit("simulated judgment receipt commit crash")

        monkeypatch.setattr(
            CustomerExecutionLedger,
            "record_completed_operation",
            fail_after_judgment,
        )
        with pytest.raises(SystemExit, match="judgment receipt commit crash"):
            runner.run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256=record.content_hash,
            )
        assert len(executor.requests) == 1
        monkeypatch.undo()

        retry_executor = FixtureCustomerExecutor()
        report = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            retry_executor,
            execution_ledger_path=ledger_path,
        ).run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )

        assert report["status"] == "in_doubt"
        assert report["passed"] is False
        assert retry_executor.requests == []
        assert verify_customer_report(report, package) == ()
    finally:
        repository.close()


def test_concurrent_customer_runners_do_not_execute_same_package_twice(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        ledger_path = tmp_path / "customer-execution-ledger.sqlite3"
        entered = threading.Event()
        release = threading.Event()

        class BlockingExecutor(FixtureCustomerExecutor):
            def __init__(self):
                super().__init__()
                self._blocked = False

            def execute(self, request):
                if not self._blocked:
                    self._blocked = True
                    entered.set()
                    if not release.wait(timeout=30):
                        raise RuntimeError("test execution was not released")
                return super().execute(request)

        first_executor = BlockingExecutor()
        first_runner = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            first_executor,
            execution_ledger_path=ledger_path,
        )
        first_outcome = {}

        def execute_first_runner():
            try:
                first_outcome["report"] = first_runner.run(
                    package,
                    skill_id=record.skill_id,
                    skill_version=record.version,
                    expected_skill_content_sha256=record.content_hash,
                )
            except BaseException as error:
                first_outcome["error"] = error

        worker = threading.Thread(target=execute_first_runner)
        worker.start()
        try:
            assert entered.wait(timeout=15)
            second_executor = FixtureCustomerExecutor()
            second_report = ControlledCustomerAcceptanceRunner(
                repository,
                _TENANT_ID,
                second_executor,
                execution_ledger_path=ledger_path,
            ).run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256=record.content_hash,
            )
            assert second_report["status"] == "in_doubt"
            assert second_report["passed"] is False
            assert second_report["ledger"]["state"] == "running"
            assert second_executor.requests == []
            assert verify_customer_report(second_report, package) == ()
        finally:
            release.set()
            worker.join(timeout=90)
        assert not worker.is_alive()
        assert "error" not in first_outcome
        assert first_outcome["report"]["status"] == "completed"
        assert first_outcome["report"]["passed"] is True
    finally:
        repository.close()


def test_independent_verifier_rejects_tampered_event(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        report = ControlledCustomerAcceptanceRunner(
            repository, _TENANT_ID, FixtureCustomerExecutor(),
            execution_ledger_path=tmp_path / "customer-execution-ledger.sqlite3",
        ).run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )
        tampered = copy.deepcopy(report)
        event = next(
            item
            for item in tampered["events"]
            if item["event_type"] == "case.executed"
        )
        event["payload"]["success"] = not event["payload"]["success"]

        failures = verify_customer_report(tampered, package)

        assert failures
        assert any("哈希" in failure or "指标" in failure for failure in failures)
    finally:
        repository.close()


def test_independent_verifier_recomputes_deterministic_oracle(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        report = ControlledCustomerAcceptanceRunner(
            repository, _TENANT_ID, FixtureCustomerExecutor(),
            execution_ledger_path=tmp_path / "customer-execution-ledger.sqlite3",
        ).run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )
        tampered = copy.deepcopy(report)
        baseline = next(
            event
            for event in tampered["events"]
            if event["event_type"] == "case.executed"
            and event["payload"]["arm"] == "baseline"
        )
        expected_case = next(
            case
            for case in package.cases
            if case.case_id == baseline["payload"]["case_id"]
        )
        baseline["payload"]["output_sha256"] = sha256_json(expected_case.oracle)
        _rehash_events(tampered)

        failures = verify_customer_report(tampered, package)

        assert any("确定性判定无法复算" in failure for failure in failures)
    finally:
        repository.close()


def test_independent_verifier_requires_external_executor_signature(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        report = ControlledCustomerAcceptanceRunner(
            repository, _TENANT_ID, FixtureCustomerExecutor(),
            execution_ledger_path=tmp_path / "customer-execution-ledger.sqlite3",
        ).run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )
        forged = copy.deepcopy(report)
        executed = next(
            event
            for event in forged["events"]
            if event["event_type"] == "case.executed"
        )
        executed["payload"]["executor_attestation_signature"] = "0" * 128
        _rehash_events(forged)

        failures = verify_customer_report(forged, package)

        assert any("执行器签名无效" in failure for failure in failures)
    finally:
        repository.close()


def test_independent_verifier_rejects_tampered_resource_measurement(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        report = ControlledCustomerAcceptanceRunner(
            repository, _TENANT_ID, FixtureCustomerExecutor(),
            execution_ledger_path=tmp_path / "customer-execution-ledger.sqlite3",
        ).run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )
        forged = copy.deepcopy(report)
        executed = next(
            event
            for event in forged["events"]
            if event["event_type"] == "case.executed"
        )
        executed["payload"]["cpu_time_ms"] = 0.0001
        _rehash_events(forged)

        failures = verify_customer_report(forged, package)

        assert any("执行器签名无效" in failure for failure in failures)
    finally:
        repository.close()


def test_customer_package_rejects_non_improving_performance_thresholds(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, _ = _package(tmp_path, record)
        manifest_path = root / "manifest.json"
        payload = manifest_path.read_bytes().replace(
            b'"minimum_throughput_ratio":1.1',
            b'"minimum_throughput_ratio":1.0',
        )
        manifest_path.write_bytes(payload)

        with pytest.raises(
            CustomerPackageError, match="minimum_throughput_ratio"
        ):
            load_customer_package(root, _sha(payload))
    finally:
        repository.close()


def test_skill_hash_and_allowlist_are_rechecked_before_injection(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        runner = ControlledCustomerAcceptanceRunner(
            repository, _TENANT_ID, FixtureCustomerExecutor(),
            execution_ledger_path=tmp_path / "customer-execution-ledger.sqlite3",
        )

        with pytest.raises(CustomerPackageError, match="固定候选"):
            runner.run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256="0" * 64,
            )
    finally:
        repository.close()


def test_skill_hash_must_be_strict_sha256(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)

        with pytest.raises(CustomerPackageError, match="SHA-256 十六进制"):
            ControlledCustomerAcceptanceRunner(
                repository, _TENANT_ID, FixtureCustomerExecutor(),
                execution_ledger_path=tmp_path / "customer-execution-ledger.sqlite3",
            ).run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256="not-a-sha256",
            )
    finally:
        repository.close()


def test_runner_and_verifier_bind_the_same_implementation_files():
    assert runner_implementation_fingerprint() == (
        verifier_implementation_fingerprint()
    )


def test_candidate_state_change_during_execution_fails_closed(tmp_path):
    repository, skills_dir, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        service = GovernedSkillService(repository, skills_dir, _TENANT_ID)
        validator = IdentityContext(
            tenant_id=_TENANT_ID,
            actor_user_id="validator",
            roles=frozenset({"skill:validate"}),
            trace_id="trace-customer-reject",
            auth_source="customer-test",
        )

        class RejectingExecutor(FixtureCustomerExecutor):
            rejected = False

            def execute(self, request):
                result = super().execute(request)
                if request.skill is not None and not self.rejected:
                    self.rejected = True
                    service.reject(
                        validator,
                        record.skill_id,
                        record.version,
                        "评测期间撤回候选",
                        "customer-reject",
                    )
                return result

        with pytest.raises(CustomerPackageError, match="评测期间发生变化"):
            ControlledCustomerAcceptanceRunner(
                repository, _TENANT_ID, RejectingExecutor(),
                execution_ledger_path=tmp_path / "customer-execution-ledger.sqlite3",
            ).run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256=record.content_hash,
            )
    finally:
        repository.close()


def test_customer_blind_review_requires_separate_judge(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, _ = _package(tmp_path, record)
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["oracle"]["kind"] = "customer_blind_review"
        manifest["attestation"]["judge"] = {
            "id": "customer-blind-judge",
            "version": "1.0.0",
            "artifact_sha256": "e" * 64,
            "ed25519_public_key": _TEST_PUBLIC_KEY_HEX,
        }
        payload = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest_path.write_bytes(payload)
        package = load_customer_package(root, _sha(payload))

        with pytest.raises(CustomerPackageError, match="缺少独立判定器"):
            ControlledCustomerAcceptanceRunner(
                repository, _TENANT_ID, FixtureCustomerExecutor(),
                execution_ledger_path=tmp_path / "customer-execution-ledger.sqlite3",
            ).run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256=record.content_hash,
            )
    finally:
        repository.close()


def test_executor_environment_snapshot_mismatch_is_rejected(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)

        with pytest.raises(CustomerPackageError, match="环境快照"):
            ControlledCustomerAcceptanceRunner(
                repository, _TENANT_ID, MismatchedSnapshotExecutor(),
                execution_ledger_path=tmp_path / "customer-execution-ledger.sqlite3",
            ).run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256=record.content_hash,
            )
    finally:
        repository.close()


def test_executor_observed_release_mismatch_is_rejected_even_if_signed(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)

        class WrongReleaseExecutor(FixtureCustomerExecutor):
            def execute(self, request):
                result = super().execute(request)
                observed = "0" * 64
                attestation = execution_attestation_payload(
                    run_id=request.run_id,
                    case_id=request.case_id,
                    arm=request.arm,
                    request_sha256=result.request_sha256,
                    execution_snapshot_sha256=(
                        result.execution_snapshot_sha256
                    ),
                    output_sha256=sha256_json(result.output),
                    latency_ms=result.latency_ms,
                    cpu_time_ms=result.cpu_time_ms,
                    peak_rss_bytes=result.peak_rss_bytes,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    comparison_environment_sha256=(
                        request.comparison_environment_sha256
                    ),
                    requested_release_identity_sha256=(
                        result.requested_release_identity_sha256
                    ),
                    observed_release_identity_sha256=observed,
                    executor_artifact_sha256=result.executor_artifact_sha256,
                )
                return CustomerExecutionResult(
                    output=result.output,
                    latency_ms=result.latency_ms,
                    cpu_time_ms=result.cpu_time_ms,
                    peak_rss_bytes=result.peak_rss_bytes,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    execution_snapshot_sha256=result.execution_snapshot_sha256,
                    request_sha256=result.request_sha256,
                    requested_release_identity_sha256=(
                        result.requested_release_identity_sha256
                    ),
                    observed_release_identity_sha256=observed,
                    executor_artifact_sha256=result.executor_artifact_sha256,
                    attestation_signature=_TEST_PRIVATE_KEY.sign(
                        canonical_json_bytes(attestation)
                    ).hex(),
                )

        with pytest.raises(CustomerPackageError, match="release"):
            ControlledCustomerAcceptanceRunner(
                repository, _TENANT_ID, WrongReleaseExecutor(),
                execution_ledger_path=tmp_path / "customer-execution-ledger.sqlite3",
            ).run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256=record.content_hash,
            )
    finally:
        repository.close()


def test_verifier_rejects_resigned_candidate_receipt_for_baseline_release(
    tmp_path,
):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        report = ControlledCustomerAcceptanceRunner(
            repository, _TENANT_ID, FixtureCustomerExecutor(),
            execution_ledger_path=tmp_path / "customer-execution-ledger.sqlite3",
        ).run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )
        forged = copy.deepcopy(report)
        candidate = next(
            event
            for event in forged["events"]
            if event["event_type"] == "case.executed"
            and event["payload"]["arm"] == "candidate"
        )
        baseline_identity = release_identity_sha256(_BASELINE_RELEASE)
        candidate["payload"]["requested_release_identity_sha256"] = (
            baseline_identity
        )
        candidate["payload"]["observed_release_identity_sha256"] = (
            baseline_identity
        )
        _resign_execution_event(forged, candidate)
        _rehash_events(forged)

        failures = verify_customer_report(forged, package)

        assert any("candidate release" in failure for failure in failures)
    finally:
        repository.close()


def test_subprocess_executor_uses_strict_protocol_and_isolated_workspace(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "customer_executor_adapter.py"
    executor = SubprocessCustomerCaseExecutor(
        [sys.executable, str(fixture)],
        executor_id="subprocess-fixture",
        executor_version="1.0.0",
        run_root=tmp_path / "runs",
    )
    request = CustomerExecutionRequest(
        run_id="run-1",
        case_id="case-1",
        arm="candidate",
        tenant_id=_TENANT_ID,
        model_id=_MODEL_ID,
        model_parameters={"temperature": 0},
        endpoint_sha256="c" * 64,
        prompt_sha256="a" * 64,
        tools_sha256="b" * 64,
        comparison_environment_sha256="9" * 64,
        requested_release_identity_sha256="8" * 64,
        case_input={"candidate_output": {"answer": "expected"}},
        skill={"skill_id": "skill-1"},
    )

    result = executor.execute(request)

    assert result.output == {"answer": "expected"}
    assert result.latency_ms == 0.8
    assert result.cpu_time_ms == 0.6
    assert result.peak_rss_bytes == 800
    assert result.input_tokens == 10
    assert result.output_tokens == 2
    assert list((tmp_path / "runs").iterdir()) == []


def test_subprocess_executor_rejects_reparse_run_root(monkeypatch, tmp_path):
    monkeypatch.setattr(
        customer_executor_module,
        "has_link_or_reparse_component",
        lambda path: True,
    )

    with pytest.raises(CustomerExecutionError, match="重解析点"):
        SubprocessCustomerCaseExecutor(
            [sys.executable, "adapter.py"],
            executor_id="subprocess-fixture",
            executor_version="1.0.0",
            run_root=tmp_path / "runs",
        )


def test_customer_cli_without_package_returns_pending(capsys):
    exit_code = customer_main([])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert report["status"] == "pending_customer_inputs"
    assert report["passed"] is False


def test_customer_cli_reuses_completed_ledger_report_without_executor_replay(
    capsys,
    monkeypatch,
    tmp_path,
):
    repository, skills_dir, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        fixture = Path(__file__).parent / "fixtures" / "customer_executor_adapter.py"
        run_root = tmp_path / "customer-cli-run"
        output = tmp_path / "customer-cli-report.json"
        arguments = [
            "--package-root",
            str(root),
            "--manifest-sha256",
            manifest_sha256,
            "--skills-db",
            str(skills_dir / ".system" / "governed-skills.db"),
            "--tenant-id",
            _TENANT_ID,
            "--skill-id",
            record.skill_id,
            "--skill-version",
            str(record.version),
            "--skill-content-sha256",
            record.content_hash,
            "--executor-json",
            json.dumps([sys.executable, str(fixture)]),
            "--executor-id",
            "fixture-customer-executor",
            "--executor-version",
            "1.0.0",
            "--run-root",
            str(run_root),
            "--output",
            str(output),
        ]
        calls = []
        original_execute = (
            customer_main_module.SubprocessCustomerCaseExecutor.execute
        )

        def counted_execute(self, request):
            calls.append(request)
            return original_execute(self, request)

        monkeypatch.setattr(
            customer_main_module.SubprocessCustomerCaseExecutor,
            "execute",
            counted_execute,
        )
        assert customer_main(arguments) == 0
        first_report = json.loads(output.read_text(encoding="utf-8"))
        assert first_report["status"] == "completed"
        assert first_report["passed"] is True
        assert len(calls) == 60
        capsys.readouterr()

        calls.clear()
        assert customer_main(arguments) == 0
        replay_report = json.loads(output.read_text(encoding="utf-8"))
        assert replay_report == first_report
        assert calls == []
        assert (run_root / "customer_execution_ledger.sqlite3").is_file()
    finally:
        repository.close()


def test_customer_cli_surfaces_in_doubt_without_executor_replay(
    capsys,
    monkeypatch,
    tmp_path,
):
    repository, skills_dir, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        run_root = tmp_path / "customer-cli-run"
        ledger_path = run_root / "customer_execution_ledger.sqlite3"

        class CrashAfterExternalEffectExecutor(FixtureCustomerExecutor):
            def execute(self, request):
                super().execute(request)
                raise SystemExit("seed an unresolved customer execution")

        with pytest.raises(SystemExit, match="seed an unresolved"):
            ControlledCustomerAcceptanceRunner(
                repository,
                _TENANT_ID,
                CrashAfterExternalEffectExecutor(),
                execution_ledger_path=ledger_path,
            ).run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256=record.content_hash,
            )

        fixture = Path(__file__).parent / "fixtures" / "customer_executor_adapter.py"
        output = tmp_path / "customer-cli-report.json"
        arguments = [
            "--package-root",
            str(root),
            "--manifest-sha256",
            manifest_sha256,
            "--skills-db",
            str(skills_dir / ".system" / "governed-skills.db"),
            "--tenant-id",
            _TENANT_ID,
            "--skill-id",
            record.skill_id,
            "--skill-version",
            str(record.version),
            "--skill-content-sha256",
            record.content_hash,
            "--executor-json",
            json.dumps([sys.executable, str(fixture)]),
            "--executor-id",
            "fixture-customer-executor",
            "--executor-version",
            "1.0.0",
            "--run-root",
            str(run_root),
            "--output",
            str(output),
        ]
        calls = []
        original_execute = (
            customer_main_module.SubprocessCustomerCaseExecutor.execute
        )

        def counted_execute(self, request):
            calls.append(request)
            return original_execute(self, request)

        monkeypatch.setattr(
            customer_main_module.SubprocessCustomerCaseExecutor,
            "execute",
            counted_execute,
        )
        assert customer_main(arguments) == 1
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["status"] == "in_doubt"
        assert report["passed"] is False
        assert calls == []
        assert "in_doubt" in capsys.readouterr().out
    finally:
        repository.close()
