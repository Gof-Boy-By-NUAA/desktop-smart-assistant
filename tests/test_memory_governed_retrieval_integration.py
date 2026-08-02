"""MemoryManager 与租户化词法索引的集成测试。"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from agent.memory.config import MemoryConfig
from agent.memory.manager import MemoryManager
from agent.memory.storage import MemoryChunk, MemoryStorage


class MemoryGovernedRetrievalIntegrationTest(unittest.TestCase):
    """验证现有记忆接口保持兼容并使用新版关键词检索。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.manager = MemoryManager(
            MemoryConfig(
                workspace_root=str(self.workspace),
                enable_governed_retrieval=True,
                tenant_id="tenant-test",
            ),
            embedding_provider=None,
        )

    def tearDown(self):
        self.manager.close()
        self.temp_dir.cleanup()

    def test_shared_memory_uses_improved_chinese_retrieval(self):
        memory_file = self.workspace / "MEMORY.md"
        memory_file.write_text(
            "# 长期记忆\n北京是中华人民共和国的首都，也是全国政治中心。\n",
            encoding="utf-8",
        )
        asyncio.run(self.manager.sync(force=True))

        results = asyncio.run(
            self.manager.search("中国的首都和政治中心在哪里？", max_results=5)
        )

        self.assertTrue(results)
        self.assertEqual("MEMORY.md", results[0].path)
        self.assertGreater(results[0].score, 0.1)
        self.assertTrue(self.manager.get_status()["governed_retrieval_enabled"])

    def test_user_memory_is_filtered_by_session_identity(self):
        user_dir = self.workspace / "memory" / "users" / "alice"
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "preferences.md").write_text(
            "用户偏好所有回答都使用简体中文。",
            encoding="utf-8",
        )
        asyncio.run(self.manager.sync(force=True))

        alice_results = asyncio.run(
            self.manager.search(
                "用户偏好使用什么语言回答？",
                user_id="alice",
                session_id="alice",
            )
        )
        bob_results = asyncio.run(
            self.manager.search(
                "用户偏好使用什么语言回答？",
                user_id="bob",
                session_id="bob",
            )
        )

        self.assertTrue(any("preferences.md" in result.path for result in alice_results))
        self.assertFalse(any("preferences.md" in result.path for result in bob_results))

    def test_deleted_file_is_removed_from_new_and_legacy_indexes(self):
        memory_file = self.workspace / "MEMORY.md"
        memory_file.write_text(
            "一次性发布暗号是北斗七星。",
            encoding="utf-8",
        )
        asyncio.run(self.manager.sync(force=True))
        self.assertTrue(
            asyncio.run(self.manager.search("一次性发布暗号是什么？"))
        )

        memory_file.unlink()
        asyncio.run(self.manager.sync(force=True))

        self.assertEqual(
            [],
            asyncio.run(self.manager.search("一次性发布暗号是什么？")),
        )


def test_manager_startup_synchronously_purges_legacy_knowledge_index(tmp_path):
    config = MemoryConfig(
        workspace_root=str(tmp_path),
        enable_governed_retrieval=False,
        tenant_id="tenant-test",
    )
    storage = MemoryStorage(config.get_db_path())
    storage.save_chunks_batch(
        [
            MemoryChunk(
                id="legacy-knowledge-chunk",
                user_id=None,
                scope="shared",
                source="knowledge",
                path="knowledge/private.md",
                start_line=1,
                end_line=1,
                text="legacy-knowledge-leak-canary-8427",
                embedding=None,
                hash="legacy-hash",
            )
        ]
    )
    storage.update_file_metadata(
        "knowledge/private.md", "knowledge", "file-hash", 1, 32
    )
    storage.close()

    manager = MemoryManager(config, embedding_provider=None)
    try:
        assert manager.storage.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE source = 'knowledge'"
        ).fetchone()[0] == 0
        assert manager.storage.conn.execute(
            "SELECT COUNT(*) FROM files WHERE source = 'knowledge'"
        ).fetchone()[0] == 0
        assert asyncio.run(
            manager.search("legacy-knowledge-leak-canary-8427", max_results=5)
        ) == []
    finally:
        manager.close()


if __name__ == "__main__":
    unittest.main()
