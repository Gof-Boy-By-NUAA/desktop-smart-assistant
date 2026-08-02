"""跨进程提交治理记忆事实后等待统一排空信号。"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Sequence

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from agent.memory.config import MemoryConfig
from agent.memory.governance import IdentityContext, MemoryScope, MemoryWriteCommand
from agent.memory.manager import MemoryManager


def main(arguments: Sequence[str]) -> int:
    """写入一个新版本，等待父进程放行后排空持久化派生任务。"""

    if len(arguments) != 7:
        raise ValueError("memory_outbox_worker 参数数量无效")
    workspace, tenant_id, memory_id, content, key, ready, go = arguments
    manager = MemoryManager(
        MemoryConfig(
            workspace_root=workspace,
            enable_governed_retrieval=True,
            tenant_id=tenant_id,
        ),
        embedding_provider=None,
    )
    identity = IdentityContext(
        tenant_id=tenant_id,
        actor_user_id="alice",
        roles=frozenset(),
        trace_id="trace-worker-%s" % key,
        auth_source="memory-outbox-worker",
    )
    try:
        record = manager.governance_service.write(
            identity,
            MemoryWriteCommand(
                content=content,
                scope=MemoryScope.USER,
                source_type="process-test",
                source_ref="process://%s" % key,
                idempotency_key=key,
                memory_id=memory_id,
                metadata={"title": "多进程派生测试"},
            ),
        )
        Path(ready).write_text(str(record.version), encoding="utf-8")
        deadline = time.monotonic() + 30.0
        while not Path(go).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("等待父进程排空信号超时")
            time.sleep(0.05)
        manager._drain_governed_derivative_job(tenant_id, memory_id)
        return 0
    finally:
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
