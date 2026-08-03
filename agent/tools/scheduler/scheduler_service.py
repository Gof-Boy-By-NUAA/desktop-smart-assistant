"""
Background scheduler service for executing scheduled tasks
"""

import time
import threading
import uuid
from datetime import datetime, timedelta
from typing import Callable, Optional
from croniter import croniter
from common.log import logger


def _parse_naive_local(iso_str: str) -> datetime:
    """Parse an ISO datetime and coerce it to tz-naive local time.

    The scheduler uses ``datetime.now()`` (tz-naive) for all comparisons,
    so any persisted timestamp must be normalized to the same flavor —
    otherwise comparing naive vs aware raises TypeError.
    """
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


class SchedulerService:
    """
    Background service that executes scheduled tasks
    """
    
    def __init__(self, task_store, execute_callback: Callable):
        """
        Initialize scheduler service
        
        Args:
            task_store: TaskStore instance
            execute_callback: Function to call when executing a task
        """
        self.task_store = task_store
        self.execute_callback = execute_callback
        self.running = False
        self.thread = None
        self._lock = threading.Lock()
        self._stop_event = None
        self._generation = 0
        self._execution_lock = threading.Lock()
        self._active_task_ids = set()
        # This value is intentionally process/service-local. A durable
        # "running" ledger row from another process (or before restart) is
        # uncertain, not proof that this instance is still executing it.
        self._runner_id = uuid.uuid4().hex
        self._active_execution_ids = set()
    
    def start(self):
        """Start the scheduler service"""
        with self._lock:
            if self.thread is not None and self.thread.is_alive():
                logger.warning("[Scheduler] Service already running or still stopping")
                return False
            
            self._generation += 1
            generation = self._generation
            stop_event = threading.Event()
            self.running = True
            self._stop_event = stop_event
            self.thread = threading.Thread(
                target=self._run_loop,
                args=(generation, stop_event),
                daemon=True,
                name=f"scheduler-loop-{generation}",
            )
            self.thread.start()
            return True
    
    def stop(self, timeout: float = 5.0) -> bool:
        """Stop the scheduler service without allowing an overlapping restart.

        A timeout is a failed stop, not permission to start another loop. The
        caller can retry only after the existing worker has actually exited.
        """
        with self._lock:
            thread = self.thread
            stop_event = self._stop_event
            if thread is None:
                self.running = False
                return True
            
            self.running = False
            if stop_event is not None:
                stop_event.set()
        thread.join(timeout=timeout)
        stopped = not thread.is_alive()
        with self._lock:
            if stopped and self.thread is thread:
                self.thread = None
                self._stop_event = None
            elif not stopped:
                logger.error(
                    "[Scheduler] Stop timed out; refusing a new scheduler loop "
                    "until the existing worker exits"
                )
        if stopped:
            logger.info("[Scheduler] Service stopped")
        return stopped
    
    def _run_loop(
        self,
        generation: Optional[int] = None,
        stop_event: Optional[threading.Event] = None,
    ):
        """Main scheduler loop"""
        logger.info("[Scheduler] Scheduler loop started")
        if generation is None:
            generation = self._generation
        if stop_event is None:
            stop_event = self._stop_event or threading.Event()
        try:
            while not stop_event.is_set():
                try:
                    self._check_and_execute_tasks()
                except Exception as e:
                    logger.error(f"[Scheduler] Error in scheduler loop: {e}")
                # Waiting on the event makes normal shutdown immediate instead
                # of leaving a 30-second sleep that races a subsequent start.
                stop_event.wait(30)
        finally:
            with self._lock:
                if generation == self._generation:
                    self.running = False
                    if self.thread is threading.current_thread():
                        self.thread = None
                        self._stop_event = None
    
    def _check_and_execute_tasks(self):
        """Claim due occurrences durably before executing external side effects."""
        now = datetime.now()
        tasks = self.task_store.list_tasks(enabled_only=True)
        
        for task in tasks:
            try:
                if self._is_task_due(task, now):
                    logger.info(f"[Scheduler] Executing task: {task['id']} - {task['name']}")
                    scheduled_for = task.get("next_run_at")
                    if not scheduled_for:
                        # _is_task_due normally initializes this before it
                        # returns True. Do not invent an idempotency key if a
                        # concurrent task edit has invalidated that invariant.
                        logger.error(
                            f"[Scheduler] Task {task['id']} is due without a persisted occurrence"
                        )
                        continue
                    claim = self.task_store.claim_scheduled_execution(
                        task["id"], scheduled_for, self._runner_id
                    )
                    claim_status = claim.get("status")
                    if claim_status == "succeeded":
                        # A prior worker delivered the side effect and crashed
                        # before moving tasks.json. Advance the exact durable
                        # occurrence without running the callback again.
                        next_run = self._calculate_next_run(task, now)
                        self.task_store.advance_scheduled_execution(
                            task["id"],
                            scheduled_for,
                            claim["execution_id"],
                            next_run.isoformat() if next_run else None,
                        )
                        logger.info(
                            f"[Scheduler] Reconciled completed occurrence for {task['id']}"
                        )
                        continue
                    if claim_status != "claimed":
                        logger.info(
                            f"[Scheduler] Task {task['id']} occurrence is "
                            f"{claim_status}; refusing duplicate execution"
                        )
                        continue

                    try:
                        ok, detail = self._execute_task(task)
                    except BaseException:
                        # Do not release/retry a claim after an unexpected
                        # BaseException. The durable 'running' state is safer
                        # than pretending an external action did not happen.
                        logger.exception(
                            f"[Scheduler] Task {task['id']} interrupted after durable claim"
                        )
                        raise
                    if not ok:
                        # A callback may fail after an outbound service accepts
                        # the message. Retry would turn this uncertainty into a
                        # duplicate customer-visible side effect, so retain an
                        # explicit in_doubt record for operator review.
                        self.task_store.finish_execution(
                            task["id"],
                            claim["execution_id"],
                            claim["lease_token"],
                            succeeded=False,
                            detail=detail,
                        )
                        self.task_store.record_scheduled_execution_uncertain(
                            task["id"],
                            scheduled_for,
                            claim["execution_id"],
                            detail,
                        )
                        logger.warning(
                            f"[Scheduler] Task {task['id']} execution is in_doubt; "
                            "automatic retry is blocked"
                        )
                        continue

                    # Record success before changing the JSON schedule. If the
                    # second write crashes, the next scanner reconciles this
                    # successful occurrence without executing it again.
                    self.task_store.finish_execution(
                        task["id"],
                        claim["execution_id"],
                        claim["lease_token"],
                        succeeded=True,
                    )
                    next_run = self._calculate_next_run(task, now)
                    advanced = self.task_store.advance_scheduled_execution(
                        task["id"],
                        scheduled_for,
                        claim["execution_id"],
                        next_run.isoformat() if next_run else None,
                    )
                    if next_run and not advanced:
                        logger.warning(
                            f"[Scheduler] Task {task['id']} succeeded but schedule "
                            "changed before advancement; not overwriting user state"
                        )
                    elif not next_run and advanced:
                        logger.info(f"[Scheduler] One-time task completed and removed: {task['id']}")
            except Exception as e:
                logger.error(f"[Scheduler] Error processing task {task.get('id')}: {e}")

    def run_task_now(
        self,
        task_id: str,
        owner_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Queue one immediate execution without changing the task schedule.

        Disabled and one-time tasks may be run manually for testing. The
        stored ``next_run_at`` remains unchanged, so a manual run never
        consumes or delays the next scheduled occurrence.

        Raises:
            ValueError: if the task does not exist.
            RuntimeError: if the same task is already executing.
        """
        task = self.task_store.get_task(task_id, owner_id=owner_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")
        claim = self.task_store.claim_manual_execution(
            task_id,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            runner_id=self._runner_id,
        )
        claim_status = claim.get("status")
        if claim_status == "succeeded":
            return {
                "status": "already_completed",
                "execution_id": claim.get("execution_id"),
            }
        if (
            claim_status == "running"
            and claim.get("runner_id") == self._runner_id
            and self._is_execution_active(claim.get("execution_id"))
        ):
            return {
                "status": "already_queued",
                "execution_id": claim.get("execution_id"),
            }
        if (
            claim_status == "blocked"
            and claim.get("runner_id") == self._runner_id
            and self._is_execution_active(claim.get("execution_id"))
        ):
            raise RuntimeError(f"Task '{task_id}' is already running")
        if claim_status != "claimed":
            raise RuntimeError(
                f"Task '{task_id}' has an uncertain execution; operator review is required"
            )
        if not self._register_execution(claim["execution_id"]):
            raise RuntimeError(
                f"Task '{task_id}' has an uncertain execution; operator review is required"
            )

        def _run():
            try:
                logger.info(f"[Scheduler] Manually executing task: {task_id} - {task.get('name', '')}")
                ok, detail = self._execute_task(task)
                self.task_store.finish_execution(
                    task_id,
                    claim["execution_id"],
                    claim["lease_token"],
                    succeeded=ok,
                    detail=detail,
                )
                self.task_store.record_manual_execution(
                    task_id,
                    claim["execution_id"],
                    succeeded=ok,
                    detail=detail,
                )
                if ok:
                    logger.info(f"[Scheduler] Manual execution completed: {task_id}")
                else:
                    logger.warning(
                        f"[Scheduler] Manual execution is in_doubt: {task_id}; "
                        "automatic retry is blocked"
                    )
            except BaseException:
                # The persisted 'running' claim must survive process/thread
                # interruption. A later click cannot silently rerun it.
                logger.exception(
                    f"[Scheduler] Manual task {task_id} interrupted after durable claim"
                )
            finally:
                self._unregister_execution(claim["execution_id"])

        worker = threading.Thread(
            target=_run,
            daemon=True,
            name=f"scheduler-manual-{task_id}",
        )
        try:
            worker.start()
        except BaseException:
            self._unregister_execution(claim["execution_id"])
            # The ledger remains running/in_doubt instead of accepting a
            # second request when thread creation itself is uncertain.
            raise
        return {"status": "queued", "execution_id": claim["execution_id"]}

    def _claim_task(self, task_id: str) -> bool:
        """Prevent scheduled and manual runs of the same task from overlapping."""
        with self._execution_lock:
            if task_id in self._active_task_ids:
                return False
            self._active_task_ids.add(task_id)
            return True

    def _release_task(self, task_id: str) -> None:
        with self._execution_lock:
            self._active_task_ids.discard(task_id)

    def _register_execution(self, execution_id: str) -> bool:
        with self._execution_lock:
            if not execution_id or execution_id in self._active_execution_ids:
                return False
            self._active_execution_ids.add(execution_id)
            return True

    def _unregister_execution(self, execution_id: str) -> None:
        with self._execution_lock:
            self._active_execution_ids.discard(execution_id)

    def _is_execution_active(self, execution_id: Optional[str]) -> bool:
        with self._execution_lock:
            return bool(execution_id and execution_id in self._active_execution_ids)
    
    def _is_task_due(self, task: dict, now: datetime) -> bool:
        """
        Check if a task is due to run
        
        Args:
            task: Task dictionary
            now: Current datetime
            
        Returns:
            True if task should run now
        """
        next_run_str = task.get("next_run_at")
        if not next_run_str:
            # Calculate initial next_run_at
            next_run = self._calculate_next_run(task, now)
            if next_run:
                self.task_store.update_task(task['id'], {
                    "next_run_at": next_run.isoformat()
                })
                return False
            return False
        
        try:
            next_run = _parse_naive_local(next_run_str)

            if next_run < now:
                time_diff = (now - next_run).total_seconds()
                schedule = task.get("schedule", {})
                schedule_type = schedule.get("type")

                # Catch-up window: fire if we're within 10 minutes of the
                # scheduled tick. Beyond that we'd rather skip than push a
                # stale daily report to the user.
                if time_diff <= 600:
                    return True

                logger.warning(
                    f"[Scheduler] Task {task['id']} is overdue by {int(time_diff)}s, "
                    f"skipping and scheduling next run"
                )

                if schedule_type == "once":
                    self.task_store.delete_task(task['id'])
                    logger.info(f"[Scheduler] One-time task {task['id']} expired, removed")
                    return False

                next_next_run = self._calculate_next_run(task, now)
                if next_next_run:
                    self.task_store.update_task(task['id'], {
                        "next_run_at": next_next_run.isoformat()
                    })
                    logger.info(f"[Scheduler] Rescheduled task {task['id']} to {next_next_run}")
                return False

            return now >= next_run
        except Exception as e:
            logger.error(
                f"[Scheduler] Failed to evaluate due-state for task "
                f"{task.get('id')} (next_run_at={next_run_str!r}): {e}"
            )
            return False
    
    def _calculate_next_run(self, task: dict, from_time: datetime) -> Optional[datetime]:
        """
        Calculate next run time for a task
        
        Args:
            task: Task dictionary
            from_time: Calculate from this time
            
        Returns:
            Next run datetime or None for one-time tasks
        """
        schedule = task.get("schedule", {})
        schedule_type = schedule.get("type")
        
        if schedule_type == "cron":
            # Cron expression
            expression = schedule.get("expression")
            if not expression:
                return None
            
            try:
                cron = croniter(expression, from_time)
                return cron.get_next(datetime)
            except Exception as e:
                logger.error(f"[Scheduler] Invalid cron expression '{expression}': {e}")
                return None
        
        elif schedule_type == "interval":
            # Interval in seconds
            seconds = schedule.get("seconds", 0)
            if seconds <= 0:
                return None
            return from_time + timedelta(seconds=seconds)
        
        elif schedule_type == "once":
            # One-time task at specific time
            run_at_str = schedule.get("run_at")
            if not run_at_str:
                return None
            
            try:
                run_at = _parse_naive_local(run_at_str)
                if run_at > from_time:
                    return run_at
            except Exception as e:
                logger.error(
                    f"[Scheduler] Failed to parse once-task run_at "
                    f"{run_at_str!r}: {e}"
                )
            return None
        
        return None
    
    def _execute_task(self, task: dict) -> tuple[bool, Optional[str]]:
        """
        Execute a task.

        Returns a success flag and diagnostic. Callback False means the
        side-effect result is unknown, rather than permission to retry; an
        external service may have accepted the request immediately before the
        client observed an error. Callback None retains legacy success
        behavior.
        """
        try:
            result = self.execute_callback(task)
            if result is False:
                return False, (
                    "Execution callback reported failure; external side effect "
                    "status is unconfirmed"
                )
            return True, None
        except Exception as e:
            logger.error(f"[Scheduler] Error executing task {task['id']}: {e}")
            return False, (
                f"Execution callback raised {type(e).__name__}; external side "
                "effect status is unconfirmed"
            )
