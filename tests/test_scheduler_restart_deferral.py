"""Scheduler restart-deferral regression: "not yet started" must not become in_doubt.

评审缺陷 P1-2：重启后、目标会话尚无入站消息时，定时任务到期。
integration 的就绪探测在**任何副作用发生之前**返回 False，而
scheduler_service 把 False 记为 in_doubt，唯一活跃索引随即封锁同一
任务，导致该任务永久停止自动执行，需要人工介入。

修复语义（本文件验证）：
  - 回调以 TaskDeferredBeforeStart 声明"未开始"→ 服务必须释放持久
    租约、不推进排程、不写 in_doubt，下一 tick 重新认领并执行；
  - 回调返回 False 仍表示"外部副作用结果不确定"→ 保持 in_doubt
    并拒绝重复执行（fail-closed 语义不回归）。

全部使用真实 TaskStore（真实 tasks.json + SQLite 账本）与真实
SchedulerService，不使用任何测试替身。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from agent.tools.scheduler.scheduler_service import (
    SchedulerService,
    TaskDeferredBeforeStart,
)
from agent.tools.scheduler.task_store import TaskStore

OWNER = "web:" + "b" * 32


def _due_task(task_id: str = "task-defer") -> dict:
    now = datetime.now()
    return {
        "id": task_id,
        "name": "restart deferral task",
        "enabled": True,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "next_run_at": (now - timedelta(seconds=5)).isoformat(),
        "schedule": {"type": "interval", "seconds": 3600},
        "action": {"type": "send_message", "channel_type": "web"},
        "creator_owner_id": OWNER,
    }


def test_not_ready_defers_then_recovers_when_channel_ready(tmp_path):
    store = TaskStore(str(tmp_path / "scheduler" / "tasks.json"))
    task = _due_task()
    store.add_task(task)

    state = {"ready": False}
    calls = {"count": 0}

    def callback(scheduled_task):
        calls["count"] += 1
        if not state["ready"]:
            # 与修复后的 integration.execute_task_callback 一致：就绪探测
            # 失败时声明"未开始"，而不是返回 False。
            raise TaskDeferredBeforeStart(
                "channel not ready (restart before any inbound message)"
            )
        return True

    service = SchedulerService(store, callback)
    original_next_run = task["next_run_at"]

    # 第一次扫描：未就绪 → 只应延期（释放租约、不推进、不 in_doubt）。
    service._check_and_execute_tasks()
    after_defer = store.get_task("task-defer")
    assert calls["count"] == 1
    assert after_defer["next_run_at"] == original_next_run, "延期不得推进排程"
    assert (
        after_defer.get("last_execution_status") != "in_doubt"
    ), "未开始的任务不得进入 in_doubt"

    # 用户发来消息，会话队列就绪 → 下一次扫描必须真正执行。
    state["ready"] = True
    service._check_and_execute_tasks()
    after_success = store.get_task("task-defer")

    assert calls["count"] == 2, "就绪后同一任务必须恢复自动执行"
    assert after_success["next_run_at"] != original_next_run, "成功后必须推进排程"
    assert after_success.get("last_execution_status") == "succeeded"


def test_uncertain_failure_stays_in_doubt_and_blocks_retry(tmp_path):
    """fail-closed 回归：真正的"结果不确定"必须维持人工处理状态。"""

    store = TaskStore(str(tmp_path / "scheduler" / "tasks.json"))
    store.add_task(_due_task())

    state = {"fail": True}
    calls = {"count": 0}

    def callback(scheduled_task):
        calls["count"] += 1
        if state["fail"]:
            return False  # 外部副作用结果未知（与"未开始"不同）
        return True

    service = SchedulerService(store, callback)
    service._check_and_execute_tasks()

    after_failure = store.get_task("task-defer")
    assert after_failure.get("last_execution_status") == "in_doubt"

    state["fail"] = False
    service._check_and_execute_tasks()
    assert calls["count"] == 1, "in_doubt 期间必须拒绝重复执行"


def test_released_claim_lets_next_occurrence_run(tmp_path):
    """释放的租约不得残留在唯一活跃索引中（真实 SQLite 断言）。"""

    store = TaskStore(str(tmp_path / "scheduler" / "tasks.json"))
    task = _due_task()
    store.add_task(task)

    service = SchedulerService(store, lambda scheduled_task: (_ for _ in ()).throw(
        TaskDeferredBeforeStart("probe failed")
    ))
    service._check_and_execute_tasks()

    # 直接对真实账本断言：延期后不存在 running/in_doubt 活跃行，
    # 同一 occurrence 可被重新认领。
    import sqlite3

    connection = sqlite3.connect(store._execution_db_path)
    try:
        live = connection.execute(
            "SELECT COUNT(*) FROM scheduler_executions "
            "WHERE task_id = ? AND status IN ('running', 'in_doubt')",
            ("task-defer",),
        ).fetchone()[0]
    finally:
        connection.close()
    assert live == 0, "延期释放后不得残留活跃租约行"

    re_claim = store.claim_scheduled_execution(
        "task-defer", task["next_run_at"], "runner-after-recovery"
    )
    assert re_claim["status"] == "claimed"
    store.finish_execution(
        "task-defer",
        re_claim["execution_id"],
        re_claim["lease_token"],
        succeeded=True,
    )
