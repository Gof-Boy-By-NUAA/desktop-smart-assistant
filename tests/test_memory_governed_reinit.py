"""治理索引重建校验范围的回归测试（真实链路）。

评审缺陷 P1-1：首次同步成功后，再次初始化 MemoryManager 时，
_restore_governed_runtime 用整个租户的索引校验 governed 集合，
导致正常存在的 workspace 记录被判为异常并抛 RuntimeError。

本文件全部使用真实 MemoryManager、真实 SQLite 检索库与真实临时
工作区文件，不使用任何测试替身。
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from agent.memory.config import MemoryConfig
from agent.memory.manager import MemoryManager


def _make_config(workspace_root: str) -> MemoryConfig:
    return MemoryConfig(
        workspace_root=workspace_root,
        enable_governed_retrieval=True,
        tenant_id="tenant-reinit",
    )


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


if __name__ == "__main__":
    unittest.main()
