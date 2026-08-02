"""外部工作手册技能治理核心的独立回归测试。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path

import pytest

from agent.skills.governance import (
    ControlledPairedSuiteRunner,
    EvaluationPolicy,
    EvaluationRunResult,
    GovernedSkillRepository,
    GovernedSkillService,
    IdempotencyConflictError,
    IdentityContext,
    PairedCaseExecutor,
    PairedSampleResult,
    SkillAuthorizationError,
    SkillEvaluationCommand,
    SkillIdentity,
    SkillProposal,
    SkillPublishGateError,
    SkillStatus,
    SkillTamperError,
    SkillValidationError,
    SourceEvidence,
)
from agent.skills.loader import SkillLoader
from agent.skills.manager import SkillManager
from agent.protocol.agent_stream import AgentStreamExecutor


TENANT_ID = "tenant-one"
MODEL_ID = "model-a@2026-07"


class FixturePairedCaseExecutor(PairedCaseExecutor):
    """测试专用可信执行器，根据输入场景返回实际输出。"""

    def __init__(self):
        self.calls = []
        self._clock_value_ns = 0

    def clock_ns(self):
        return self._clock_value_ns

    @property
    def executor_id(self) -> str:
        return "fixture-executor"

    @property
    def executor_version(self) -> str:
        return "1.0.0"

    def execute_baseline(self, *, model_id, case_input):
        return self._execute("baseline", model_id, case_input)

    def execute_candidate(self, *, model_id, candidate, case_input):
        assert candidate.model_compatibility == (model_id,)
        return self._execute("candidate", model_id, case_input)

    def _execute(self, variant, model_id, case_input):
        scenario = case_input["scenario"]
        self.calls.append((variant, model_id, scenario))
        slow_candidate = scenario.startswith("slow-")
        outcome_name = scenario.removeprefix("slow-")
        if slow_candidate:
            latency_ms = 2.0 if variant == "baseline" else 25.0
        else:
            latency_ms = 10.0 if variant == "baseline" else 1.0
        self._clock_value_ns += int(latency_ms * 1_000_000)
        outcomes = {
            "both-pass": (True, True),
            "candidate-improves": (False, True),
            "both-fail": (False, False),
            "candidate-regresses": (True, False),
        }
        baseline_success, candidate_success = outcomes[outcome_name]
        success = baseline_success if variant == "baseline" else candidate_success
        return {"answer": "expected" if success else "unexpected"}


def _fixture_runner(tmp_path, runner_type=ControlledPairedSuiteRunner):
    executor = FixturePairedCaseExecutor()
    return runner_type(tmp_path, executor, clock_ns=executor.clock_ns)


class MismatchedModelRunner(ControlledPairedSuiteRunner):
    """用于证明运行器同模型声明会被服务端核验。"""

    @property
    def runner_id(self) -> str:
        return "mismatched-model-runner"

    def run(self, *, suite_path, suite_sha256, model_id, candidate):
        result = super().run(
            suite_path=suite_path,
            suite_sha256=suite_sha256,
            model_id=model_id,
            candidate=candidate,
        )
        return EvaluationRunResult(
            suite_sha256=result.suite_sha256,
            baseline_model_id=model_id,
            candidate_model_id="different-model",
            samples=result.samples,
        )


def _identity(user_id: str, *roles: str, trace: str = "trace") -> SkillIdentity:
    return SkillIdentity(
        tenant_id=TENANT_ID,
        actor_user_id=user_id,
        roles=frozenset(roles),
        trace_id="%s-%s" % (trace, user_id),
        auth_source="test-auth",
    )


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _proposal(
    *,
    name: str = "data-check",
    description: str = "在计算前检查输入数据。",
    idempotency_key: str = "proposal-1",
    source_payload: bytes = b'{"trace":"verified"}',
) -> SkillProposal:
    return SkillProposal(
        name=name,
        description=description,
        applicability=("表格抽取后需要计算",),
        steps=("核对原始行列标签", "确认单位和正负号", "执行计算"),
        validation_rules=("关键输入必须与原文逐项一致",),
        contraindications=("没有可核验原始数据时不得套用",),
        model_compatibility=(MODEL_ID,),
        sources=(
            SourceEvidence(
                source_type="execution-trace",
                source_ref="trace://run-1",
                payload=source_payload,
                sha256=_sha(source_payload),
            ),
        ),
        idempotency_key=idempotency_key,
    )


def _takeover_proposal(path: Path, key: str = "takeover-1") -> SkillProposal:
    payload = path.read_bytes()
    base = _proposal(name=path.parent.name, idempotency_key=key)
    return SkillProposal(
        name=base.name,
        description=base.description,
        applicability=base.applicability,
        steps=base.steps,
        validation_rules=base.validation_rules,
        contraindications=base.contraindications,
        model_compatibility=base.model_compatibility,
        sources=(
            SourceEvidence(
                source_type="existing-skill",
                source_ref=str(path.resolve()),
                payload=payload,
                sha256=_sha(payload),
            ),
        ),
        idempotency_key=key,
    )


def _case(scenario: str) -> dict:
    return {
        "input": {"scenario": scenario},
        "expected": {"answer": "expected"},
    }


def _passing_cases() -> tuple[dict, ...]:
    return (
        _case("both-pass"),
        _case("candidate-improves"),
        _case("both-fail"),
    )


def _evaluation_command(
    tmp_path: Path,
    record,
    *,
    cases: tuple[dict, ...] | None = None,
    candidate_model_id: str = MODEL_ID,
    key: str = "evaluation-1",
    filename: str = "suite.json",
) -> SkillEvaluationCommand:
    suite_path = tmp_path / filename
    if not suite_path.exists():
        suite_path.write_text(
            json.dumps(
                {"cases": list(cases or _passing_cases())},
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    suite_hash = _sha(suite_path.read_bytes())
    return SkillEvaluationCommand(
        skill_id=record.skill_id,
        version=record.version,
        suite_path=str(suite_path),
        suite_sha256=suite_hash,
        model_id=candidate_model_id,
        idempotency_key=key,
    )


@pytest.fixture
def governed(tmp_path):
    skills_dir = tmp_path / "skills"
    repository = GovernedSkillRepository(
        skills_dir / ".system" / "governed-skills.db"
    )
    service = GovernedSkillService(
        repository,
        skills_dir,
        TENANT_ID,
        EvaluationPolicy(
            minimum_sample_count=3,
            max_candidate_p95_latency_ms=200.0,
            max_latency_regression_ratio=1.10,
        ),
        _fixture_runner(tmp_path),
    )
    try:
        yield service, repository, skills_dir
    finally:
        repository.close()


def _publish_first(service, tmp_path, proposal=None):
    proposer = _identity("proposer", "skill:propose")
    validator = _identity("validator", "skill:validate")
    publisher = _identity("publisher", "skill:publish")
    candidate = service.propose(proposer, proposal or _proposal())
    evaluation = service.evaluate(
        validator, _evaluation_command(tmp_path, candidate)
    )
    active = service.publish(
        publisher,
        candidate.skill_id,
        candidate.version,
        evaluation.evaluation_id,
        "publish-1",
    )
    return candidate, evaluation, active


def _create_windows_junction_or_skip(link: Path, target: Path) -> None:
    """创建 Windows 目录联接点；权限不足时跳过对应安全测试。"""

    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("当前环境不能创建 Windows 目录联接点")


def test_candidate_is_not_projected_and_publish_creates_loader_compatible_skill(
    governed, tmp_path
):
    service, _, skills_dir = governed
    proposer = _identity("proposer", "skill:propose")
    validator = _identity("validator", "skill:validate")
    publisher = _identity("publisher", "skill:publish")

    candidate = service.propose(proposer, _proposal())
    projection = skills_dir / "data-check" / "SKILL.md"
    assert candidate.status is SkillStatus.CANDIDATE
    assert not projection.exists()
    assert candidate.provenance[0]["sha256"] == _sha(b'{"trace":"verified"}')

    evaluation = service.evaluate(
        validator, _evaluation_command(tmp_path, candidate)
    )
    assert evaluation.gate_passed
    assert evaluation.sample_count == 3
    assert evaluation.candidate_passed > evaluation.baseline_passed
    assert evaluation.regression_count == 0
    assert evaluation.baseline_model_id == evaluation.candidate_model_id
    assert not projection.exists()

    active = service.publish(
        publisher,
        candidate.skill_id,
        candidate.version,
        evaluation.evaluation_id,
        "publish-1",
    )
    assert active.status is SkillStatus.ACTIVE
    assert projection.is_file()
    assert service.verify_projection(publisher, "data-check") == active
    loaded = SkillLoader().load_skills_from_dir(str(skills_dir), "custom")
    assert [skill.name for skill in loaded.skills] == ["data-check"]
    assert loaded.skills[0].description == active.description
    assert [event.action for event in service.list_audit(publisher, active.skill_id)] == [
        "skill.proposed",
        "skill.evaluated",
        "skill.published",
    ]


def test_skill_manager_verifies_active_projection_and_forces_enabled(
    governed, tmp_path
):
    service, _, skills_dir = governed
    _, _, active = _publish_first(service, tmp_path)
    config_path = skills_dir / "skills_config.json"
    config_path.write_text(
        json.dumps(
            {
                active.name: {
                    "name": active.name,
                    "description": active.description,
                    "source": "custom",
                    "enabled": False,
                    "category": "skill",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    manager = SkillManager(
        builtin_dir=str(builtin_dir),
        custom_dir=str(skills_dir),
        tenant_id=TENANT_ID,
        identity_context=_identity("reader", "skill:read"),
    )
    try:
        assert manager.is_skill_enabled(active.name) is True
        assert manager.skills_config[active.name]["managed_by"] == "governance"
        assert active.name in manager.build_skills_prompt()

        projection = skills_dir / active.name / "SKILL.md"
        projection.write_text(
            "---\nname: data-check\ndescription: SHELL_TAMPER_INJECTION\n---\n",
            encoding="utf-8",
        )
        manager.refresh_skills()

        assert manager.get_skill(active.name) is None
        assert "SHELL_TAMPER_INJECTION" not in manager.build_skills_prompt()
        assert service.repository.read_active_by_name(TENANT_ID, active.name) == active
    finally:
        manager.close()


def test_tool_not_found_uses_only_eligible_verified_skill_snapshot(
    governed, tmp_path
):
    service, _, skills_dir = governed
    _, _, active = _publish_first(service, tmp_path)
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    manager = SkillManager(
        builtin_dir=str(builtin_dir),
        custom_dir=str(skills_dir),
        tenant_id=TENANT_ID,
        identity_context=_identity("reader", "skill:read"),
    )
    executor = object.__new__(AgentStreamExecutor)
    executor.tools = {}
    executor.agent = type("AgentStub", (), {"skill_manager": manager})()
    try:
        projection = skills_dir / active.name / "SKILL.md"
        projection.write_text(
            "---\nname: data-check\ndescription: POST_VERIFY_TAMPER\n---\n",
            encoding="utf-8",
        )
        message = executor._build_tool_not_found_message(active.name)
        assert active.description in message
        assert "POST_VERIFY_TAMPER" not in message

        manager.skills_config[active.name]["enabled"] = False
        assert manager.get_eligible_skill(active.name) is None
        disabled_message = executor._build_tool_not_found_message(active.name)
        assert active.description not in disabled_message
        assert "matching skill" not in disabled_message
    finally:
        manager.close()


def test_skill_identity_reuses_shared_trusted_identity_contract():
    assert SkillIdentity is IdentityContext


def test_proposal_only_service_hard_fails_evaluation_without_trusted_runner(tmp_path):
    skills_dir = tmp_path / "skills"
    repository = GovernedSkillRepository(skills_dir / ".system" / "skills.db")
    service = GovernedSkillService(repository, skills_dir, TENANT_ID)
    try:
        candidate = service.propose(
            _identity("proposer", "skill:propose"), _proposal()
        )
        assert "samples" not in {field.name for field in fields(SkillEvaluationCommand)}
        with pytest.raises(SkillValidationError, match="未配置可信评测运行器"):
            service.evaluate(
                _identity("validator", "skill:validate"),
                _evaluation_command(tmp_path, candidate),
            )
    finally:
        repository.close()


def test_runner_must_attest_same_model_for_paired_execution(tmp_path):
    skills_dir = tmp_path / "skills"
    repository = GovernedSkillRepository(skills_dir / ".system" / "skills.db")
    service = GovernedSkillService(
        repository,
        skills_dir,
        TENANT_ID,
        EvaluationPolicy(3, 200.0, 1.10),
        _fixture_runner(tmp_path, MismatchedModelRunner),
    )
    try:
        candidate = service.propose(
            _identity("proposer", "skill:propose"), _proposal()
        )
        with pytest.raises(SkillValidationError, match="同模型配对基线"):
            service.evaluate(
                _identity("validator", "skill:validate"),
                _evaluation_command(tmp_path, candidate),
            )
        assert service.list_evaluations(
            _identity("reader", "skill:read"), candidate.skill_id, candidate.version
        ) == ()
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("scenarios", "expected_failure"),
    [
        (
            ("both-pass", "both-fail", "both-fail"),
            "配对基线未严格提升",
        ),
        (
            ("candidate-regresses", "candidate-improves", "candidate-improves"),
            "存在回归样本",
        ),
        (
            ("slow-both-pass", "slow-candidate-improves", "slow-both-fail"),
            "候选 P95 延迟超过相对阈值",
        ),
    ],
)
def test_publish_gate_records_failures_and_refuses_publish(
    governed,
    tmp_path,
    scenarios,
    expected_failure,
):
    service, _, skills_dir = governed
    candidate = service.propose(
        _identity("proposer", "skill:propose"), _proposal()
    )
    evaluation = service.evaluate(
        _identity("validator", "skill:validate"),
        _evaluation_command(
            tmp_path,
            candidate,
            cases=tuple(_case(scenario) for scenario in scenarios),
        ),
    )
    assert not evaluation.gate_passed
    assert expected_failure in evaluation.gate_failures
    with pytest.raises(SkillPublishGateError):
        service.publish(
            _identity("publisher", "skill:publish"),
            candidate.skill_id,
            candidate.version,
            evaluation.evaluation_id,
            "publish-failed-gate",
        )
    assert not (skills_dir / "data-check" / "SKILL.md").exists()
    assert service.get_version(
        _identity("reader", "skill:read"), candidate.skill_id, candidate.version
    ).status is SkillStatus.CANDIDATE


def test_publish_rejects_evaluation_from_different_current_policy(
    governed, tmp_path
):
    service, repository, skills_dir = governed
    candidate = service.propose(
        _identity("proposer", "skill:propose"), _proposal()
    )
    evaluation = service.evaluate(
        _identity("validator", "skill:validate"),
        _evaluation_command(tmp_path, candidate),
    )
    stricter_service = GovernedSkillService(
        repository,
        skills_dir,
        TENANT_ID,
        EvaluationPolicy(4, 150.0, 1.05),
        service.evaluation_runner,
    )

    with pytest.raises(SkillPublishGateError, match="当前发布策略"):
        stricter_service.publish(
            _identity("publisher", "skill:publish"),
            candidate.skill_id,
            candidate.version,
            evaluation.evaluation_id,
            "publish-stricter-policy",
        )
    assert not (skills_dir / candidate.name / "SKILL.md").exists()
    assert stricter_service.get_version(
        _identity("reader", "skill:read"), candidate.skill_id, candidate.version
    ).status is SkillStatus.CANDIDATE


def test_publish_recomputes_all_gates_from_paired_samples(governed, tmp_path):
    service, repository, _ = governed
    candidate = service.propose(
        _identity("proposer", "skill:propose"), _proposal()
    )
    evaluation = service.evaluate(
        _identity("validator", "skill:validate"),
        _evaluation_command(tmp_path, candidate),
    )
    forged_samples = (
        PairedSampleResult("both-pass", True, True, 4.0, 2.0),
        PairedSampleResult("candidate-improves", False, True, 4.0, 2.0),
        PairedSampleResult("candidate-regresses", True, False, 4.0, 2.0),
    )
    forged = replace(
        evaluation,
        evaluation_id="forged-gate-summary",
        baseline_passed=2,
        candidate_passed=2,
        regression_count=1,
        baseline_p95_latency_ms=4.0,
        candidate_p95_latency_ms=2.0,
        gate_passed=True,
        gate_failures=(),
        samples=forged_samples,
        record_hash="",
    )
    forged = replace(
        forged,
        record_hash=_sha(
            json.dumps(
                repository.evaluation_payload(forged),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
    )
    with repository.transaction() as connection:
        repository.insert_evaluation(connection, forged)

    with pytest.raises(SkillPublishGateError, match="严格提升|回归样本"):
        service.publish(
            _identity("publisher", "skill:publish"),
            candidate.skill_id,
            candidate.version,
            forged.evaluation_id,
            "publish-forged-gate-summary",
        )


def test_publish_rejects_legacy_gate_schema(governed, tmp_path):
    service, repository, _ = governed
    candidate = service.propose(
        _identity("proposer", "skill:propose"), _proposal()
    )
    evaluation = service.evaluate(
        _identity("validator", "skill:validate"),
        _evaluation_command(tmp_path, candidate),
    )
    legacy = replace(
        evaluation,
        evaluation_id="legacy-gate-schema",
        gate_schema_version=0,
        record_hash="",
    )
    legacy = replace(
        legacy,
        record_hash=_sha(
            json.dumps(
                repository.evaluation_payload(legacy),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
    )
    with repository.transaction() as connection:
        repository.insert_evaluation(connection, legacy)

    with pytest.raises(SkillPublishGateError, match="门禁版本"):
        service.publish(
            _identity("publisher", "skill:publish"),
            candidate.skill_id,
            candidate.version,
            legacy.evaluation_id,
            "publish-legacy-gate-schema",
        )


def test_minimum_sample_count_is_derived_from_runner_output(governed, tmp_path):
    service, _, _ = governed
    candidate = service.propose(
        _identity("proposer", "skill:propose"), _proposal()
    )
    two_cases = _passing_cases()[:2]
    evaluation = service.evaluate(
        _identity("validator", "skill:validate"),
        _evaluation_command(tmp_path, candidate, cases=two_cases),
    )
    assert not evaluation.gate_passed
    assert "样本数不足" in evaluation.gate_failures
    assert evaluation.sample_count == len(two_cases)
    assert evaluation.runner_id == "controlled-paired-suite:fixture-executor"
    assert evaluation.runner_version == "1.1.0+1.0.0"


def test_role_and_three_party_separation_are_enforced(governed, tmp_path):
    service, _, _ = governed
    owner = _identity(
        "owner", "skill:propose", "skill:validate", "skill:publish"
    )
    candidate = service.propose(owner, _proposal())
    with pytest.raises(SkillAuthorizationError, match="不能验证"):
        service.evaluate(owner, _evaluation_command(tmp_path, candidate))

    validator_publisher = _identity(
        "validator-publisher", "skill:validate", "skill:publish"
    )
    evaluation = service.evaluate(
        validator_publisher, _evaluation_command(tmp_path, candidate)
    )
    with pytest.raises(SkillAuthorizationError, match="验证者不能发布"):
        service.publish(
            validator_publisher,
            candidate.skill_id,
            candidate.version,
            evaluation.evaluation_id,
            "publish-same-validator",
        )
    with pytest.raises(SkillAuthorizationError, match="缺少角色"):
        service.propose(_identity("reader", "skill:read"), _proposal())
    foreign = SkillIdentity(
        tenant_id="tenant-two",
        actor_user_id="publisher",
        roles=frozenset({"skill:publish"}),
        trace_id="foreign-trace",
        auth_source="test-auth",
    )
    with pytest.raises(SkillAuthorizationError, match="租户"):
        service.publish(
            foreign,
            candidate.skill_id,
            candidate.version,
            evaluation.evaluation_id,
            "foreign-publish",
        )


def test_reject_is_append_only_and_owner_cannot_reject(governed):
    service, _, _ = governed
    owner = _identity("owner", "skill:propose", "skill:validate")
    candidate = service.propose(owner, _proposal())
    with pytest.raises(SkillAuthorizationError, match="不能拒绝"):
        service.reject(owner, candidate.skill_id, 1, "质量不足", "reject-owner")
    rejected = service.reject(
        _identity("validator", "skill:validate"),
        candidate.skill_id,
        1,
        "质量不足",
        "reject-valid",
    )
    assert rejected.status is SkillStatus.REJECTED
    assert len(service.list_versions(owner, candidate.skill_id)) == 1


def test_list_candidates_is_cross_skill_read_only_and_excludes_terminal_states(
    governed,
):
    service, _, _ = governed
    proposer = _identity("proposer", "skill:propose")
    first = service.propose(
        proposer, _proposal(name="candidate-a", idempotency_key="candidate-a")
    )
    second = service.propose(
        proposer, _proposal(name="candidate-b", idempotency_key="candidate-b")
    )
    third = service.propose(
        proposer, _proposal(name="candidate-c", idempotency_key="candidate-c")
    )
    service.reject(
        _identity("validator", "skill:validate"),
        second.skill_id,
        second.version,
        "重复技能",
        "reject-b",
    )
    candidates = service.list_candidates(_identity("reader", "skill:read"))
    assert {record.skill_id for record in candidates} == {
        first.skill_id,
        third.skill_id,
    }
    assert all(record.status is SkillStatus.CANDIDATE for record in candidates)
    assert [record.created_at for record in candidates] == sorted(
        (record.created_at for record in candidates), reverse=True
    )


def test_existing_skill_takeover_requires_matching_path_and_hash(governed, tmp_path):
    service, _, skills_dir = governed
    existing = skills_dir / "legacy-skill" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    original = b"---\nname: legacy-skill\ndescription: legacy\n---\n"
    existing.write_bytes(original)
    candidate = service.propose(
        _identity("proposer", "skill:propose"), _takeover_proposal(existing)
    )
    evaluation = service.evaluate(
        _identity("validator", "skill:validate"),
        _evaluation_command(tmp_path, candidate, filename="takeover-suite.json"),
    )
    active = service.publish(
        _identity("publisher", "skill:publish"),
        candidate.skill_id,
        candidate.version,
        evaluation.evaluation_id,
        "takeover-publish",
    )
    assert existing.read_bytes() != original
    assert service.verify_projection(
        _identity("reader", "skill:read"), "legacy-skill"
    ) == active


def test_existing_skill_takeover_fails_if_file_changes_after_proposal(
    governed, tmp_path
):
    service, _, skills_dir = governed
    existing = skills_dir / "legacy-skill" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"legacy-v1")
    candidate = service.propose(
        _identity("proposer", "skill:propose"), _takeover_proposal(existing)
    )
    evaluation = service.evaluate(
        _identity("validator", "skill:validate"),
        _evaluation_command(tmp_path, candidate, filename="takeover-suite.json"),
    )
    existing.write_bytes(b"legacy-v2-tampered")
    with pytest.raises(SkillTamperError, match="接管来源证据"):
        service.publish(
            _identity("publisher", "skill:publish"),
            candidate.skill_id,
            candidate.version,
            evaluation.evaluation_id,
            "takeover-publish",
        )
    assert existing.read_bytes() == b"legacy-v2-tampered"
    assert service.get_version(
        _identity("reader", "skill:read"), candidate.skill_id, candidate.version
    ).status is SkillStatus.CANDIDATE


def test_source_suite_projection_and_sqlite_tampering_are_detected(
    governed, tmp_path
):
    service, repository, skills_dir = governed
    payload = b"authentic-trace"
    bad_source = SourceEvidence(
        "execution-trace", "trace://bad", payload, "0" * 64
    )
    invalid = _proposal(source_payload=payload)
    invalid = SkillProposal(
        name=invalid.name,
        description=invalid.description,
        applicability=invalid.applicability,
        steps=invalid.steps,
        validation_rules=invalid.validation_rules,
        contraindications=invalid.contraindications,
        model_compatibility=invalid.model_compatibility,
        sources=(bad_source,),
        idempotency_key=invalid.idempotency_key,
    )
    with pytest.raises(SkillTamperError, match="来源轨迹"):
        service.propose(_identity("proposer", "skill:propose"), invalid)

    candidate = service.propose(
        _identity("proposer", "skill:propose"), _proposal()
    )
    command = _evaluation_command(tmp_path, candidate)
    evaluation = service.evaluate(
        _identity("validator", "skill:validate"), command
    )
    Path(command.suite_path).write_bytes(b"changed-after-validation")
    with pytest.raises(SkillTamperError, match="验证后发生变化"):
        service.publish(
            _identity("publisher", "skill:publish"),
            candidate.skill_id,
            candidate.version,
            evaluation.evaluation_id,
            "publish-changed-suite",
        )
    assert not (skills_dir / candidate.name / "SKILL.md").exists()

    raw = sqlite3.connect(str(repository.db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(
                "UPDATE governed_skill_versions SET description = 'tampered'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM governed_skill_evaluations")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM governed_skill_audit")
    finally:
        raw.close()


def test_projection_tampering_blocks_verification_and_next_publish(
    governed, tmp_path
):
    service, _, skills_dir = governed
    first, _, _ = _publish_first(service, tmp_path)
    projection = skills_dir / first.name / "SKILL.md"
    projection.write_text("tampered", encoding="utf-8")
    with pytest.raises(SkillTamperError, match="不一致"):
        service.verify_projection(
            _identity("reader", "skill:read"), first.name
        )

    second = service.propose(
        _identity("proposer", "skill:propose"),
        _proposal(description="第二版说明。", idempotency_key="proposal-2"),
    )
    evaluation = service.evaluate(
        _identity("validator", "skill:validate"),
        _evaluation_command(
            tmp_path, second, key="evaluation-2", filename="suite-2.json"
        ),
    )
    with pytest.raises(SkillTamperError, match="外部修改"):
        service.publish(
            _identity("publisher", "skill:publish"),
            second.skill_id,
            second.version,
            evaluation.evaluation_id,
            "publish-2",
        )
    assert service.get_version(
        _identity("reader", "skill:read"), second.skill_id, second.version
    ).status is SkillStatus.CANDIDATE


def test_publish_failure_restores_previous_projection_and_database_state(
    governed, tmp_path, monkeypatch
):
    service, _, skills_dir = governed
    first, _, active = _publish_first(service, tmp_path)
    projection = skills_dir / first.name / "SKILL.md"
    original_bytes = projection.read_bytes()
    second = service.propose(
        _identity("proposer", "skill:propose"),
        _proposal(description="第二版说明。", idempotency_key="proposal-2"),
    )
    second_evaluation = service.evaluate(
        _identity("validator", "skill:validate"),
        _evaluation_command(
            tmp_path, second, key="evaluation-2", filename="suite-2.json"
        ),
    )

    def fail_after_replacement(record):
        service._atomic_replace(
            projection, service.render_projection(record).encode("utf-8")
        )
        raise OSError("simulated projection failure")

    monkeypatch.setattr(service, "_project_record", fail_after_replacement)
    with pytest.raises(OSError, match="simulated"):
        service.publish(
            _identity("publisher", "skill:publish"),
            second.skill_id,
            second.version,
            second_evaluation.evaluation_id,
            "publish-2",
        )
    assert projection.read_bytes() == original_bytes
    assert service.verify_projection(
        _identity("reader", "skill:read"), first.name
    ) == active
    versions = service.list_versions(
        _identity("reader", "skill:read"), first.skill_id
    )
    assert [record.status for record in versions] == [
        SkillStatus.ACTIVE,
        SkillStatus.CANDIDATE,
    ]
    second_actions = [
        event.action
        for event in service.list_audit(
            _identity("reader", "skill:read"), second.skill_id
        )
        if event.version == second.version
    ]
    assert second_actions == ["skill.proposed", "skill.evaluated"]


def test_startup_recovers_projection_after_precommit_hard_crash(
    governed, tmp_path
):
    service, repository, skills_dir = governed
    first, _, active = _publish_first(service, tmp_path)
    projection = skills_dir / first.name / "SKILL.md"
    original_bytes = projection.read_bytes()
    second = service.propose(
        _identity("proposer", "skill:propose"),
        _proposal(description="第二版说明。", idempotency_key="proposal-2"),
    )
    projected_second = replace(second, status=SkillStatus.ACTIVE)

    with pytest.raises(RuntimeError, match="模拟硬崩溃"):
        with repository.transaction() as conn:
            current_active = repository.get_active_by_name(
                conn, TENANT_ID, first.name
            )
            _, previous_bytes, previous_existed = service._snapshot_projection(
                current_active, second
            )
            service._write_projection_journal(
                projected_second,
                previous_bytes,
                previous_existed,
                "publish",
            )
            service._project_record(projected_second)
            raise RuntimeError("模拟硬崩溃")

    assert projection.read_bytes() != original_bytes
    assert service._projection_journal_path.exists()
    recovery_repository = GovernedSkillRepository(repository.db_path)
    try:
        recovery_service = GovernedSkillService(
            recovery_repository,
            skills_dir,
            TENANT_ID,
            service.evaluation_policy,
            service.evaluation_runner,
        )
        assert projection.read_bytes() == original_bytes
        assert not recovery_service._projection_journal_path.exists()
        assert recovery_service.verify_projection(
            _identity("reader", "skill:read"), first.name
        ) == active
    finally:
        recovery_repository.close()


def test_subprocess_hard_exit_is_recovered_from_durable_journal(
    governed, tmp_path
):
    service, repository, skills_dir = governed
    first, _, active = _publish_first(service, tmp_path)
    projection = skills_dir / first.name / "SKILL.md"
    original_bytes = projection.read_bytes()
    second = service.propose(
        _identity("proposer", "skill:propose"),
        _proposal(description="第二版说明。", idempotency_key="proposal-2"),
    )
    worker = Path(__file__).parent / "fixtures" / "skill_projection_crash_worker.py"
    result = subprocess.run(
        [
            sys.executable,
            str(worker),
            str(skills_dir),
            str(repository.db_path),
            TENANT_ID,
            second.skill_id,
            str(second.version),
        ],
        cwd=str(Path(__file__).parents[1]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 73, result.stderr.decode("utf-8", errors="replace")
    assert repository.read_version(
        TENANT_ID, second.skill_id, second.version
    ).status is SkillStatus.CANDIDATE
    assert projection.read_bytes() != original_bytes
    assert service._projection_journal_path.exists()

    recovery_repository = GovernedSkillRepository(repository.db_path)
    try:
        recovery_service = GovernedSkillService(
            recovery_repository,
            skills_dir,
            TENANT_ID,
            service.evaluation_policy,
            service.evaluation_runner,
        )
        assert projection.read_bytes() == original_bytes
        assert not recovery_service._projection_journal_path.exists()
        assert recovery_service.verify_projection(
            _identity("reader", "skill:read"), first.name
        ) == active
    finally:
        recovery_repository.close()


def test_startup_finishes_projection_after_postcommit_hard_crash(
    governed, tmp_path, monkeypatch
):
    service, repository, skills_dir = governed
    candidate = service.propose(
        _identity("proposer", "skill:propose"), _proposal()
    )
    evaluation = service.evaluate(
        _identity("validator", "skill:validate"),
        _evaluation_command(tmp_path, candidate),
    )

    def fail_to_clear_journal():
        raise OSError("模拟提交后硬崩溃")

    monkeypatch.setattr(service, "_clear_projection_journal", fail_to_clear_journal)
    with pytest.raises(OSError, match="提交后硬崩溃"):
        service.publish(
            _identity("publisher", "skill:publish"),
            candidate.skill_id,
            candidate.version,
            evaluation.evaluation_id,
            "publish-postcommit-crash",
        )

    assert service._projection_journal_path.exists()
    assert repository.read_version(
        TENANT_ID, candidate.skill_id, candidate.version
    ).status is SkillStatus.ACTIVE
    recovery_repository = GovernedSkillRepository(repository.db_path)
    try:
        recovery_service = GovernedSkillService(
            recovery_repository,
            skills_dir,
            TENANT_ID,
            service.evaluation_policy,
            service.evaluation_runner,
        )
        assert not recovery_service._projection_journal_path.exists()
        assert recovery_service.verify_projection(
            _identity("reader", "skill:read"), candidate.name
        ).status is SkillStatus.ACTIVE
    finally:
        recovery_repository.close()


def test_projection_lock_serializes_independent_processes(tmp_path):
    skills_dir = tmp_path / "skills"
    database = skills_dir / ".system" / "governed-skills.db"
    held_marker = tmp_path / "held.marker"
    probe_marker = tmp_path / "probe.marker"
    release_marker = tmp_path / "release.marker"
    worker = Path(__file__).parent / "fixtures" / "skill_governance_lock_worker.py"
    common_args = [str(skills_dir), str(database), TENANT_ID]
    holder = subprocess.Popen(
        [
            sys.executable,
            str(worker),
            "hold",
            *common_args,
            str(held_marker),
            str(release_marker),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    probe = None
    try:
        deadline = time.monotonic() + 10.0
        while not held_marker.exists():
            if holder.poll() is not None:
                stdout, stderr = holder.communicate()
                raise AssertionError(
                    "持锁子进程提前退出: %s\n%s" % (stdout, stderr)
                )
            if time.monotonic() >= deadline:
                raise AssertionError("持锁子进程没有创建就绪标记")
            time.sleep(0.02)
        probe = subprocess.Popen(
            [
                sys.executable,
                str(worker),
                "probe",
                *common_args,
                str(probe_marker),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.3)
        assert probe.poll() is None
        assert not probe_marker.exists()

        release_marker.write_text("release", encoding="utf-8")
        holder_stdout, holder_stderr = holder.communicate(timeout=10)
        probe_stdout, probe_stderr = probe.communicate(timeout=10)
        assert holder.returncode == 0, (holder_stdout, holder_stderr)
        assert probe.returncode == 0, (probe_stdout, probe_stderr)
        assert probe_marker.read_text(encoding="utf-8") == "acquired"
    finally:
        release_marker.write_text("release", encoding="utf-8")
        for process in (holder, probe):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_rollback_creates_new_version_without_rewriting_history(
    governed, tmp_path
):
    service, _, _ = governed
    first, _, _ = _publish_first(service, tmp_path)
    second = service.propose(
        _identity("proposer", "skill:propose"),
        _proposal(description="第二版说明。", idempotency_key="proposal-2"),
    )
    second_evaluation = service.evaluate(
        _identity("validator-2", "skill:validate"),
        _evaluation_command(
            tmp_path, second, key="evaluation-2", filename="suite-2.json"
        ),
    )
    service.publish(
        _identity("publisher-2", "skill:publish"),
        second.skill_id,
        second.version,
        second_evaluation.evaluation_id,
        "publish-2",
    )
    restored = service.rollback(
        _identity("rollback-publisher", "skill:publish"),
        first.skill_id,
        first.version,
        "第二版出现生产问题",
        "rollback-1",
    )

    assert restored.version == 3
    assert restored.rollback_of_version == 1
    assert restored.content_hash == first.content_hash
    assert restored.description == first.description
    versions = service.list_versions(
        _identity("reader", "skill:read"), first.skill_id
    )
    assert [record.version for record in versions] == [1, 2, 3]
    assert [record.status for record in versions] == [
        SkillStatus.SUPERSEDED,
        SkillStatus.SUPERSEDED,
        SkillStatus.ACTIVE,
    ]
    assert versions[0].created_at == first.created_at
    assert service.verify_projection(
        _identity("reader", "skill:read"), first.name
    ) == restored
    assert service.list_audit(
        _identity("reader", "skill:read"), first.skill_id
    )[-1].action == "skill.rolled_back"


def test_rollback_can_target_a_previous_rollback_version(governed, tmp_path):
    service, _, _ = governed
    first, _, _ = _publish_first(service, tmp_path)
    second = service.propose(
        _identity("proposer", "skill:propose"),
        _proposal(description="第二版说明。", idempotency_key="proposal-2"),
    )
    second_evaluation = service.evaluate(
        _identity("validator-2", "skill:validate"),
        _evaluation_command(
            tmp_path, second, key="evaluation-2", filename="suite-2.json"
        ),
    )
    service.publish(
        _identity("publisher-2", "skill:publish"),
        second.skill_id,
        second.version,
        second_evaluation.evaluation_id,
        "publish-2",
    )
    first_rollback = service.rollback(
        _identity("rollback-publisher-1", "skill:publish"),
        first.skill_id,
        first.version,
        "恢复第一版",
        "rollback-1",
    )
    fourth = service.propose(
        _identity("proposer", "skill:propose"),
        _proposal(description="第四版说明。", idempotency_key="proposal-4"),
    )
    fourth_evaluation = service.evaluate(
        _identity("validator-4", "skill:validate"),
        _evaluation_command(
            tmp_path, fourth, key="evaluation-4", filename="suite-4.json"
        ),
    )
    service.publish(
        _identity("publisher-4", "skill:publish"),
        fourth.skill_id,
        fourth.version,
        fourth_evaluation.evaluation_id,
        "publish-4",
    )

    second_rollback = service.rollback(
        _identity("rollback-publisher-2", "skill:publish"),
        first.skill_id,
        first_rollback.version,
        "再次恢复第一版正文",
        "rollback-2",
    )
    assert second_rollback.version == 5
    assert second_rollback.rollback_of_version == first_rollback.version
    assert second_rollback.content_hash == first.content_hash
    assert service.verify_projection(
        _identity("reader", "skill:read"), first.name
    ) == second_rollback


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 支持目录联接点")
def test_projection_rejects_windows_junction(governed, tmp_path):
    service, _, skills_dir = governed
    candidate = service.propose(
        _identity("proposer", "skill:propose"), _proposal()
    )
    evaluation = service.evaluate(
        _identity("validator", "skill:validate"),
        _evaluation_command(tmp_path, candidate),
    )
    target = skills_dir / "junction-target"
    target.mkdir()
    junction = skills_dir / candidate.name
    _create_windows_junction_or_skip(junction, target)
    try:
        with pytest.raises(SkillTamperError, match="重解析点"):
            service.publish(
                _identity("publisher", "skill:publish"),
                candidate.skill_id,
                candidate.version,
                evaluation.evaluation_id,
                "publish-junction",
            )
    finally:
        os.rmdir(junction)


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 支持目录联接点")
def test_evaluation_rejects_windows_junction(governed, tmp_path):
    service, _, _ = governed
    candidate = service.propose(
        _identity("proposer", "skill:propose"), _proposal()
    )
    physical = tmp_path / "physical-suite"
    physical.mkdir()
    suite = physical / "junction-suite.json"
    suite.write_text(
        json.dumps({"cases": list(_passing_cases())}, ensure_ascii=False),
        encoding="utf-8",
    )
    junction = tmp_path / "suite-junction"
    _create_windows_junction_or_skip(junction, physical)
    try:
        command = SkillEvaluationCommand(
            skill_id=candidate.skill_id,
            version=candidate.version,
            suite_path=str(junction / suite.name),
            suite_sha256=_sha(suite.read_bytes()),
            model_id=MODEL_ID,
            idempotency_key="evaluation-junction",
        )
        with pytest.raises(SkillTamperError, match="重解析点"):
            service.evaluate(
                _identity("validator", "skill:validate"), command
            )
    finally:
        os.rmdir(junction)


def test_idempotency_conflict_and_cross_connection_concurrency(tmp_path):
    skills_dir = tmp_path / "skills"
    db_path = skills_dir / ".system" / "governed-skills.db"
    repositories = [GovernedSkillRepository(db_path) for _ in range(4)]
    services = [
        GovernedSkillService(
            repository,
            skills_dir,
            TENANT_ID,
            EvaluationPolicy(3, 200.0, 1.10),
            _fixture_runner(tmp_path),
        )
        for repository in repositories
    ]
    identity = _identity("proposer", "skill:propose")
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            same_results = list(
                executor.map(
                    lambda service: service.propose(identity, _proposal()), services
                )
            )
        assert {(record.skill_id, record.version) for record in same_results} == {
            (same_results[0].skill_id, 1)
        }

        proposals = [
            _proposal(
                description="并发候选 %d。" % index,
                idempotency_key="concurrent-%d" % index,
                source_payload=("trace-%d" % index).encode("utf-8"),
            )
            for index in range(12)
        ]
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(
                    services[index % len(services)].propose, identity, proposal
                )
                for index, proposal in enumerate(proposals)
            ]
            records = [future.result() for future in futures]
        assert sorted(record.version for record in records) == list(range(2, 14))
        assert len({record.skill_id for record in records}) == 1

        conflict = _proposal(description="不同请求。", idempotency_key="proposal-1")
        with pytest.raises(IdempotencyConflictError):
            services[0].propose(identity, conflict)
    finally:
        for repository in repositories:
            repository.close()


def test_governance_write_and_read_microbenchmark(governed):
    service, _, _ = governed
    proposer = _identity("benchmark-proposer", "skill:propose")
    started = time.perf_counter()
    first = None
    for index in range(80):
        record = service.propose(
            proposer,
            _proposal(
                name="benchmark-skill",
                description="微基准候选 %d。" % index,
                idempotency_key="benchmark-%d" % index,
                source_payload=("benchmark-trace-%d" % index).encode("utf-8"),
            ),
        )
        first = first or record
    write_seconds = time.perf_counter() - started

    read_started = time.perf_counter()
    versions = service.list_versions(proposer, first.skill_id)
    read_seconds = time.perf_counter() - read_started
    assert len(versions) == 80
    assert write_seconds < 10.0
    assert read_seconds < 1.0
    print(
        "governed_skill_microbenchmark writes=80 write_seconds=%.6f read_seconds=%.6f"
        % (write_seconds, read_seconds)
    )
