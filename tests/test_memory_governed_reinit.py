"""治理索引重建校验范围的回归测试（真实链路）。

评审缺陷 P1-1：首次同步成功后，再次初始化 MemoryManager 时，
_restore_governed_runtime 用整个租户的索引校验 governed 集合，
导致正常存在的 workspace 记录被判为异常并抛 RuntimeError。

性能门修复回归：恢复循环曾对每条记录做双重核验（已匹配记录也被
第二次 _governed_projection_matches 复查），修复后每条记录恰好核验
一次、损坏记录核验两次（发现不匹配 + 写后重读）。本文件还覆盖并行
恢复中首次写入失败的暴露、派生任务不被错误完成、以及去故障后的
收敛性。

测试替身声明：本文件中的计数包装器与故障注入包装器是公开的
test double——仅包装真实方法用于计数/注错，底层 MemoryManager、
SQLite、文件系统全部真实。它们不得用于证明性能；正式性能证据
只来自真实官方门（benchmarks/memory/outbox.py）。

本文件其余部分不使用任何测试替身。
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from agent.memory.config import MemoryConfig
from agent.memory.governance import (
    IdentityContext,
    MemoryScope,
    MemoryWriteCommand,
)
from agent.memory.manager import MemoryManager

_TENANT_ID = "tenant-reinit"


def _make_config(workspace_root: str) -> MemoryConfig:
    return MemoryConfig(
        workspace_root=workspace_root,
        enable_governed_retrieval=True,
        tenant_id=_TENANT_ID,
    )


def _make_identity() -> IdentityContext:
    return IdentityContext(
        tenant_id=_TENANT_ID,
        actor_user_id="reinit-tester",
        roles=frozenset(),
        trace_id="trace-memory-governed-reinit",
        auth_source="reinit-test",
    )


def _write_records(manager: MemoryManager, count: int, prefix: str):
    """经生产写入链路写入 count 条治理记忆并返回记录对象。"""

    identity = _make_identity()
    return [
        manager.governance_service.write(
            identity,
            MemoryWriteCommand(
                content=f"治理记忆回归 {prefix} 第 {i} 条：投影核验测试内容。",
                scope=MemoryScope.USER,
                source_type="governed-reinit-test",
                source_ref=f"governed-reinit:{prefix}:{i}",
                idempotency_key=f"governed-reinit:{prefix}:{i}",
            ),
        )
        for i in range(count)
    ]


class MemoryGovernedReinitTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_reinitialization_after_workspace_sync_keeps_memory_usable(self):
        # 第一个实例：写入真实 workspace 记忆并同步成功。
        memory_file = self.workspace / "MEMORY.md"
        memory_file.write_text(
            "# 长期记忆\n上海是中国的经济中心城市。\n",
            encoding="utf-8",
        )
        first = MemoryManager(_make_config(str(self.workspace)), embedding_provider=None)
        asyncio.run(first.sync(force=True))
        first.close()

        # 第二个实例：同一工作区再次初始化，当前实现抛
        # "治理记忆索引重建后缺失或内容不一致" —— 这是缺陷的 RED。
        second = MemoryManager(_make_config(str(self.workspace)), embedding_provider=None)
        try:
            results = asyncio.run(
                second.search("中国的经济中心城市是哪里？", max_results=5)
            )
        finally:
            second.close()

        # 现有记忆数据必须保留且可检索。
        self.assertTrue(results)
        self.assertEqual("MEMORY.md", results[0].path)

    def _settle_records(self, count: int, prefix: str):
        """写入记录并经一次完整重建收敛：投影就位、派生任务清空。"""

        first = MemoryManager(
            _make_config(str(self.workspace)), embedding_provider=None
        )
        records = _write_records(first, count, prefix)
        first.close()
        settled = MemoryManager(
            _make_config(str(self.workspace)), embedding_provider=None
        )
        settled.close()
        return records

    def test_explicit_restore_verifies_settled_projection_exactly_once(self):
        # 三条已有正确投影：显式 restore 后每条恰好核验 1 次。
        # 修复前实现对已匹配记录做第二次核验（计数为 2），此断言即 RED。
        records = self._settle_records(3, "once")
        counted = MemoryManager(
            _make_config(str(self.workspace)), embedding_provider=None
        )

        counts = {}
        real_matches = counted._governed_projection_matches

        def counting_matches(record):
            # test double：仅计数，透传真实实现；不用于性能证明。
            counts[record.memory_id] = counts.get(record.memory_id, 0) + 1
            return real_matches(record)

        counted._governed_projection_matches = counting_matches
        try:
            counted._restore_governed_runtime()
        finally:
            del counted._governed_projection_matches
            counted.close()

        for record in records:
            self.assertEqual(
                counts.get(record.memory_id),
                1,
                f"已匹配记录应恰好核验一次：{record.memory_id} 实际 "
                f"{counts.get(record.memory_id)} 次",
            )

    def test_explicit_restore_repairs_corrupted_projection_with_exact_counts(self):
        # 损坏其中一条投影：损坏记录核验 2 次（发现不匹配 + 写后重读），
        # 其他正确记录仍各核验 1 次；损坏内容最终恢复为规范投影。
        records = self._settle_records(3, "corrupt")
        counted = MemoryManager(
            _make_config(str(self.workspace)), embedding_provider=None
        )
        active = counted.governance_repository.list_active_records(_TENANT_ID)
        self.assertEqual(len(active), 3)
        victim = active[0]
        projection_path = counted._governed_projection_path(victim.memory_id)
        canonical_content = projection_path.read_text(encoding="utf-8")
        projection_path.write_text("被破坏的投影内容\n", encoding="utf-8")

        counts = {}
        real_matches = counted._governed_projection_matches

        def counting_matches(record):
            # test double：仅计数，透传真实实现；不用于性能证明。
            counts[record.memory_id] = counts.get(record.memory_id, 0) + 1
            return real_matches(record)

        counted._governed_projection_matches = counting_matches
        try:
            counted._restore_governed_runtime()
            victim_repaired = real_matches(victim)
        finally:
            del counted._governed_projection_matches
            counted.close()

        self.assertEqual(counts.get(victim.memory_id), 2)
        intact = [r for r in records if r.memory_id != victim.memory_id]
        for record in intact:
            self.assertEqual(
                counts.get(record.memory_id),
                1,
                f"未损坏记录应恰好核验一次：{record.memory_id} 实际 "
                f"{counts.get(record.memory_id)} 次",
            )
        self.assertTrue(victim_repaired, "损坏投影必须在恢复后与事实一致")
        self.assertEqual(
            projection_path.read_text(encoding="utf-8"),
            canonical_content,
            "损坏投影必须恢复为规范投影内容",
        )

    def test_parallel_restore_surfaces_first_write_failure_and_reconverges(self):
        # 多记录并行恢复中注入第一条写入失败：异常必须向调用方暴露；
        # derivative jobs 不得被错误标记完成；取消故障注入后再次
        # restore 必须全部收敛。
        manager = MemoryManager(
            _make_config(str(self.workspace)), embedding_provider=None
        )
        records = _write_records(manager, 4, "fault")
        self.assertEqual(
            manager.governance_repository.count_derivative_jobs(_TENANT_ID),
            4,
            "写入后应留下 4 条待收敛派生任务",
        )

        original_write = manager._write_governed_projection
        injected = {"failed": False}

        def faulty_first_write(record):
            # test double：仅对第一次写入注入失败，之后透传真实实现。
            if not injected["failed"]:
                injected["failed"] = True
                raise RuntimeError("injected: first projection write fails")
            return original_write(record)

        manager._write_governed_projection = faulty_first_write
        try:
            with self.assertRaises(RuntimeError):
                manager._restore_governed_runtime()
        finally:
            del manager._write_governed_projection

        self.assertEqual(
            manager.governance_repository.count_derivative_jobs(_TENANT_ID),
            4,
            "恢复失败后派生任务不得被错误标记完成",
        )

        manager._restore_governed_runtime()
        active = manager.governance_repository.list_active_records(_TENANT_ID)
        self.assertEqual(len(active), 4)
        for record in active:
            self.assertTrue(
                manager._governed_projection_matches(record),
                f"去故障后必须全部收敛：{record.memory_id}",
            )
        self.assertEqual(
            manager.governance_repository.count_derivative_jobs(_TENANT_ID),
            0,
            "收敛完成后派生任务应全部清空",
        )
        manager.close()


if __name__ == "__main__":
    unittest.main()
