"""后台进化接入技能候选治理的回归测试。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from agent.evolution import executor
from agent.memory.config import MemoryConfig
from agent.memory.governance import IdentityContext
from agent.skills import SkillManager
from agent.skills.governance import GovernedSkillRepository, GovernedSkillService


class _Model:
    model = "test-model-v1"


class _MainAgent:
    def __init__(self):
        self.messages = [
            {"role": "user", "content": "以后每次检查报表都先核对单位和正负号。"},
            {"role": "assistant", "content": "明白，这是一套可重复的校验流程。"},
        ]
        self.messages_lock = threading.Lock()
        self.tools = []
        self.model = _Model()
        self.skill_manager = _EvidenceSkillManager()
        self.memory_manager = None
        self.runtime_info = {"_get_model": lambda: "test-model-v1"}
        self._skill_shadow_completed_runs = [
            {"run_id": "shadow-run-1", "message_start": 0, "message_end": 2}
        ]


class _EvidenceRuntime:
    def export_evidence(self, run_id):
        return json.dumps(
            {
                "schema_version": 2,
                "run": {
                    "run_id": run_id,
                    "tenant_id": "tenant-local",
                    "task_hmac": "a" * 64,
                    "task_char_count": 24,
                    "task_byte_count": 72,
                    "actor_hmac": "b" * 64,
                    "session_hmac": "c" * 64,
                    "model_id": "test-model-v1",
                    "retriever_version": "shadow-v1",
                    "index_generation": "d" * 64,
                    "top_k": 5,
                    "retrieval_latency_ms": 1.5,
                    "status": "completed",
                    "tool_count": 0,
                    "injection_requested": 1,
                    "injection_status": "no_match",
                    "injected_count": 0,
                    "final_type": "str",
                    "final_char_count": 12,
                    "final_hmac": "e" * 64,
                },
                "candidates": [],
                "tools": [],
            },
            ensure_ascii=False,
        ).encode("utf-8")


class _EvidenceSkillManager:
    def __init__(self):
        self.runtime = _EvidenceRuntime()

    def get_shadow_runtime(self):
        return self.runtime


class _ReviewAgent:
    def __init__(self, tools):
        self.tools = tools
        self.model = None

    def run_stream(self, user_message, clear_history=False):
        tool = next(tool for tool in self.tools if tool.name == "skill_propose")
        result = tool.execute(
            {
                "name": "report-input-validation",
                "description": "在报表计算前核对输入数据。",
                "applicability": ["从报表抽取数值后准备计算"],
                "steps": ["核对原始行列标签", "确认单位和正负号", "再执行计算"],
                "validation_rules": ["关键输入必须逐项回看原文"],
                "contraindications": ["没有可核验原始数据时不得使用"],
            }
        )
        assert result.status == "success"
        return "[SILENT]"


class _Bridge:
    def __init__(self, agent):
        self.agents = {"session-1": agent}
        self.default_agent = agent
        self.injected = []

    def create_agent(self, **kwargs):
        return _ReviewAgent(kwargs["tools"])

    def remember_scheduled_output(
        self, session_id, content, channel_type="", task_description=""
    ):
        self.injected.append(content)


def _read_candidates(workspace: Path):
    repository = GovernedSkillRepository(
        workspace / "skills" / ".system" / "governed-skills.db"
    )
    service = GovernedSkillService(
        repository, workspace / "skills", "tenant-local"
    )
    identity = IdentityContext(
        tenant_id="tenant-local",
        actor_user_id="reader",
        roles=frozenset({"skill:read"}),
        trace_id="trace-read",
        auth_source="test",
    )
    try:
        return service.list_candidates(identity)
    finally:
        repository.close()


def test_evolution_submits_inactive_candidate_without_skill_projection(
    tmp_path, monkeypatch
):
    memory_config = MemoryConfig(workspace_root=str(tmp_path))
    monkeypatch.setattr(
        "agent.memory.config.get_default_memory_config", lambda: memory_config
    )
    monkeypatch.setattr(
        executor,
        "get_evolution_config",
        lambda: SimpleNamespace(enabled=True, max_steps=4),
    )
    monkeypatch.setattr(executor, "_builtin_skill_names", lambda: {"protected"})

    agent = _MainAgent()
    bridge = _Bridge(agent)
    changed = executor.run_evolution_for_session(bridge, "session-1")

    assert changed is True
    projection = tmp_path / "skills" / "report-input-validation" / "SKILL.md"
    assert not projection.exists()
    candidates = _read_candidates(tmp_path)
    assert len(candidates) == 1
    assert candidates[0].name == "report-input-validation"
    assert candidates[0].model_compatibility == ("test-model-v1",)
    assert candidates[0].provenance[0]["source_type"] == "skill-shadow-evidence"
    assert candidates[0].provenance[0]["source_ref"].startswith(
        "skill-shadow://evidence/sha256/"
    )
    assert "session-1" not in candidates[0].provenance[0]["source_ref"]
    assert len(bridge.injected) == 1
    summary = bridge.injected[0].lower()
    assert "候选" in summary or "candidate" in summary
    assert "尚未发布" in summary or "not published" in summary
    assert "backup_id" not in summary

    empty_builtin = tmp_path / "builtin"
    empty_builtin.mkdir()
    manager = SkillManager(
        builtin_dir=str(empty_builtin), custom_dir=str(tmp_path / "skills")
    )
    assert "report-input-validation" not in manager.skills
    assert "report-input-validation" not in manager.build_skills_prompt()


def test_skill_candidate_database_creation_is_not_an_evolution_change(
    tmp_path, monkeypatch
):
    memory_config = MemoryConfig(workspace_root=str(tmp_path))
    monkeypatch.setattr(
        "agent.memory.config.get_default_memory_config", lambda: memory_config
    )
    monkeypatch.setattr(
        executor,
        "get_evolution_config",
        lambda: SimpleNamespace(enabled=True, max_steps=2),
    )
    monkeypatch.setattr(executor, "_builtin_skill_names", lambda: set())

    class SilentReview(_ReviewAgent):
        def run_stream(self, user_message, clear_history=False):
            return "[SILENT]"

    class SilentBridge(_Bridge):
        def create_agent(self, **kwargs):
            return SilentReview(kwargs["tools"])

    bridge = SilentBridge(_MainAgent())
    changed = executor.run_evolution_for_session(bridge, "session-1")

    assert changed is False
    assert bridge.injected == []
    assert _read_candidates(tmp_path) == ()


def test_evolution_without_completed_shadow_evidence_hides_skill_propose(
    tmp_path, monkeypatch
):
    memory_config = MemoryConfig(workspace_root=str(tmp_path))
    monkeypatch.setattr(
        "agent.memory.config.get_default_memory_config", lambda: memory_config
    )
    monkeypatch.setattr(
        executor,
        "get_evolution_config",
        lambda: SimpleNamespace(enabled=True, max_steps=2),
    )
    monkeypatch.setattr(executor, "_builtin_skill_names", lambda: set())

    observed = {}

    class NoEvidenceReview(_ReviewAgent):
        def run_stream(self, user_message, clear_history=False):
            observed["tool_names"] = [tool.name for tool in self.tools]
            return "[SILENT]"

    class NoEvidenceBridge(_Bridge):
        def create_agent(self, **kwargs):
            return NoEvidenceReview(kwargs["tools"])

    agent = _MainAgent()
    agent._skill_shadow_completed_runs = []
    changed = executor.run_evolution_for_session(
        NoEvidenceBridge(agent), "session-1"
    )

    assert changed is False
    assert "skill_propose" not in observed["tool_names"]
    assert _read_candidates(tmp_path) == ()


def test_shadow_evidence_batch_only_contains_runs_for_new_message_range():
    agent = _MainAgent()
    agent._skill_shadow_completed_runs = [
        {"run_id": "old-run", "message_start": 0, "message_end": 2},
        {"run_id": "new-run-one", "message_start": 2, "message_end": 4},
        {"run_id": "new-run-two", "message_start": 4, "message_end": 6},
        {"run_id": "running-outside", "message_start": 6, "message_end": 8},
    ]
    agent._evo_reviewed_shadow_run_ids = ("old-run",)

    source = executor._shadow_source_batch(agent, 2, 6)

    assert source is not None
    source_type, source_ref, payload, run_ids = source
    document = json.loads(payload.decode("utf-8"))
    assert source_type == "skill-shadow-evidence"
    assert source_ref.startswith("skill-shadow://evidence/sha256/")
    assert run_ids == ("new-run-one", "new-run-two")
    assert [item["run"]["run_id"] for item in document["runs"]] == [
        "new-run-one",
        "new-run-two",
    ]
    assert document["schema_version"] == 2
    assert all(item["schema_version"] == 2 for item in document["runs"])


def test_evolution_prompt_requires_governed_candidate_not_direct_skill_edit():
    prompt = executor.EVOLUTION_SYSTEM_PROMPT
    assert "call `skill_propose`" in prompt
    assert "Never use write/edit on any path under `skills/`" in prompt
    assert "create `skills/<name>/SKILL.md`" not in prompt
    assert "correct action is to EDIT that skill" not in prompt
