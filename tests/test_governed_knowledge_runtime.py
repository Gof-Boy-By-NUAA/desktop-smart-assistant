import hashlib
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from agent.knowledge import GovernedKnowledgeRuntime, KnowledgeWriteCommand
from agent.knowledge.contracts import (
    KnowledgeAuthorizationError,
    KnowledgeCitationIntegrityError,
    KnowledgeCitationVersionError,
    KnowledgeIdempotencyConflictError,
    KnowledgeValidationError,
)
from agent.knowledge.repository import KnowledgeRepository
from agent.memory.governance import IdentityContext, MemoryScope, Sensitivity
from agent.retrieval import TenantAwareLexicalIndex
from agent.tools.edit.edit import Edit
from agent.tools.bash.bash import Bash
from agent.tools.read.read import Read
from agent.tools.search_files.search_files import SearchFiles
from agent.tools.write.write import Write
from agent.tools.knowledge import (
    KnowledgeGetTool,
    KnowledgeSearchTool,
    KnowledgeWriteTool,
)


def identity(user_id="alice", roles=()):
    return IdentityContext(
        tenant_id="tenant-local",
        actor_user_id=user_id,
        roles=frozenset(roles),
        trace_id="trace-%s" % user_id,
        auth_source="test-session",
    )


def command(
    content,
    key,
    document_id=None,
    path="notes/test.md",
    collection_id="notes",
    scope=MemoryScope.USER,
    sensitivity=Sensitivity.PRIVATE,
    source_ref=None,
):
    return KnowledgeWriteCommand(
        content=content,
        title="测试文档",
        source_ref=source_ref or "knowledge/%s" % path,
        collection_id=collection_id,
        idempotency_key=key,
        document_id=document_id,
        projection_path=path,
        scope=scope,
        sensitivity=sensitivity,
    )


def projected_command(*args, **kwargs):
    """构造允许生成明文兼容投影的共享知识命令。"""

    kwargs["scope"] = MemoryScope.SHARED
    kwargs["sensitivity"] = Sensitivity.INTERNAL
    return command(*args, **kwargs)


def unprojected_command(content, key, source_ref):
    """构造不生成兼容文件、可进入新文档批写路径的命令。"""

    return KnowledgeWriteCommand(
        content=content,
        title="批量知识",
        source_ref=source_ref,
        collection_id="batch",
        idempotency_key=key,
        scope=MemoryScope.USER,
        sensitivity=Sensitivity.PRIVATE,
    )


def projection_identity():
    """构造有权维护共享兼容投影的身份。"""

    return identity(
        "admin",
        ("admin", "knowledge:write_shared", "knowledge:manage"),
    )


def create_legacy_knowledge_documents_table(conn):
    """创建尚无 projection_key 的旧版事实表。"""

    conn.execute(
        """
        CREATE TABLE knowledge_documents (
            tenant_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL,
            scope TEXT NOT NULL,
            owner_user_id TEXT,
            session_id TEXT,
            sensitivity TEXT NOT NULL,
            collection_id TEXT NOT NULL,
            title TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            projection_path TEXT,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_by TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, document_id, version)
        )
        """
    )


def test_write_update_search_and_exact_utf8_citation(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        first = runtime.write(
            identity(),
            command("# 阀门\n关闭总阀后执行复位。", "write-v1"),
        )
        second = runtime.write(
            identity(),
            command(
                "# 阀门\n关闭总阀、挂牌并验压后执行复位。",
                "write-v2",
                document_id=first.document_id,
            ),
        )

        assert second.version == 2
        assert first.content_hash != second.content_hash
        assert runtime.get(identity(), first.document_id).version == 2
        assert runtime.get(identity(), first.document_id, version=1).content == first.content

        results = runtime.search(identity(), "挂牌并验压", limit=5)
        assert len(results) == 1
        citation = results[0].citation
        source_bytes = second.content.encode("utf-8")
        assert source_bytes[citation.byte_start:citation.byte_end].decode("utf-8") == citation.quote
        assert citation.document_version == 2
        assert citation.content_hash == second.content_hash
        assert citation.uri.startswith("knowledge://%s/v/2/" % first.document_id)
        assert not (tmp_path / "knowledge/notes/test.md").exists()

        with sqlite3.connect(str(tmp_path / "knowledge/.system/knowledge.db")) as conn:
            rows = conn.execute(
                "SELECT version, status FROM knowledge_documents ORDER BY version"
            ).fetchall()
        assert rows == [(1, "superseded"), (2, "active")]
    finally:
        runtime.close()


def test_idempotency_is_stable_and_conflicts_fail(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        original = command("# 内容\n同一请求", "same-key")
        first = runtime.write(identity(), original)
        repeated = runtime.write(identity(), original)
        assert repeated == first
        with pytest.raises(KnowledgeIdempotencyConflictError):
            runtime.write(identity(), command("# 内容\n不同请求", "same-key"))
    finally:
        runtime.close()


def test_replaying_old_idempotent_write_does_not_revert_active_projection(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    actor = projection_identity()
    try:
        original = projected_command("# 第一版\n旧正文", "version-one")
        first = runtime.write(actor, original)
        second = runtime.write(
            actor,
            projected_command(
                "# 第二版\n当前正文",
                "version-two",
                document_id=first.document_id,
            ),
        )

        replayed = runtime.write(actor, original)

        assert replayed.version == 1
        assert runtime.get(actor, first.document_id).version == second.version
        assert (tmp_path / "knowledge/notes/test.md").read_text(
            encoding="utf-8"
        ) == second.content
    finally:
        runtime.close()


def test_batch_write_rolls_back_every_document_on_idempotency_conflict(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        runtime.write(
            identity(),
            command("# 已存在\n原始请求", "used-key", path="notes/existing.md"),
        )

        with pytest.raises(KnowledgeIdempotencyConflictError):
            runtime.write_batch(
                identity(),
                [
                    command("# 批量一\n必须回滚", "batch-one", path="notes/one.md"),
                    command("# 冲突\n不同请求", "used-key", path="notes/two.md"),
                ],
            )

        assert runtime.find_by_source(identity(), "knowledge/notes/one.md") is None
        assert not (tmp_path / "knowledge/notes/one.md").exists()
        assert len(runtime.list_active(identity())) == 1
    finally:
        runtime.close()


def test_batch_write_commits_all_documents_and_rebuilds_derivatives(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    actor = projection_identity()
    try:
        records = runtime.write_batch(
            actor,
            [
                projected_command("# 批量甲\n标记 batch-alpha-431", "batch-a", path="batch/a.md"),
                projected_command("# 批量乙\n标记 batch-beta-762", "batch-b", path="batch/b.md"),
            ],
        )

        assert len(records) == 2
        assert (tmp_path / "knowledge/batch/a.md").is_file()
        assert (tmp_path / "knowledge/batch/b.md").is_file()
        assert runtime.search(actor, "batch-alpha-431", limit=5)
        assert runtime.search(actor, "batch-beta-762", limit=5)
    finally:
        runtime.close()


def test_unprojected_batch_pipeline_commits_facts_index_and_audit(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    actor = identity()
    try:
        records = runtime.write_batch(
            actor,
            [
                unprojected_command(
                    "# 批次甲\n原子流水线标记 pipeline-alpha-731",
                    "pipeline-a",
                    "external:pipeline-a",
                ),
                unprojected_command(
                    "# 批次乙\n原子流水线标记 pipeline-beta-842",
                    "pipeline-b",
                    "external:pipeline-b",
                ),
            ],
        )

        assert len(records) == 2
        assert runtime.search(actor, "pipeline-alpha-731", limit=5)
        with runtime.repository.connection() as conn:
            assert conn.execute("PRAGMA page_size").fetchone()[0] == 65536
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
            counts = {
                table: conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                for table in (
                    "knowledge_documents",
                    "knowledge_sections",
                    "knowledge_evidence",
                    "knowledge_audit",
                    "knowledge_idempotency",
                )
            }
            assert counts == {
                "knowledge_documents": 2,
                "knowledge_sections": 2,
                "knowledge_evidence": 2,
                "knowledge_audit": 2,
                "knowledge_idempotency": 2,
            }
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_derivative_jobs"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_derivative_batches"
            ).fetchone()[0] == 0
    finally:
        runtime.close()


def test_unprojected_batch_mapping_failure_rolls_back_facts(tmp_path, monkeypatch):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    actor = identity()
    try:
        monkeypatch.setattr(runtime.index, "_matches_tenant_rows", lambda *_: False)

        with pytest.raises(RuntimeError, match="内容集合"):
            runtime.write_batch(
                actor,
                [
                    unprojected_command(
                        "索引失败不得提交 index-failure-951",
                        "index-failure-a",
                        "external:index-failure-a",
                    ),
                    unprojected_command(
                        "索引失败不得提交 index-failure-962",
                        "index-failure-b",
                        "external:index-failure-b",
                    ),
                ],
            )

        assert runtime.list_active(actor) == []
        assert runtime.search(actor, "index-failure-951", limit=5) == []
        with runtime.repository.connection() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_derivative_batches"
            ).fetchone()[0] == 0
    finally:
        runtime.close()


def test_fact_commit_failure_filters_and_rebuilds_orphan_index(
    tmp_path, monkeypatch
):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    actor = identity()
    original_connect = runtime.repository._connect

    class CommitFailureConnection:
        """只在事实提交点注入失败，其余 SQLite 行为保持透明。"""

        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def commit(self):
            self._connection.rollback()
            raise sqlite3.OperationalError("注入事实提交失败")

    fail_next = {"value": True}

    def failing_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        if fail_next["value"]:
            fail_next["value"] = False
            return CommitFailureConnection(connection)
        return connection

    try:
        monkeypatch.setattr(runtime.repository, "_connect", failing_connect)
        with pytest.raises(sqlite3.OperationalError, match="注入事实提交失败"):
            runtime.write_batch(
                actor,
                [
                    unprojected_command(
                        "事实失败孤立索引 orphan-index-417",
                        "orphan-a",
                        "external:orphan-a",
                    ),
                    unprojected_command(
                        "事实失败孤立索引 orphan-index-528",
                        "orphan-b",
                        "external:orphan-b",
                    ),
                ],
            )

        assert runtime.list_active(actor) == []
        assert runtime.search(actor, "orphan-index-417", limit=5) == []
    finally:
        runtime.close()

    restarted = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        assert restarted.search(actor, "orphan-index-417", limit=5) == []
        assert restarted.index._conn.execute(
            "SELECT COUNT(*) FROM retrieval_documents"
        ).fetchone()[0] == 0
    finally:
        restarted.close()


def test_idempotent_retry_repairs_derivatives_after_sync_failure(tmp_path, monkeypatch):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    actor = projection_identity()
    write_command = projected_command(
        "# 自愈\n同步失败后恢复 repair-9581",
        "repair-after-sync-failure",
        path="repair/page.md",
    )
    original_write_projection = runtime._write_projection
    try:
        monkeypatch.setattr(
            runtime,
            "_write_projection",
            lambda _record: (_ for _ in ()).throw(OSError("注入投影故障")),
        )
        with pytest.raises(OSError, match="注入投影故障"):
            runtime.write(actor, write_command)
        with sqlite3.connect(str(tmp_path / "knowledge/.system/knowledge.db")) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_derivative_jobs"
            ).fetchone()[0] == 1

        monkeypatch.setattr(runtime, "_write_projection", original_write_projection)
        repaired = runtime.write(actor, write_command)

        assert repaired.version == 1
        assert (tmp_path / "knowledge/repair/page.md").read_text(
            encoding="utf-8"
        ) == write_command.content
        assert runtime.search(actor, "repair-9581", limit=5)
        with sqlite3.connect(str(tmp_path / "knowledge/.system/knowledge.db")) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_derivative_jobs"
            ).fetchone()[0] == 0
    finally:
        runtime.close()


def test_index_write_noop_keeps_derivative_job_for_retry(tmp_path, monkeypatch):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    write_command = command(
        "# 索引核验\n静默写入故障恢复 index-verify-8241",
        "index-write-noop",
        path="repair/index-noop.md",
    )
    original_index_documents = runtime.index.index_documents
    try:
        monkeypatch.setattr(runtime.index, "index_documents", lambda _documents: None)
        with pytest.raises(RuntimeError, match="写入后缺失或内容不一致"):
            runtime.write(identity(), write_command)

        with sqlite3.connect(str(tmp_path / "knowledge/.system/knowledge.db")) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_derivative_jobs"
            ).fetchone()[0] == 1
        assert runtime.search(identity(), "index-verify-8241", limit=5) == []

        monkeypatch.setattr(
            runtime.index, "index_documents", original_index_documents
        )
        repaired = runtime.write(identity(), write_command)

        assert repaired.version == 1
        assert runtime.search(identity(), "index-verify-8241", limit=5)
        with sqlite3.connect(str(tmp_path / "knowledge/.system/knowledge.db")) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_derivative_jobs"
            ).fetchone()[0] == 0
    finally:
        runtime.close()


def test_fts_trigger_noop_is_detected_and_repaired_on_restart(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    write_command = command(
        "# FTS 核验\n触发器故障恢复 fts-trigger-6412",
        "fts-trigger-noop",
        path="repair/fts-trigger.md",
    )
    try:
        runtime.index._conn.execute("DROP TRIGGER retrieval_documents_ai")
        runtime.index._conn.execute(
            "CREATE TRIGGER retrieval_documents_ai "
            "AFTER INSERT ON retrieval_documents BEGIN SELECT 1; END"
        )
        runtime.index._conn.commit()

        with pytest.raises(RuntimeError, match="写入后缺失或内容不一致"):
            runtime.write(identity(), write_command)

        with sqlite3.connect(str(tmp_path / "knowledge/.system/knowledge.db")) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_derivative_jobs"
            ).fetchone()[0] == 1
        assert runtime.search(identity(), "fts-trigger-6412", limit=5) == []
    finally:
        runtime.close()

    restarted = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        assert restarted.search(identity(), "fts-trigger-6412", limit=5)
        with sqlite3.connect(str(tmp_path / "knowledge/.system/knowledge.db")) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_derivative_jobs"
            ).fetchone()[0] == 0
    finally:
        restarted.close()


def test_startup_rebuild_noop_keeps_derivative_jobs(tmp_path, monkeypatch):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    deferred_command = command(
        "# 启动重建\n静默重建故障 startup-rebuild-7395",
        "startup-rebuild-noop",
        path="repair/startup-rebuild.md",
    )
    runtime.write(identity(), deferred_command, sync_derivatives=False)
    runtime.close()

    original_replace_tenant = TenantAwareLexicalIndex.replace_tenant
    monkeypatch.setattr(
        TenantAwareLexicalIndex,
        "replace_tenant",
        lambda _index, _tenant_id, _documents: None,
    )
    with pytest.raises(RuntimeError, match="重建后缺失"):
        GovernedKnowledgeRuntime(str(tmp_path))

    with sqlite3.connect(str(tmp_path / "knowledge/.system/knowledge.db")) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM knowledge_derivative_jobs"
        ).fetchone()[0] == 1

    monkeypatch.setattr(
        TenantAwareLexicalIndex,
        "replace_tenant",
        original_replace_tenant,
    )
    restarted = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        assert restarted.search(identity(), "startup-rebuild-7395", limit=5)
        with sqlite3.connect(str(tmp_path / "knowledge/.system/knowledge.db")) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_derivative_jobs"
            ).fetchone()[0] == 0
    finally:
        restarted.close()


def test_startup_rebuild_drains_deferred_derivative_job(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    actor = projection_identity()
    deferred_command = projected_command(
        "# 延迟同步\n启动恢复标记 startup-repair-673",
        "deferred-write",
        path="repair/startup.md",
    )
    runtime.write(actor, deferred_command, sync_derivatives=False)
    assert not (tmp_path / "knowledge/repair/startup.md").exists()
    runtime.close()

    restarted = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        assert (tmp_path / "knowledge/repair/startup.md").read_text(
            encoding="utf-8"
        ) == deferred_command.content
        assert restarted.search(actor, "startup-repair-673", limit=5)
        with sqlite3.connect(str(tmp_path / "knowledge/.system/knowledge.db")) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_derivative_jobs"
            ).fetchone()[0] == 0
    finally:
        restarted.close()


def test_concurrent_runtimes_cannot_leave_projection_on_old_version(tmp_path):
    first_runtime = GovernedKnowledgeRuntime(str(tmp_path))
    second_runtime = GovernedKnowledgeRuntime(str(tmp_path))
    actor = projection_identity()
    projection_started = threading.Event()
    release_projection = threading.Event()
    original_write_projection = first_runtime._write_projection
    thread_errors = []
    update_started = threading.Event()
    updated_records = []

    def delayed_projection(record):
        projection_started.set()
        assert release_projection.wait(timeout=5)
        original_write_projection(record)

    def write_first_version():
        try:
                first_runtime.write(
                    actor,
                    projected_command("# 版本一\n旧投影", "concurrent-v1"),
            )
        except Exception as error:
            thread_errors.append(error)

    def write_second_version(document_id):
        update_started.set()
        try:
            updated_records.append(
                second_runtime.write(
                    actor,
                    projected_command(
                        "# 版本二\n最终投影",
                        "concurrent-v2",
                        document_id=document_id,
                    ),
                )
            )
        except Exception as error:
            thread_errors.append(error)

    first_runtime._write_projection = delayed_projection
    worker = threading.Thread(target=write_first_version)
    try:
        worker.start()
        assert projection_started.wait(timeout=5)
        first = second_runtime.find_by_source(
            actor, "knowledge/notes/test.md"
        )
        assert first is not None
        updater = threading.Thread(
            target=write_second_version, args=(first.document_id,)
        )
        updater.start()
        assert update_started.wait(timeout=5)
        assert worker.is_alive()
        release_projection.set()
        worker.join(timeout=5)
        updater.join(timeout=5)

        assert not worker.is_alive()
        assert not updater.is_alive()
        assert thread_errors == []
        assert len(updated_records) == 1
        latest = updated_records[0]
        assert second_runtime.get(actor, first.document_id).version == 2
        assert (tmp_path / "knowledge/notes/test.md").read_text(
            encoding="utf-8"
        ) == latest.content
    finally:
        release_projection.set()
        worker.join(timeout=5)
        first_runtime.close()
        second_runtime.close()


def test_batch_rejects_projection_path_owned_by_different_documents(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        with pytest.raises(KnowledgeValidationError, match="projection_path"):
            runtime.write_batch(
                identity(),
                [
                    command(
                        "# 文档一\n来源一",
                        "projection-one",
                        path="shared/path.md",
                        source_ref="external:first",
                    ),
                    command(
                        "# 文档二\n来源二",
                        "projection-two",
                        path="shared/path.md",
                        source_ref="external:second",
                    ),
                ],
            )

        assert runtime.list_active(identity()) == []
        assert not (tmp_path / "knowledge/shared/path.md").exists()
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "path",
    [
        "INDEX.md",
        "notes/CON.md",
        "notes/trailing./page.md",
        "notes/bad:name.md",
    ],
)
def test_projection_rejects_cross_platform_filesystem_aliases(tmp_path, path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        with pytest.raises(KnowledgeValidationError, match="projection_path"):
            runtime.write(identity(), command("# 非法路径", "invalid-" + path, path=path))
    finally:
        runtime.close()


def test_batch_rejects_unicode_normalization_projection_alias(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        with pytest.raises(KnowledgeValidationError, match="projection_path"):
            runtime.write_batch(
                identity(),
                [
                    command(
                        "# 分解字符",
                        "unicode-nfd",
                        path="unicode/cafe\u0301.md",
                        source_ref="external:nfd",
                    ),
                    command(
                        "# 预组字符",
                        "unicode-nfc",
                        path="unicode/caf\u00e9.md",
                        source_ref="external:nfc",
                    ),
                ],
            )
        assert runtime.list_active(identity()) == []
    finally:
        runtime.close()


def test_user_collection_and_restricted_access_are_filtered_before_return(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        runtime.write(
            identity("alice"),
            command("# Alice\n专属校准口令 delta-7391", "alice-private"),
        )
        runtime.write(
            identity("admin", ("admin", "knowledge:write_shared")),
            command(
                "# 受限\n受限维护说明 omega-8842",
                "restricted",
                path="secure/restricted.md",
                collection_id="secure",
                scope=MemoryScope.SHARED,
                sensitivity=Sensitivity.RESTRICTED,
            ),
        )

        assert runtime.search(identity("bob"), "delta-7391", limit=5) == []
        assert runtime.search(identity("bob"), "omega-8842", limit=5) == []
        assert len(
            runtime.search(
                identity("admin", ("admin", "knowledge:read_restricted")),
                "omega-8842",
                limit=5,
                collection_ids=["secure"],
            )
        ) == 1
        assert runtime.search(
            identity("admin", ("admin", "knowledge:read_restricted")),
            "omega-8842",
            limit=5,
            collection_ids=["notes"],
        ) == []
    finally:
        runtime.close()


def test_revoke_fails_closed_even_when_index_delete_fails(tmp_path, monkeypatch):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        record = runtime.write(
            identity(),
            command("# 撤销\n污染检测标记 revoke-5519", "write"),
        )
        original_delete = runtime.index.delete_document
        monkeypatch.setattr(runtime.index, "delete_document", lambda *_: False)
        with pytest.raises(RuntimeError, match="索引删除后仍有残留"):
            runtime.revoke(identity(), record.document_id, "revoke", "来源失效")

        with sqlite3.connect(str(tmp_path / "knowledge/.system/knowledge.db")) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_derivative_jobs"
            ).fetchone()[0] == 1
        assert runtime.search(identity(), "revoke-5519", limit=5) == []
        assert not (tmp_path / "knowledge/notes/test.md").exists()

        monkeypatch.setattr(runtime.index, "delete_document", original_delete)
        revoked = runtime.revoke(
            identity(), record.document_id, "revoke", "来源失效"
        )
        assert revoked.status.value == "revoked"
        with sqlite3.connect(str(tmp_path / "knowledge/.system/knowledge.db")) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM knowledge_derivative_jobs"
            ).fetchone()[0] == 0
    finally:
        runtime.close()


def test_rollback_and_restart_rebuild_derivatives(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    actor = projection_identity()
    first = runtime.write(actor, projected_command("# 版本一\n恢复标记 alpha-1122", "v1"))
    runtime.write(
        actor,
        projected_command("# 版本二\n替换标记 beta-3344", "v2", document_id=first.document_id),
    )
    runtime.revoke(actor, first.document_id, "revoke", "测试撤销")
    restored = runtime.rollback(actor, first.document_id, 1, "rollback", "恢复第一版")
    assert restored.version == 4
    runtime.close()

    for suffix in ("", "-wal", "-shm"):
        path = Path(str(tmp_path / "knowledge/.system/retrieval.db") + suffix)
        if path.exists():
            path.unlink()

    restarted = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        results = restarted.search(actor, "alpha-1122", limit=5)
        assert len(results) == 1
        assert results[0].citation.document_version == 4
        assert (tmp_path / "knowledge/notes/test.md").read_text(encoding="utf-8") == first.content
    finally:
        restarted.close()


def test_rollback_rejects_projection_path_owned_by_another_document(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    actor = projection_identity()
    try:
        first = runtime.write(
            actor,
            projected_command("# 文档一\n历史路径 rollback-path-101", "document-one-v1"),
        )
        current = runtime.write(
            actor,
            projected_command(
                "# 文档一\n当前路径 rollback-path-202",
                "document-one-v2",
                document_id=first.document_id,
                path="notes/current.md",
            ),
        )
        occupant = runtime.write(
            actor,
            projected_command(
                "# 文档二\n占用历史路径 rollback-path-303",
                "document-two-v1",
                path="notes/test.md",
                source_ref="knowledge/occupant.md",
            ),
        )

        with pytest.raises(KnowledgeValidationError, match="projection_path"):
            runtime.rollback(
                actor, first.document_id, 1, "rollback-conflict", "恢复第一版"
            )

        assert runtime.get(actor, first.document_id) == current
        assert runtime.get(actor, occupant.document_id) == occupant
        assert (tmp_path / "knowledge/notes/test.md").read_text(
            encoding="utf-8"
        ) == occupant.content
    finally:
        runtime.close()


def test_legacy_projection_is_imported_once(tmp_path):
    legacy = tmp_path / "knowledge/legacy/page.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# 旧页面\n迁移标记 legacy-9081", encoding="utf-8")

    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        results = runtime.search(
            identity("local-user"),
            "legacy-9081",
            limit=5,
        )
        assert len(results) == 1
        document_id = results[0].citation.document_id
    finally:
        runtime.close()

    restarted = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        assert restarted.get(
            identity("local-user"), document_id
        ).version == 1
    finally:
        restarted.close()


def test_old_schema_migration_allows_same_private_path_across_tenants(tmp_path):
    database_path = tmp_path / "knowledge/.system/knowledge.db"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(str(database_path)) as conn:
        create_legacy_knowledge_documents_table(conn)
        rows = []
        for tenant_id, document_id, projection_path in (
            ("tenant-a", "document-a", "unicode/cafe\u0301.md"),
            ("tenant-b", "document-b", "unicode/caf\u00e9.md"),
        ):
            rows.append(
                (
                    tenant_id,
                    document_id,
                    1,
                    "active",
                    "user",
                    "alice",
                    None,
                    "private",
                    "unicode",
                    "旧记录",
                    "legacy:%s" % document_id,
                    projection_path,
                    "正文",
                    "legacy-hash",
                    "legacy-parser",
                    "{}",
                    "alice",
                    "legacy-trace",
                    "2026-07-29T00:00:00+00:00",
                )
            )
        conn.executemany(
            "INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    repository = KnowledgeRepository(database_path)
    try:
        with repository.connection() as conn:
            keys = conn.execute(
                "SELECT projection_key FROM knowledge_documents ORDER BY tenant_id"
            ).fetchall()
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert [row[0] for row in keys] == [
            "unicode/caf\u00e9.md",
            "unicode/caf\u00e9.md",
        ]
        assert user_version == 3
    finally:
        repository.close()


def test_old_schema_migration_backfills_without_changing_facts(tmp_path):
    database_path = tmp_path / "knowledge/.system/knowledge.db"
    database_path.parent.mkdir(parents=True)
    legacy_row = (
        "tenant-a",
        "document-a",
        1,
        "active",
        "user",
        "alice",
        None,
        "private",
        "legacy",
        "旧记录",
        "legacy:document-a",
        "Mixed/Cafe\u0301.md",
        "不可变正文 legacy-backfill-818",
        "legacy-hash",
        "legacy-parser",
        '{"source":"legacy"}',
        "alice",
        "legacy-trace",
        "2026-07-29T00:00:00+00:00",
    )
    with sqlite3.connect(str(database_path)) as conn:
        create_legacy_knowledge_documents_table(conn)
        conn.execute(
            "INSERT INTO knowledge_documents VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            legacy_row,
        )

    repository = KnowledgeRepository(database_path)
    try:
        with repository.connection() as conn:
            migrated = conn.execute(
                "SELECT projection_key, content, metadata_json "
                "FROM knowledge_documents WHERE document_id = ?",
                ("document-a",),
            ).fetchone()
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
            schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
            indexes = {
                row[1]: (row[2], row[4])
                for row in conn.execute("PRAGMA index_list(knowledge_documents)")
            }

        assert migrated["projection_key"] == "mixed/caf\u00e9.md"
        assert migrated["content"] == legacy_row[12]
        assert migrated["metadata_json"] == legacy_row[15]
        assert user_version == 3
        assert indexes["knowledge_documents_one_projection"] == (1, 1)
    finally:
        repository.close()

    reopened = KnowledgeRepository(database_path)
    try:
        with reopened.connection() as conn:
            assert conn.execute("PRAGMA schema_version").fetchone()[0] == schema_version
    finally:
        reopened.close()


def test_schema_migration_repairs_projection_key_and_unique_index(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        record = runtime.write(
            identity(),
            command(
                "# 迁移\n修复旧投影键 migration-key-711",
                "migration-key",
                path="Mixed/Case.md",
            ),
        )
    finally:
        runtime.close()

    database_path = tmp_path / "knowledge/.system/knowledge.db"
    with sqlite3.connect(str(database_path)) as conn:
        conn.execute(
            "UPDATE knowledge_documents SET projection_key = ? "
            "WHERE tenant_id = ? AND document_id = ? AND status = 'active'",
            ("stale-key", record.tenant_id, record.document_id),
        )
        conn.execute("DROP INDEX knowledge_documents_one_projection")
        conn.execute(
            "CREATE INDEX knowledge_documents_one_projection "
            "ON knowledge_documents(projection_key)"
        )
        conn.execute("PRAGMA user_version = 0")

    repository = KnowledgeRepository(database_path)
    try:
        with repository.connection() as conn:
            projection_key = conn.execute(
                "SELECT projection_key FROM knowledge_documents "
                "WHERE tenant_id = ? AND document_id = ? AND status = 'active'",
                (record.tenant_id, record.document_id),
            ).fetchone()[0]
            indexes = {
                row[1]: row[2]
                for row in conn.execute("PRAGMA index_list(knowledge_documents)")
            }
            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]

        assert projection_key == KnowledgeRepository.projection_key("Mixed/Case.md")
        assert indexes["knowledge_documents_one_projection"] == 1
        assert busy_timeout == 30000
    finally:
        repository.close()

    reopened = KnowledgeRepository(database_path)
    try:
        with reopened.connection() as conn:
            assert conn.execute("PRAGMA schema_version").fetchone()[0] == schema_version
    finally:
        reopened.close()


def test_current_schema_rejects_wrong_partial_projection_index(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        runtime.write(
            identity(),
            command("# 第一份\n索引定义校验 schema-101", "schema-one"),
        )
        runtime.write(
            identity(),
            command(
                "# 第二份\n重复路径校验 schema-202",
                "schema-two",
                path="notes/second.md",
            ),
        )
    finally:
        runtime.close()

    database_path = tmp_path / "knowledge/.system/knowledge.db"
    with sqlite3.connect(str(database_path)) as conn:
        first_key = conn.execute(
            "SELECT projection_key FROM knowledge_documents "
            "WHERE status = 'active' ORDER BY document_id LIMIT 1"
        ).fetchone()[0]
        second_document_id = conn.execute(
            "SELECT document_id FROM knowledge_documents "
            "WHERE status = 'active' ORDER BY document_id LIMIT 1 OFFSET 1"
        ).fetchone()[0]
        conn.execute("DROP INDEX knowledge_documents_one_projection")
        conn.execute(
            "CREATE UNIQUE INDEX knowledge_documents_one_projection "
            "ON knowledge_documents(projection_key) WHERE status = 'revoked'"
        )

    with pytest.raises(KnowledgeValidationError, match="投影索引"):
        KnowledgeRepository(database_path)


def test_current_schema_rejects_projection_key_mismatch(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        record = runtime.write(
            identity(),
            command("# 当前模式\n错误规范键 schema-key-515", "schema-key"),
        )
    finally:
        runtime.close()

    database_path = tmp_path / "knowledge/.system/knowledge.db"
    with sqlite3.connect(str(database_path)) as conn:
        conn.execute(
            "UPDATE knowledge_documents SET projection_key = ? "
            "WHERE tenant_id = ? AND document_id = ? AND status = 'active'",
            ("wrong/current-key.md", record.tenant_id, record.document_id),
        )

    with pytest.raises(KnowledgeValidationError, match="投影索引"):
        KnowledgeRepository(database_path)


def test_v1_migration_drops_full_history_unique_index_before_backfill(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        first = runtime.write(
            identity(),
            command("# 第一版\n历史索引 migration-v1-101", "migration-v1"),
        )
        runtime.write(
            identity(),
            command(
                "# 第二版\n历史索引 migration-v1-202",
                "migration-v2",
                document_id=first.document_id,
            ),
        )
    finally:
        runtime.close()

    database_path = tmp_path / "knowledge/.system/knowledge.db"
    with sqlite3.connect(str(database_path)) as conn:
        conn.execute("DROP INDEX knowledge_documents_one_projection")
        conn.execute(
            "UPDATE knowledge_documents SET projection_key = "
            "'legacy-' || CAST(version AS TEXT)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX knowledge_documents_one_projection "
            "ON knowledge_documents(projection_key)"
        )
        conn.execute("PRAGMA user_version = 1")

    repository = KnowledgeRepository(database_path)
    try:
        with repository.connection() as conn:
            rows = conn.execute(
                "SELECT projection_key FROM knowledge_documents ORDER BY version"
            ).fetchall()
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert [row[0] for row in rows] == ["notes/test.md", "notes/test.md"]
        assert user_version == 3
    finally:
        repository.close()


def test_v2_projection_schema_migrates_atomically_to_identity_scoped_v3(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        runtime.write(
            identity("alice"),
            command("# 私有路径\nv2 migration", "v2-private"),
        )
    finally:
        runtime.close()

    database_path = tmp_path / "knowledge/.system/knowledge.db"
    with sqlite3.connect(str(database_path)) as conn:
        for index_name in (
            "knowledge_documents_one_projection",
            "knowledge_documents_one_shared_path",
            "knowledge_documents_one_user_path",
            "knowledge_documents_one_session_path",
        ):
            conn.execute("DROP INDEX %s" % index_name)
        conn.execute(
            "CREATE UNIQUE INDEX knowledge_documents_one_projection "
            "ON knowledge_documents(projection_key) "
            "WHERE status = 'active' AND projection_key IS NOT NULL"
        )
        conn.execute("PRAGMA user_version = 2")

    repository = KnowledgeRepository(database_path)
    try:
        with repository.connection() as conn:
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
            index_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND name = 'knowledge_documents_one_projection'"
            ).fetchone()[0]
            category_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'knowledge_categories'"
            ).fetchone()
        assert user_version == 3
        assert "scope = 'shared'" in index_sql
        assert category_table is not None
    finally:
        repository.close()


def test_future_schema_version_fails_closed(tmp_path):
    database_path = tmp_path / "knowledge/.system/knowledge.db"
    repository = KnowledgeRepository(database_path)
    repository.close()
    with sqlite3.connect(str(database_path)) as conn:
        conn.execute("PRAGMA user_version = 4")

    with pytest.raises(KnowledgeValidationError, match="版本高于"):
        KnowledgeRepository(database_path)


def test_repository_waits_beyond_previous_five_second_lock_budget(tmp_path):
    database_path = tmp_path / "knowledge/.system/knowledge.db"
    repository = KnowledgeRepository(database_path)
    blocker = sqlite3.connect(str(database_path), timeout=30.0)
    blocker.execute("PRAGMA busy_timeout=30000")
    blocker.execute("BEGIN IMMEDIATE")
    started = threading.Event()
    result = {}

    def write_state():
        started.set()
        before = time.monotonic()
        try:
            repository.set_state("lock-budget-test", "completed")
        except Exception as error:
            result["error"] = error
        finally:
            result["elapsed"] = time.monotonic() - before

    worker = threading.Thread(target=write_state)
    worker.start()
    try:
        assert started.wait(timeout=2)
        time.sleep(5.2)
        blocker.commit()
        worker.join(timeout=10)

        assert not worker.is_alive()
        assert "error" not in result
        assert result["elapsed"] >= 5.0
        assert repository.get_state("lock-budget-test") == "completed"
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()
        worker.join(timeout=1)
        repository.close()


def test_generic_file_tools_cannot_bypass_knowledge_runtime(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        runtime.write(
            identity(),
            command("# 受保护\n知识事实库正文 guard-4172", "guard"),
        )
        config = {"cwd": str(tmp_path)}
        write_result = Write(config).execute(
            {"path": "knowledge/notes/test.md", "content": "绕过事实库"}
        )
        edit_result = Edit(config).execute(
            {
                "path": "knowledge/notes/test.md",
                "oldText": "知识事实库正文",
                "newText": "绕过事实库",
            }
        )
        read_private = Read(config).execute(
            {"path": "knowledge/.system/knowledge.db"}
        )
        search_private = SearchFiles(config).execute(
            {"path": "knowledge/.system", "pattern": "guard-4172", "no_ignore": True}
        )
        bash_write = Bash(config).execute(
            {"command": "echo bypass > knowledge/notes/test.md"}
        )

        assert write_result.status == "error"
        assert edit_result.status == "error"
        assert read_private.status == "error"
        assert search_private.status == "error"
        assert bash_write.status == "error"
        assert runtime.search(identity(), "guard-4172", limit=5)
    finally:
        runtime.close()


def test_knowledge_tools_use_injected_identity_and_return_citations(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    alice = identity("alice")
    try:
        write_tool = KnowledgeWriteTool(runtime, alice, session_id="session-a")
        args = {
            "path": "concepts/calibration.md",
            "title": "校准流程",
            "content": "# 校准流程\n先执行零点校准，再记录读数 tool-6318。",
        }
        first = write_tool.execute(args)
        repeated = write_tool.execute(args)
        assert first.status == "success"
        assert repeated.result == first.result

        search = KnowledgeSearchTool(runtime, alice, session_id="session-a").execute(
            {"query": "零点校准记录", "limit": 5}
        )
        assert search.status == "success"
        assert search.result["result_count"] == 1
        citation = search.result["results"][0]["citation"]
        assert citation["uri"].startswith("knowledge://")
        assert citation["citation_version"] == 3
        assert citation["source_ref_hash"] == hashlib.sha256(
            citation["source_ref"].encode("utf-8")
        ).hexdigest()
        assert "&source_ref_hash=%s&citation_version=3" % (
            citation["source_ref_hash"]
        ) in citation["uri"]

        get = KnowledgeGetTool(runtime, alice, session_id="session-a").execute(
            {"uri": citation["uri"]}
        )
        assert get.status == "success"
        assert "tool-6318" in get.result["content"]
    finally:
        runtime.close()


def test_verified_citation_allows_shared_reader_search_to_get(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        _, reader, record, citation = _write_shared_citation(
            runtime, "citation-shared-8712"
        )

        resolved = runtime.resolve_verified_citation(reader, citation.uri)
        assert resolved == citation
        assert "content_hash=%s" % citation.content_hash in citation.uri
        assert "quote_hash=%s" % citation.quote_hash in citation.uri
        assert "source_ref_hash=%s" % citation.source_ref_hash in citation.uri

        result = KnowledgeGetTool(runtime, reader).execute({"uri": citation.uri})
        assert result.status == "success", result.result
        assert result.result["document_id"] == record.document_id
        assert result.result["version"] == citation.document_version
        assert "citation-shared-8712" in result.result["content"]
    finally:
        runtime.close()


def test_verified_citation_rejects_legacy_and_hash_downgrade(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        _, reader, _, citation = _write_shared_citation(
            runtime, "citation-legacy-6204"
        )
        legacy_uri = citation.uri.split("&content_hash=", 1)[0]

        v2_uri = citation.uri.replace(
            "&source_ref_hash=%s&citation_version=3" % citation.source_ref_hash,
            "&citation_version=2",
        )

        assert citation.uri.endswith("&citation_version=3")
        with pytest.raises(KnowledgeCitationIntegrityError) as legacy_error:
            runtime.resolve_verified_citation(reader, legacy_uri)
        with pytest.raises(KnowledgeValidationError):
            runtime.resolve_verified_citation(
                reader, citation.uri.rsplit("&citation_version=3", 1)[0]
            )
        with pytest.raises(KnowledgeCitationVersionError) as version_error:
            runtime.resolve_verified_citation(reader, v2_uri)
        result = KnowledgeGetTool(runtime, reader).execute({"uri": legacy_uri})
        assert result.status == "error"
        v2_result = KnowledgeGetTool(runtime, reader).execute({"uri": v2_uri})
        assert v2_result.status == "error"
        assert legacy_error.value.code == "citation_integrity_failed"
        assert version_error.value.code == "unsupported_citation_version"
        assert "citation_integrity_failed" in result.result
        assert "unsupported_citation_version" in v2_result.result
    finally:
        runtime.close()


def test_verified_citation_rejects_noncanonical_and_tampered_uri(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        _, reader, _, citation = _write_shared_citation(
            runtime, "citation-tamper-4821"
        )
        uri = citation.uri
        tampered_uris = (
            "prefix-" + uri,
            uri + "-suffix",
            uri.replace(citation.document_id, "forged-document", 1),
            uri.replace(
                "/v/%s/" % citation.document_version,
                "/v/%s/" % (citation.document_version + 1),
                1,
            ),
            uri.replace(citation.section_id, "0" * 64, 1),
            uri.replace(citation.evidence_id, "1" * 64, 1),
            uri.replace(
                "bytes=%s-%s" % (citation.byte_start, citation.byte_end),
                "bytes=%s-%s" % (citation.byte_start, citation.byte_end + 1),
                1,
            ),
            uri.replace(citation.content_hash, "2" * 64, 1),
            uri.replace(citation.quote_hash, "3" * 64, 1),
            uri.replace(citation.source_ref_hash, "4" * 64, 1),
            uri.replace("citation_version=3", "citation_version=1", 1),
            uri.replace("citation_version=3", "citation_version=2", 1),
            uri.replace(citation.content_hash, citation.content_hash.upper(), 1),
            uri.replace(citation.quote_hash, citation.quote_hash.upper(), 1),
            uri.replace(
                citation.source_ref_hash,
                citation.source_ref_hash.upper(),
                1,
            ),
            uri.replace(
                "&content_hash=%s&quote_hash=%s"
                % (citation.content_hash, citation.quote_hash),
                "&quote_hash=%s&content_hash=%s"
                % (citation.quote_hash, citation.content_hash),
                1,
            ),
            uri.replace(
                "&citation_version=3",
                "&source_ref_hash=%s&citation_version=3"
                % citation.source_ref_hash,
                1,
            ),
            uri + "&unknown_parameter=1",
        )

        for tampered_uri in tampered_uris:
            assert tampered_uri != uri
            with pytest.raises(KnowledgeValidationError):
                runtime.resolve_verified_citation(reader, tampered_uri)
    finally:
        runtime.close()


def test_verified_citation_classifies_hash_tamper_as_integrity_failure(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        _, reader, _, citation = _write_shared_citation(
            runtime, "citation-error-code-7281"
        )
        tampered_uri = citation.uri.replace(citation.content_hash, "9" * 64, 1)

        with pytest.raises(KnowledgeCitationIntegrityError) as error:
            runtime.resolve_verified_citation(reader, tampered_uri)

        assert error.value.code == "citation_integrity_failed"
        result = KnowledgeGetTool(runtime, reader).execute({"uri": tampered_uri})
        assert result.status == "error"
        assert "citation_integrity_failed" in result.result
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("table_name", "hash_column", "key_column"),
    (
        ("knowledge_documents", "content_hash", "document_id"),
        ("knowledge_evidence", "quote_hash", "evidence_id"),
    ),
)
def test_verified_citation_rejects_tampered_stored_hash(
    tmp_path, table_name, hash_column, key_column
):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        _, reader, record, citation = _write_shared_citation(
            runtime, "citation-hash-6934"
        )
        key_value = (
            record.document_id if key_column == "document_id" else citation.evidence_id
        )
        with runtime.repository.transaction() as conn:
            conn.execute(
                "UPDATE %s SET %s = ? WHERE tenant_id = ? AND %s = ?"
                % (table_name, hash_column, key_column),
                ("f" * 64, record.tenant_id, key_value),
            )

        with pytest.raises(KnowledgeValidationError):
            runtime.resolve_verified_citation(reader, citation.uri)
    finally:
        runtime.close()


def test_verified_citation_binds_exact_stripped_unicode_source_ref(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    writer = identity("writer", ("knowledge:write_shared",))
    reader = identity("reader")
    try:
        stored_source_ref = "knowledge/资料/e\u0301 source 文档.md"
        runtime.write(
            writer,
            command(
                "# 来源绑定\nUnicode 与空格 source-ref-3197",
                "source-ref-unicode",
                path="shared/source-ref-unicode.md",
                scope=MemoryScope.SHARED,
                sensitivity=Sensitivity.INTERNAL,
                source_ref="  %s  " % stored_source_ref,
            ),
        )
        citation = runtime.search(reader, "source-ref-3197", limit=5)[0].citation

        assert citation.source_ref == stored_source_ref
        assert citation.source_ref_hash == hashlib.sha256(
            stored_source_ref.encode("utf-8")
        ).hexdigest()
        assert runtime.resolve_verified_citation(reader, citation.uri) == citation
    finally:
        runtime.close()


def test_verified_citation_rejects_tampered_stored_source_ref(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        _, reader, record, citation = _write_shared_citation(
            runtime, "citation-source-ref-tamper-4271"
        )
        with runtime.repository.transaction() as conn:
            conn.execute(
                "UPDATE knowledge_documents SET source_ref = ? "
                "WHERE tenant_id = ? AND document_id = ? AND status = 'active'",
                (
                    "knowledge/forged-source.md",
                    record.tenant_id,
                    record.document_id,
                ),
            )

        with pytest.raises(KnowledgeValidationError):
            runtime.resolve_verified_citation(reader, citation.uri)
    finally:
        runtime.close()


def test_verified_citation_rejects_tampered_section_relation_and_hash(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    writer = identity("writer", ("knowledge:write_shared",))
    reader = identity("reader")
    try:
        record = runtime.write(
            writer,
            command(
                "# 第一节\nsection-first-5124\n# 第二节\nsection-second-6235",
                "citation-section",
                path="shared/citation-section.md",
                scope=MemoryScope.SHARED,
                sensitivity=Sensitivity.INTERNAL,
            ),
        )
        citation = runtime.search(reader, "section-first-5124", limit=5)[0].citation
        with runtime.repository.transaction() as conn:
            other_section_id = conn.execute(
                "SELECT section_id FROM knowledge_sections "
                "WHERE tenant_id = ? AND document_id = ? AND document_version = ? "
                "AND section_id <> ? ORDER BY section_index LIMIT 1",
                (
                    record.tenant_id,
                    record.document_id,
                    record.version,
                    citation.section_id,
                ),
            ).fetchone()[0]
            conn.execute(
                "UPDATE knowledge_evidence SET section_id = ? "
                "WHERE tenant_id = ? AND evidence_id = ?",
                (other_section_id, record.tenant_id, citation.evidence_id),
            )
        with pytest.raises(KnowledgeValidationError):
            runtime.resolve_verified_citation(reader, citation.uri)

        with runtime.repository.transaction() as conn:
            conn.execute(
                "UPDATE knowledge_evidence SET section_id = ? "
                "WHERE tenant_id = ? AND evidence_id = ?",
                (citation.section_id, record.tenant_id, citation.evidence_id),
            )
            conn.execute(
                "UPDATE knowledge_sections SET content_hash = ? "
                "WHERE tenant_id = ? AND section_id = ?",
                ("e" * 64, record.tenant_id, citation.section_id),
            )
        with pytest.raises(KnowledgeValidationError):
            runtime.resolve_verified_citation(reader, citation.uri)
    finally:
        runtime.close()


def test_verified_citation_expires_after_update_and_revoke(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        writer, reader, record, first_citation = _write_shared_citation(
            runtime, "citation-lifecycle-1411"
        )
        runtime.write(
            writer,
            command(
                "# 共享引用\n更新后的正文 citation-lifecycle-2522",
                "citation-lifecycle-v2",
                document_id=record.document_id,
                path="shared/citation-lifecycle-1411.md",
                scope=MemoryScope.SHARED,
                sensitivity=Sensitivity.INTERNAL,
            ),
        )
        with pytest.raises(KnowledgeValidationError):
            runtime.resolve_verified_citation(reader, first_citation.uri)

        second_citation = runtime.search(
            reader, "citation-lifecycle-2522", limit=5
        )[0].citation
        runtime.revoke(
            writer,
            record.document_id,
            "citation-lifecycle-revoke",
            "引用来源撤销",
        )
        with pytest.raises(KnowledgeValidationError):
            runtime.resolve_verified_citation(reader, second_citation.uri)
    finally:
        runtime.close()


def test_verified_citation_enforces_tenant_user_and_session_boundaries(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    alice = identity("alice")
    try:
        _, reader, _, shared_citation = _write_shared_citation(
            runtime, "citation-tenant-7316"
        )
        other_tenant = IdentityContext(
            tenant_id="tenant-other",
            actor_user_id=reader.actor_user_id,
            roles=frozenset(),
            trace_id="trace-other-tenant",
            auth_source="test-session",
        )
        with pytest.raises(KnowledgeAuthorizationError):
            runtime.resolve_verified_citation(other_tenant, shared_citation.uri)

        runtime.write(
            alice,
            command(
                "# 用户引用\n仅 Alice 可见 citation-user-8843",
                "citation-user",
                path="private/citation-user.md",
            ),
        )
        user_citation = runtime.search(alice, "citation-user-8843", limit=5)[0].citation
        with pytest.raises(KnowledgeAuthorizationError):
            runtime.resolve_verified_citation(identity("bob"), user_citation.uri)

        runtime.write(
            alice,
            KnowledgeWriteCommand(
                content="# 会话引用\n仅 session-a 可见 citation-session-9917",
                title="会话引用",
                source_ref="knowledge/session/citation.md",
                collection_id="session",
                idempotency_key="citation-session",
                projection_path="session/citation.md",
                scope=MemoryScope.SESSION,
                session_id="session-a",
                sensitivity=Sensitivity.PRIVATE,
            ),
        )
        session_citation = runtime.search(
            alice, "citation-session-9917", limit=5, session_id="session-a"
        )[0].citation
        assert "&session_binding=" in session_citation.uri
        assert runtime.resolve_verified_citation(
            alice, session_citation.uri
        ) == session_citation
        assert runtime.resolve_verified_citation(
            alice, session_citation.uri, session_id="session-a"
        ) == session_citation

        with pytest.raises(KnowledgeAuthorizationError):
            runtime.resolve_verified_citation(
                alice, session_citation.uri, session_id="session-b"
            )
        with pytest.raises(KnowledgeAuthorizationError):
            runtime.resolve_verified_citation(
                identity("bob"), session_citation.uri
            )

        unbound = re.sub(
            r"&session_binding=[A-Za-z0-9_-]+\.[0-9a-f]{1,16}\.[0-9a-f]{64}",
            "",
            session_citation.uri,
        )
        with pytest.raises(KnowledgeCitationIntegrityError):
            runtime.resolve_verified_citation(alice, unbound)

        prefix, signature_and_tail = session_citation.uri.split(
            "&session_binding=", 1
        )
        binding, tail = signature_and_tail.split("&citation_version=3", 1)
        encoded, expires_hex, signature = binding.split(".", 2)
        forged_signature = ("0" if signature[0] != "0" else "1") + signature[1:]
        forged = (
            prefix
            + "&session_binding="
            + encoded
            + "."
            + expires_hex
            + "."
            + forged_signature
            + "&citation_version=3"
            + tail
        )
        with pytest.raises(KnowledgeCitationIntegrityError):
            runtime.resolve_verified_citation(alice, forged)
    finally:
        runtime.close()



def test_session_citation_capability_has_fixed_expiry(monkeypatch, tmp_path):
    from agent.knowledge import runtime as knowledge_runtime

    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    alice = identity("alice-expiry")
    monkeypatch.setattr(knowledge_runtime.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(
        "config.conf",
        lambda: {"knowledge_session_citation_ttl_seconds": 60},
    )
    try:
        runtime.write(
            alice,
            KnowledgeWriteCommand(
                content="# Expiry\nfixed-expiry-citation-1173",
                title="Expiry",
                source_ref="knowledge/session/expiry.md",
                collection_id="session",
                idempotency_key="citation-expiry",
                projection_path="session/expiry.md",
                scope=MemoryScope.SESSION,
                session_id="expiry-session",
                sensitivity=Sensitivity.PRIVATE,
            ),
        )
        citation = runtime.search(
            alice, "fixed-expiry-citation-1173", session_id="expiry-session"
        )[0].citation
        assert runtime.resolve_verified_citation(alice, citation.uri) == citation
        monkeypatch.setattr(knowledge_runtime.time, "time", lambda: 1_061.0)
        with pytest.raises(KnowledgeCitationIntegrityError, match="expired"):
            runtime.resolve_verified_citation(alice, citation.uri)
    finally:
        runtime.close()


def test_session_citation_resolution_preserves_original_expiry_binding(
    monkeypatch, tmp_path
):
    from agent.knowledge import runtime as knowledge_runtime

    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    alice = identity("alice-binding-clock")
    clock = {"now": 2_000.0}
    monkeypatch.setattr(knowledge_runtime.time, "time", lambda: clock["now"])
    try:
        runtime.write(
            alice,
            KnowledgeWriteCommand(
                content="# Stable URI\nstable-session-binding-5519",
                title="Stable URI",
                source_ref="knowledge/session/stable-uri.md",
                collection_id="session",
                idempotency_key="stable-session-binding",
                projection_path="session/stable-uri.md",
                scope=MemoryScope.SESSION,
                session_id="stable-uri-session",
                sensitivity=Sensitivity.PRIVATE,
            ),
        )
        citation = runtime.search(
            alice,
            "stable-session-binding-5519",
            session_id="stable-uri-session",
        )[0].citation
        clock["now"] = 2_001.0
        assert runtime.resolve_verified_citation(alice, citation.uri) == citation
    finally:
        runtime.close()

def test_knowledge_get_rejects_uri_and_explicit_citation_conflicts(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        _, reader, _, citation = _write_shared_citation(
            runtime, "citation-conflict-6148"
        )
        exact_arguments = {
            "uri": citation.uri,
            "citation_version": citation.citation_version,
            "document_id": citation.document_id,
            "version": citation.document_version,
            "document_version": citation.document_version,
            "section_id": citation.section_id,
            "evidence_id": citation.evidence_id,
            "source_ref": citation.source_ref,
            "source_ref_hash": citation.source_ref_hash,
            "byte_start": citation.byte_start,
            "byte_end": citation.byte_end,
            "content_hash": citation.content_hash,
            "quote_hash": citation.quote_hash,
            "quote": citation.quote,
        }
        exact = KnowledgeGetTool(runtime, reader).execute(exact_arguments)
        assert exact.status == "success", exact.result

        conflicting_values = {
            "citation_version": 2,
            "document_id": "forged-document",
            "version": citation.document_version + 1,
            "document_version": citation.document_version + 1,
            "section_id": "0" * 64,
            "evidence_id": "1" * 64,
            "source_ref": "forged-source",
            "source_ref_hash": "4" * 64,
            "byte_start": citation.byte_start + 1,
            "byte_end": citation.byte_end + 1,
            "content_hash": "2" * 64,
            "quote_hash": "3" * 64,
            "quote": citation.quote + "tampered",
        }
        for field_name, value in conflicting_values.items():
            result = KnowledgeGetTool(runtime, reader).execute(
                {"uri": citation.uri, field_name: value}
            )
            assert result.status == "error", (field_name, result.result)
            assert field_name in str(result.result)
        for field_name, value in (
            ("version", True),
            ("document_version", str(citation.document_version)),
            ("byte_start", str(citation.byte_start)),
            ("byte_start", bool(citation.byte_start)),
        ):
            result = KnowledgeGetTool(runtime, reader).execute(
                {"uri": citation.uri, field_name: value}
            )
            assert result.status == "error", (field_name, result.result)
            assert field_name in str(result.result)
        for field_name in (
            "citation_version",
            "section_id",
            "evidence_id",
            "source_ref",
            "source_ref_hash",
            "byte_start",
            "byte_end",
            "content_hash",
            "quote_hash",
            "quote",
        ):
            result = KnowledgeGetTool(runtime, reader).execute(
                {
                    "document_id": citation.document_id,
                    field_name: getattr(citation, field_name),
                }
            )
            assert result.status == "error", (field_name, result.result)
            assert "requires uri" in str(result.result)
    finally:
        runtime.close()


def test_knowledge_get_keeps_admin_historical_version_compatibility(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    admin = identity("admin", ("admin", "knowledge:manage"))
    try:
        first = runtime.write(
            admin,
            command("# 历史版本\n第一版 history-101", "history-v1"),
        )
        runtime.write(
            admin,
            command(
                "# 历史版本\n第二版 history-202",
                "history-v2",
                document_id=first.document_id,
            ),
        )

        result = KnowledgeGetTool(runtime, admin).execute(
            {"document_id": first.document_id, "version": 1}
        )
        assert result.status == "success", result.result
        assert result.result["version"] == 1
        assert "history-101" in result.result["content"]
    finally:
        runtime.close()


def test_knowledge_get_rejects_tampered_admin_historical_content(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    admin = identity("admin", ("admin", "knowledge:manage"))
    try:
        first = runtime.write(
            admin,
            command("# 历史完整性\n第一版 history-integrity-303", "integrity-v1"),
        )
        runtime.write(
            admin,
            command(
                "# 历史完整性\n第二版 history-integrity-404",
                "integrity-v2",
                document_id=first.document_id,
            ),
        )
        with runtime.repository.transaction() as conn:
            conn.execute(
                "UPDATE knowledge_documents SET content = ? "
                "WHERE tenant_id = ? AND document_id = ? AND version = 1",
                ("TAMPERED HISTORY", first.tenant_id, first.document_id),
            )

        result = KnowledgeGetTool(runtime, admin).execute(
            {"document_id": first.document_id, "version": 1}
        )
        assert result.status == "error"
        assert "完整性" in str(result.result)
        assert "TAMPERED HISTORY" not in str(result.result)
    finally:
        runtime.close()


def test_knowledge_get_fails_closed_on_update_after_citation_resolution(
    tmp_path, monkeypatch
):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        writer, reader, record, citation = _write_shared_citation(
            runtime, "citation-race-3185"
        )
        original_resolve = runtime.resolve_verified_citation

        def resolve_then_update(*args, **kwargs):
            resolved = original_resolve(*args, **kwargs)
            runtime.write(
                writer,
                command(
                    "# 共享引用\n并发更新正文 citation-race-4296",
                    "citation-race-v2",
                    document_id=record.document_id,
                    path="shared/citation-race-3185.md",
                    scope=MemoryScope.SHARED,
                    sensitivity=Sensitivity.INTERNAL,
                ),
            )
            return resolved

        monkeypatch.setattr(runtime, "resolve_verified_citation", resolve_then_update)
        result = KnowledgeGetTool(runtime, reader).execute({"uri": citation.uri})

        assert result.status == "error"
        assert "失效" in str(result.result)
        assert "citation-race-4296" not in str(result.result)
    finally:
        runtime.close()


def test_knowledge_get_fails_closed_on_revoke_after_citation_resolution(
    tmp_path, monkeypatch
):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        writer, reader, record, citation = _write_shared_citation(
            runtime, "citation-revoke-race-5362"
        )
        original_resolve = runtime.resolve_verified_citation

        def resolve_then_revoke(*args, **kwargs):
            resolved = original_resolve(*args, **kwargs)
            runtime.revoke(
                writer,
                record.document_id,
                "citation-revoke-race",
                "引用读取期间撤销",
            )
            return resolved

        monkeypatch.setattr(runtime, "resolve_verified_citation", resolve_then_revoke)
        result = KnowledgeGetTool(runtime, reader).execute({"uri": citation.uri})

        assert result.status == "error"
        assert "citation-revoke-race-5362" not in str(result.result)
    finally:
        runtime.close()


def test_knowledge_get_rehashes_content_after_citation_resolution(
    tmp_path, monkeypatch
):
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        _, reader, record, citation = _write_shared_citation(
            runtime, "citation-content-race-7412"
        )
        original_resolve = runtime.resolve_verified_citation

        def resolve_then_tamper(*args, **kwargs):
            resolved = original_resolve(*args, **kwargs)
            with runtime.repository.transaction() as conn:
                conn.execute(
                    "UPDATE knowledge_documents SET content = ? "
                    "WHERE tenant_id = ? AND document_id = ? AND status = 'active'",
                    (
                        "TAMPERED AFTER RESOLVE",
                        record.tenant_id,
                        record.document_id,
                    ),
                )
            return resolved

        monkeypatch.setattr(runtime, "resolve_verified_citation", resolve_then_tamper)
        result = KnowledgeGetTool(runtime, reader).execute({"uri": citation.uri})

        assert result.status == "error"
        assert "失效" in str(result.result)
        assert "TAMPERED AFTER RESOLVE" not in str(result.result)
    finally:
        runtime.close()


def _write_shared_citation(runtime, marker):
    writer = identity("writer", ("knowledge:write_shared",))
    reader = identity("reader")
    record = runtime.write(
        writer,
        command(
            "# 共享引用\n普通读者可见 %s" % marker,
            "shared-%s" % marker,
            path="shared/%s.md" % marker,
            scope=MemoryScope.SHARED,
            sensitivity=Sensitivity.INTERNAL,
        ),
    )
    results = runtime.search(reader, marker, limit=5)
    assert len(results) == 1
    return writer, reader, record, results[0].citation

