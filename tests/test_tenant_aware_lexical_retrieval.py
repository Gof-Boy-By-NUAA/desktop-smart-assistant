"""租户化中文词法检索测试。"""

import tempfile
import unittest
from pathlib import Path

from agent.memory.governance import IdentityContext, MemoryScope, Sensitivity
from agent.retrieval import IndexedDocument, TenantAwareLexicalIndex
from agent.retrieval.lexical import (
    _build_verification_trigrams,
    _verification_probes,
)


class TenantAwareLexicalIndexTest(unittest.TestCase):
    """验证中文召回和 SQL 权限过滤。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.index = TenantAwareLexicalIndex(
            Path(self.temp_dir.name) / "retrieval.db",
            candidate_limit=20,
        )
        self.alice = identity("tenant-a", "alice")
        self.bob = identity("tenant-a", "bob")

    def tearDown(self):
        self.index.close()
        self.temp_dir.cleanup()

    def test_partial_chinese_question_retrieves_relevant_document(self):
        self.index.index_documents(
            [
                shared_document(
                    "tenant-a",
                    "beijing",
                    "北京",
                    "北京是中华人民共和国的首都，也是全国政治中心。",
                ),
                shared_document(
                    "tenant-a",
                    "shanghai",
                    "上海",
                    "上海位于长江入海口，是重要的经济中心。",
                ),
            ]
        )

        results = self.index.search(self.alice, "中国的首都和政治中心在哪里？")

        self.assertEqual("beijing", results[0].document_id)

    def test_tenant_filter_is_applied_inside_search(self):
        self.index.index_documents(
            [
                shared_document("tenant-a", "a", "项目", "项目密钥轮换流程"),
                shared_document("tenant-b", "b", "项目", "项目密钥轮换流程"),
            ]
        )

        results = self.index.search(self.alice, "项目密钥如何轮换？")

        self.assertEqual(["a"], [result.document_id for result in results])

    def test_user_and_session_scope_require_matching_identity(self):
        self.index.index_documents(
            [
                IndexedDocument(
                    tenant_id="tenant-a",
                    document_id="alice-user",
                    scope=MemoryScope.USER,
                    owner_user_id="alice",
                    title="用户偏好",
                    text="用户偏好使用中文回答",
                    source_ref="memory:alice",
                ),
                IndexedDocument(
                    tenant_id="tenant-a",
                    document_id="alice-session",
                    scope=MemoryScope.SESSION,
                    owner_user_id="alice",
                    session_id="session-1",
                    title="会话计划",
                    text="会话计划是在周五发布版本",
                    source_ref="session:1",
                ),
            ]
        )

        self.assertEqual(
            ["alice-user"],
            [result.document_id for result in self.index.search(self.alice, "用户偏好是什么？")],
        )
        self.assertEqual([], self.index.search(self.bob, "用户偏好是什么？"))
        self.assertEqual([], self.index.search(self.alice, "会话计划何时发布？"))
        self.assertEqual(
            ["alice-session"],
            [
                result.document_id
                for result in self.index.search(
                    self.alice, "会话计划何时发布？", session_id="session-1"
                )
            ],
        )

    def test_restricted_document_requires_read_role(self):
        self.index.index_documents(
            [
                IndexedDocument(
                    tenant_id="tenant-a",
                    document_id="restricted",
                    scope=MemoryScope.SHARED,
                    title="安全方案",
                    text="安全方案要求使用硬件密钥",
                    source_ref="knowledge:security",
                    sensitivity=Sensitivity.RESTRICTED,
                )
            ]
        )

        self.assertEqual([], self.index.search(self.alice, "安全方案使用什么密钥？"))
        security_reader = identity("tenant-a", "alice", "memory:read_restricted")
        self.assertEqual(
            ["restricted"],
            [
                result.document_id
                for result in self.index.search(security_reader, "安全方案使用什么密钥？")
            ],
        )

    def test_replace_collection_removes_deleted_documents_and_keeps_metadata(self):
        first = IndexedDocument(
            tenant_id="tenant-a",
            document_id="first",
            scope=MemoryScope.SHARED,
            title="发布计划",
            text="发布计划定在周五",
            source_ref="knowledge:first",
            collection_id="workspace",
            metadata={"path": "knowledge/first.md", "start_line": 3},
        )
        second = IndexedDocument(
            tenant_id="tenant-a",
            document_id="second",
            scope=MemoryScope.SHARED,
            title="测试计划",
            text="测试计划定在周四",
            source_ref="knowledge:second",
            collection_id="workspace",
        )
        self.index.replace_collection("tenant-a", "workspace", [first, second])

        result = self.index.search(self.alice, "发布计划在什么时候？")[0]
        self.assertEqual("knowledge/first.md", result.metadata["path"])

        self.index.replace_collection("tenant-a", "workspace", [second])

        self.assertEqual([], self.index.search(self.alice, "发布计划在什么时候？"))

    def test_identical_reindex_does_not_rewrite_content_or_fts_rows(self):
        document = shared_document(
            "tenant-a",
            "stable-document",
            "稳定索引",
            "相同内容重复登记时不应触发 FTS 更新。",
        )
        self.index.index_documents([document])
        changes_before = self.index._conn.total_changes

        self.index.index_documents([document])

        self.assertEqual(changes_before, self.index._conn.total_changes)
        self.assertTrue(self.index.matches_document(document))

    def test_busy_timeout_matches_fact_repository_budget(self):
        busy_timeout = self.index._conn.execute("PRAGMA busy_timeout").fetchone()[0]

        self.assertEqual(30000, busy_timeout)

    def test_new_index_uses_profiled_page_and_cache_budget(self):
        self.assertEqual(
            65536, self.index._conn.execute("PRAGMA page_size").fetchone()[0]
        )
        self.assertEqual(
            -8192, self.index._conn.execute("PRAGMA cache_size").fetchone()[0]
        )

    def test_tenant_mapping_rejects_every_stored_field_and_missing_docsize(self):
        document = shared_document(
            "tenant-a",
            "mapping-fields",
            "字段核验",
            "字段核验必须逐值精确 mapping-field-739",
        )
        self.index.index_documents([document])
        mutations = {
            "tenant_id": "tenant-z",
            "document_id": "mapping-fields-mutated",
            "scope": "user",
            "owner_user_id": "mallory",
            "session_id": "session-z",
            "sensitivity": "public",
            "title": "被篡改标题",
            "text": "被篡改正文 mapping-field-000",
            "source_ref": "knowledge:mutated",
            "collection_id": "mutated",
            "metadata_json": '{"mutated":true}',
            "content_hash": "0" * 64,
        }

        for field_name, mutated_value in mutations.items():
            with self.subTest(field=field_name):
                self.index._conn.execute("SAVEPOINT mapping_field_mutation")
                try:
                    self.index._conn.execute(
                        "UPDATE retrieval_documents SET %s = ? "
                        "WHERE tenant_id = ? AND document_id = ?" % field_name,
                        (mutated_value, document.tenant_id, document.document_id),
                    )
                    self.assertFalse(
                        self.index.matches_tenant(document.tenant_id, [document])
                    )
                finally:
                    self.index._conn.execute(
                        "ROLLBACK TO SAVEPOINT mapping_field_mutation"
                    )
                    self.index._conn.execute("RELEASE SAVEPOINT mapping_field_mutation")

        rowid = self.index._conn.execute(
            "SELECT rowid FROM retrieval_documents "
            "WHERE tenant_id = ? AND document_id = ?",
            (document.tenant_id, document.document_id),
        ).fetchone()[0]
        self.index._conn.execute("SAVEPOINT mapping_docsize_mutation")
        try:
            self.index._conn.execute(
                "DELETE FROM retrieval_documents_fts_docsize WHERE id = ?",
                (rowid,),
            )
            self.assertFalse(
                self.index.matches_tenant(document.tenant_id, [document])
            )
        finally:
            self.index._conn.execute(
                "ROLLBACK TO SAVEPOINT mapping_docsize_mutation"
            )
            self.index._conn.execute("RELEASE SAVEPOINT mapping_docsize_mutation")

        self.assertTrue(self.index.matches_tenant(document.tenant_id, [document]))

    def test_replace_tenant_preserves_other_tenant_rows(self):
        first = shared_document(
            "tenant-a", "tenant-a-old", "租户甲", "租户甲旧内容 tenant-a-old-741"
        )
        other = shared_document(
            "tenant-b", "tenant-b-stable", "租户乙", "租户乙稳定内容 tenant-b-852"
        )
        replacement = shared_document(
            "tenant-a", "tenant-a-new", "租户甲", "租户甲新内容 tenant-a-new-963"
        )
        self.index.index_documents([first, other])

        self.index.replace_tenant("tenant-a", [replacement])

        self.assertTrue(self.index.matches_tenant("tenant-a", [replacement]))
        self.assertTrue(self.index.matches_tenant("tenant-b", [other]))
        self.assertFalse(self.index.contains_document("tenant-a", first.document_id))
        self.assertTrue(self.index.contains_document("tenant-b", other.document_id))

    def test_verification_matches_fullwidth_ascii_and_mixed_unicode(self):
        documents = [
            shared_document(
                "tenant-a",
                "fullwidth",
                "唐文粹",
                "南宋嘉泰元年（１２０１）才付印。",
            ),
            shared_document(
                "tenant-a",
                "ascii",
                "ASCII Check",
                "Mixed CASE abcDEF012345 verification",
            ),
            shared_document(
                "tenant-a",
                "unicode",
                "混合 Unicode",
                "Cafe\u0301、CAFÉ、αβγ、😀😃😄共同出现。",
            ),
        ]

        self.index.index_documents(documents)

        self.assertTrue(self.index.matches_tenant("tenant-a", documents))
        for document in documents:
            self.assertTrue(self.index.matches_document(document))

    def test_integrity_check_rejects_index_containing_only_probe_terms(self):
        document = shared_document(
            "tenant-a",
            "partial-index",
            "partial corruption check",
            (
                "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ "
                "unique-tail-marker-987654321"
            ),
        )
        self.index.index_documents([document])
        terms = _build_verification_trigrams((document.title, document.text))
        probes = _verification_probes(terms)
        partial_text = " ".join(probes)
        row = self.index._conn.execute(
            "SELECT rowid, * FROM retrieval_documents "
            "WHERE tenant_id = ? AND document_id = ?",
            (document.tenant_id, document.document_id),
        ).fetchone()
        values = (
            row["rowid"],
            row["title"],
            row["text"],
            row["tenant_id"],
            row["document_id"],
            row["scope"],
            row["owner_user_id"],
            row["session_id"],
            row["sensitivity"],
        )
        self.index._conn.execute(
            """
            INSERT INTO retrieval_documents_fts(
                retrieval_documents_fts, rowid, title, text, tenant_id,
                document_id, scope, owner_user_id, session_id, sensitivity
            ) VALUES ('delete', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        self.index._conn.execute(
            """
            INSERT INTO retrieval_documents_fts(
                rowid, title, text, tenant_id, document_id, scope,
                owner_user_id, session_id, sensitivity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["rowid"],
                "",
                partial_text,
                row["tenant_id"],
                row["document_id"],
                row["scope"],
                row["owner_user_id"],
                row["session_id"],
                row["sensitivity"],
            ),
        )
        self.index._conn.commit()

        # 八个映射探针都存在，但整库完整性检查必须拒绝部分 posting。
        self.assertFalse(self.index.matches_document(document))
        self.assertFalse(self.index.matches_tenant("tenant-a", [document]))


def identity(tenant_id, user_id, *roles):
    """构造测试认证身份。"""

    return IdentityContext(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        roles=frozenset(roles),
        trace_id="trace-test",
        auth_source="test-authenticator",
    )


def shared_document(tenant_id, document_id, title, text):
    """构造共享测试文档。"""

    return IndexedDocument(
        tenant_id=tenant_id,
        document_id=document_id,
        scope=MemoryScope.SHARED,
        title=title,
        text=text,
        source_ref="knowledge:%s" % document_id,
    )


if __name__ == "__main__":
    unittest.main()
