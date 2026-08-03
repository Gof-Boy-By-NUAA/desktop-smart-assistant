"""客户包、受控技能注入和独立证据复算测试。"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
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
from benchmarks.customer.__main__ import main as customer_main
from benchmarks.customer import executor as customer_executor_module
from benchmarks.customer import package as customer_package_module
from benchmarks.customer.attestation import execution_attestation_payload
from benchmarks.customer.contracts import (
    CustomerExecutionError,
    CustomerPackageError,
)
from benchmarks.customer.json_utils import canonical_json_bytes, sha256_json
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
                "input": {"candidate_output": {"answer": "expected"}},
                "oracle": {"answer": "expected"},
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
        "schema_version": 1,
        "package_id": "customer-package-v1",
        "tenant_id": _TENANT_ID,
        "model": {
            "id": _MODEL_ID,
            "parameters": {"temperature": 0, "seed": 42},
            "endpoint_sha256": "c" * 64,
            "prompt_sha256": "a" * 64,
            "tools_sha256": "b" * 64,
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


def test_controlled_candidate_arm_passes_and_does_not_mutate_skill_store(tmp_path):
    repository, skills_dir, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        executor = FixtureCustomerExecutor()
        runner = ControlledCustomerAcceptanceRunner(
            repository, _TENANT_ID, executor
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


def test_independent_verifier_rejects_tampered_event(tmp_path):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        report = ControlledCustomerAcceptanceRunner(
            repository, _TENANT_ID, FixtureCustomerExecutor()
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
            repository, _TENANT_ID, FixtureCustomerExecutor()
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
        baseline["payload"]["output_sha256"] = sha256_json(
            {"answer": "expected"}
        )
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
            repository, _TENANT_ID, FixtureCustomerExecutor()
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
            repository, _TENANT_ID, FixtureCustomerExecutor()
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
            repository, _TENANT_ID, FixtureCustomerExecutor()
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
                repository, _TENANT_ID, FixtureCustomerExecutor()
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
                repository, _TENANT_ID, RejectingExecutor()
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
                repository, _TENANT_ID, FixtureCustomerExecutor()
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
                repository, _TENANT_ID, MismatchedSnapshotExecutor()
            ).run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256=record.content_hash,
            )
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
