"""协调同一进程内的技能发布和控制台目录变更。"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import BinaryIO

from common.path_safety import is_link_or_reparse_point


_REGISTRY_LOCK = threading.Lock()
_ROOT_LOCKS: dict[str, "SkillRootLock"] = {}


class SkillRootLock:
    """用线程锁和文件锁串行化同一技能根目录的状态变更。"""

    def __init__(self, skills_dir: str | Path, timeout_seconds: float = 60.0):
        self._skills_dir = Path(
            os.path.realpath(os.path.abspath(os.fspath(skills_dir)))
        )
        self._lock_path = self._skills_dir / ".system" / "governance.lock"
        self._thread_lock = threading.RLock()
        self._owner_thread_id: int | None = None
        self._depth = 0
        self._handle: BinaryIO | None = None
        self._timeout_seconds = timeout_seconds

    def acquire(self) -> bool:
        """先取得进程内锁，再阻塞取得跨进程独占锁。"""

        self._thread_lock.acquire()
        thread_id = threading.get_ident()
        if self._owner_thread_id == thread_id:
            self._depth += 1
            return True
        handle = None
        try:
            if is_link_or_reparse_point(self._lock_path.parent):
                raise ValueError("技能治理锁目录不能是符号链接或重解析点")
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            if is_link_or_reparse_point(self._lock_path.parent):
                raise ValueError("技能治理锁目录不能是符号链接或重解析点")
            if is_link_or_reparse_point(self._lock_path):
                raise ValueError("技能治理锁文件不能是符号链接或重解析点")
            handle = self._lock_path.open("a+b")
            if os.name == "nt":
                self._acquire_windows(handle)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._handle = handle
            self._owner_thread_id = thread_id
            self._depth = 1
            return True
        except Exception:
            if handle is not None and not handle.closed:
                handle.close()
            self._thread_lock.release()
            raise

    def _acquire_windows(self, handle: BinaryIO) -> None:
        """在 Windows 上轮询非阻塞字节锁，并提供确定的超时。"""

        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError("等待技能治理跨进程锁超时")
                time.sleep(0.05)

    def release(self) -> None:
        """在最外层退出时释放跨进程锁。"""

        if self._owner_thread_id != threading.get_ident() or self._depth <= 0:
            raise RuntimeError("当前线程没有持有技能根目录锁")
        self._depth -= 1
        try:
            if self._depth == 0:
                handle = self._handle
                self._handle = None
                self._owner_thread_id = None
                if handle is not None:
                    try:
                        if os.name == "nt":
                            import msvcrt

                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()
        finally:
            self._thread_lock.release()

    def __enter__(self) -> "SkillRootLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def skill_root_lock(skills_dir: str | Path) -> SkillRootLock:
    """返回技能根目录对应的进程内共享可重入锁。"""

    key = os.path.normcase(
        os.path.realpath(os.path.abspath(os.fspath(skills_dir)))
    )
    with _REGISTRY_LOCK:
        lock = _ROOT_LOCKS.get(key)
        if lock is None:
            lock = SkillRootLock(skills_dir)
            _ROOT_LOCKS[key] = lock
        return lock
