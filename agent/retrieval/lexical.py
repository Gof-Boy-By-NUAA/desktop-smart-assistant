"""租户化中文词法检索索引。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agent.memory.governance import (
    IdentityContext,
    MemoryScope,
    Sensitivity,
    ValidationError,
)


_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_ASCII_RUN = re.compile(r"[A-Za-z0-9_]+")
_RETRIEVAL_TRIGGER_SQL = {
    "retrieval_documents_ai": """
        CREATE TRIGGER retrieval_documents_ai
        AFTER INSERT ON retrieval_documents BEGIN
            INSERT INTO retrieval_documents_fts(
                rowid, title, text, tenant_id, document_id, scope,
                owner_user_id, session_id, sensitivity
            ) VALUES (
                new.rowid, new.title, new.text, new.tenant_id, new.document_id,
                new.scope, new.owner_user_id, new.session_id, new.sensitivity
            );
        END
    """,
    "retrieval_documents_ad": """
        CREATE TRIGGER retrieval_documents_ad
        AFTER DELETE ON retrieval_documents BEGIN
            INSERT INTO retrieval_documents_fts(
                retrieval_documents_fts, rowid, title, text, tenant_id,
                document_id, scope, owner_user_id, session_id, sensitivity
            ) VALUES (
                'delete', old.rowid, old.title, old.text, old.tenant_id,
                old.document_id, old.scope, old.owner_user_id,
                old.session_id, old.sensitivity
            );
        END
    """,
    "retrieval_documents_au": """
        CREATE TRIGGER retrieval_documents_au
        AFTER UPDATE ON retrieval_documents BEGIN
            INSERT INTO retrieval_documents_fts(
                retrieval_documents_fts, rowid, title, text, tenant_id,
                document_id, scope, owner_user_id, session_id, sensitivity
            ) VALUES (
                'delete', old.rowid, old.title, old.text, old.tenant_id,
                old.document_id, old.scope, old.owner_user_id,
                old.session_id, old.sensitivity
            );
            INSERT INTO retrieval_documents_fts(
                rowid, title, text, tenant_id, document_id, scope,
                owner_user_id, session_id, sensitivity
            ) VALUES (
                new.rowid, new.title, new.text, new.tenant_id, new.document_id,
                new.scope, new.owner_user_id, new.session_id, new.sensitivity
            );
        END
    """,
}


@dataclass(frozen=True)
class IndexedDocument:
    """进入词法索引的权限化文档。"""

    tenant_id: str
    document_id: str
    scope: MemoryScope
    title: str
    text: str
    source_ref: str
    collection_id: str = "default"
    owner_user_id: Optional[str] = None
    session_id: Optional[str] = None
    sensitivity: Sensitivity = Sensitivity.PRIVATE
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "tenant_id",
            "document_id",
            "text",
            "source_ref",
            "collection_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError("%s 不能为空" % field_name)
        if not isinstance(self.title, str):
            raise ValidationError("title 必须是字符串")
        if self.scope is MemoryScope.SHARED:
            if self.owner_user_id is not None or self.session_id is not None:
                raise ValidationError("共享文档不能绑定用户或会话")
        elif not self.owner_user_id:
            raise ValidationError("用户或会话文档必须指定 owner_user_id")
        if self.scope is MemoryScope.USER and self.session_id is not None:
            raise ValidationError("用户文档不能绑定会话")
        if self.scope is MemoryScope.SESSION and not self.session_id:
            raise ValidationError("会话文档必须指定 session_id")
        if not isinstance(self.metadata, dict):
            raise ValidationError("metadata 必须是字典")
        try:
            json.dumps(self.metadata, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise ValidationError("metadata 必须可以序列化为 JSON") from error


@dataclass(frozen=True)
class LexicalSearchResult:
    """带来源和排序分量的词法检索结果。"""

    document_id: str
    title: str
    text: str
    source_ref: str
    score: float
    bm25_score: float
    query_coverage: float
    metadata: Dict[str, Any]


class TenantAwareLexicalIndex:
    """使用 SQLite FTS5 trigram 的权限化中文检索索引。"""

    def __init__(self, db_path: Path, candidate_limit: int = 40):
        if candidate_limit <= 0:
            raise ValidationError("candidate_limit 必须大于零")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.candidate_limit = candidate_limit
        self._lock = threading.RLock()
        self._conn = None
        last_error = None
        for attempt in range(10):
            conn = sqlite3.connect(
                str(self.db_path), timeout=30.0, check_same_thread=False
            )
            self._conn = conn
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=30000")
                # Large pages apply only to new DBs; never VACUUM at startup.
                conn.execute("PRAGMA page_size=65536")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA cache_size=-8192")
                conn.execute("PRAGMA temp_store=MEMORY")
                self._init_schema()
                last_error = None
                break
            except sqlite3.OperationalError as exc:
                conn.close()
                self._conn = None
                last_error = exc
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                time.sleep(min(0.5, 0.01 * (2 ** attempt)))
            except Exception:
                conn.close()
                self._conn = None
                raise
        if last_error is not None:
            raise last_error

    def _init_schema(self) -> None:
        """初始化内容表、FTS5 索引和同步触发器。"""

        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS retrieval_documents (
                tenant_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                scope TEXT NOT NULL CHECK(scope IN ('shared', 'user', 'session')),
                owner_user_id TEXT,
                session_id TEXT,
                sensitivity TEXT NOT NULL CHECK(sensitivity IN ('public', 'internal', 'private', 'restricted')),
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, document_id)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_documents_fts USING fts5(
                title,
                text,
                tenant_id UNINDEXED,
                document_id UNINDEXED,
                scope UNINDEXED,
                owner_user_id UNINDEXED,
                session_id UNINDEXED,
                sensitivity UNINDEXED,
                content='retrieval_documents',
                content_rowid='rowid',
                tokenize='trigram case_sensitive 0'
            );

            CREATE TRIGGER IF NOT EXISTS retrieval_documents_ai
            AFTER INSERT ON retrieval_documents BEGIN
                INSERT INTO retrieval_documents_fts(
                    rowid, title, text, tenant_id, document_id, scope,
                    owner_user_id, session_id, sensitivity
                ) VALUES (
                    new.rowid, new.title, new.text, new.tenant_id, new.document_id,
                    new.scope, new.owner_user_id, new.session_id, new.sensitivity
                );
            END;

            CREATE TRIGGER IF NOT EXISTS retrieval_documents_ad
            AFTER DELETE ON retrieval_documents BEGIN
                INSERT INTO retrieval_documents_fts(
                    retrieval_documents_fts, rowid, title, text, tenant_id,
                    document_id, scope, owner_user_id, session_id, sensitivity
                ) VALUES (
                    'delete', old.rowid, old.title, old.text, old.tenant_id,
                    old.document_id, old.scope, old.owner_user_id,
                    old.session_id, old.sensitivity
                );
            END;

            CREATE TRIGGER IF NOT EXISTS retrieval_documents_au
            AFTER UPDATE ON retrieval_documents BEGIN
                INSERT INTO retrieval_documents_fts(
                    retrieval_documents_fts, rowid, title, text, tenant_id,
                    document_id, scope, owner_user_id, session_id, sensitivity
                ) VALUES (
                    'delete', old.rowid, old.title, old.text, old.tenant_id,
                    old.document_id, old.scope, old.owner_user_id,
                    old.session_id, old.sensitivity
                );
                INSERT INTO retrieval_documents_fts(
                    rowid, title, text, tenant_id, document_id, scope,
                    owner_user_id, session_id, sensitivity
                ) VALUES (
                    new.rowid, new.title, new.text, new.tenant_id, new.document_id,
                    new.scope, new.owner_user_id, new.session_id, new.sensitivity
                );
            END;

            """
        )
        self._conn.commit()
        self._ensure_fts_triggers()

    def _ensure_fts_triggers(self) -> None:
        rows = self._conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
        actual = {
            str(row["name"]): _normalize_schema_sql(row["sql"])
            for row in rows
            if row["name"] in _RETRIEVAL_TRIGGER_SQL
        }
        expected = {
            name: _normalize_schema_sql(sql)
            for name, sql in _RETRIEVAL_TRIGGER_SQL.items()
        }
        if actual == expected:
            return

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for name in _RETRIEVAL_TRIGGER_SQL:
                self._conn.execute("DROP TRIGGER IF EXISTS %s" % name)
            for sql in _RETRIEVAL_TRIGGER_SQL.values():
                self._conn.execute(sql)
            self._conn.execute(
                "INSERT INTO retrieval_documents_fts(retrieval_documents_fts) "
                "VALUES ('rebuild')"
            )
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def index_documents(self, documents: Sequence[IndexedDocument]) -> None:
        """在单个事务中新增或更新文档。"""

        rows = self._document_rows(documents)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(_UPSERT_DOCUMENT_SQL, rows)
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def replace_collection(
        self,
        tenant_id: str,
        collection_id: str,
        documents: Sequence[IndexedDocument],
    ) -> None:
        """原子替换一个租户集合，并删除来源中已经消失的文档。"""

        if not tenant_id.strip() or not collection_id.strip():
            raise ValidationError("tenant_id 和 collection_id 不能为空")
        for document in documents:
            if document.tenant_id != tenant_id:
                raise ValidationError("集合文档 tenant_id 不一致")
            if document.collection_id != collection_id:
                raise ValidationError("集合文档 collection_id 不一致")

        rows = self._document_rows(documents)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS retrieval_sync_ids "
                    "(document_id TEXT PRIMARY KEY)"
                )
                self._conn.execute("DELETE FROM retrieval_sync_ids")
                self._conn.executemany(
                    "INSERT INTO retrieval_sync_ids(document_id) VALUES (?)",
                    [(document.document_id,) for document in documents],
                )
                self._conn.executemany(_UPSERT_DOCUMENT_SQL, rows)
                self._conn.execute(
                    """
                    DELETE FROM retrieval_documents
                    WHERE tenant_id = ? AND collection_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM retrieval_sync_ids AS synced
                          WHERE synced.document_id = retrieval_documents.document_id
                      )
                    """,
                    (tenant_id, collection_id),
                )
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def replace_tenant(
        self,
        tenant_id: str,
        documents: Sequence[IndexedDocument],
    ) -> None:
        """原子替换一个租户的全部索引文档，保留文档各自的集合标识。"""

        if not tenant_id.strip():
            raise ValidationError("tenant_id 不能为空")
        for document in documents:
            if document.tenant_id != tenant_id:
                raise ValidationError("租户文档 tenant_id 不一致")
        rows = self._document_rows(documents)
        with self._lock:
            if self._matches_tenant_rows(tenant_id, rows):
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                other_tenant = self._conn.execute(
                    "SELECT 1 FROM retrieval_documents "
                    "WHERE tenant_id <> ? LIMIT 1",
                    (tenant_id,),
                ).fetchone()
                if other_tenant is None:
                    self._replace_only_tenant(rows)
                else:
                    self._replace_tenant_incrementally(tenant_id, documents, rows)
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def replace_tenant_coordinated(
        self,
        tenant_id: str,
        documents: Sequence[IndexedDocument],
        allow_commit: Callable[[], bool],
    ) -> bool:
        """构建并核验租户索引，等待事实提交结果后再决定提交或回滚。"""

        if not tenant_id.strip():
            raise ValidationError("tenant_id 不能为空")
        if not callable(allow_commit):
            raise ValidationError("allow_commit 必须可以调用")
        for document in documents:
            if document.tenant_id != tenant_id:
                raise ValidationError("租户文档 tenant_id 不一致")
        rows = self._document_rows(documents)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                other_tenant = self._conn.execute(
                    "SELECT 1 FROM retrieval_documents "
                    "WHERE tenant_id <> ? LIMIT 1",
                    (tenant_id,),
                ).fetchone()
                if other_tenant is None:
                    self._replace_only_tenant(rows)
                else:
                    self._replace_tenant_incrementally(
                        tenant_id, documents, rows
                    )
                if not self._matches_tenant_rows(tenant_id, rows):
                    raise RuntimeError("租户索引内容集合核验失败")
                # 事务内精确核验内容行与 FTS docsize 映射；完整 posting 扫描由
                # matches_tenant() 在启动恢复、健康检查和独立验收时执行。每批次
                # 同步扫描整库会把可重建派生索引的成本错误地放大到写入关键路径。
                should_commit = bool(allow_commit())
            except Exception:
                self._conn.rollback()
                raise
            if not should_commit:
                self._conn.rollback()
                return False
            try:
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return True

    def _replace_only_tenant(self, rows: Sequence[Tuple[object, ...]]) -> None:
        """在检索库只有一个租户时原子重建完整 FTS，减少碎片和触发器往返。"""

        for name in _RETRIEVAL_TRIGGER_SQL:
            self._conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        self._conn.execute("DELETE FROM retrieval_documents")
        self._conn.executemany(_INSERT_DOCUMENT_SQL, rows)
        self._conn.execute(
            "INSERT INTO retrieval_documents_fts(retrieval_documents_fts) "
            "VALUES ('rebuild')"
        )
        for sql in _RETRIEVAL_TRIGGER_SQL.values():
            self._conn.execute(sql)

    def _replace_tenant_incrementally(
        self,
        tenant_id: str,
        documents: Sequence[IndexedDocument],
        rows: Sequence[Tuple[object, ...]],
    ) -> None:
        """存在其他租户时只更新目标租户，保持共享检索库隔离。"""

        self._conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS retrieval_tenant_sync_ids "
            "(document_id TEXT PRIMARY KEY)"
        )
        self._conn.execute("DELETE FROM retrieval_tenant_sync_ids")
        self._conn.executemany(
            "INSERT INTO retrieval_tenant_sync_ids(document_id) VALUES (?)",
            [(document.document_id,) for document in documents],
        )
        self._conn.executemany(_UPSERT_DOCUMENT_SQL, rows)
        self._conn.execute(
            """
            DELETE FROM retrieval_documents
            WHERE tenant_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM retrieval_tenant_sync_ids AS synced
                  WHERE synced.document_id = retrieval_documents.document_id
              )
            """,
            (tenant_id,),
        )

    def delete_document(self, tenant_id: str, document_id: str) -> bool:
        """按租户删除单个索引文档。"""

        if not tenant_id.strip() or not document_id.strip():
            raise ValidationError("tenant_id 和 document_id 不能为空")
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM retrieval_documents WHERE tenant_id = ? AND document_id = ?",
                (tenant_id, document_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def contains_document(self, tenant_id: str, document_id: str) -> bool:
        """核验指定租户的索引文档是否仍然存在。"""

        if not tenant_id.strip() or not document_id.strip():
            raise ValidationError("tenant_id 和 document_id 不能为空")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM retrieval_documents
                WHERE tenant_id = ? AND document_id = ? LIMIT 1
                """,
                (tenant_id, document_id),
            ).fetchone()
        return row is not None

    def matches_document(self, document: IndexedDocument) -> bool:
        """核验内容表和 FTS5 查询面是否都与预期一致。"""

        with self._lock:
            return self._matches_document_mapping(document) and self._fts_integrity_valid()

    def matches_tenant(
        self,
        tenant_id: str,
        documents: Sequence[IndexedDocument],
    ) -> bool:
        """核验一个租户的完整派生索引集合。"""

        if not tenant_id.strip():
            raise ValidationError("tenant_id 不能为空")
        rows = self._document_rows(documents)
        expected_ids = [document.document_id for document in documents]
        if len(expected_ids) != len(set(expected_ids)):
            return False
        if any(document.tenant_id != tenant_id for document in documents):
            return False
        with self._lock:
            if not self._matches_tenant_rows(tenant_id, rows):
                return False
            return self._fts_integrity_valid()

    def _matches_tenant_rows(
        self, tenant_id: str, rows: Sequence[Tuple[object, ...]]
    ) -> bool:
        """单次读回精确核验租户内容行和 FTS 文档大小映射。"""

        try:
            columns = (
                "tenant_id, document_id, scope, owner_user_id, session_id, "
                "sensitivity, title, text, source_ref, collection_id, "
                "metadata_json, content_hash"
            )
            actual = [
                tuple(row)
                for row in self._conn.execute(
                    "SELECT %s FROM retrieval_documents WHERE tenant_id = ? "
                    "ORDER BY tenant_id, document_id" % columns,
                    (tenant_id,),
                )
            ]
            expected = sorted(rows, key=lambda row: (row[0], row[1]))
            if actual != expected:
                return False
            missing_docsize = self._conn.execute(
                """
                SELECT 1
                FROM retrieval_documents AS documents
                LEFT JOIN retrieval_documents_fts_docsize AS sizes
                  ON sizes.id = documents.rowid
                WHERE documents.tenant_id = ? AND sizes.id IS NULL
                LIMIT 1
                """,
                (tenant_id,),
            ).fetchone()
            return missing_docsize is None
        except sqlite3.DatabaseError:
            return False

    def _matches_document_mapping(self, document: IndexedDocument) -> bool:
        """核验内容行、FTS 行标识和原始字符探针映射。"""

        expected = self._document_rows((document,))[0]
        row = self._conn.execute(
            """
            SELECT
                rowid, tenant_id, document_id, scope, owner_user_id,
                session_id, sensitivity, title, text, source_ref,
                collection_id, metadata_json, content_hash
            FROM retrieval_documents
            WHERE tenant_id = ? AND document_id = ?
            """,
            (document.tenant_id, document.document_id),
        ).fetchone()
        if row is None or tuple(row)[1:] != expected:
            return False
        rowid = int(row["rowid"])
        docsize = self._conn.execute(
            "SELECT 1 FROM retrieval_documents_fts_docsize WHERE id = ?",
            (rowid,),
        ).fetchone()
        if docsize is None:
            return False
        terms = _build_verification_trigrams((document.title, document.text))
        probes = _verification_probes(terms)
        if not probes:
            return True
        match_query = " AND ".join(
            '"%s"' % _escape_match(term) for term in probes
        )
        indexed = self._conn.execute(
            "SELECT 1 FROM retrieval_documents_fts "
            "WHERE retrieval_documents_fts MATCH ? AND rowid = ?",
            (match_query, rowid),
        ).fetchone()
        return indexed is not None

    def _fts_integrity_valid(self) -> bool:
        """精确核验外部内容表与整库 FTS5 posting 是否一致。"""

        try:
            self._conn.execute("SAVEPOINT retrieval_fts_integrity_check")
        except sqlite3.DatabaseError:
            return False

        valid = True
        try:
            try:
                self._conn.execute(
                    "INSERT INTO retrieval_documents_fts("
                    "retrieval_documents_fts, rank) "
                    "VALUES ('integrity-check', 1)"
                )
            except sqlite3.DatabaseError:
                valid = False
        finally:
            try:
                self._conn.execute(
                    "ROLLBACK TO SAVEPOINT retrieval_fts_integrity_check"
                )
                self._conn.execute(
                    "RELEASE SAVEPOINT retrieval_fts_integrity_check"
                )
            except sqlite3.DatabaseError:
                valid = False
                try:
                    self._conn.rollback()
                except sqlite3.DatabaseError:
                    pass
        return valid

    @staticmethod
    def _document_rows(documents: Sequence[IndexedDocument]) -> List[Tuple[object, ...]]:
        """把领域对象转换为批量写入参数。"""

        return [
            (
                document.tenant_id,
                document.document_id,
                document.scope.value,
                document.owner_user_id,
                document.session_id,
                document.sensitivity.value,
                document.title,
                document.text,
                document.source_ref,
                document.collection_id,
                json.dumps(
                    document.metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
            )
            for document in documents
        ]

    def search(
        self,
        identity: IdentityContext,
        query: str,
        limit: int = 10,
        session_id: Optional[str] = None,
        collection_ids: Optional[Sequence[str]] = None,
    ) -> Sequence[LexicalSearchResult]:
        """在 SQL 权限过滤后执行候选召回和覆盖率重排。"""

        if not isinstance(query, str) or not query.strip():
            raise ValidationError("query 不能为空")
        if limit <= 0:
            raise ValidationError("limit 必须大于零")
        normalized_collections = _normalize_collection_ids(collection_ids)

        terms = build_query_trigrams(query)
        if not terms:
            return self._search_short_query(
                identity,
                query,
                limit,
                session_id,
                normalized_collections,
            )
        match_query = " OR ".join('"%s"' % _escape_match(term) for term in terms)
        visibility_sql, visibility_params = self._visibility_filter(identity, session_id)
        collection_sql, collection_params = _collection_filter(normalized_collections)
        candidate_limit = max(limit, self.candidate_limit)
        sql = """
            SELECT documents.*, bm25(retrieval_documents_fts, 4.0, 1.0) AS rank
            FROM retrieval_documents_fts
            JOIN retrieval_documents AS documents
              ON documents.rowid = retrieval_documents_fts.rowid
            WHERE retrieval_documents_fts MATCH ?
              AND documents.tenant_id = ?
              AND %s
              %s
            ORDER BY rank ASC
            LIMIT ?
        """ % (visibility_sql, collection_sql)
        params = [match_query, identity.tenant_id]
        params.extend(visibility_params)
        params.extend(collection_params)
        params.append(candidate_limit)

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        results = [self._rank_row(row, terms) for row in rows]
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:limit]

    def _visibility_filter(
        self, identity: IdentityContext, session_id: Optional[str]
    ) -> Tuple[str, List[object]]:
        """生成租户内的作用域和敏感级别过滤条件。"""

        conditions = [
            "(documents.scope = 'shared'",
            " OR (documents.scope = 'user' AND documents.owner_user_id = ?)",
            " OR (documents.scope = 'session' AND documents.owner_user_id = ? AND documents.session_id = ?))",
        ]
        params: List[object] = [identity.actor_user_id, identity.actor_user_id, session_id]
        if not identity.has_any_role(
            "admin", "memory:read_restricted", "knowledge:read_restricted"
        ):
            conditions.append(" AND documents.sensitivity <> 'restricted'")
        return "".join(conditions), params

    @staticmethod
    def _rank_row(row: sqlite3.Row, terms: Sequence[str]) -> LexicalSearchResult:
        """融合 BM25、正文覆盖率和标题覆盖率。"""

        normalized_title = normalize_text(row["title"])
        normalized_text = normalize_text(row["text"])
        matched_text = sum(term in normalized_text for term in terms)
        matched_title = sum(term in normalized_title for term in terms)
        coverage = matched_text / float(len(terms))
        title_coverage = matched_title / float(len(terms))
        rank = abs(float(row["rank"] or 0.0))
        bm25_score = rank / (1.0 + rank)
        raw_score = 0.55 * bm25_score + 0.35 * coverage + 0.10 * title_coverage
        score = 0.3 + 0.69 * min(1.0, raw_score)
        return LexicalSearchResult(
            document_id=row["document_id"],
            title=row["title"],
            text=row["text"],
            source_ref=row["source_ref"],
            score=score,
            bm25_score=bm25_score,
            query_coverage=coverage,
            metadata=json.loads(row["metadata_json"]),
        )

    def _search_short_query(
        self,
        identity: IdentityContext,
        query: str,
        limit: int,
        session_id: Optional[str],
        collection_ids: Sequence[str],
    ) -> Sequence[LexicalSearchResult]:
        """处理不足三个字符、无法进入 trigram 索引的查询。"""

        visibility_sql, visibility_params = self._visibility_filter(identity, session_id)
        collection_sql, collection_params = _collection_filter(collection_ids)
        normalized = normalize_text(query)
        escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = """
            SELECT documents.*, 0.0 AS rank
            FROM retrieval_documents AS documents
            WHERE documents.tenant_id = ?
              AND %s
              %s
              AND (LOWER(documents.title) LIKE ? ESCAPE '\\'
                   OR LOWER(documents.text) LIKE ? ESCAPE '\\')
            LIMIT ?
        """ % (visibility_sql, collection_sql)
        params: List[object] = [identity.tenant_id]
        params.extend(visibility_params)
        params.extend(collection_params)
        params.extend(["%%%s%%" % escaped, "%%%s%%" % escaped, limit])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            LexicalSearchResult(
                document_id=row["document_id"],
                title=row["title"],
                text=row["text"],
                source_ref=row["source_ref"],
                score=0.5,
                bm25_score=0.0,
                query_coverage=1.0,
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]

    def close(self) -> None:
        """释放 SQLite 连接。"""

        with self._lock:
            self._conn.close()


def normalize_text(value: str) -> str:
    """统一全角字符和大小写，保留中文字符顺序。"""

    return unicodedata.normalize("NFKC", value).lower()


def build_query_trigrams(query: str) -> Sequence[str]:
    """从中文和英文连续片段生成稳定的去重字符三元组。"""

    normalized = normalize_text(query)
    terms: List[str] = []
    seen = set()
    for pattern in (_CJK_RUN, _ASCII_RUN):
        for match in pattern.finditer(normalized):
            run = match.group(0)
            if len(run) < 3:
                continue
            for index in range(len(run) - 2):
                term = run[index : index + 3]
                if term not in seen:
                    seen.add(term)
                    terms.append(term)
    return terms


def _escape_match(value: str) -> str:
    """转义 FTS5 引号。"""

    return value.replace('"', '""')


def _build_verification_trigrams(values: Sequence[str]) -> Sequence[str]:
    """按 FTS5 原始字符输入生成探针，不引入 Python 侧 Unicode 归一化。"""

    terms: List[str] = []
    seen = set()
    for value in values:
        for index in range(len(value) - 2):
            term = value[index : index + 3]
            # SQLite 参数不能携带 NUL；整库完整性检查仍会覆盖这类内容。
            if "\x00" in term or term in seen:
                continue
            seen.add(term)
            terms.append(term)
    return tuple(terms)


def _verification_probes(terms: Sequence[str], limit: int = 8) -> Sequence[str]:
    """从全文三元组中选择固定上限且覆盖首尾的倒排探针。"""

    if len(terms) <= limit:
        return tuple(terms)
    indexes = {
        round(index * (len(terms) - 1) / float(limit - 1))
        for index in range(limit)
    }
    return tuple(terms[index] for index in sorted(indexes))


def _normalize_schema_sql(value: object) -> str:
    """生成 SQLite 模式定义的稳定比较文本。"""

    return " ".join(str(value or "").casefold().split())


def _normalize_collection_ids(
    collection_ids: Optional[Sequence[str]],
) -> Sequence[str]:
    """验证集合过滤值，空序列表示不限制集合。"""

    if collection_ids is None:
        return ()
    normalized = []
    seen = set()
    for collection_id in collection_ids:
        if not isinstance(collection_id, str) or not collection_id.strip():
            raise ValidationError("collection_ids 不能包含空值")
        value = collection_id.strip()
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def _collection_filter(collection_ids: Sequence[str]) -> Tuple[str, List[object]]:
    """生成参数化集合过滤条件。"""

    if not collection_ids:
        return "", []
    placeholders = ",".join("?" for _ in collection_ids)
    return "AND documents.collection_id IN (%s)" % placeholders, list(collection_ids)


_INSERT_DOCUMENT_SQL = """
    INSERT INTO retrieval_documents (
        tenant_id, document_id, scope, owner_user_id, session_id,
        sensitivity, title, text, source_ref, collection_id,
        metadata_json, content_hash, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
"""


_UPSERT_DOCUMENT_SQL = _INSERT_DOCUMENT_SQL + """
    ON CONFLICT(tenant_id, document_id) DO UPDATE SET
        scope = excluded.scope,
        owner_user_id = excluded.owner_user_id,
        session_id = excluded.session_id,
        sensitivity = excluded.sensitivity,
        title = excluded.title,
        text = excluded.text,
        source_ref = excluded.source_ref,
        collection_id = excluded.collection_id,
        metadata_json = excluded.metadata_json,
        content_hash = excluded.content_hash,
        updated_at = CURRENT_TIMESTAMP
    WHERE retrieval_documents.scope IS NOT excluded.scope
       OR retrieval_documents.owner_user_id IS NOT excluded.owner_user_id
       OR retrieval_documents.session_id IS NOT excluded.session_id
       OR retrieval_documents.sensitivity IS NOT excluded.sensitivity
       OR retrieval_documents.title IS NOT excluded.title
       OR retrieval_documents.text IS NOT excluded.text
       OR retrieval_documents.source_ref IS NOT excluded.source_ref
       OR retrieval_documents.collection_id IS NOT excluded.collection_id
       OR retrieval_documents.metadata_json IS NOT excluded.metadata_json
       OR retrieval_documents.content_hash IS NOT excluded.content_hash
"""

