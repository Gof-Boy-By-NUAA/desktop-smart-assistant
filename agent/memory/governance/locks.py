"""协调同一租户的治理记忆派生发布。"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import BinaryIO, Dict

from common.path_safety import is_link_or_reparse_point


_REGISTRY_LOCK = threading.Lock()
_RUNTIME_LOCKS: Dict[str, "GovernedRuntimeLock"] = {}


class GovernedRuntimeLock:
    """用线程锁和文件锁串行化一个租户的派生数据发布。"""

    def __init__(
        self,
        memory_dir: str | Path,
        tenant_id: str,
        timeout_seconds: float = 60.0,
    ):
        root = Path(os.path.realpath(os.path.abspath(os.fspath(memory_dir))))
        tenant_key = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
        self._lock_path = root / ".governed" / ".locks" / (
            tenant_key + ".lock"
        )
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
                raise ValueError("治理记忆锁目录不能是符号链接或重解析点")
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            if is_link_or_reparse_point(self._lock_path.parent):
                raise ValueError("治理记忆锁目录不能是符号链接或重解析点")
            if is_link_or_reparse_point(self._lock_path):
                raise ValueError("治理记忆锁文件不能是符号链接或重解析点")
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
        """在 Windows 上轮询字节锁，并提供确定的超时。"""

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
                    raise TimeoutError("等待治理记忆跨进程锁超时")
                time.sleep(0.05)

    def release(self) -> None:
        """在最外层退出时释放跨进程锁。"""

        if self._owner_thread_id != threading.get_ident() or self._depth <= 0:
            raise RuntimeError("当前线程没有持有治理记忆运行时锁")
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

    def __enter__(self) -> "GovernedRuntimeLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def governed_runtime_lock(
    memory_dir: str | Path, tenant_id: str
) -> GovernedRuntimeLock:
    """返回同一工作区和租户共享的运行时锁。"""

    root = os.path.normcase(
        os.path.realpath(os.path.abspath(os.fspath(memory_dir)))
    )
    key = root + "\0" + tenant_id
    with _REGISTRY_LOCK:
        lock = _RUNTIME_LOCKS.get(key)
        if lock is None:
            lock = GovernedRuntimeLock(memory_dir, tenant_id)
            _RUNTIME_LOCKS[key] = lock
        return lock
