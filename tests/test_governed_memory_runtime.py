"""治理记忆运行时、工具和文件边界的端到端测试。"""

import asyncio
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from agent.memory.config import MemoryConfig
from agent.memory.governance import (
    IdentityContext,
    GovernedMemoryRepository,
    GovernedMemoryService,
    MemoryNotFoundError,
    MemoryScope,
    MemoryWriteCommand,
    ValidationError,
)
from agent.memory.manager import MemoryManager
from agent.tools.edit.edit import Edit
from agent.tools.memory.memory_get import MemoryGetTool
from agent.tools.memory.memory_lifecycle import MemoryWriteTool
from agent.tools.read.read import Read
from agent.tools.search_files.search_files import SearchFiles
from agent.tools.write.write import Write


class GovernedMemoryRuntimeTest(unittest.TestCase):
    """验证事实库到派生投影和检索结果的完整生命周期。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.config = MemoryConfig(
            workspace_root=str(self.workspace),
            enable_governed_retrieval=True,
            tenant_id="tenant-runtime",
        )
        self.manager = MemoryManager(self.config, embedding_provider=None)
        self.alice = self._identity("alice")
        self.bob = self._identity("bob")

    def tearDown(self):
        if self.manager is not None:
            self.manager.close()
        self.temp_dir.cleanup()

    def _identity(self, user_id: str) -> IdentityContext:
        return IdentityContext(
            tenant_id="tenant-runtime",
            actor_user_id=user_id,
            roles=frozenset(),
            trace_id="trace-%s" % user_id,
            auth_source="runtime-test",
        )

    @staticmethod
    def _command(
        content: str,
        key: str,
        memory_id=None,
        scope=MemoryScope.USER,
        session_id=None,
    ) -> MemoryWriteCommand:
        return MemoryWriteCommand(
            content=content,
            scope=scope,
            source_type="conversation",
            source_ref="session:test#message:1",
            idempotency_key=key,
            memory_id=memory_id,
            session_id=session_id,
            metadata={"title": "回答语言偏好"},
        )

    def _search(self, query: str, user_id: str, session_id=None):
        return asyncio.run(
            self.manager.search(
                query,
                user_id=user_id,
                session_id=session_id,
                min_score=0,
            )
        )

    def _pending_derivative_jobs(self) -> int:
        db_path = self.workspace / "memory" / "long-term" / "governance.db"
        conn = sqlite3.connect(str(db_path))
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM governed_memory_derivative_jobs"
                ).fetchone()[0]
            )
        finally:
            conn.close()

    def test_write_update_revoke_and_rollback_refresh_all_derivatives(self):
        first = self.manager.remember(
            self.alice,
            self._command("用户偏好所有回答使用简体中文。", "write-1"),
        )
        projection = self.manager._governed_projection_path(first.memory_id)
        self.assertTrue(projection.exists())
        self.assertIn(first.content_hash, projection.read_text(encoding="utf-8"))
        self.assertTrue(self._search("用户偏好什么回答语言", "alice"))
        self.assertFalse(self._search("用户偏好什么回答语言", "bob"))

        second = self.manager.remember(
            self.alice,
            self._command(
                "用户偏好所有回答使用英语。",
                "write-2",
                memory_id=first.memory_id,
            ),
        )
        self.assertEqual(2, second.version)
        self.assertIn("英语", projection.read_text(encoding="utf-8"))
        self.assertNotIn("简体中文", projection.read_text(encoding="utf-8"))

        revoked = self.manager.revoke(
            self.alice,
            first.memory_id,
            "revoke-1",
            "用户要求忘记",
        )
        self.assertEqual("revoked", revoked.status.value)
        self.assertFalse(projection.exists())
        self.assertFalse(self._search("用户偏好所有回答使用英语", "alice"))

        restored = self.manager.rollback(
            self.alice,
            first.memory_id,
            target_version=1,
            idempotency_key="rollback-1",
            reason="用户明确要求恢复第一版",
        )
        self.assertEqual(4, restored.version)
        self.assertTrue(projection.exists())
        results = self._search("用户偏好所有回答使用简体中文", "alice")
        self.assertTrue(any(result.path == f"governed://{first.memory_id}" for result in results))

    def test_restart_rebuilds_deleted_projection_and_lexical_database(self):
        record = self.manager.remember(
            self.alice,
            self._command("发布会的内部代号是长城计划。", "restart-write"),
        )
        projection = self.manager._governed_projection_path(record.memory_id)
        self.manager.close()
        self.manager = None

        projection.unlink()
        for suffix in ("", "-wal", "-shm"):
            db_file = self.workspace / "memory" / "long-term" / (
                "retrieval-v2.db" + suffix
            )
            if db_file.exists():
                db_file.unlink()

        self.manager = MemoryManager(self.config, embedding_provider=None)

        self.assertTrue(projection.exists())
        self.assertTrue(self._search("发布会内部代号是什么", "alice"))

    def test_idempotent_retry_repairs_projection_after_sync_failure(self):
        command = self._command(
            "用户的内部项目代号是远山计划。",
            "projection-repair",
        )
        original_write = self.manager._write_governed_projection

        with mock.patch.object(
            self.manager,
            "_write_governed_projection",
            side_effect=OSError("注入投影写入故障"),
        ):
            with self.assertRaisesRegex(OSError, "注入投影写入故障"):
                self.manager.remember(self.alice, command)

        records = self.manager.governance_repository.list_active_records(
            self.config.tenant_id
        )
        self.assertEqual(1, len(records))
        self.assertEqual(1, self._pending_derivative_jobs())

        with mock.patch.object(
            self.manager,
            "_write_governed_projection",
            side_effect=original_write,
        ):
            repaired = self.manager.remember(self.alice, command)

        self.assertEqual(1, repaired.version)
        self.assertEqual(0, self._pending_derivative_jobs())
        projection = self.manager._governed_projection_path(repaired.memory_id)
        self.assertIn("远山计划", projection.read_text(encoding="utf-8"))
        self.assertTrue(self._search("内部项目代号", "alice"))

    def test_index_noop_keeps_job_until_idempotent_retry_repairs_it(self):
        command = self._command(
            "用户的差旅审批编号是 TRAVEL-4821。",
            "index-noop-repair",
        )

        with mock.patch.object(
            self.manager.lexical_index,
            "index_documents",
            return_value=None,
        ):
            with self.assertRaisesRegex(RuntimeError, "索引写入后缺失"):
                self.manager.remember(self.alice, command)

        self.assertEqual(1, self._pending_derivative_jobs())
        repaired = self.manager.remember(self.alice, command)

        self.assertEqual(1, repaired.version)
        self.assertEqual(0, self._pending_derivative_jobs())
        self.assertTrue(self._search("TRAVEL-4821", "alice"))

    def test_revoke_noop_keeps_job_and_retry_removes_stale_index(self):
        record = self.manager.remember(
            self.alice,
            self._command("一次性授权码是 ORBIT-7392。", "revoke-noop-write"),
        )

        with mock.patch.object(
            self.manager.lexical_index,
            "delete_document",
            return_value=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "撤销索引删除后仍有残留"):
                self.manager.revoke(
                    self.alice,
                    record.memory_id,
                    "revoke-noop",
                    "授权已失效",
                )

        self.assertEqual(1, self._pending_derivative_jobs())
        self.assertFalse(self._search("ORBIT-7392", "alice"))

        replayed = self.manager.revoke(
            self.alice,
            record.memory_id,
            "revoke-noop",
            "授权已失效",
        )
        self.assertEqual("revoked", replayed.status.value)
        self.assertEqual(0, self._pending_derivative_jobs())
        self.assertFalse(
            self.manager.lexical_index.contains_document(
                self.config.tenant_id, record.memory_id
            )
        )

    def test_rollback_failure_keeps_job_and_retry_projects_restored_version(self):
        first = self.manager.remember(
            self.alice,
            self._command("回滚目标内容 V1。", "rollback-v1"),
        )
        self.manager.remember(
            self.alice,
            self._command(
                "回滚前内容 V2。",
                "rollback-v2",
                memory_id=first.memory_id,
            ),
        )

        with mock.patch.object(
            self.manager,
            "_write_governed_projection",
            side_effect=OSError("注入回滚投影故障"),
        ):
            with self.assertRaisesRegex(OSError, "注入回滚投影故障"):
                self.manager.rollback(
                    self.alice,
                    first.memory_id,
                    1,
                    "rollback-repair",
                    "恢复第一版",
                )

        self.assertEqual(1, self._pending_derivative_jobs())
        self.assertFalse(self._search("回滚前内容 V2", "alice"))
        replayed = self.manager.rollback(
            self.alice,
            first.memory_id,
            1,
            "rollback-repair",
            "恢复第一版",
        )

        self.assertEqual(3, replayed.version)
        self.assertEqual(0, self._pending_derivative_jobs())
        projection = self.manager._governed_projection_path(first.memory_id)
        projection_text = projection.read_text(encoding="utf-8")
        self.assertIn("回滚目标内容 V1", projection_text)
        self.assertNotIn("回滚前内容 V2", projection_text)

    def test_restart_rebuild_clears_committed_pending_job(self):
        command = self._command(
            "重启恢复探针是 RESTART-6153。",
            "restart-outbox",
        )
        with mock.patch.object(
            self.manager,
            "_write_governed_projection",
            side_effect=OSError("注入进程中断前故障"),
        ):
            with self.assertRaisesRegex(OSError, "注入进程中断前故障"):
                self.manager.remember(self.alice, command)

        record = self.manager.governance_repository.list_active_records(
            self.config.tenant_id
        )[0]
        self.assertEqual(1, self._pending_derivative_jobs())
        self.manager.close()
        self.manager = MemoryManager(self.config, embedding_provider=None)

        self.assertEqual(0, self._pending_derivative_jobs())
        projection = self.manager._governed_projection_path(record.memory_id)
        self.assertTrue(projection.exists())
        self.assertTrue(self._search("RESTART-6153", "alice"))

    def test_restart_index_noop_keeps_committed_pending_job(self):
        command = self._command(
            "批量恢复静默故障探针是 REBUILD-4286。",
            "restart-index-noop",
        )
        with mock.patch.object(
            self.manager,
            "_write_governed_projection",
            side_effect=OSError("注入重启前投影故障"),
        ):
            with self.assertRaisesRegex(OSError, "注入重启前投影故障"):
                self.manager.remember(self.alice, command)

        self.assertEqual(1, self._pending_derivative_jobs())
        with mock.patch.object(
            self.manager.lexical_index,
            "replace_collection",
            return_value=None,
        ):
            with self.assertRaisesRegex(RuntimeError, "索引重建后缺失"):
                self.manager._restore_governed_runtime()

        self.assertEqual(1, self._pending_derivative_jobs())
        self.manager._restore_governed_runtime()
        self.assertEqual(0, self._pending_derivative_jobs())
        self.assertTrue(self._search("REBUILD-4286", "alice"))

    def test_slow_derivative_io_does_not_hold_fact_database_write_lock(self):
        self.manager.governance_service.write(
            self.alice,
            self._command("慢派生恢复中的第一条事实。", "slow-derive-first"),
        )
        derive_started = threading.Event()
        allow_derive = threading.Event()
        original_write = self.manager._write_governed_projection
        calls = {"count": 0}
        errors = []

        def blocking_write(record):
            calls["count"] += 1
            if calls["count"] == 1:
                derive_started.set()
                if not allow_derive.wait(timeout=10.0):
                    raise TimeoutError("等待慢派生测试放行超时")
            original_write(record)

        def restore():
            try:
                self.manager._restore_governed_runtime()
            except Exception as error:
                errors.append(error)

        with mock.patch.object(
            self.manager,
            "_write_governed_projection",
            side_effect=blocking_write,
        ):
            thread = threading.Thread(target=restore)
            thread.start()
            self.assertTrue(derive_started.wait(timeout=5.0))
            repository = GovernedMemoryRepository(
                self.config.get_governance_db_path()
            )
            try:
                service = GovernedMemoryService(repository)
                started = time.perf_counter()
                service.write(
                    self.alice,
                    self._command(
                        "慢派生期间仍可提交第二条事实。",
                        "slow-derive-second",
                    ),
                )
                elapsed = time.perf_counter() - started
            finally:
                repository.close()
                allow_derive.set()
            thread.join(timeout=15.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(0, self._pending_derivative_jobs())

    def test_restore_purges_untrusted_legacy_flat_projections(self):
        record = self.manager.remember(
            self.alice,
            self._command("事实源中的有效投影。", "legacy-purge"),
        )
        projection_root = self.workspace / "memory" / ".governed"
        legacy_projection = projection_root / (record.memory_id + ".md")
        legacy_temp = projection_root / ".interrupted.tmp"
        legacy_projection.write_text("篡改的旧版无租户投影", encoding="utf-8")
        legacy_temp.write_text("中断残留", encoding="utf-8")

        self.manager._restore_governed_runtime()

        self.assertFalse(legacy_projection.exists())
        self.assertFalse(legacy_temp.exists())
        current_projection = self.manager._governed_projection_path(
            record.memory_id
        )
        self.assertTrue(current_projection.exists())
        self.assertIn(
            record.content_hash,
            current_projection.read_text(encoding="utf-8"),
        )

    def test_replaying_old_write_keeps_latest_projection_and_index(self):
        first_command = self._command("用户偏好绿色。", "old-write")
        first = self.manager.remember(self.alice, first_command)
        latest = self.manager.remember(
            self.alice,
            self._command(
                "用户偏好蓝色。",
                "new-write",
                memory_id=first.memory_id,
            ),
        )

        replayed = self.manager.remember(self.alice, first_command)

        self.assertEqual(1, replayed.version)
        self.assertEqual(2, latest.version)
        self.assertEqual(0, self._pending_derivative_jobs())
        projection = self.manager._governed_projection_path(first.memory_id)
        projection_text = projection.read_text(encoding="utf-8")
        self.assertIn("用户偏好蓝色", projection_text)
        self.assertNotIn("用户偏好绿色", projection_text)
        self.assertTrue(self._search("用户偏好蓝色", "alice"))

    def test_ack_failure_keeps_job_after_derivatives_are_written(self):
        command = self._command(
            "完成确认故障探针是 ACK-9047。",
            "ack-failure",
        )
        with mock.patch.object(
            self.manager.governance_repository,
            "complete_derivative_job",
            return_value=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "完成标记发生并发冲突"):
                self.manager.remember(self.alice, command)

        records = self.manager.governance_repository.list_active_records(
            self.config.tenant_id
        )
        self.assertEqual(1, len(records))
        self.assertEqual(1, self._pending_derivative_jobs())
        self.assertEqual(
            1,
            self.manager.get_status()["governed_memory_pending_derivatives"],
        )
        projection = self.manager._governed_projection_path(
            records[0].memory_id
        )
        self.assertIn("ACK-9047", projection.read_text(encoding="utf-8"))

        replayed = self.manager.remember(self.alice, command)
        self.assertEqual(1, replayed.version)
        self.assertEqual(0, self._pending_derivative_jobs())

    def test_two_runtime_drainers_converge_on_latest_version(self):
        second_manager = MemoryManager(self.config, embedding_provider=None)
        try:
            first_command = self._command("并发投影版本一。", "runtime-v1")
            first = self.manager.governance_service.write(
                self.alice, first_command
            )
            latest = second_manager.governance_service.write(
                self.alice,
                self._command(
                    "并发投影版本二。",
                    "runtime-v2",
                    memory_id=first.memory_id,
                ),
            )
            self.assertEqual(2, latest.version)
            self.assertEqual(1, self._pending_derivative_jobs())

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        manager._drain_governed_derivative_job,
                        self.config.tenant_id,
                        first.memory_id,
                    )
                    for manager in (self.manager, second_manager)
                ]
                for future in futures:
                    future.result()

            self.assertEqual(0, self._pending_derivative_jobs())
            projection = self.manager._governed_projection_path(
                first.memory_id
            )
            projection_text = projection.read_text(encoding="utf-8")
            self.assertIn("并发投影版本二", projection_text)
            self.assertNotIn("并发投影版本一", projection_text)
            self.assertTrue(
                second_manager.lexical_index.matches_document(
                    second_manager._governed_index_document(latest)
                )
            )
        finally:
            second_manager.close()

    def test_derivative_jobs_are_isolated_by_tenant(self):
        other_config = MemoryConfig(
            workspace_root=str(self.workspace),
            enable_governed_retrieval=True,
            tenant_id="tenant-other",
        )
        other_manager = MemoryManager(other_config, embedding_provider=None)
        other_identity = IdentityContext(
            tenant_id="tenant-other",
            actor_user_id="alice",
            roles=frozenset(),
            trace_id="trace-other-alice",
            auth_source="runtime-test",
        )
        try:
            first = self.manager.governance_service.write(
                self.alice,
                self._command("主租户记忆。", "tenant-primary"),
            )
            other = other_manager.governance_service.write(
                other_identity,
                self._command("其他租户记忆。", "tenant-other"),
            )
            self.assertEqual(2, self._pending_derivative_jobs())

            self.manager._drain_governed_derivative_job(
                self.config.tenant_id, first.memory_id
            )
            self.assertEqual(1, self._pending_derivative_jobs())
            other_manager._drain_governed_derivative_job(
                other_config.tenant_id, other.memory_id
            )
            self.assertEqual(0, self._pending_derivative_jobs())
            self.assertTrue(
                self.manager.lexical_index.contains_document(
                    self.config.tenant_id, first.memory_id
                )
            )
            self.assertTrue(
                other_manager.lexical_index.contains_document(
                    other_config.tenant_id, other.memory_id
                )
            )
        finally:
            other_manager.close()

    def test_starting_other_tenant_keeps_existing_projection(self):
        first = self.manager.remember(
            self.alice,
            self._command("主租户投影不能被其他租户删除。", "tenant-projection"),
        )
        first_projection = self.manager._governed_projection_path(
            first.memory_id
        )
        other_config = MemoryConfig(
            workspace_root=str(self.workspace),
            enable_governed_retrieval=True,
            tenant_id="tenant-other",
        )

        other_manager = MemoryManager(other_config, embedding_provider=None)
        try:
            self.assertTrue(first_projection.exists())
            self.assertNotEqual(
                self.manager._governed_projection_dir(),
                other_manager._governed_projection_dir(),
            )
            self.assertTrue(
                self.manager.lexical_index.contains_document(
                    self.config.tenant_id, first.memory_id
                )
            )
        finally:
            other_manager.close()

    def test_runtime_rejects_identity_from_other_tenant(self):
        other_identity = IdentityContext(
            tenant_id="tenant-other",
            actor_user_id="alice",
            roles=frozenset(),
            trace_id="trace-tenant-mismatch",
            auth_source="runtime-test",
        )

        with self.assertRaisesRegex(
            ValidationError, "身份租户与记忆运行时租户不一致"
        ):
            self.manager.remember(
                other_identity,
                self._command("不得写入错配租户。", "tenant-mismatch"),
            )

        self.assertEqual(
            [],
            self.manager.governance_repository.list_active_records(
                "tenant-other"
            ),
        )

    def test_two_processes_converge_after_committing_before_drain(self):
        first = self.manager.remember(
            self.alice,
            self._command("多进程初始版本。", "process-v1"),
        )
        self.manager.close()
        self.manager = None
        fixture = Path(__file__).parent / "fixtures" / "memory_outbox_worker.py"
        go_path = self.workspace / "workers.go"
        processes = []
        ready_paths = []
        for ordinal in (1, 2):
            ready_path = self.workspace / ("worker-%d.ready" % ordinal)
            ready_paths.append(ready_path)
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(fixture),
                        str(self.workspace),
                        self.config.tenant_id,
                        first.memory_id,
                        "多进程版本 %d。" % ordinal,
                        "process-v%d" % (ordinal + 1),
                        str(ready_path),
                        str(go_path),
                    ],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        try:
            deadline = time.monotonic() + 30.0
            while not all(path.exists() for path in ready_paths):
                if any(process.poll() is not None for process in processes):
                    details = []
                    for process in processes:
                        if process.poll() is None:
                            continue
                        stdout, stderr = process.communicate()
                        details.append(
                            (stdout + stderr).decode("utf-8", errors="replace")
                        )
                    self.fail(
                        "多进程事实提交前子进程退出:\n%s"
                        % "\n".join(details)
                    )
                if time.monotonic() >= deadline:
                    self.fail("等待多进程事实提交超时")
                time.sleep(0.05)
            go_path.write_text("go", encoding="utf-8")

            outputs = [process.communicate(timeout=30) for process in processes]
            for process, (stdout, stderr) in zip(processes, outputs):
                details = (stdout + stderr).decode("utf-8", errors="replace")
                self.assertEqual(0, process.returncode, details)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

        self.manager = MemoryManager(self.config, embedding_provider=None)
        latest = self.manager.governance_repository.read_latest(
            self.config.tenant_id, first.memory_id
        )
        self.assertEqual(3, latest.version)
        self.assertEqual(0, self._pending_derivative_jobs())
        projection = self.manager._governed_projection_path(first.memory_id)
        projection_text = projection.read_text(encoding="utf-8")
        self.assertIn(latest.content, projection_text)
        self.assertTrue(
            self.manager.lexical_index.matches_document(
                self.manager._governed_index_document(latest)
            )
        )

    def test_fact_source_check_filters_stale_index_after_delete_failure(self):
        record = self.manager.remember(
            self.alice,
            self._command("临时访问口令是星河九号。", "stale-write"),
        )
        with mock.patch.object(
            self.manager.lexical_index,
            "delete_document",
            side_effect=RuntimeError("simulated index failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.manager.revoke(
                    self.alice,
                    record.memory_id,
                    "stale-revoke",
                    "立即撤销",
                )

        with self.assertRaises(MemoryNotFoundError):
            self.manager.get_governed_memory(self.alice, record.memory_id)
        self.assertFalse(self._search("临时访问口令是什么", "alice"))

    def test_memory_tools_use_injected_identity_and_stable_idempotency(self):
        write_tool = MemoryWriteTool(self.manager, self.alice, session_id="session-a")
        args = {
            "content": "Alice 偏好低糖饮品。",
            "source_ref": "session:session-a#message:8",
            "evidence_quote": "我喜欢低糖饮品",
        }
        first = write_tool.execute(args)
        second = write_tool.execute(args)

        self.assertEqual("success", first.status, first.result)
        self.assertEqual(first.result, second.result)
        memory_id = first.result["memory_id"]
        self.assertEqual(
            1,
            len(self.manager.governance_service.list_versions(self.alice, memory_id)),
        )

        alice_get = MemoryGetTool(
            self.manager,
            identity=self.alice,
            session_id="session-a",
        ).execute({"memory_id": memory_id})
        bob_get = MemoryGetTool(
            self.manager,
            identity=self.bob,
            session_id="session-b",
        ).execute({"memory_id": memory_id})
        self.assertEqual("success", alice_get.status)
        self.assertIn("低糖饮品", alice_get.result)
        self.assertEqual("error", bob_get.status)

    def test_generic_file_tools_cannot_bypass_governance(self):
        record = self.manager.remember(
            self.alice,
            self._command("受保护的治理记忆正文。", "guard-write"),
        )
        projection = self.manager._governed_projection_path(record.memory_id)
        config = {"cwd": str(self.workspace), "memory_manager": self.manager}

        read_result = Read(config).execute({"path": str(projection)})
        write_result = Write(config).execute(
            {"path": "MEMORY.md", "content": "绕过治理"}
        )
        edit_result = Edit(config).execute(
            {"path": "MEMORY.md", "oldText": "", "newText": "绕过治理"}
        )
        direct_get = MemoryGetTool(
            self.manager,
            identity=self.alice,
        ).execute({"path": str(projection)})
        direct_search = SearchFiles(config).execute(
            {"path": str(projection), "pattern": "受保护"}
        )
        workspace_search = SearchFiles(config).execute(
            {"pattern": "受保护的治理记忆正文", "no_ignore": True}
        )

        self.assertEqual("error", read_result.status)
        self.assertEqual("error", write_result.status)
        self.assertEqual("error", edit_result.status)
        self.assertEqual("error", direct_get.status)
        self.assertEqual("error", direct_search.status)
        self.assertEqual(
            0,
            workspace_search.result["match_count"],
            msg=f"governed content leaked into generic search: {workspace_search.result}",
        )


class GovernedMemoryToolInjectionTest(unittest.TestCase):
    """验证初始化器不会遗漏任何治理生命周期工具。"""

    def test_initializer_injects_memory_and_knowledge_tools(self):
        from bridge.agent_initializer import AgentInitializer

        with tempfile.TemporaryDirectory() as workspace:
            initializer = AgentInitializer(bridge=None, agent_bridge=None)
            with mock.patch.object(
                initializer,
                "_init_embedding_provider",
                return_value=None,
            ):
                manager, tools = initializer._setup_memory_system(
                    workspace,
                    session_id="alice-session",
                )
            try:
                self.assertEqual(
                    {
                        "memory_search",
                        "memory_get",
                        "memory_write",
                        "memory_revoke",
                        "memory_rollback",
                        "knowledge_search",
                        "knowledge_get",
                        "knowledge_write",
                        "knowledge_revoke",
                        "knowledge_rollback",
                    },
                    {tool.name for tool in tools},
                )
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
