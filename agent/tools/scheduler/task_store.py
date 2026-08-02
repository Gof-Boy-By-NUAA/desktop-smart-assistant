"""
Task storage management for scheduler
"""

import json
import os
import shutil
import tempfile
import threading
from datetime import datetime
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional
from pathlib import Path
from common.utils import expand_path


class TaskStoreCorruptionError(RuntimeError):
    """The task file exists but is not a valid scheduler snapshot."""


class TaskStore:
    """
    Manages persistent storage of scheduled tasks
    """
    
    _path_locks: Dict[str, threading.RLock] = {}
    _path_locks_guard = threading.Lock()

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
            if "creator_owner_id" in updates:
                raise ValueError("creator_owner_id is immutable")

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
                return self._assert_owner(task, owner_id)
            except ValueError:
                return None

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
            task_list = list(tasks.values())

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
        
        return task_list
    
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
