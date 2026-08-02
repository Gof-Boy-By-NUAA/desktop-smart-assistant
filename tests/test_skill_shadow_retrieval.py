"""有效技能影子检索的治理、隐私和统一入口测试。"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import nullcontext
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent.chat.service import ChatService
from agent.evolution import executor as evolution_executor
from agent.memory.governance import IdentityContext
from agent.protocol.agent import Agent
from agent.protocol.agent_stream import AgentStreamExecutor
from agent.skills.governance import (
    ControlledPairedSuiteRunner,
    EvaluationPolicy,
    GovernedSkillRepository,
    GovernedSkillService,
    PairedCaseExecutor,
    SkillEvaluationCommand,
    SkillAuthorizationError,
    SkillProposal,
    SkillStatus,
    SourceEvidence,
)
from agent.skills.retrieval import (
    ActiveSkillShadowRuntime,
    ShadowCandidate,
    ShadowRun,
)
from agent.skills.retrieval.telemetry import ShadowTelemetryRepository
from agent.skills.manager import SkillManager


TENANT_ID = "shadow-tenant"
MODEL_ID = "shadow-model@1"


def _identity(user_id: str, *roles: str) -> IdentityContext:
    return IdentityContext(
        tenant_id=TENANT_ID,
        actor_user_id=user_id,
        roles=frozenset(roles),
        trace_id="trace-%s" % user_id,
        auth_source="shadow-test",
    )


class _AlwaysImprovesExecutor(PairedCaseExecutor):
    """让候选稳定优于基线的测试执行器。"""

    @property
    def executor_id(self) -> str:
        return "shadow-fixture"

    @property
    def executor_version(self) -> str:
        return "1.0.0"

    def execute_baseline(self, *, model_id, case_input):
        time.sleep(0.002)
        return {"accepted": False}

    def execute_candidate(self, *, model_id, candidate, case_input):
        return {"accepted": True}


@pytest.fixture
def governed_shadow(tmp_path):
    skills_dir = tmp_path / "skills"
    repository = GovernedSkillRepository(
        skills_dir / ".system" / "governed-skills.db"
    )
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(
        json.dumps(
            {"cases": [{"input": {"case": "one"}, "expected": {"accepted": True}}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = GovernedSkillService(
        repository,
        skills_dir,
        TENANT_ID,
        EvaluationPolicy(
            minimum_sample_count=1,
            max_candidate_p95_latency_ms=1000.0,
            max_latency_regression_ratio=1.0,
        ),
        ControlledPairedSuiteRunner(tmp_path, _AlwaysImprovesExecutor()),
    )
    runtime = ActiveSkillShadowRuntime(
        repository,
        skills_dir / ".system" / "shadow-index.db",
        skills_dir / ".system" / "shadow-telemetry.db",
        TENANT_ID,
    )
    try:
        yield service, repository, runtime, suite_path
    finally:
        runtime.close()
        repository.close()


def _proposal(name: str, description: str, key: str) -> SkillProposal:
    source = ("source-%s" % key).encode("utf-8")
    return SkillProposal(
        name=name,
        description=description,
        applicability=("需要核对财务表格输入",),
        steps=("核对原始行列", "检查单位和正负号", "执行计算"),
        validation_rules=("逐项对照原始数据",),
        contraindications=("没有原始数据时不得使用",),
        model_compatibility=(MODEL_ID,),
        sources=(
            SourceEvidence(
                source_type="fixture",
                source_ref="fixture://%s" % key,
                payload=source,
                sha256=hashlib.sha256(source).hexdigest(),
            ),
        ),
        idempotency_key=key,
    )


def _evaluate_and_publish(service, suite_path: Path, record, key: str):
    suite_hash = hashlib.sha256(suite_path.read_bytes()).hexdigest()
    evaluation = service.evaluate(
        _identity("validator-%s" % key, "skill:validate"),
        SkillEvaluationCommand(
            skill_id=record.skill_id,
            version=record.version,
            suite_path=str(suite_path),
            suite_sha256=suite_hash,
            model_id=MODEL_ID,
            idempotency_key="evaluation-%s" % key,
        ),
    )
    return service.publish(
        _identity("publisher-%s" % key, "skill:publish"),
        record.skill_id,
        record.version,
        evaluation.evaluation_id,
        "publish-%s" % key,
    )


def test_shadow_index_only_contains_current_active_versions(governed_shadow):
    service, _, runtime, suite_path = governed_shadow
    proposer = _identity("proposer", "skill:propose")
    reader = _identity("reader", "skill:read")

    candidate_only = service.propose(
        proposer,
        _proposal("candidate-only", "候选技能特征词 候选孤岛", "candidate-only"),
    )
    rejected = service.propose(
        proposer,
        _proposal("rejected-only", "拒绝技能特征词 拒绝孤岛", "rejected-only"),
    )
    service.reject(
        _identity("rejector", "skill:validate"),
        rejected.skill_id,
        rejected.version,
        "不满足发布条件",
        "reject-once",
    )

    first = service.propose(
        proposer,
        _proposal("finance-check", "财务表格正负号核对", "finance-v1"),
    )
    active_v1 = _evaluate_and_publish(service, suite_path, first, "v1")
    first_run = runtime.start_run(
        reader, "财务表格正负号核对", MODEL_ID, "session-one", top_k=10
    )

    assert active_v1.status is SkillStatus.ACTIVE
    assert [(item.skill_id, item.version) for item in first_run.candidates] == [
        (active_v1.skill_id, 1)
    ]
    assert not runtime.index.contains_document(TENANT_ID, candidate_only.skill_id)
    assert not runtime.index.contains_document(TENANT_ID, rejected.skill_id)

    second = service.propose(
        proposer,
        _proposal("finance-check", "财务表格正负号二次复核", "finance-v2"),
    )
    before_publish = runtime.start_run(
        reader, "财务表格正负号核对", MODEL_ID, "session-one", top_k=10
    )
    assert [(item.skill_id, item.version) for item in before_publish.candidates] == [
        (active_v1.skill_id, 1)
    ]

    active_v2 = _evaluate_and_publish(service, suite_path, second, "v2")
    after_publish = runtime.start_run(
        reader, "财务表格正负号二次复核", MODEL_ID, "session-one", top_k=10
    )
    assert service.get_version(reader, active_v1.skill_id, 1).status is SkillStatus.SUPERSEDED
    assert [(item.skill_id, item.version) for item in after_publish.candidates] == [
        (active_v2.skill_id, 2)
    ]
    assert after_publish.index_generation != first_run.index_generation


def test_candidate_is_rechecked_against_governance_before_telemetry(
    governed_shadow, monkeypatch
):
    service, _, runtime, suite_path = governed_shadow
    proposer = _identity("proposer-race", "skill:propose")
    reader = _identity("reader-race", "skill:read")
    first = service.propose(
        proposer,
        _proposal("race-check", "竞态核对技能", "race-v1"),
    )
    active_v1 = _evaluate_and_publish(service, suite_path, first, "race-v1")
    runtime.rebuild_active_index()
    stale_results = runtime.index.search(
        reader,
        "竞态核对技能",
        limit=5,
        session_id="race-session",
        collection_ids=("governed-skills-active",),
    )
    assert stale_results

    second = service.propose(
        proposer,
        _proposal("race-check", "竞态核对技能新版本", "race-v2"),
    )
    published = False

    def publish_during_search(*args, **kwargs):
        nonlocal published
        if not published:
            _evaluate_and_publish(service, suite_path, second, "race-v2")
            published = True
        return stale_results

    monkeypatch.setattr(runtime.index, "search", publish_during_search)
    run = runtime.start_run(
        reader, "竞态核对技能", MODEL_ID, "race-session", top_k=5
    )
    evidence = json.loads(runtime.export_evidence(run.run_id).decode("utf-8"))

    assert service.get_version(reader, active_v1.skill_id, 1).status is SkillStatus.SUPERSEDED
    assert run.candidates == ()
    assert evidence["candidates"] == []


def test_content_hash_mismatch_on_final_recheck_is_not_persisted(
    governed_shadow, monkeypatch
):
    service, repository, runtime, suite_path = governed_shadow
    candidate = service.propose(
        _identity("hash-proposer", "skill:propose"),
        _proposal("hash-check", "内容哈希复核", "hash-v1"),
    )
    active = _evaluate_and_publish(service, suite_path, candidate, "hash-v1")
    original_read = repository.read_version
    calls = 0

    def mismatch_on_second_read(tenant_id, skill_id, version):
        nonlocal calls
        fact = original_read(tenant_id, skill_id, version)
        if skill_id == active.skill_id:
            calls += 1
            if calls >= 2:
                return replace(fact, content_hash="f" * 64)
        return fact

    monkeypatch.setattr(repository, "read_version", mismatch_on_second_read)
    run = runtime.start_run(
        _identity("hash-reader", "skill:read"),
        "内容哈希复核",
        MODEL_ID,
        "hash-session",
    )
    evidence = json.loads(runtime.export_evidence(run.run_id).decode("utf-8"))

    assert calls >= 2
    assert run.candidates == ()
    assert evidence["candidates"] == []


def test_runtime_serializes_generation_rebuild_and_search(
    governed_shadow, monkeypatch
):
    service, _, runtime, suite_path = governed_shadow
    proposer = _identity("serial-proposer", "skill:propose")
    reader = _identity("serial-reader", "skill:read")
    first = service.propose(
        proposer,
        _proposal("serial-check", "串行索引旧版本", "serial-v1"),
    )
    _evaluate_and_publish(service, suite_path, first, "serial-v1")
    runtime.rebuild_active_index()

    second = service.propose(
        proposer,
        _proposal("serial-check", "串行索引新版本", "serial-v2"),
    )
    entered = threading.Event()
    release = threading.Event()
    original_search = runtime.index.search
    search_calls = 0

    def blocking_first_search(*args, **kwargs):
        nonlocal search_calls
        search_calls += 1
        if search_calls == 1:
            entered.set()
            assert release.wait(timeout=5)
        return original_search(*args, **kwargs)

    monkeypatch.setattr(runtime.index, "search", blocking_first_search)
    second_runtime = ActiveSkillShadowRuntime(
        runtime.governance_repository,
        runtime.index_path,
        runtime.telemetry_path,
        TENANT_ID,
    )
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            first_future = pool.submit(
                runtime.start_run,
                reader,
                "串行索引旧版本",
                MODEL_ID,
                "serial-one",
            )
            assert entered.wait(timeout=5)
            publish_future = pool.submit(
                _evaluate_and_publish,
                service,
                suite_path,
                second,
                "serial-v2",
            )
            time.sleep(0.05)
            assert not publish_future.done()
            release.set()
            first_run = first_future.result(timeout=5)
            active_v2 = publish_future.result(timeout=5)
            second_run = second_runtime.start_run(
                reader,
                "串行索引新版本",
                MODEL_ID,
                "serial-two",
            )
    finally:
        second_runtime.close()

    assert [(item.skill_id, item.version) for item in first_run.candidates] == [
        (first.skill_id, 1)
    ]
    assert [(item.skill_id, item.version) for item in second_run.candidates] == [
        (active_v2.skill_id, 2)
    ]
    assert first_run.index_generation != second_run.index_generation


def test_unchanged_generation_still_repairs_tampered_index(governed_shadow):
    service, _, runtime, suite_path = governed_shadow
    candidate = service.propose(
        _identity("tamper-proposer", "skill:propose"),
        _proposal("tamper-check", "正常财务核对", "tamper-v1"),
    )
    active = _evaluate_and_publish(service, suite_path, candidate, "tamper-v1")
    reader = _identity("tamper-reader", "skill:read")
    first_run = runtime.start_run(
        reader, "正常财务核对", MODEL_ID, "tamper-session"
    )
    assert [item.skill_id for item in first_run.candidates] == [active.skill_id]

    runtime.index._conn.execute(
        "UPDATE retrieval_documents SET title = ?, text = ? "
        "WHERE tenant_id = ? AND document_id = ?",
        ("恶意触发词", "恶意触发词", TENANT_ID, active.skill_id),
    )
    runtime.index._conn.commit()
    tampered_run = runtime.start_run(
        reader, "恶意触发词", MODEL_ID, "tamper-session"
    )
    repaired_run = runtime.start_run(
        reader, "正常财务核对", MODEL_ID, "tamper-session"
    )

    assert tampered_run.index_generation == first_run.index_generation
    assert tampered_run.candidates == ()
    assert [item.skill_id for item in repaired_run.candidates] == [active.skill_id]


def test_shadow_telemetry_never_exports_task_arguments_or_results(governed_shadow):
    service, _, runtime, suite_path = governed_shadow
    candidate = service.propose(
        _identity("privacy-proposer", "skill:propose"),
        _proposal("privacy-check", "隐私边界核对", "privacy-v1"),
    )
    _evaluate_and_publish(service, suite_path, candidate, "privacy-v1")

    task_secret = "TOP_SECRET_TASK_51A7"
    argument_secret = "TOP_SECRET_ARGUMENT_93B2"
    result_secret = "TOP_SECRET_RESULT_42C9"
    identity = _identity("private-actor", "skill:read")
    run = runtime.start_run(
        identity,
        "隐私边界核对 " + task_secret,
        MODEL_ID,
        "private-session",
        top_k=5,
    )
    runtime.record_tool_use(
        run,
        "private-call",
        "bash",
        {"command": argument_secret, "timeout": 30},
    )
    runtime.record_tool_result(
        run,
        "private-call",
        "error",
        12.5,
        result_secret,
    )
    runtime.finish_run(run, "completed", "answer " + result_secret)

    evidence_bytes = runtime.export_evidence(run.run_id)
    evidence = json.loads(evidence_bytes.decode("utf-8"))
    forbidden = (
        task_secret,
        argument_secret,
        result_secret,
        identity.actor_user_id,
        "private-session",
        "private-call",
    )
    for value in forbidden:
        assert value.encode("utf-8") not in evidence_bytes

    shape = evidence["tools"][0]["arguments_shape"]
    assert set(shape) == {"command", "timeout"}
    assert set(shape["command"]) == {"type", "length", "hmac"}
    assert evidence["tools"][0]["result_type"] == "str"
    assert evidence["tools"][0]["error_class"] == "tool_error"

    runtime.telemetry._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    runtime.telemetry._conn.commit()
    sidecars = [
        runtime.telemetry_path,
        Path(str(runtime.telemetry_path) + "-wal"),
        Path(str(runtime.telemetry_path) + "-shm"),
    ]
    for path in sidecars:
        if not path.exists():
            continue
        persisted = path.read_bytes()
        for value in forbidden:
            assert value.encode("utf-8") not in persisted


def test_finished_shadow_run_is_sealed(governed_shadow):
    service, _, runtime, suite_path = governed_shadow
    candidate = service.propose(
        _identity("seal-proposer", "skill:propose"),
        _proposal("seal-check", "封存边界核对", "seal-v1"),
    )
    _evaluate_and_publish(service, suite_path, candidate, "seal-v1")
    run = runtime.start_run(
        _identity("seal-reader", "skill:read"),
        "封存边界核对",
        MODEL_ID,
        "seal-session",
    )
    runtime.finish_run(run, "completed", "done")
    first_export = runtime.export_evidence(run.run_id)

    with pytest.raises(ValueError, match="封存"):
        runtime.record_tool_use(run, "late-call", "read", {"path": "secret"})
    with pytest.raises(ValueError, match="封存"):
        runtime.finish_run(run, "completed", "changed")

    assert runtime.export_evidence(run.run_id) == first_export


def test_missing_tool_result_rolls_back_and_next_run_can_start(governed_shadow):
    _, _, runtime, _ = governed_shadow
    identity = _identity("rollback-reader", "skill:read")
    run = runtime.start_run(
        identity, "没有候选也可以记录", MODEL_ID, "rollback-session"
    )

    with pytest.raises(KeyError, match="工具调用不存在"):
        runtime.telemetry.record_tool_result(
            run.run_id, "missing-call", "error", 1.0, "missing"
        )

    assert runtime.telemetry._conn.in_transaction is False
    next_run = runtime.start_run(
        identity, "事务回滚后继续", MODEL_ID, "rollback-session"
    )
    assert next_run.run_id != run.run_id


def test_future_telemetry_schema_is_rejected_without_mutation(tmp_path):
    db_path = tmp_path / "future-shadow.db"
    with sqlite3.connect(str(db_path)) as connection:
        connection.execute("CREATE TABLE future_only (value TEXT)")
        connection.execute("PRAGMA user_version=99")
        before = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()

    with pytest.raises(ValueError, match="版本高于"):
        ShadowTelemetryRepository(db_path, tmp_path / "future.key")

    with sqlite3.connect(str(db_path)) as connection:
        after = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert after == before == [("future_only",)]
    assert version == 99


def test_hmac_domains_prevent_cross_field_linkability(tmp_path):
    telemetry = ShadowTelemetryRepository(
        tmp_path / "domains.db", tmp_path / "domains.key"
    )
    try:
        payload = b"same-value"
        assert telemetry.digest(payload, "task") != telemetry.digest(payload, "actor")
        assert telemetry.digest(payload, "actor") != telemetry.digest(payload, "session")
    finally:
        telemetry.close()


def test_runtime_constructor_closes_index_when_telemetry_fails(
    tmp_path, monkeypatch
):
    from agent.skills.retrieval import runtime as runtime_module

    created = []

    class IndexStub:
        def __init__(self, *args, **kwargs):
            self.closed = False
            created.append(self)

        def close(self):
            self.closed = True

    class TelemetryStub:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("telemetry init failed")

    monkeypatch.setattr(runtime_module, "TenantAwareLexicalIndex", IndexStub)
    monkeypatch.setattr(runtime_module, "ShadowTelemetryRepository", TelemetryStub)
    repository = object()

    with pytest.raises(RuntimeError, match="telemetry init failed"):
        ActiveSkillShadowRuntime(
            repository,
            tmp_path / "index.db",
            tmp_path / "telemetry.db",
            TENANT_ID,
        )

    assert len(created) == 1
    assert created[0].closed is True


def test_runtime_constructor_closes_both_resources_when_prune_fails(
    tmp_path, monkeypatch
):
    from agent.skills.retrieval import runtime as runtime_module

    indexes = []
    telemetry_instances = []

    class IndexStub:
        def __init__(self, *args, **kwargs):
            self.closed = False
            indexes.append(self)

        def close(self):
            self.closed = True

    class TelemetryStub:
        def __init__(self, *args, **kwargs):
            self.closed = False
            telemetry_instances.append(self)

        def prune(self, retention_days):
            raise RuntimeError("prune failed")

        def close(self):
            self.closed = True

    monkeypatch.setattr(runtime_module, "TenantAwareLexicalIndex", IndexStub)
    monkeypatch.setattr(runtime_module, "ShadowTelemetryRepository", TelemetryStub)

    with pytest.raises(RuntimeError, match="prune failed"):
        ActiveSkillShadowRuntime(
            object(),
            tmp_path / "index.db",
            tmp_path / "telemetry.db",
            TENANT_ID,
        )

    assert indexes[0].closed is True
    assert telemetry_instances[0].closed is True


def test_skill_manager_creates_one_shadow_runtime_under_concurrency(tmp_path):
    skills_dir = tmp_path / "skills"
    repository = GovernedSkillRepository(
        skills_dir / ".system" / "governed-skills.db"
    )
    service = GovernedSkillService(repository, skills_dir, TENANT_ID)
    service.propose(
        _identity("manager-proposer", "skill:propose"),
        _proposal("manager-check", "并发初始化核对", "manager-v1"),
    )
    repository.close()
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir()
    manager = SkillManager(
        builtin_dir=str(builtin_dir),
        custom_dir=str(skills_dir),
        tenant_id=TENANT_ID,
    )
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            runtimes = list(pool.map(lambda _: manager.get_shadow_runtime(), range(16)))
        assert all(runtime is runtimes[0] for runtime in runtimes)
        assert manager._shadow_runtime is runtimes[0]
    finally:
        manager.close()


class _DummyModel:
    model = MODEL_ID
    model_name = MODEL_ID
    session_id = ""
    channel_type = ""


class _RecordingShadowRuntime:
    def __init__(self):
        self.started = []
        self.finished = []
        self.tool_uses = []
        self.tool_results = []
        self.injections = []

    def start_run(self, identity, task, model_id, session_id, top_k=5):
        self.started.append((identity, task, model_id, session_id, top_k))
        return ShadowRun("recorded-run", "recorded-generation", ())

    def finish_run(self, run, status, final_response):
        self.finished.append((run, status, final_response))

    def record_tool_use(self, run, call_id, tool_name, arguments):
        self.tool_uses.append((run, call_id, tool_name, arguments))

    def record_tool_result(self, run, call_id, status, latency_ms, result):
        self.tool_results.append((run, call_id, status, latency_ms, result))

    def record_injection(self, run, status, candidates):
        self.injections.append((run, status, tuple(candidates)))


class _FailingShadowRuntime(_RecordingShadowRuntime):
    def __init__(self, stage):
        super().__init__()
        self.stage = stage

    def start_run(self, identity, task, model_id, session_id, top_k=5):
        if self.stage == "start":
            raise RuntimeError("shadow start failed")
        return super().start_run(identity, task, model_id, session_id, top_k)

    def finish_run(self, run, status, final_response):
        if self.stage == "finish":
            raise RuntimeError("shadow finish failed")
        return super().finish_run(run, status, final_response)


class _RecordingSkillManager:
    def __init__(self, runtime):
        self.runtime = runtime

    def get_shadow_runtime(self):
        return self.runtime

    def build_skills_prompt(self, skill_filter=None):
        return ""


def _new_agent(runtime, session_id="agent-session"):
    agent = Agent(
        "fixed-system-prompt",
        model=_DummyModel(),
        tools=[],
        max_steps=2,
        skill_manager=_RecordingSkillManager(runtime),
    )
    agent.identity_context = _identity("entry-user", "skill:read")
    agent.session_id = session_id
    return agent


def test_agent_entry_shadow_mode_does_not_change_llm_inputs(monkeypatch):
    from config import conf

    snapshots = []

    def fake_call(executor, retry_on_empty=True):
        snapshots.append(
            {
                "system_prompt": executor.system_prompt,
                "messages": copy.deepcopy(executor.messages),
                "tools": tuple(executor.tools),
            }
        )
        return "fixed-answer", []

    monkeypatch.setattr(AgentStreamExecutor, "_call_llm_stream", fake_call)
    settings = conf()

    disabled_runtime = _RecordingShadowRuntime()
    monkeypatch.setitem(settings, "skill_shadow_retrieval_enabled", False)
    disabled_answer = _new_agent(disabled_runtime).run_stream("same task")
    disabled_snapshot = snapshots.pop()

    enabled_runtime = _RecordingShadowRuntime()
    monkeypatch.setitem(settings, "skill_shadow_retrieval_enabled", True)
    enabled_answer = _new_agent(enabled_runtime).run_stream("same task")
    enabled_snapshot = snapshots.pop()

    assert disabled_answer == enabled_answer == "fixed-answer"
    assert disabled_snapshot == enabled_snapshot
    assert disabled_runtime.started == []
    assert len(enabled_runtime.started) == 1
    assert enabled_runtime.finished[0][1:] == ("completed", "fixed-answer")
    enabled_agent = _new_agent(_RecordingShadowRuntime())
    enabled_agent.run_stream("binding task")
    assert enabled_agent._skill_shadow_completed_runs == [
        {"run_id": "recorded-run", "message_start": 0, "message_end": 1}
    ]


def test_missing_shadow_config_key_defaults_to_disabled(monkeypatch):
    from config import conf

    monkeypatch.delitem(
        conf(), "skill_shadow_retrieval_enabled", raising=False
    )
    monkeypatch.setattr(
        AgentStreamExecutor,
        "_call_llm_stream",
        lambda executor, retry_on_empty=True: ("fixed-answer", []),
    )
    runtime = _RecordingShadowRuntime()

    assert _new_agent(runtime).run_stream("same task") == "fixed-answer"
    assert runtime.started == []


def test_chat_service_entry_uses_the_same_shadow_hook(monkeypatch):
    from config import conf

    runtime = _RecordingShadowRuntime()
    agent = _new_agent(runtime, session_id="chat-session")
    expected_prompt = agent.get_full_system_prompt()
    observed = {}

    def fake_call(executor, retry_on_empty=True):
        observed["system_prompt"] = executor.system_prompt
        observed["messages"] = copy.deepcopy(executor.messages)
        observed["tools"] = tuple(executor.tools)
        return "chat-answer", []

    class BridgeStub:
        def get_agent(self, session_id):
            return agent

    monkeypatch.setattr(AgentStreamExecutor, "_call_llm_stream", fake_call)
    settings = conf()
    monkeypatch.setitem(settings, "skill_shadow_retrieval_enabled", True)
    monkeypatch.setitem(settings, "conversation_persistence", False)

    ChatService(BridgeStub()).run(
        "chat task",
        "chat-session",
        lambda chunk: None,
        channel_type="test",
    )

    assert observed["system_prompt"] == expected_prompt
    assert observed["tools"] == ()
    assert observed["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "chat task"}]}
    ]
    assert len(runtime.started) == 1
    assert runtime.started[0][3] == "chat-session"
    assert runtime.finished[0][1:] == ("completed", "chat-answer")


def test_missing_external_tool_ids_still_get_unique_shadow_events():
    runtime = _RecordingShadowRuntime()
    executor = object.__new__(AgentStreamExecutor)
    executor._skill_shadow_runtime = runtime
    executor._skill_shadow_run = ShadowRun("tool-run", "generation", ())
    executor._skill_shadow_call_ids = {}
    executor._skill_shadow_tool_ordinal = 0
    first = {"name": "bash", "arguments": {"command": "one"}}
    second = {"name": "bash", "arguments": {"command": "two"}}

    executor._shadow_record_tool_use(first)
    executor._shadow_record_tool_result(
        first, {"status": "success", "execution_time": 0.1, "result": "one"}
    )
    executor._shadow_record_tool_use(second)
    executor._shadow_record_tool_result(
        second, {"status": "error", "execution_time": 0.2, "result": "two"}
    )

    use_ids = [item[1] for item in runtime.tool_uses]
    result_ids = [item[1] for item in runtime.tool_results]
    assert use_ids == result_ids == ["1:missing", "2:missing"]


@pytest.mark.parametrize("stage", ["start", "finish"])
def test_shadow_failure_does_not_change_agent_answer(monkeypatch, stage):
    from config import conf

    monkeypatch.setitem(conf(), "skill_shadow_retrieval_enabled", True)
    monkeypatch.setattr(
        AgentStreamExecutor,
        "_call_llm_stream",
        lambda executor, retry_on_empty=True: ("answer-survives", []),
    )
    agent = _new_agent(_FailingShadowRuntime(stage))

    assert agent.run_stream("failure task") == "answer-survives"
    assert not hasattr(agent, "_skill_shadow_completed_runs")


class _ProductionSkillManager:
    """模拟自动选择边界，同时保留非治理技能。"""

    def __init__(self, runtime, reject_all=False):
        self.runtime = runtime
        self.reject_all = reject_all
        self.prompt_filters = []
        self.refresh_count = 0

    def refresh_skills(self):
        self.refresh_count += 1

    def get_shadow_runtime(self):
        return self.runtime

    def build_skills_prompt(self, skill_filter=None):
        self.prompt_filters.append(
            None if skill_filter is None else tuple(skill_filter)
        )
        names = (
            ("builtin-safe", "custom-safe", "gov-a", "gov-b")
            if skill_filter is None
            else tuple(skill_filter)
        )
        return "\n".join("<name>%s</name>" % name for name in names)

    def production_injection_lock(self):
        return nullcontext()

    def verify_production_candidates(self, identity, candidates, model_id):
        if self.reject_all:
            return (), ()
        verified = tuple(
            candidate
            for candidate in candidates
            if candidate.model_compatible
        )
        return verified, tuple(candidate.skill_id for candidate in verified)

    @staticmethod
    def production_skill_filter(selected_governed_names):
        return ["builtin-safe", "custom-safe", *selected_governed_names]


class _ProductionRuntime(_RecordingShadowRuntime):
    def __init__(self, candidates=(), fail_start=False):
        super().__init__()
        self.candidates = tuple(candidates)
        self.fail_start = fail_start

    def start_run(self, identity, task, model_id, session_id, top_k=5):
        if self.fail_start:
            raise RuntimeError("retrieval unavailable")
        self.started.append((identity, task, model_id, session_id, top_k))
        return ShadowRun(
            "production-run", "production-generation", self.candidates[:top_k]
        )


def _production_candidate(
    skill_id="gov-a", *, model_compatible=True, rank=1
):
    return ShadowCandidate(
        rank=rank,
        skill_id=skill_id,
        version=1,
        content_hash=("a" if skill_id == "gov-a" else "b") * 64,
        score=1.0,
        bm25_score=1.0,
        query_coverage=1.0,
        model_compatible=model_compatible,
    )


def _production_agent(manager, session_id="production-session"):
    agent = Agent(
        "cached-system-prompt",
        model=_DummyModel(),
        tools=[],
        max_steps=2,
        skill_manager=manager,
    )
    agent.identity_context = _identity("production-user", "skill:read")
    agent.session_id = session_id
    return agent


def test_explicit_skill_filter_is_honored_and_bypasses_auto_selection(monkeypatch):
    from config import conf

    runtime = _ProductionRuntime((_production_candidate(),))
    manager = _ProductionSkillManager(runtime)
    agent = _production_agent(manager)
    observed = {}

    def fake_call(executor, retry_on_empty=True):
        observed["prompt"] = executor.system_prompt
        return "explicit-answer", []

    monkeypatch.setattr(AgentStreamExecutor, "_call_llm_stream", fake_call)
    monkeypatch.setitem(conf(), "skill_shadow_retrieval_enabled", False)
    monkeypatch.setitem(conf(), "skill_retrieval_injection_enabled", True)

    assert agent.run_stream("explicit task", skill_filter=["custom-safe"]) == "explicit-answer"
    assert "<name>custom-safe</name>" in observed["prompt"]
    assert "<name>gov-a</name>" not in observed["prompt"]
    assert runtime.started == []
    assert agent._last_skill_injection["status"] == "explicit_filter"


def test_missing_production_config_key_defaults_to_disabled(monkeypatch):
    from config import conf

    runtime = _ProductionRuntime((_production_candidate(),))
    agent = _production_agent(_ProductionSkillManager(runtime))
    monkeypatch.setitem(conf(), "skill_shadow_retrieval_enabled", False)
    monkeypatch.delitem(
        conf(), "skill_retrieval_injection_enabled", raising=False
    )
    monkeypatch.setattr(
        AgentStreamExecutor,
        "_call_llm_stream",
        lambda executor, retry_on_empty=True: ("disabled-answer", []),
    )

    assert agent.run_stream("disabled production task") == "disabled-answer"
    assert runtime.started == []
    assert not hasattr(agent, "_last_skill_injection")


def test_agent_entry_injects_verified_top_k_and_preserves_ungoverned(monkeypatch):
    from config import conf

    runtime = _ProductionRuntime(
        (
            _production_candidate("gov-a", rank=1),
            _production_candidate("gov-b", rank=2),
        )
    )
    manager = _ProductionSkillManager(runtime)
    agent = _production_agent(manager)
    observed = {}

    def fake_call(executor, retry_on_empty=True):
        observed["prompt"] = executor.system_prompt
        return "production-answer", []

    monkeypatch.setattr(AgentStreamExecutor, "_call_llm_stream", fake_call)
    settings = conf()
    monkeypatch.setitem(settings, "skill_shadow_retrieval_enabled", False)
    monkeypatch.setitem(settings, "skill_retrieval_injection_enabled", True)
    monkeypatch.setitem(settings, "skill_retrieval_injection_top_k", 1)

    secret_task = "PRIVATE_PRODUCTION_TASK_7F1C"
    assert agent.run_stream(secret_task) == "production-answer"
    assert "<name>builtin-safe</name>" in observed["prompt"]
    assert "<name>custom-safe</name>" in observed["prompt"]
    assert "<name>gov-a</name>" in observed["prompt"]
    assert "<name>gov-b</name>" not in observed["prompt"]
    assert runtime.injections[0][1] == "injected"
    state_json = json.dumps(agent._last_skill_injection, sort_keys=True)
    assert secret_task not in state_json
    assert agent._last_skill_injection["selected_count"] == 1
    assert agent._last_skill_injection["telemetry_recorded"] is True


def test_chat_service_uses_same_production_injection_path(monkeypatch):
    from config import conf

    runtime = _ProductionRuntime((_production_candidate(),))
    manager = _ProductionSkillManager(runtime)
    agent = _production_agent(manager, session_id="chat-production")
    observed = {}

    class BridgeStub:
        def get_agent(self, session_id):
            return agent

    def fake_call(executor, retry_on_empty=True):
        observed["prompt"] = executor.system_prompt
        return "chat-production-answer", []

    monkeypatch.setattr(AgentStreamExecutor, "_call_llm_stream", fake_call)
    settings = conf()
    monkeypatch.setitem(settings, "skill_shadow_retrieval_enabled", False)
    monkeypatch.setitem(settings, "skill_retrieval_injection_enabled", True)
    monkeypatch.setitem(settings, "skill_retrieval_injection_top_k", 1)
    monkeypatch.setitem(settings, "conversation_persistence", False)

    ChatService(BridgeStub()).run(
        "chat production task",
        "chat-production",
        lambda chunk: None,
        channel_type="test",
    )

    assert "<name>builtin-safe</name>" in observed["prompt"]
    assert "<name>gov-a</name>" in observed["prompt"]
    assert runtime.started[0][3] == "chat-production"
    assert runtime.injections[0][1] == "injected"


def test_retrieval_failure_removes_governed_metadata_without_full_fallback(monkeypatch):
    from config import conf

    runtime = _ProductionRuntime(fail_start=True)
    manager = _ProductionSkillManager(runtime)
    agent = _production_agent(manager)
    observed = {}

    def fake_call(executor, retry_on_empty=True):
        observed["prompt"] = executor.system_prompt
        return "fail-closed-answer", []

    monkeypatch.setattr(AgentStreamExecutor, "_call_llm_stream", fake_call)
    monkeypatch.setitem(conf(), "skill_shadow_retrieval_enabled", False)
    monkeypatch.setitem(conf(), "skill_retrieval_injection_enabled", True)

    assert agent.run_stream("retrieval failure task") == "fail-closed-answer"
    assert "<name>builtin-safe</name>" in observed["prompt"]
    assert "<name>custom-safe</name>" in observed["prompt"]
    assert "<name>gov-a</name>" not in observed["prompt"]
    assert "<name>gov-b</name>" not in observed["prompt"]
    assert agent._last_skill_injection["status"] == "retrieval_failed"


def test_telemetry_failure_prevents_unaudited_injection(monkeypatch):
    from config import conf

    runtime = _ProductionRuntime((_production_candidate(),))
    manager = _ProductionSkillManager(runtime)
    agent = _production_agent(manager)
    observed = {}

    def fail_record(*args, **kwargs):
        raise RuntimeError("telemetry unavailable")

    def fake_call(executor, retry_on_empty=True):
        observed["prompt"] = executor.system_prompt
        return "telemetry-fail-answer", []

    monkeypatch.setattr(runtime, "record_injection", fail_record)
    monkeypatch.setattr(AgentStreamExecutor, "_call_llm_stream", fake_call)
    monkeypatch.setitem(conf(), "skill_shadow_retrieval_enabled", False)
    monkeypatch.setitem(conf(), "skill_retrieval_injection_enabled", True)

    assert agent.run_stream("telemetry failure task") == "telemetry-fail-answer"
    assert "<name>builtin-safe</name>" in observed["prompt"]
    assert "<name>custom-safe</name>" in observed["prompt"]
    assert "<name>gov-a</name>" not in observed["prompt"]
    assert agent._last_skill_injection["status"] == "telemetry_failed"
    assert agent._last_skill_injection["telemetry_recorded"] is False


def test_real_manager_verifies_projection_model_and_persists_injection_evidence(
    governed_shadow, tmp_path
):
    service, _, runtime, suite_path = governed_shadow
    proposed = service.propose(
        _identity("production-proposer", "skill:propose"),
        _proposal(
            "production-check",
            "PRODUCTION_MATCH_ALPHA 财务逐项核对",
            "production-v1",
        ),
    )
    active = _evaluate_and_publish(
        service, suite_path, proposed, "production-v1"
    )
    builtin_dir = tmp_path / "builtin"
    builtin_skill = builtin_dir / "builtin-safe"
    builtin_skill.mkdir(parents=True)
    (builtin_skill / "SKILL.md").write_text(
        "---\nname: builtin-safe\ndescription: Builtin safe skill\n---\n",
        encoding="utf-8",
    )
    custom_skill = service.skills_dir / "custom-safe"
    custom_skill.mkdir()
    (custom_skill / "SKILL.md").write_text(
        "---\nname: custom-safe\ndescription: Custom safe skill\n---\n",
        encoding="utf-8",
    )
    manager = SkillManager(
        builtin_dir=str(builtin_dir),
        custom_dir=str(service.skills_dir),
        tenant_id=TENANT_ID,
    )
    reader = _identity("production-reader", "skill:read")
    task_secret = "PRODUCTION_MATCH_ALPHA PRIVATE_32A9"
    run = runtime.start_run(reader, task_secret, MODEL_ID, "production-real", top_k=5)
    try:
        verified, names = manager.verify_production_candidates(
            reader, run.candidates, MODEL_ID
        )
        assert names == (active.name,)
        assert len(verified) == 1
        assert set(manager.production_skill_filter(names)) == {
            "builtin-safe",
            "custom-safe",
            active.name,
        }

        incompatible = replace(verified[0], model_compatible=False)
        assert manager.verify_production_candidates(
            reader, (incompatible,), MODEL_ID
        ) == ((), ())

        runtime.record_injection(run, "injected", verified)
        runtime.finish_run(run, "completed", "PRIVATE_RESULT_18B4")
        evidence_bytes = runtime.export_evidence(run.run_id)
        evidence = json.loads(evidence_bytes.decode("utf-8"))
        assert task_secret.encode("utf-8") not in evidence_bytes
        assert b"PRIVATE_RESULT_18B4" not in evidence_bytes
        assert evidence["run"]["injection_requested"] == 1
        assert evidence["run"]["injection_status"] == "injected"
        assert evidence["run"]["injected_count"] == 1
        assert evidence["candidates"][0]["projection_verified"] == 1
        assert evidence["candidates"][0]["injected"] == 1

        source_agent = type("SourceAgent", (), {})()
        source_agent.skill_manager = type(
            "RuntimeManager",
            (),
            {"get_shadow_runtime": lambda self: runtime},
        )()
        source_agent._skill_shadow_completed_runs = [
            {"run_id": run.run_id, "message_start": 0, "message_end": 1}
        ]
        source = evolution_executor._shadow_source_batch(source_agent, 0, 1)
        assert source is not None
        source_document = json.loads(source[2].decode("utf-8"))
        assert source_document["schema_version"] == 2
        assert source_document["runs"][0]["schema_version"] == 2
        assert source_document["runs"][0]["run"]["injection_status"] == "injected"

        projection = service.skills_dir / active.name / "SKILL.md"
        projection.write_text(
            "---\nname: production-check\ndescription: TAMPERED\n---\n",
            encoding="utf-8",
        )
        manager.refresh_skills()
        assert manager.verify_production_candidates(
            reader, verified, MODEL_ID
        ) == ((), ())
    finally:
        manager.close()


def test_agent_llm_model_id_uses_current_configured_model(monkeypatch):
    from bridge.agent_bridge import AgentLLMModel
    from config import conf

    monkeypatch.setitem(conf(), "model", "configured-production-model@7")
    executor = object.__new__(AgentStreamExecutor)
    executor.model = AgentLLMModel(bridge=None)

    assert executor._skill_model_id() == "configured-production-model@7"


def test_shadow_retrieval_requires_skill_read_before_writing_telemetry(
    governed_shadow,
):
    _service, _repository, runtime, _suite_path = governed_shadow

    with pytest.raises(SkillAuthorizationError):
        runtime.start_run(
            _identity("unauthorized-reader"),
            "不得记录的未授权任务",
            MODEL_ID,
            "unauthorized-session",
        )

    with sqlite3.connect(runtime.telemetry_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM skill_shadow_runs"
        ).fetchone()[0]
    assert count == 0


def test_real_manager_explicit_filter_rechecks_identity_and_model(
    governed_shadow, tmp_path
):
    service, _repository, _runtime, suite_path = governed_shadow
    proposed = service.propose(
        _identity("explicit-proposer", "skill:propose"),
        _proposal(
            "explicit-governed",
            "EXPLICIT_GOVERNED_METADATA",
            "explicit-governed-v1",
        ),
    )
    active = _evaluate_and_publish(
        service, suite_path, proposed, "explicit-governed-v1"
    )
    builtin_dir = tmp_path / "explicit-builtin"
    builtin_skill = builtin_dir / "builtin-safe"
    builtin_skill.mkdir(parents=True)
    (builtin_skill / "SKILL.md").write_text(
        "---\nname: builtin-safe\ndescription: BUILTIN_SAFE_METADATA\n---\n",
        encoding="utf-8",
    )
    manager = SkillManager(
        builtin_dir=str(builtin_dir),
        custom_dir=str(service.skills_dir),
        tenant_id=TENANT_ID,
    )
    agent = Agent(
        "cached-system-prompt",
        model=_DummyModel(),
        tools=[],
        skill_manager=manager,
    )
    try:
        agent.identity_context = _identity("no-read-role")
        denied = agent.get_full_system_prompt(
            skill_filter=["builtin-safe", active.name]
        )
        assert "BUILTIN_SAFE_METADATA" in denied
        assert "EXPLICIT_GOVERNED_METADATA" not in denied

        agent.identity_context = _identity("compatible-reader", "skill:read")
        agent.model.model = "incompatible-model@1"
        incompatible = agent.get_full_system_prompt(
            skill_filter=[active.name]
        )
        assert "EXPLICIT_GOVERNED_METADATA" not in incompatible

        agent.model.model = MODEL_ID
        compatible = agent.get_full_system_prompt(
            skill_filter=[active.name]
        )
        assert "EXPLICIT_GOVERNED_METADATA" in compatible
    finally:
        manager.close()


def test_disabling_production_injection_clears_previous_round_state(
    monkeypatch,
):
    from config import conf

    runtime = _ProductionRuntime((_production_candidate(),))
    agent = _production_agent(_ProductionSkillManager(runtime))
    monkeypatch.setattr(
        AgentStreamExecutor,
        "_call_llm_stream",
        lambda executor, retry_on_empty=True: ("state-answer", []),
    )
    settings = conf()
    monkeypatch.setitem(settings, "skill_shadow_retrieval_enabled", False)
    monkeypatch.setitem(settings, "skill_retrieval_injection_enabled", True)

    assert agent.run_stream("first round") == "state-answer"
    assert agent._last_skill_injection["status"] == "injected"

    monkeypatch.setitem(settings, "skill_retrieval_injection_enabled", False)
    assert agent.run_stream("second round") == "state-answer"
    assert not hasattr(agent, "_last_skill_injection")


def test_prompt_build_failure_during_injection_fails_closed(monkeypatch):
    from config import conf

    runtime = _ProductionRuntime((_production_candidate(),))
    manager = _ProductionSkillManager(runtime)
    original_build = manager.build_skills_prompt
    build_calls = {"count": 0}

    def fail_after_initial_prompt(skill_filter=None):
        build_calls["count"] += 1
        if build_calls["count"] >= 2:
            raise RuntimeError("strict prompt rebuild failed")
        return original_build(skill_filter=skill_filter)

    manager.build_skills_prompt = fail_after_initial_prompt
    agent = _production_agent(manager)
    observed = {}

    def fake_call(executor, retry_on_empty=True):
        observed["prompt"] = executor.system_prompt
        return "prompt-failure-answer", []

    monkeypatch.setattr(AgentStreamExecutor, "_call_llm_stream", fake_call)
    settings = conf()
    monkeypatch.setitem(settings, "skill_shadow_retrieval_enabled", False)
    monkeypatch.setitem(settings, "skill_retrieval_injection_enabled", True)

    assert agent.run_stream("prompt failure task") == "prompt-failure-answer"
    assert "<name>gov-a</name>" not in observed["prompt"]
    assert agent._last_skill_injection["status"] == "retrieval_failed"
    assert agent._last_skill_injection["failure_class"] == "RuntimeError"


def test_cached_prompt_fallback_never_expands_governed_skill_visibility():
    class FailingManager:
        def __init__(self):
            self.read_allowed = False

        def set_identity_context(self, identity):
            self.identity = identity

        def refresh_skills(self):
            raise RuntimeError("governance refresh failed")

        def _governed_prompt_read_allowed(self):
            return self.read_allowed

    cached = (
        "## \U0001f9e9 技能系统（mandatory）\n\n"
        "<available_skills><name>GOVERNED_SECRET</name></available_skills>\n\n"
        "## 下一节\n\nSAFE_CONTEXT\n"
    )
    manager = FailingManager()
    agent = Agent(
        cached,
        model=_DummyModel(),
        tools=[],
        skill_manager=manager,
    )

    denied = agent.get_full_system_prompt()
    assert "GOVERNED_SECRET" not in denied
    assert "SAFE_CONTEXT" in denied

    manager.read_allowed = True
    explicit = agent.get_full_system_prompt(skill_filter=["requested-only"])
    assert "GOVERNED_SECRET" not in explicit
    assert "SAFE_CONTEXT" in explicit


def test_explicit_filter_build_is_serialized_with_governed_publication(
    governed_shadow, tmp_path
):
    service, _repository, _runtime, suite_path = governed_shadow
    first = service.propose(
        _identity("race-proposer-v1", "skill:propose"),
        _proposal(
            "explicit-race",
            "EXPLICIT_RACE_VERSION_ONE",
            "explicit-race-v1",
        ),
    )
    active_v1 = _evaluate_and_publish(
        service, suite_path, first, "explicit-race-v1"
    )
    second = service.propose(
        _identity("race-proposer-v2", "skill:propose"),
        _proposal(
            "explicit-race",
            "EXPLICIT_RACE_VERSION_TWO",
            "explicit-race-v2",
        ),
    )
    suite_sha256 = hashlib.sha256(suite_path.read_bytes()).hexdigest()
    evaluation = service.evaluate(
        _identity("race-validator-v2", "skill:validate"),
        SkillEvaluationCommand(
            skill_id=second.skill_id,
            version=second.version,
            suite_path=str(suite_path),
            suite_sha256=suite_sha256,
            model_id=MODEL_ID,
            idempotency_key="evaluate-explicit-race-v2",
        ),
    )
    builtin_dir = tmp_path / "race-builtin"
    builtin_dir.mkdir()
    manager = SkillManager(
        builtin_dir=str(builtin_dir),
        custom_dir=str(service.skills_dir),
        tenant_id=TENANT_ID,
        identity_context=_identity("race-reader", "skill:read"),
    )
    agent = Agent(
        "cached-system-prompt",
        model=_DummyModel(),
        tools=[],
        skill_manager=manager,
    )
    agent.identity_context = _identity("race-reader", "skill:read")
    verification_finished = threading.Event()
    publication_started = threading.Event()
    publication_finished = threading.Event()
    build_observed = threading.Event()
    publication_errors = []
    original_verify = manager.verify_explicit_skill_filter
    original_build = manager.build_skills_prompt
    first_verification = {"pending": True}

    def gated_verify(identity, requested_names, model_id):
        result = original_verify(identity, requested_names, model_id)
        if first_verification["pending"]:
            first_verification["pending"] = False
            verification_finished.set()
            assert publication_started.wait(2)
        return result

    def observed_build(skill_filter=None):
        assert not publication_finished.is_set()
        build_observed.set()
        return original_build(skill_filter=skill_filter)

    def publish_second_version():
        try:
            assert verification_finished.wait(2)
            publication_started.set()
            service.publish(
                _identity("race-publisher-v2", "skill:publish"),
                second.skill_id,
                second.version,
                evaluation.evaluation_id,
                "publish-explicit-race-v2",
            )
        except Exception as error:
            publication_errors.append(error)
        finally:
            publication_finished.set()

    manager.verify_explicit_skill_filter = gated_verify
    manager.build_skills_prompt = observed_build
    publisher = threading.Thread(target=publish_second_version)
    publisher.start()
    try:
        first_prompt = agent.get_full_system_prompt(
            skill_filter=[active_v1.name]
        )
        assert build_observed.is_set()
    finally:
        publisher.join(timeout=5)
        manager.close()

    assert not publisher.is_alive()
    assert publication_errors == []
    assert publication_finished.is_set()
    assert "EXPLICIT_RACE_VERSION_ONE" in first_prompt
    assert "EXPLICIT_RACE_VERSION_TWO" not in first_prompt

    manager = SkillManager(
        builtin_dir=str(builtin_dir),
        custom_dir=str(service.skills_dir),
        tenant_id=TENANT_ID,
        identity_context=_identity("race-reader-after", "skill:read"),
    )
    agent.skill_manager = manager
    agent.identity_context = _identity("race-reader-after", "skill:read")
    try:
        next_prompt = agent.get_full_system_prompt(
            skill_filter=[active_v1.name]
        )
        assert "EXPLICIT_RACE_VERSION_ONE" not in next_prompt
        assert "EXPLICIT_RACE_VERSION_TWO" in next_prompt
    finally:
        manager.close()


def test_skill_runtime_lock_order_is_governance_before_instance_lock():
    class TrackingContext:
        def __init__(self, name, events):
            self.name = name
            self.events = events

        def __enter__(self):
            self.events.append(self.name)
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

    manager_events = []
    manager = object.__new__(SkillManager)
    sentinel_runtime = object()
    manager._shadow_runtime = sentinel_runtime
    manager._runtime_lock = TrackingContext("runtime", manager_events)
    manager.production_injection_lock = lambda: TrackingContext(
        "governance", manager_events
    )

    assert manager.get_shadow_runtime() is sentinel_runtime
    assert manager_events[:2] == ["governance", "runtime"]

    class IndexStub:
        def replace_tenant(self, tenant_id, documents):
            return None

        def matches_tenant(self, tenant_id, documents):
            return True

    runtime_events = []
    runtime = object.__new__(ActiveSkillShadowRuntime)
    runtime._governance_lock = TrackingContext(
        "governance", runtime_events
    )
    runtime._lock = TrackingContext("runtime", runtime_events)
    runtime._closed = False
    runtime.tenant_id = TENANT_ID
    runtime.index = IndexStub()
    runtime._active_records = lambda: ()
    runtime._index_generation = ""

    runtime.rebuild_active_index()
    assert runtime_events[:2] == ["governance", "runtime"]


def test_first_shadow_runtime_and_full_prompt_complete_without_deadlock(
    governed_shadow, tmp_path
):
    """真实首次运行时初始化与完整提示词构建并发时必须完成。"""

    service, _repository, _runtime, suite_path = governed_shadow
    candidate = service.propose(
        _identity("deadlock-proposer", "skill:propose"),
        _proposal(
            "deadlock-check",
            "DEADLOCK_CHECK_ACTIVE_SKILL",
            "deadlock-check-v1",
        ),
    )
    active = _evaluate_and_publish(
        service, suite_path, candidate, "deadlock-check-v1"
    )
    builtin_dir = tmp_path / "deadlock-builtin"
    builtin_dir.mkdir()
    reader = _identity("deadlock-reader", "skill:read")
    manager = SkillManager(
        builtin_dir=str(builtin_dir),
        custom_dir=str(service.skills_dir),
        tenant_id=TENANT_ID,
        identity_context=reader,
    )
    agent = Agent(
        "cached-system-prompt",
        model=_DummyModel(),
        tools=[],
        skill_manager=manager,
    )
    agent.identity_context = reader
    start = threading.Barrier(2, timeout=5)
    runtime_future = Future()
    prompt_future = Future()

    def run_into_future(future, operation):
        if not future.set_running_or_notify_cancel():
            return
        try:
            start.wait()
            future.set_result(operation())
        except BaseException as error:
            future.set_exception(error)

    runtime_thread = threading.Thread(
        target=run_into_future,
        args=(runtime_future, manager.get_shadow_runtime),
        name="first-shadow-runtime",
        daemon=True,
    )
    prompt_thread = threading.Thread(
        target=run_into_future,
        args=(prompt_future, agent.get_full_system_prompt),
        name="full-system-prompt",
        daemon=True,
    )
    runtime_thread.start()
    prompt_thread.start()
    completed = False
    try:
        runtime = runtime_future.result(timeout=10)
        prompt = prompt_future.result(timeout=10)
        completed = True
        assert runtime is manager._shadow_runtime
        assert active.name in prompt
        assert "DEADLOCK_CHECK_ACTIVE_SKILL" in prompt
    finally:
        if completed:
            manager.close()

    runtime_thread.join(timeout=1)
    prompt_thread.join(timeout=1)
    assert not runtime_thread.is_alive()
    assert not prompt_thread.is_alive()
