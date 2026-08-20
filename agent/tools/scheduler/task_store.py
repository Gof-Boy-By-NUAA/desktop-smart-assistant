"""
Task storage management for scheduler
"""

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Tuple
from pathlib import Path
from common.utils import expand_path


class TaskStoreCorruptionError(RuntimeError):
    """The task file exists but is not a valid scheduler snapshot."""


class TaskExecutionStoreError(RuntimeError):
    """The durable scheduler execution ledger cannot be safely used."""


class TaskStore:
    """
    Manages persistent storage of scheduled tasks
    """
    
    _path_locks: Dict[str, threading.RLock] = {}
    _path_locks_guard = threading.Lock()
    _EXECUTION_GENERATION_FIELD = "_execution_generation"

    def __init__(self, store_path: str = None):
        """
        Initialize task store
        
        Args:
            store_path: Path to tasks.json file. Defaults to ~/cow/scheduler/tasks.json
        """
        if store_path is None:
            # Default to ~/cow/scheduler/tasks.json
            home = expand_path("~")
            store_path = os.path.join(home, "cow", "scheduler", "tasks.json")
        
        self.store_path = os.path.realpath(store_path)
        self.lock = self._lock_for_path(self.store_path)
        self._ensure_store_dir()
        self._initialize_execution_store()

    @classmethod
    def _lock_for_path(cls, store_path: str) -> threading.RLock:
        """Share one re-entrant lock between TaskStore instances in-process."""
        with cls._path_locks_guard:
            lock = cls._path_locks.get(store_path)
            if lock is None:
                lock = threading.RLock()
                cls._path_locks[store_path] = lock
            return lock
    
    def _ensure_store_dir(self):
        """Ensure the storage directory exists"""
        store_dir = os.path.dirname(self.store_path)
        os.makedirs(store_dir, exist_ok=True)

    @property
    def _backup_path(self) -> str:
        return f"{self.store_path}.bak"

    @property
    def _lock_path(self) -> str:
        return f"{self.store_path}.lock"

    @property
    def _execution_db_path(self) -> str:
        """A durable ledger kept next to, but separate from, task definitions.

        Task definitions remain JSON for backward compatibility.  Execution
        state is deliberately SQLite: an in-memory "currently running" set
        cannot prevent a second process from delivering the same task after a
        restart or concurrent deployment.
        """
        return f"{self.store_path}.executions.sqlite3"

    def _execution_connection(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self._execution_db_path,
                timeout=10,
                isolation_level=None,
            )
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            return connection
        except sqlite3.Error as exc:
            raise TaskExecutionStoreError(
                f"Scheduler execution ledger is unavailable: {exc}"
            ) from exc

    def _initialize_execution_store(self) -> None:
        """Create a crash-safe, cross-process execution ledger.

        A `running` entry is never leased away automatically. A process can
        die after an external channel has accepted a message but before it
        records success; automatic expiration would turn that uncertainty into
        a duplicate side effect. Such entries instead remain fail-closed for
        operator review.
        """
        with self._transaction():
            connection = self._execution_connection()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS scheduler_executions (
                        task_id TEXT NOT NULL,
                        occurrence_id TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        mode TEXT NOT NULL CHECK(mode IN ('scheduled', 'manual')),
                        status TEXT NOT NULL CHECK(
                            status IN ('running', 'succeeded', 'in_doubt')
                        ),
                        lease_token TEXT NOT NULL,
                        runner_id TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        completed_at REAL,
                        detail TEXT,
                        PRIMARY KEY(task_id, occurrence_id)
                    )
                    """
                )
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(scheduler_executions)"
                    ).fetchall()
                }
                # Early development builds did not record the process/service
                # identity. Preserve their unresolved rows as foreign rather
                # than interpreting a restart as an active local worker.
                if "runner_id" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE scheduler_executions
                        ADD COLUMN runner_id TEXT NOT NULL DEFAULT ''
                        """
                    )
                # At most one task execution may be active or uncertain. It
                # covers cross-process scheduled/manual races as well as
                # duplicate scheduler service instances.
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    scheduler_executions_one_live_task
                    ON scheduler_executions(task_id)
                    WHERE status IN ('running', 'in_doubt')
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS scheduler_executions_latest
                    ON scheduler_executions(task_id, updated_at DESC)
                    """
                )
            except sqlite3.Error as exc:
                raise TaskExecutionStoreError(
                    f"Scheduler execution ledger initialization failed: {exc}"
                ) from exc
            finally:
                connection.close()

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Hold an OS lock as well as the in-process lock for one transaction."""
        with open(self._lock_path, "a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            try:
                yield
            finally:
                unlock()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Serialize every load-modify-save sequence across threads/processes."""
        with self.lock:
            with self._file_lock():
                yield

    @staticmethod
    def _fsync_parent(directory: str) -> None:
        """Persist a rename on POSIX; Windows does not expose O_DIRECTORY."""
        if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
            return
        descriptor = None
        try:
            descriptor = os.open(directory, os.O_DIRECTORY)
            os.fsync(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _read_task_file(path: str) -> Dict[str, dict]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:
            raise TaskStoreCorruptionError(
                f"Scheduler task store is unreadable: {path} ({exc})"
            ) from exc
        if not isinstance(data, dict):
            raise TaskStoreCorruptionError(
                f"Scheduler task store has invalid top-level data: {path}"
            )
        tasks = data.get("tasks", {})
        if not isinstance(tasks, dict):
            raise TaskStoreCorruptionError(
                f"Scheduler task store has invalid tasks map: {path}"
            )
        return tasks

    def _load_tasks_unlocked(self) -> Dict[str, dict]:
        if not os.path.exists(self.store_path):
            return {}
        return self._read_task_file(self.store_path)

    def _save_tasks_unlocked(self, tasks: Dict[str, dict]) -> None:
        if not isinstance(tasks, dict):
            raise ValueError("tasks must be a dictionary")
        store_dir = os.path.dirname(self.store_path)
        data = {
            "version": 1,
            "updated_at": datetime.now().isoformat(),
            "tasks": tasks,
        }
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{Path(self.store_path).name}.",
            suffix=".tmp",
            dir=store_dir,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            # Copy the last known-good primary before replacing it. If the
            # backup itself cannot be written, leave the primary untouched.
            if os.path.exists(self.store_path):
                shutil.copyfile(self.store_path, self._backup_path)
                # Windows rejects fsync on a read-only descriptor.
                with open(self._backup_path, "rb+") as handle:
                    os.fsync(handle.fileno())
            os.replace(temporary_path, self.store_path)
            temporary_path = ""
            self._fsync_parent(store_dir)
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)
    
    @staticmethod
    def _effective_owner(task: dict) -> str:
        owner = str(task.get("creator_owner_id") or "")
        if owner:
            return owner
        action = task.get("action") or {}
        # Legacy Web tasks predate owner metadata. They remain accessible only
        # to the explicit local/no-password legacy principal.
        if isinstance(action, dict) and action.get("channel_type") == "web":
            return "web:legacy"
        return ""

    @classmethod
    def _ensure_execution_generation_unlocked(cls, task: dict) -> Tuple[str, bool]:
        """Return a server-generated lifetime identifier for this task record.

        A task id can be reused after deletion.  Binding the execution ledger
        only to task id plus due timestamp would allow an old "succeeded" row
        to suppress a new task created with the same id and schedule.  Legacy
        tasks receive a generation under the same file transaction before
        their first claim.
        """
        generation = task.get(cls._EXECUTION_GENERATION_FIELD)
        if (
            isinstance(generation, str)
            and len(generation) == 32
            and all(character in "0123456789abcdef" for character in generation)
        ):
            return generation, False
        generation = uuid.uuid4().hex
        task[cls._EXECUTION_GENERATION_FIELD] = generation
        task["updated_at"] = datetime.now().isoformat()
        return generation, True

    @classmethod
    def _assert_owner(cls, task: Optional[dict], owner_id: Optional[str]) -> dict:
        if task is None:
            raise ValueError("Task not found")
        if owner_id is not None and cls._effective_owner(task) != owner_id:
            # Do not disclose whether a foreign task exists.
            raise ValueError("Task not found")
        return task

    def load_tasks(self) -> Dict[str, dict]:
        """
        Load all tasks from storage
        
        Returns:
            Dictionary of task_id -> task_data
        """
        with self._transaction():
            return self._load_tasks_unlocked()
    
    def save_tasks(self, tasks: Dict[str, dict]):
        """
        Save all tasks to storage
        
        Args:
            tasks: Dictionary of task_id -> task_data
        """
        with self._transaction():
            self._save_tasks_unlocked(tasks)
    
    def add_task(self, task: dict) -> bool:
        """
        Add a new task
        
        Args:
            task: Task data dictionary
            
        Returns:
            True if successful
        """
        with self._transaction():
            tasks = self._load_tasks_unlocked()
            task_id = task.get("id")
            
            if not task_id:
                raise ValueError("Task must have an 'id' field")
            
            if task_id in tasks:
                raise ValueError(f"Task with id '{task_id}' already exists")
            
            task = dict(task)
            action = task.get("action") or {}
            if (
                isinstance(action, dict)
                and action.get("channel_type") == "web"
                and not task.get("creator_owner_id")
            ):
                task["creator_owner_id"] = "web:legacy"
            # Never honor a caller-supplied generation. It is a lifecycle
            # fence, not user-editable scheduler metadata.
            task[self._EXECUTION_GENERATION_FIELD] = uuid.uuid4().hex
            tasks[task_id] = task
            self._save_tasks_unlocked(tasks)
        return True
    
    def update_task(
        self, task_id: str, updates: dict, owner_id: Optional[str] = None
    ) -> bool:
        """
        Update an existing task
        
        Args:
            task_id: Task ID
            updates: Dictionary of fields to update
            
        Returns:
            True if successful
        """
        with self._transaction():
            tasks = self._load_tasks_unlocked()
            task = self._assert_owner(tasks.get(task_id), owner_id)
            if (
                "creator_owner_id" in updates
                or self._EXECUTION_GENERATION_FIELD in updates
            ):
                raise ValueError("task ownership and execution generation are immutable")

            # Update fields
            task.update(updates)
            tasks[task_id]["updated_at"] = datetime.now().isoformat()
            self._save_tasks_unlocked(tasks)
        return True
    
    def delete_task(self, task_id: str, owner_id: Optional[str] = None) -> bool:
        """
        Delete a task
        
        Args:
            task_id: Task ID
            
        Returns:
            True if successful
        """
        with self._transaction():
            tasks = self._load_tasks_unlocked()
            self._assert_owner(tasks.get(task_id), owner_id)
            del tasks[task_id]
            self._save_tasks_unlocked(tasks)
        return True
    
    def get_task(
        self, task_id: str, owner_id: Optional[str] = None
    ) -> Optional[dict]:
        """
        Get a specific task
        
        Args:
            task_id: Task ID
            
        Returns:
            Task data or None if not found
        """
        with self._transaction():
            tasks = self._load_tasks_unlocked()
            task = tasks.get(task_id)
            if task is None:
                return None
            try:
                task = dict(self._assert_owner(task, owner_id))
            except ValueError:
                return None
        return self._with_execution_state(
            task, self._latest_execution_states([task_id]).get(task_id)
        )

    def list_tasks(
        self, enabled_only: bool = False, owner_id: Optional[str] = None
    ) -> List[dict]:
        """
        List all tasks
        
        Args:
            enabled_only: If True, only return enabled tasks
            
        Returns:
            List of task dictionaries
        """
        with self._transaction():
            tasks = self._load_tasks_unlocked()
            task_list = [dict(task) for task in tasks.values()]

            if owner_id is not None:
                task_list = [
                    task for task in task_list
                    if self._effective_owner(task) == owner_id
                ]
            if enabled_only:
                task_list = [t for t in task_list if t.get("enabled", True)]
        
        # Sort by enabled status (enabled first), then by next_run_at
        def sort_key(t):
            enabled = t.get("enabled", True)
            next_run = t.get("next_run_at", "")
            # Enabled tasks first (0), disabled tasks second (1)
            # Then sort by next_run_at (empty string sorts last)
            return (0 if enabled else 1, next_run if next_run else "9999-12-31")
        
        task_list.sort(key=sort_key)
        
        states = self._latest_execution_states(
            [str(task.get("id") or "") for task in task_list if task.get("id")]
        )
        return [
            self._with_execution_state(task, states.get(task.get("id")))
            for task in task_list
        ]
    
    def enable_task(
        self, task_id: str, enabled: bool = True, owner_id: Optional[str] = None
    ) -> bool:
        """
        Enable or disable a task
        
        Args:
            task_id: Task ID
            enabled: True to enable, False to disable
            
        Returns:
            True if successful
        """
        return self.update_task(task_id, {"enabled": enabled}, owner_id=owner_id)

    @staticmethod
    def _execution_detail(detail: Optional[object]) -> Optional[str]:
        if detail is None:
            return None
        return str(detail)[:2000]

    @staticmethod
    def _execution_record(row) -> dict:
        return {
            "task_id": row[0],
            "occurrence_id": row[1],
            "owner_id": row[2],
            "mode": row[3],
            "status": row[4],
            "lease_token": row[5],
            "runner_id": row[6],
            "created_at": row[7],
            "updated_at": row[8],
            "completed_at": row[9],
            "detail": row[10],
        }

    def _claim_execution_unlocked(
        self,
        task_id: str,
        occurrence_id: str,
        owner_id: str,
        mode: str,
        runner_id: str,
    ) -> dict:
        """Persist a no-overlap execution claim while holding the task lock."""
        now = time.time()
        lease_token = uuid.uuid4().hex
        connection = self._execution_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT task_id, occurrence_id, owner_id, mode, status, lease_token,
                       runner_id, created_at, updated_at, completed_at, detail
                FROM scheduler_executions
                WHERE task_id = ? AND occurrence_id = ?
                """,
                (task_id, occurrence_id),
            ).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                record = self._execution_record(existing)
                return {
                    "status": record["status"],
                    "execution_id": occurrence_id,
                    "lease_token": None,
                    "runner_id": record["runner_id"],
                    "detail": record["detail"],
                }

            live = connection.execute(
                """
                SELECT task_id, occurrence_id, owner_id, mode, status, lease_token,
                       runner_id, created_at, updated_at, completed_at, detail
                FROM scheduler_executions
                WHERE task_id = ? AND status IN ('running', 'in_doubt')
                """,
                (task_id,),
            ).fetchone()
            if live is not None:
                connection.execute("COMMIT")
                record = self._execution_record(live)
                return {
                    "status": "blocked",
                    "execution_id": record["occurrence_id"],
                    "lease_token": None,
                    "runner_id": record["runner_id"],
                    "detail": record["detail"],
                }

            connection.execute(
                """
                INSERT INTO scheduler_executions(
                    task_id, occurrence_id, owner_id, mode, status, lease_token,
                    runner_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    task_id,
                    occurrence_id,
                    owner_id,
                    mode,
                    lease_token,
                    runner_id,
                    now,
                    now,
                ),
            )
            connection.execute("COMMIT")
            return {
                "status": "claimed",
                "execution_id": occurrence_id,
                "lease_token": lease_token,
                "runner_id": runner_id,
                "detail": None,
            }
        except sqlite3.Error as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise TaskExecutionStoreError(
                f"Scheduler execution claim failed: {exc}"
            ) from exc
        finally:
            connection.close()

    def claim_scheduled_execution(
        self, task_id: str, scheduled_for: str, runner_id: str
    ) -> dict:
        """Atomically claim the persisted due occurrence, or fail closed.

        The scheduled timestamp is part of the idempotency key. A scheduler
        instance holding a stale task snapshot cannot execute a task that a
        user has disabled, deleted, or rescheduled in the meantime.
        """
        if (
            not task_id
            or not isinstance(scheduled_for, str)
            or not scheduled_for
            or not isinstance(runner_id, str)
            or not runner_id
        ):
            raise ValueError("task_id, scheduled_for, and runner_id are required")
        with self._transaction():
            tasks = self._load_tasks_unlocked()
            task = tasks.get(task_id)
            if task is None:
                return {"status": "missing", "execution_id": None, "lease_token": None}
            if not task.get("enabled", True):
                return {"status": "disabled", "execution_id": None, "lease_token": None}
            if task.get("next_run_at") != scheduled_for:
                return {"status": "stale", "execution_id": None, "lease_token": None}
            generation, changed = self._ensure_execution_generation_unlocked(task)
            if changed:
                self._save_tasks_unlocked(tasks)
            return self._claim_execution_unlocked(
                task_id,
                f"scheduled:{generation}:{scheduled_for}",
                self._effective_owner(task),
                "scheduled",
                runner_id,
            )

    def claim_manual_execution(
        self,
        task_id: str,
        owner_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        runner_id: Optional[str] = None,
    ) -> dict:
        """Claim a manual execution with a caller-stable idempotency key."""
        if not task_id:
            raise ValueError("task_id is required")
        if not isinstance(runner_id, str) or not runner_id:
            raise ValueError("runner_id is required")
        if idempotency_key is None:
            idempotency_key = uuid.uuid4().hex
        if (
            not isinstance(idempotency_key, str)
            or not 8 <= len(idempotency_key) <= 128
            or any(ord(character) < 33 or ord(character) > 126 for character in idempotency_key)
        ):
            raise ValueError("idempotency_key must be 8-128 printable ASCII characters")
        with self._transaction():
            tasks = self._load_tasks_unlocked()
            task = self._assert_owner(tasks.get(task_id), owner_id)
            generation, changed = self._ensure_execution_generation_unlocked(task)
            if changed:
                self._save_tasks_unlocked(tasks)
            return self._claim_execution_unlocked(
                task_id,
                f"manual:{generation}:{idempotency_key}",
                self._effective_owner(task),
                "manual",
                runner_id,
            )

    def finish_execution(
        self,
        task_id: str,
        execution_id: str,
        lease_token: str,
        *,
        succeeded: bool,
        detail: Optional[object] = None,
    ) -> None:
        """Finish only the exact durable claim that performed the side effect."""
        if not task_id or not execution_id or not lease_token:
            raise ValueError("task_id, execution_id, and lease_token are required")
        status = "succeeded" if succeeded else "in_doubt"
        now = time.time()
        connection = self._execution_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE scheduler_executions
                SET status = ?, updated_at = ?, completed_at = ?, detail = ?
                WHERE task_id = ? AND occurrence_id = ? AND lease_token = ?
                  AND status = 'running'
                """,
                (
                    status,
                    now,
                    now,
                    self._execution_detail(detail),
                    task_id,
                    execution_id,
                    lease_token,
                ),
            )
            if cursor.rowcount != 1:
                connection.execute("ROLLBACK")
                raise TaskExecutionStoreError(
                    "Scheduler execution completion was rejected (stale or missing lease)"
                )
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise TaskExecutionStoreError(
                f"Scheduler execution completion failed: {exc}"
            ) from exc
        finally:
            connection.close()

    def _latest_execution_states(self, task_ids: List[str]) -> Dict[str, dict]:
        if not task_ids:
            return {}
        placeholders = ", ".join("?" for _ in task_ids)
        connection = self._execution_connection()
        try:
            rows = connection.execute(
                f"""
                SELECT task_id, occurrence_id, owner_id, mode, status, lease_token,
                       runner_id, created_at, updated_at, completed_at, detail
                FROM scheduler_executions
                WHERE task_id IN ({placeholders})
                ORDER BY task_id ASC, updated_at DESC
                """,
                task_ids,
            ).fetchall()
        except sqlite3.Error as exc:
            raise TaskExecutionStoreError(
                f"Scheduler execution ledger read failed: {exc}"
            ) from exc
        finally:
            connection.close()

        latest: Dict[str, dict] = {}
        for row in rows:
            record = self._execution_record(row)
            latest.setdefault(record["task_id"], record)
        return latest

    @classmethod
    def _with_execution_state(cls, task: dict, execution: Optional[dict]) -> dict:
        result = dict(task)
        # Lifecycle fencing is server-only metadata. Exposing it would invite
        # clients to treat it as an editable execution capability.
        result.pop(cls._EXECUTION_GENERATION_FIELD, None)
        if execution is None:
            return result
        result.update(
            {
                "last_execution_id": execution["occurrence_id"],
                "last_execution_mode": execution["mode"],
                "last_execution_status": execution["status"],
                "last_execution_at": datetime.fromtimestamp(
                    execution["updated_at"]
                ).isoformat(),
                "last_execution_detail": execution["detail"],
            }
        )
        return result

    def record_manual_execution(
        self,
        task_id: str,
        execution_id: str,
        *,
        succeeded: bool,
        detail: Optional[object] = None,
    ) -> bool:
        """Surface a manual result without changing its scheduled occurrence."""
        with self._transaction():
            tasks = self._load_tasks_unlocked()
            task = tasks.get(task_id)
            if task is None:
                return False
            now = datetime.now().isoformat()
            task["last_execution_id"] = execution_id
            task["last_execution_mode"] = "manual"
            task["last_execution_status"] = "succeeded" if succeeded else "in_doubt"
            task["last_execution_at"] = now
            task["last_execution_detail"] = self._execution_detail(detail)
            if succeeded:
                task["last_run_at"] = now
                task["last_manual_run_at"] = now
            task["updated_at"] = now
            self._save_tasks_unlocked(tasks)
            return True

    def record_scheduled_execution_uncertain(
        self,
        task_id: str,
        scheduled_for: str,
        execution_id: str,
        detail: Optional[object],
    ) -> bool:
        """Persist a visible fail-closed state for an uncertain side effect."""
        with self._transaction():
            tasks = self._load_tasks_unlocked()
            task = tasks.get(task_id)
            if (
                task is None
                or not task.get("enabled", True)
                or task.get("next_run_at") != scheduled_for
            ):
                return False
            now = datetime.now().isoformat()
            task["last_execution_id"] = execution_id
            task["last_execution_mode"] = "scheduled"
            task["last_execution_status"] = "in_doubt"
            task["last_execution_at"] = now
            task["last_execution_detail"] = self._execution_detail(detail)
            task["updated_at"] = now
            self._save_tasks_unlocked(tasks)
            return True

    def advance_scheduled_execution(
        self,
        task_id: str,
        scheduled_for: str,
        execution_id: str,
        next_run_at: Optional[str],
    ) -> bool:
        """Advance only the exact occurrence that was durably completed.

        If saving the JSON task update crashes after the ledger says
        "succeeded", a later scheduler tick can call this method again. It
        will advance the schedule without rerunning the side effect.
        """
        with self._transaction():
            tasks = self._load_tasks_unlocked()
            task = tasks.get(task_id)
            if (
                task is None
                or not task.get("enabled", True)
                or task.get("next_run_at") != scheduled_for
            ):
                return False
            now = datetime.now().isoformat()
            if next_run_at:
                task["next_run_at"] = next_run_at
                task["last_run_at"] = now
                task["last_execution_id"] = execution_id
                task["last_execution_mode"] = "scheduled"
                task["last_execution_status"] = "succeeded"
                task["last_execution_at"] = now
                task["last_execution_detail"] = None
                task["updated_at"] = now
                self._save_tasks_unlocked(tasks)
            else:
                del tasks[task_id]
                self._save_tasks_unlocked(tasks)
            return True
