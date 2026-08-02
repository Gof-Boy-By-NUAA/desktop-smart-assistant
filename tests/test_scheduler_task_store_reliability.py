from __future__ import annotations

import json
import multiprocessing
import threading
from datetime import datetime

import pytest

from agent.tools.scheduler.task_store import TaskStore, TaskStoreCorruptionError


def _task(task_id: str, name: str) -> dict:
    now = datetime.now().isoformat()
    return {
        "id": task_id,
        "name": name,
        "enabled": True,
        "created_at": now,
        "updated_at": now,
        "schedule": {"type": "interval", "seconds": 60},
        "action": {"type": "send_message", "channel_type": "web"},
        "creator_owner_id": "web:" + "a" * 32,
    }


def _add_task_in_child(store_path: str, task_id: str, start, results) -> None:
    try:
        start.wait(timeout=10)
        TaskStore(store_path).add_task(_task(task_id, task_id))
        results.put((task_id, "ok"))
    except Exception as exc:  # pragma: no cover - asserted by parent
        results.put((task_id, f"error:{exc}"))


def test_distinct_task_store_instances_preserve_concurrent_adds(tmp_path):
    path = tmp_path / "scheduler" / "tasks.json"
    first = TaskStore(str(path))
    second = TaskStore(str(path))
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def add(store: TaskStore, task: dict) -> None:
        try:
            barrier.wait(timeout=5)
            store.add_task(task)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    one = threading.Thread(target=add, args=(first, _task("first", "first")))
    two = threading.Thread(target=add, args=(second, _task("second", "second")))
    one.start()
    two.start()
    one.join(timeout=10)
    two.join(timeout=10)

    assert not one.is_alive()
    assert not two.is_alive()
    assert errors == []
    assert set(first.load_tasks()) == {"first", "second"}


def test_separate_processes_preserve_concurrent_adds(tmp_path):
    path = str(tmp_path / "scheduler" / "tasks.json")
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_add_task_in_child, args=(path, "first", start, results)
    )
    second = context.Process(
        target=_add_task_in_child, args=(path, "second", start, results)
    )
    first.start()
    second.start()
    start.set()
    first.join(timeout=15)
    second.join(timeout=15)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert sorted(results.get(timeout=5) for _ in range(2)) == [
        ("first", "ok"),
        ("second", "ok"),
    ]
    assert set(TaskStore(path).load_tasks()) == {"first", "second"}


def test_corrupt_primary_fails_closed_and_does_not_replace_tasks(tmp_path):
    path = tmp_path / "scheduler" / "tasks.json"
    store = TaskStore(str(path))
    store.add_task(_task("known", "known task"))
    before = path.read_bytes()
    path.write_text('{"tasks": ', encoding="utf-8")

    with pytest.raises(TaskStoreCorruptionError):
        store.add_task(_task("new", "must not be written"))
    with pytest.raises(TaskStoreCorruptionError):
        store.list_tasks()

    assert path.read_text(encoding="utf-8") == '{"tasks": '
    assert json.loads(before.decode("utf-8"))["tasks"]["known"]["name"] == "known task"


def test_failed_atomic_replace_keeps_the_previous_primary_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "scheduler" / "tasks.json"
    store = TaskStore(str(path))
    store.add_task(_task("known", "known task"))
    before = path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("agent.tools.scheduler.task_store.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        store.update_task("known", {"name": "new name"})

    assert path.read_bytes() == before
    assert json.loads(before.decode("utf-8"))["tasks"]["known"]["name"] == "known task"
