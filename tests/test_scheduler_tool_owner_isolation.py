from __future__ import annotations

from datetime import datetime, timedelta

from agent.tools.scheduler.scheduler_tool import SchedulerTool
from agent.tools.scheduler.task_store import TaskStore


OWNER_A = "web:" + "a" * 32
OWNER_B = "web:" + "b" * 32


class _Context(dict):
    kwargs = {}


def _task(task_id: str, owner_id: str, name: str) -> dict:
    now = datetime.now()
    return {
        "id": task_id,
        "name": name,
        "enabled": True,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "next_run_at": (now + timedelta(hours=1)).isoformat(),
        "schedule": {"type": "interval", "seconds": 3600},
        "action": {"type": "send_message", "channel_type": "web"},
        "creator_owner_id": owner_id,
    }


def _tool(store: TaskStore, owner_id: str | None) -> SchedulerTool:
    tool = SchedulerTool({"channel_type": "web"})
    tool.task_store = store
    context = _Context(receiver="receiver", session_id="session")
    if owner_id is not None:
        context["session_owner_id"] = owner_id
    tool.current_context = context
    return tool


def test_web_scheduler_tool_cannot_list_read_or_mutate_foreign_tasks(tmp_path):
    store = TaskStore(str(tmp_path / "scheduler" / "tasks.json"))
    store.add_task(_task("owner-a", OWNER_A, "A private task"))
    store.add_task(_task("owner-b", OWNER_B, "B private task"))
    tool = _tool(store, OWNER_A)

    listed = tool.execute({"action": "list"})
    assert listed.status == "success"
    assert "A private task" in listed.result
    assert "B private task" not in listed.result

    before = store.get_task("owner-b", owner_id=OWNER_B).copy()
    for action in ("get", "delete", "enable", "disable"):
        result = tool.execute({"action": action, "task_id": "owner-b"})
        assert result.status == "error", action
        assert "不存在" in result.result
        assert store.get_task("owner-b", owner_id=OWNER_B) == before


def test_web_scheduler_tool_requires_a_trusted_owner_and_does_not_false_succeed(tmp_path):
    store = TaskStore(str(tmp_path / "scheduler" / "tasks.json"))
    tool = _tool(store, None)

    missing_owner = tool.execute({"action": "list"})
    assert missing_owner.status == "error"
    assert "可信所有者" in missing_owner.result

    invalid_create = _tool(store, OWNER_A).execute({"action": "create"})
    assert invalid_create.status == "error"
    assert "缺少任务名称" in invalid_create.result
    assert store.list_tasks(owner_id=OWNER_A) == []
