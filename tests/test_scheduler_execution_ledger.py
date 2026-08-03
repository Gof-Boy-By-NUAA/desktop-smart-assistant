"""Crash and concurrency regressions for durable scheduler execution claims."""

from __future__ import annotations

import multiprocessing
import threading
import time
from datetime import datetime, timedelta

from agent.tools.scheduler.scheduler_service import SchedulerService
from agent.tools.scheduler.task_store import TaskStore, TaskExecutionStoreError


OWNER = "web:" + "a" * 32


def _due_task(task_id: str = "task-1") -> dict:
    now = datetime.now()
    return {
        "id": task_id,
        "name": "durable scheduled task",
        "enabled": True,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "next_run_at": (now - timedelta(seconds=1)).isoformat(),
        "schedule": {"type": "interval", "seconds": 3600},
        "action": {"type": "send_message", "channel_type": "web"},
        "creator_owner_id": OWNER,
    }


def _claim_due_in_child(store_path: str, scheduled_for: str, start, results) -> None:
    try:
        store = TaskStore(store_path)
        start.wait(timeout=10)
        claim = store.claim_scheduled_execution(
            "task-1", scheduled_for, f"child-{multiprocessing.current_process().pid}"
        )
        results.put(claim["status"])
    except Exception as exc:  # pragma: no cover - asserted by parent
        results.put(f"error:{type(exc).__name__}:{exc}")


def test_two_processes_can_claim_one_scheduled_occurrence_only(tmp_path):
    store_path = str(tmp_path / "scheduler" / "tasks.json")
    store = TaskStore(store_path)
    task = _due_task()
    store.add_task(task)

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_claim_due_in_child,
        args=(store_path, task["next_run_at"], start, results),
    )
    second = context.Process(
        target=_claim_due_in_child,
        args=(store_path, task["next_run_at"], start, results),
    )
    first.start()
    second.start()
    start.set()
    first.join(timeout=20)
    second.join(timeout=20)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert sorted(results.get(timeout=5) for _ in range(2)) == ["claimed", "running"]


def test_running_claim_after_crash_blocks_automatic_rerun_and_is_visible(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.json"))
    task = _due_task()
    store.add_task(task)
    claim = store.claim_scheduled_execution(
        "task-1", task["next_run_at"], "test-runner-crash"
    )
    assert claim["status"] == "claimed"

    calls = []
    SchedulerService(store, lambda received: calls.append(received) or True)._check_and_execute_tasks()

    assert calls == []
    visible = store.get_task("task-1", owner_id=OWNER)
    assert visible["last_execution_status"] == "running"
    assert visible["last_execution_id"] == claim["execution_id"]


def test_success_before_schedule_update_reconciles_without_repeating_side_effect(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.json"))
    task = _due_task()
    store.add_task(task)
    claim = store.claim_scheduled_execution(
        "task-1", task["next_run_at"], "test-runner-success"
    )
    store.finish_execution(
        "task-1",
        claim["execution_id"],
        claim["lease_token"],
        succeeded=True,
    )

    calls = []
    service = SchedulerService(store, lambda received: calls.append(received) or True)
    service._check_and_execute_tasks()

    assert calls == []
    reconciled = store.get_task("task-1", owner_id=OWNER)
    assert reconciled["next_run_at"] != task["next_run_at"]
    assert reconciled["last_execution_status"] == "succeeded"
    assert reconciled["last_execution_id"] == claim["execution_id"]


def test_callback_failure_becomes_in_doubt_and_never_retries_automatically(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.json"))
    task = _due_task()
    store.add_task(task)
    calls = []

    def uncertain_callback(received):
        calls.append(received["id"])
        return False

    service = SchedulerService(store, uncertain_callback)
    service._check_and_execute_tasks()
    service._check_and_execute_tasks()

    assert calls == ["task-1"]
    visible = store.get_task("task-1", owner_id=OWNER)
    assert visible["next_run_at"] == task["next_run_at"]
    assert visible["last_execution_status"] == "in_doubt"
    assert "unconfirmed" in visible["last_execution_detail"]
    blocked = store.claim_scheduled_execution(
        "task-1", task["next_run_at"], "test-runner-retry"
    )
    assert blocked["status"] == "in_doubt"


def test_manual_idempotency_key_replays_status_without_starting_duplicate(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.json"))
    task = _due_task()
    task["enabled"] = False
    store.add_task(task)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def callback(received):
        calls.append(received["id"])
        entered.set()
        release.wait(timeout=5)
        return True

    service = SchedulerService(store, callback)
    first = service.run_task_now(
        "task-1", owner_id=OWNER, idempotency_key="manual-request-0001"
    )
    assert first["status"] == "queued"
    assert entered.wait(timeout=5)
    duplicate = service.run_task_now(
        "task-1", owner_id=OWNER, idempotency_key="manual-request-0001"
    )
    assert duplicate == {
        "status": "already_queued",
        "execution_id": first["execution_id"],
    }
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        state = store.get_task("task-1", owner_id=OWNER)
        if state and state.get("last_execution_status") == "succeeded":
            break
        time.sleep(0.01)

    assert calls == ["task-1"]
    completed = service.run_task_now(
        "task-1", owner_id=OWNER, idempotency_key="manual-request-0001"
    )
    assert completed == {
        "status": "already_completed",
        "execution_id": first["execution_id"],
    }
    assert calls == ["task-1"]


def test_interrupted_manual_worker_is_not_reported_as_still_queued(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.json"))
    task = _due_task()
    task["enabled"] = False
    store.add_task(task)
    entered = threading.Event()

    def interrupted_callback(_received):
        entered.set()
        raise SystemExit("simulated worker interruption")

    service = SchedulerService(store, interrupted_callback)
    first = service.run_task_now(
        "task-1", owner_id=OWNER, idempotency_key="manual-interrupted-0001"
    )
    assert first["status"] == "queued"
    assert entered.wait(timeout=5)
    deadline = time.time() + 5
    while time.time() < deadline and service._is_execution_active(first["execution_id"]):
        time.sleep(0.01)

    try:
        service.run_task_now(
            "task-1", owner_id=OWNER, idempotency_key="manual-interrupted-0001"
        )
    except RuntimeError as exc:
        assert "uncertain execution" in str(exc)
    else:
        raise AssertionError("interrupted worker was falsely reported as queued")

    visible = store.get_task("task-1", owner_id=OWNER)
    assert visible["last_execution_status"] == "running"


def test_malformed_manual_idempotency_keys_have_zero_execution_side_effects(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.json"))
    task = _due_task()
    task["enabled"] = False
    store.add_task(task)

    for key in ("", "short", "x" * 129, "valid-key\n", "bad key!", "\x7f" * 8):
        try:
            store.claim_manual_execution(
                "task-1",
                owner_id=OWNER,
                idempotency_key=key,
                runner_id="input-fuzz-runner",
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid key {key!r} claimed an execution")

    assert store.get_task("task-1", owner_id=OWNER).get("last_execution_status") is None
    valid = store.claim_manual_execution(
        "task-1",
        owner_id=OWNER,
        idempotency_key="valid-key-0001",
        runner_id="input-fuzz-runner",
    )
    assert valid["status"] == "claimed"


def test_foreign_owner_cannot_create_or_observe_execution_claim(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.json"))
    store.add_task(_due_task())

    try:
        store.claim_manual_execution(
            "task-1",
            owner_id="web:" + "b" * 32,
            idempotency_key="foreign-key-0001",
            runner_id="foreign-runner",
        )
    except ValueError as exc:
        assert str(exc) == "Task not found"
    else:
        raise AssertionError("foreign owner claimed another owner's task")

    assert store.get_task("task-1", owner_id=OWNER).get("last_execution_status") is None


def test_restart_never_relabels_persisted_running_manual_claim_as_queued(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.json"))
    task = _due_task()
    task["enabled"] = False
    store.add_task(task)
    previous = store.claim_manual_execution(
        "task-1",
        owner_id=OWNER,
        idempotency_key="restart-key-0001",
        runner_id="retired-runner",
    )
    assert previous["status"] == "claimed"
    calls = []
    restarted = SchedulerService(store, lambda item: calls.append(item) or True)

    try:
        restarted.run_task_now(
            "task-1",
            owner_id=OWNER,
            idempotency_key="restart-key-0001",
        )
    except RuntimeError as exc:
        assert "uncertain execution" in str(exc)
    else:
        raise AssertionError("restart falsely reported a dead worker as queued")

    assert calls == []
    assert store.get_task("task-1", owner_id=OWNER)["last_execution_status"] == "running"


def test_recreated_task_id_cannot_inherit_a_prior_occurrence_success(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.json"))
    first = _due_task()
    store.add_task(first)
    original = store.claim_scheduled_execution(
        "task-1", first["next_run_at"], "first-generation-runner"
    )
    store.finish_execution(
        "task-1",
        original["execution_id"],
        original["lease_token"],
        succeeded=True,
    )
    store.delete_task("task-1", owner_id=OWNER)

    recreated = dict(first)
    recreated["name"] = "replacement task"
    store.add_task(recreated)
    replacement = store.claim_scheduled_execution(
        "task-1", recreated["next_run_at"], "replacement-generation-runner"
    )

    assert replacement["status"] == "claimed"
    assert replacement["execution_id"] != original["execution_id"]
    assert "_execution_generation" not in store.get_task("task-1", owner_id=OWNER)


def test_stale_lease_cannot_mark_someone_elses_execution_complete(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.json"))
    task = _due_task()
    store.add_task(task)
    claim = store.claim_scheduled_execution(
        "task-1", task["next_run_at"], "test-runner-lease"
    )

    try:
        store.finish_execution(
            "task-1",
            claim["execution_id"],
            "wrong-lease",
            succeeded=True,
        )
    except TaskExecutionStoreError:
        pass
    else:
        raise AssertionError("stale lease completion returned success")

    assert (
        store.claim_scheduled_execution(
            "task-1", task["next_run_at"], "test-runner-lease-retry"
        )["status"]
        == "running"
    )
