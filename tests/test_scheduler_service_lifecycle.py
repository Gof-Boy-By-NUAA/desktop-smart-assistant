from __future__ import annotations

import threading

from agent.tools.scheduler.scheduler_service import SchedulerService


class _BlockingScheduler(SchedulerService):
    def __init__(self):
        super().__init__(task_store=object(), execute_callback=lambda _: True)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.ticks = 0

    def _check_and_execute_tasks(self):
        self.ticks += 1
        self.entered.set()
        self.release.wait(timeout=10)


def test_stop_timeout_refuses_overlapping_scheduler_restart():
    service = _BlockingScheduler()
    assert service.start() is True
    assert service.entered.wait(timeout=2)

    assert service.stop(timeout=0.01) is False
    first_thread = service.thread
    assert first_thread is not None and first_thread.is_alive()
    assert service.start() is False
    assert service.thread is first_thread

    service.release.set()
    first_thread.join(timeout=2)
    assert not first_thread.is_alive()
    assert service.start() is True
    assert service.stop(timeout=2) is True
