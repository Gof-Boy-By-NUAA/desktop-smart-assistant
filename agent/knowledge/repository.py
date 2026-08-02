"""SQLite 知识事实库。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from agent.memory.governance import MemoryScope, Sensitivity

from .contracts import (
    KnowledgeDocumentRecord,
    KnowledgeEvidence,
    KnowledgeIdempotencyConflictError,
    KnowledgeNotFoundError,
    KnowledgeSection,
    KnowledgeStatus,
    KnowledgeValidationError,
)


_PROJECTION_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class KnowledgeNewDocumentBatchItem:
    """已完成业务校验、仅待原子落库的全新知识文档。"""

    record: KnowledgeDocumentRecord
    parser_version: str
    sections: Sequence[KnowledgeSection]
    evidence: Sequence[KnowledgeEvidence]
    audit_event_id: str
    audit_action: str
    audit_details: Dict[str, object]
    idempotency_key: str
    request_hash: str


class KnowledgeRepository:
    """保存不可变文档版本、章节、证据、幂等结果和审计事件。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._read_lock = threading.RLock()
        self._read_conn = None
        last_error = None
        for attempt in range(10):
            try:
                with self.connection() as conn:
                    self._init_schema(conn)
                last_error = None
                break
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                time.sleep(min(0.5, 0.01 * (2 ** attempt)))
        if last_error is not None:
            raise last_error
        self._read_conn = self._connect(check_same_thread=False)

    def _connect(self, check_same_thread: bool = True) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=check_same_thread,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        # 新事实库使用剖析验证过的大页；既有库保持原页大小，不执行 VACUUM。
        conn.execute("PRAGMA page_size=65536")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def connection(self):
        """打开只读或短事务连接，并确保 Windows 文件句柄及时释放。"""

        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        """串行执行写事务，失败时保留原始异常。"""

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.executescript(
                """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                tenant_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0),
                status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'revoked')),
                scope TEXT NOT NULL CHECK(scope IN ('shared', 'user', 'session')),
                owner_user_id TEXT,
                session_id TEXT,
                sensitivity TEXT NOT NULL CHECK(sensitivity IN ('public', 'internal', 'private', 'restricted')),
                collection_id TEXT NOT NULL,
                title TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                projection_path TEXT,
                projection_key TEXT,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_by TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, document_id, version)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS knowledge_documents_one_active
            ON knowledge_documents(tenant_id, document_id)
            WHERE status = 'active';

            CREATE INDEX IF NOT EXISTS knowledge_documents_source
            ON knowledge_documents(
                tenant_id, source_ref, scope, owner_user_id, session_id, status
            );

            CREATE TABLE IF NOT EXISTS knowledge_sections (
                tenant_id TEXT NOT NULL,
                section_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                document_version INTEGER NOT NULL,
                section_index INTEGER NOT NULL,
                heading TEXT NOT NULL,
                char_start INTEGER NOT NULL,
                char_end INTEGER NOT NULL,
                byte_start INTEGER NOT NULL,
                byte_end INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY (tenant_id, section_id),
                UNIQUE (tenant_id, document_id, document_version, section_index),
                FOREIGN KEY (tenant_id, document_id, document_version)
                    REFERENCES knowledge_documents(tenant_id, document_id, version)
            );

            CREATE TABLE IF NOT EXISTS knowledge_evidence (
                tenant_id TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                document_version INTEGER NOT NULL,
                section_id TEXT NOT NULL,
                evidence_index INTEGER NOT NULL,
                quote TEXT NOT NULL,
                char_start INTEGER NOT NULL,
                char_end INTEGER NOT NULL,
                byte_start INTEGER NOT NULL,
                byte_end INTEGER NOT NULL,
                quote_hash TEXT NOT NULL,
                PRIMARY KEY (tenant_id, evidence_id),
                UNIQUE (tenant_id, document_id, document_version, evidence_index),
                FOREIGN KEY (tenant_id, document_id, document_version)
                    REFERENCES knowledge_documents(tenant_id, document_id, version),
                FOREIGN KEY (tenant_id, section_id)
                    REFERENCES knowledge_sections(tenant_id, section_id)
            );

            CREATE INDEX IF NOT EXISTS knowledge_evidence_document
            ON knowledge_evidence(tenant_id, document_id, document_version);

            CREATE TABLE IF NOT EXISTS knowledge_audit (
                event_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                actor_user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                document_id TEXT NOT NULL,
                document_version INTEGER NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS knowledge_audit_document
            ON knowledge_audit(tenant_id, document_id, created_at);

            CREATE TABLE IF NOT EXISTS knowledge_idempotency (
                tenant_id TEXT NOT NULL,
                actor_user_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                document_id TEXT NOT NULL,
                document_version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, actor_user_id, operation, idempotency_key)
            );

            CREATE TABLE IF NOT EXISTS knowledge_runtime_state (
                state_key TEXT PRIMARY KEY,
                state_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_derivative_jobs (
                tenant_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                target_version INTEGER NOT NULL CHECK(target_version > 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, document_id)
            );

            CREATE TABLE IF NOT EXISTS knowledge_derivative_batches (
                tenant_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, batch_id)
            );

            CREATE TABLE IF NOT EXISTS knowledge_categories (
                tenant_id TEXT NOT NULL,
                scope TEXT NOT NULL CHECK(scope IN ('shared', 'user', 'session')),
                owner_user_id TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                category_path TEXT NOT NULL,
                category_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (
                    tenant_id, scope, owner_user_id, session_id, category_key
                ),
                CHECK (
                    (scope = 'shared' AND owner_user_id = '' AND session_id = '')
                    OR (scope = 'user' AND owner_user_id <> '' AND session_id = '')
                    OR (scope = 'session' AND owner_user_id <> '' AND session_id <> '')
                )
            );

            CREATE INDEX IF NOT EXISTS knowledge_categories_tenant
            ON knowledge_categories(tenant_id, category_key);
            """
            )
            KnowledgeRepository._migrate_projection_schema(conn)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    @staticmethod
    def _migrate_projection_schema(conn: sqlite3.Connection) -> None:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(knowledge_documents)")
        }
        if user_version > _PROJECTION_SCHEMA_VERSION:
            raise KnowledgeValidationError(
                "知识事实库版本高于当前程序支持范围"
            )
        if user_version == _PROJECTION_SCHEMA_VERSION:
            if (
                "projection_key" not in columns
                or not KnowledgeRepository._projection_indexes_valid(conn)
                or not KnowledgeRepository._projection_keys_valid(conn)
                or KnowledgeRepository._has_duplicate_active_projection(conn)
                or KnowledgeRepository._has_duplicate_active_logical_path(conn)
                or not KnowledgeRepository._category_schema_valid(conn)
            ):
                raise KnowledgeValidationError(
                    "知识事实库投影索引定义不一致"
                )
            return

        if "projection_key" not in columns:
            conn.execute(
                "ALTER TABLE knowledge_documents ADD COLUMN projection_key TEXT"
            )
        conn.execute("DROP INDEX IF EXISTS knowledge_documents_projection")
        conn.execute("DROP INDEX IF EXISTS knowledge_documents_one_projection")
        conn.execute("DROP INDEX IF EXISTS knowledge_documents_one_shared_path")
        conn.execute("DROP INDEX IF EXISTS knowledge_documents_one_user_path")
        conn.execute("DROP INDEX IF EXISTS knowledge_documents_one_session_path")
        projection_rows = conn.execute(
            """
            SELECT rowid, projection_path FROM knowledge_documents
            WHERE projection_path IS NOT NULL
            """
        ).fetchall()
        conn.executemany(
            "UPDATE knowledge_documents SET projection_key = ? WHERE rowid = ?",
            [
                (KnowledgeRepository.projection_key(row["projection_path"]), row["rowid"])
                for row in projection_rows
            ],
        )
        conn.execute(
            """
            UPDATE knowledge_documents SET projection_key = NULL
            WHERE projection_path IS NULL AND projection_key IS NOT NULL
            """
        )
        if KnowledgeRepository._has_duplicate_active_projection(conn):
            raise KnowledgeValidationError(
                "知识事实库存在冲突的有效 projection_path"
            )
        if KnowledgeRepository._has_duplicate_active_logical_path(conn):
            raise KnowledgeValidationError(
                "知识事实库存在同一身份作用域内冲突的有效知识路径"
            )
        conn.execute(
            """
            CREATE INDEX knowledge_documents_projection
            ON knowledge_documents(projection_key, status)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX knowledge_documents_one_projection
            ON knowledge_documents(projection_key)
            WHERE status = 'active' AND projection_key IS NOT NULL
              AND scope = 'shared'
              AND sensitivity IN ('public', 'internal')
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX knowledge_documents_one_shared_path
            ON knowledge_documents(tenant_id, projection_key)
            WHERE status = 'active' AND projection_key IS NOT NULL
              AND scope = 'shared'
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX knowledge_documents_one_user_path
            ON knowledge_documents(tenant_id, owner_user_id, projection_key)
            WHERE status = 'active' AND projection_key IS NOT NULL
              AND scope = 'user'
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX knowledge_documents_one_session_path
            ON knowledge_documents(
                tenant_id, owner_user_id, session_id, projection_key
            )
            WHERE status = 'active' AND projection_key IS NOT NULL
              AND scope = 'session'
            """
        )
        conn.execute(
            "PRAGMA user_version = %d" % _PROJECTION_SCHEMA_VERSION
        )

    @staticmethod
    def _has_duplicate_active_projection(conn: sqlite3.Connection) -> bool:
        duplicate = conn.execute(
            """
            SELECT projection_key FROM knowledge_documents
            WHERE status = 'active' AND projection_key IS NOT NULL
              AND scope = 'shared'
              AND sensitivity IN ('public', 'internal')
            GROUP BY projection_key HAVING COUNT(*) > 1 LIMIT 1
            """
        ).fetchone()
        return duplicate is not None

    @staticmethod
    def _has_duplicate_active_logical_path(conn: sqlite3.Connection) -> bool:
        """检查每种可见范围内部是否存在含义相同的有效逻辑路径。"""

        checks = (
            """
            SELECT 1 FROM knowledge_documents
            WHERE status = 'active' AND projection_key IS NOT NULL
              AND scope = 'shared'
            GROUP BY tenant_id, projection_key HAVING COUNT(*) > 1 LIMIT 1
            """,
            """
            SELECT 1 FROM knowledge_documents
            WHERE status = 'active' AND projection_key IS NOT NULL
              AND scope = 'user'
            GROUP BY tenant_id, owner_user_id, projection_key
            HAVING COUNT(*) > 1 LIMIT 1
            """,
            """
            SELECT 1 FROM knowledge_documents
            WHERE status = 'active' AND projection_key IS NOT NULL
              AND scope = 'session'
            GROUP BY tenant_id, owner_user_id, session_id, projection_key
            HAVING COUNT(*) > 1 LIMIT 1
            """,
        )
        return any(conn.execute(statement).fetchone() is not None for statement in checks)

    @staticmethod
    def _category_schema_valid(conn: sqlite3.Connection) -> bool:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(knowledge_categories)")
        }
        return columns == {
            "tenant_id",
            "scope",
            "owner_user_id",
            "session_id",
            "category_path",
            "category_key",
            "created_at",
        }

    @staticmethod
    def _projection_keys_valid(conn: sqlite3.Connection) -> bool:
        rows = conn.execute(
            "SELECT projection_path, projection_key FROM knowledge_documents"
        ).fetchall()
        for row in rows:
            projection_path = row["projection_path"]
            projection_key = row["projection_key"]
            if projection_path is None:
                if projection_key is not None:
                    return False
            elif projection_key != KnowledgeRepository.projection_key(
                str(projection_path)
            ):
                return False
        return True

    @staticmethod
    def _projection_indexes_valid(conn: sqlite3.Connection) -> bool:
        indexes = {
            str(row["name"]): row
            for row in conn.execute("PRAGMA index_list(knowledge_documents)")
        }
        definitions = {
            "knowledge_documents_projection": (
                ["projection_key", "status"],
                False,
                False,
                None,
            ),
            "knowledge_documents_one_projection": (
                ["projection_key"],
                True,
                True,
                "where status = 'active' and projection_key is not null "
                "and scope = 'shared' "
                "and sensitivity in ('public', 'internal')",
            ),
            "knowledge_documents_one_shared_path": (
                ["tenant_id", "projection_key"],
                True,
                True,
                "where status = 'active' and projection_key is not null "
                "and scope = 'shared'",
            ),
            "knowledge_documents_one_user_path": (
                ["tenant_id", "owner_user_id", "projection_key"],
                True,
                True,
                "where status = 'active' and projection_key is not null "
                "and scope = 'user'",
            ),
            "knowledge_documents_one_session_path": (
                ["tenant_id", "owner_user_id", "session_id", "projection_key"],
                True,
                True,
                "where status = 'active' and projection_key is not null "
                "and scope = 'session'",
            ),
        }
        for name, (columns, unique, partial, predicate) in definitions.items():
            index = indexes.get(name)
            if index is None:
                return False
            actual_columns = [
                str(row["name"])
                for row in conn.execute("PRAGMA index_info(%s)" % name)
            ]
            if (
                actual_columns != columns
                or bool(index["unique"]) is not unique
                or bool(index["partial"]) is not partial
            ):
                return False
            if predicate is None:
                continue
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (name,),
            ).fetchone()
            normalized = (
                " ".join(str(schema["sql"]).casefold().split())
                if schema is not None and schema["sql"] is not None
                else ""
            )
            if predicate not in normalized:
                return False
        return True

    @staticmethod
    def projection_key(projection_path: str) -> str:
        """生成跨平台稳定、大小写不敏感的知识路径键。"""

        return unicodedata.normalize(
            "NFC", projection_path.replace("\\", "/").strip("/")
        ).casefold()

    @staticmethod
    def request_hash(payload: Dict[str, object]) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def find_idempotent_result(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        actor_user_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
    ) -> Optional[KnowledgeDocumentRecord]:
        row = conn.execute(
            """
            SELECT request_hash, document_id, document_version
            FROM knowledge_idempotency
            WHERE tenant_id = ? AND actor_user_id = ?
              AND operation = ? AND idempotency_key = ?
            """,
            (tenant_id, actor_user_id, operation, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise KnowledgeIdempotencyConflictError(
                "同一个知识幂等键对应了不同请求"
            )
        return self.get_version(
            conn,
            tenant_id,
            row["document_id"],
            int(row["document_version"]),
        )

    @staticmethod
    def has_any_idempotency_key(
        conn: sqlite3.Connection,
        tenant_id: str,
        actor_user_id: str,
        operation: str,
        idempotency_keys: Sequence[str],
    ) -> bool:
        """批量探测既有幂等键；命中时由调用方回退完整重放语义。"""

        for keys in _chunks(tuple(idempotency_keys), 400):
            placeholders = ",".join("?" for _ in keys)
            row = conn.execute(
                "SELECT 1 FROM knowledge_idempotency "
                "WHERE tenant_id = ? AND actor_user_id = ? AND operation = ? "
                "AND idempotency_key IN (%s) LIMIT 1" % placeholders,
                (tenant_id, actor_user_id, operation, *keys),
            ).fetchone()
            if row is not None:
                return True
        return False

    @staticmethod
    def has_any_active_source(
        conn: sqlite3.Connection,
        tenant_id: str,
        sources: Sequence[tuple],
    ) -> bool:
        """批量探测身份作用域内的有效来源，避免改变更新语义。"""

        expected = set(sources)
        source_refs = tuple(dict.fromkeys(source[0] for source in sources))
        for refs in _chunks(source_refs, 400):
            placeholders = ",".join("?" for _ in refs)
            rows = conn.execute(
                "SELECT source_ref, owner_user_id, session_id "
                "FROM knowledge_documents WHERE tenant_id = ? "
                "AND status = 'active' AND source_ref IN (%s)" % placeholders,
                (tenant_id, *refs),
            ).fetchall()
            if any(
                (str(row["source_ref"]), row["owner_user_id"], row["session_id"])
                in expected
                for row in rows
            ):
                return True
        return False

    @staticmethod
    def has_any_active_document(
        conn: sqlite3.Connection, tenant_id: str
    ) -> bool:
        """判断租户是否已有有效事实，用于限定全新导入流水线。"""

        row = conn.execute(
            "SELECT 1 FROM knowledge_documents "
            "WHERE tenant_id = ? AND status = 'active' LIMIT 1",
            (tenant_id,),
        ).fetchone()
        return row is not None

    @staticmethod
    def save_idempotent_result(
        conn: sqlite3.Connection,
        tenant_id: str,
        actor_user_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        record: KnowledgeDocumentRecord,
    ) -> None:
        conn.execute(
            """
            INSERT INTO knowledge_idempotency (
                tenant_id, actor_user_id, operation, idempotency_key,
                request_hash, document_id, document_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                actor_user_id,
                operation,
                idempotency_key,
                request_hash,
                record.document_id,
                record.version,
                _utc_now(),
            ),
        )

    def find_active_by_source(
        self,
        tenant_id: str,
        source_ref: str,
        owner_user_id: Optional[str],
        session_id: Optional[str],
    ) -> Optional[KnowledgeDocumentRecord]:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM knowledge_documents
                WHERE tenant_id = ? AND source_ref = ? AND status = 'active'
                  AND owner_user_id IS ? AND session_id IS ?
                ORDER BY version DESC LIMIT 1
                """,
                (tenant_id, source_ref, owner_user_id, session_id),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def get_active_by_source(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        source_ref: str,
        owner_user_id: Optional[str],
        session_id: Optional[str],
    ) -> Optional[KnowledgeDocumentRecord]:
        """在调用方事务中按稳定来源定位当前版本。"""

        row = conn.execute(
            """
            SELECT * FROM knowledge_documents
            WHERE tenant_id = ? AND source_ref = ? AND status = 'active'
              AND owner_user_id IS ? AND session_id IS ?
            ORDER BY version DESC LIMIT 1
            """,
            (tenant_id, source_ref, owner_user_id, session_id),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def get_active_by_projection(
        self,
        conn: sqlite3.Connection,
        projection_path: str,
    ) -> Optional[KnowledgeDocumentRecord]:
        """在调用方事务中跨租户定位物理投影路径的有效占用者。"""

        row = conn.execute(
            """
            SELECT * FROM knowledge_documents
            WHERE projection_key = ? AND status = 'active'
              AND scope = 'shared'
              AND sensitivity IN ('public', 'internal')
            ORDER BY version DESC LIMIT 1
            """,
            (self.projection_key(projection_path),),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def get_active_by_logical_path(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        logical_path: str,
        scope: MemoryScope,
        owner_user_id: Optional[str],
        session_id: Optional[str],
    ) -> Optional[KnowledgeDocumentRecord]:
        """在指定身份作用域内定位有效知识路径。"""

        row = conn.execute(
            """
            SELECT * FROM knowledge_documents
            WHERE tenant_id = ? AND projection_key = ? AND status = 'active'
              AND scope = ? AND owner_user_id IS ? AND session_id IS ?
            ORDER BY version DESC LIMIT 1
            """,
            (
                tenant_id,
                self.projection_key(logical_path),
                scope.value,
                owner_user_id,
                session_id,
            ),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def find_active_by_logical_path(
        self,
        tenant_id: str,
        logical_path: str,
        scope: MemoryScope,
        owner_user_id: Optional[str],
        session_id: Optional[str],
    ) -> Optional[KnowledgeDocumentRecord]:
        """使用短连接在指定身份作用域内定位有效知识路径。"""

        with self.connection() as conn:
            return self.get_active_by_logical_path(
                conn,
                tenant_id,
                logical_path,
                scope,
                owner_user_id,
                session_id,
            )

    def get_active(
        self, conn: sqlite3.Connection, tenant_id: str, document_id: str
    ) -> KnowledgeDocumentRecord:
        row = conn.execute(
            """
            SELECT * FROM knowledge_documents
            WHERE tenant_id = ? AND document_id = ? AND status = 'active'
            """,
            (tenant_id, document_id),
        ).fetchone()
        if row is None:
            raise KnowledgeNotFoundError("有效知识文档不存在")
        return _record_from_row(row)

    def get_latest(
        self, conn: sqlite3.Connection, tenant_id: str, document_id: str
    ) -> KnowledgeDocumentRecord:
        row = conn.execute(
            """
            SELECT * FROM knowledge_documents
            WHERE tenant_id = ? AND document_id = ?
            ORDER BY version DESC LIMIT 1
            """,
            (tenant_id, document_id),
        ).fetchone()
        if row is None:
            raise KnowledgeNotFoundError("知识文档不存在")
        return _record_from_row(row)

    def get_version(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        document_id: str,
        version: int,
    ) -> KnowledgeDocumentRecord:
        row = conn.execute(
            """
            SELECT * FROM knowledge_documents
            WHERE tenant_id = ? AND document_id = ? AND version = ?
            """,
            (tenant_id, document_id, version),
        ).fetchone()
        if row is None:
            raise KnowledgeNotFoundError("知识文档版本不存在")
        return _record_from_row(row)

    def read_active(self, tenant_id: str, document_id: str) -> KnowledgeDocumentRecord:
        with self.connection() as conn:
            return self.get_active(conn, tenant_id, document_id)

    def read_latest(self, tenant_id: str, document_id: str) -> KnowledgeDocumentRecord:
        with self.connection() as conn:
            return self.get_latest(conn, tenant_id, document_id)

    def read_version(
        self, tenant_id: str, document_id: str, version: int
    ) -> KnowledgeDocumentRecord:
        with self.connection() as conn:
            return self.get_version(conn, tenant_id, document_id, version)

    @staticmethod
    def next_version(
        conn: sqlite3.Connection, tenant_id: str, document_id: str
    ) -> int:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS current_version
            FROM knowledge_documents
            WHERE tenant_id = ? AND document_id = ?
            """,
            (tenant_id, document_id),
        ).fetchone()
        return int(row["current_version"]) + 1

    @staticmethod
    def supersede_active(
        conn: sqlite3.Connection, tenant_id: str, document_id: str
    ) -> None:
        conn.execute(
            """
            UPDATE knowledge_documents SET status = 'superseded'
            WHERE tenant_id = ? AND document_id = ? AND status = 'active'
            """,
            (tenant_id, document_id),
        )

    @staticmethod
    def insert_document(
        conn: sqlite3.Connection,
        record: KnowledgeDocumentRecord,
        parser_version: str,
        sections: Sequence[KnowledgeSection],
        evidence: Sequence[KnowledgeEvidence],
    ) -> None:
        conn.execute(_INSERT_KNOWLEDGE_DOCUMENT_SQL, _document_row(record, parser_version))
        conn.executemany(
            _INSERT_KNOWLEDGE_SECTION_SQL,
            [_section_row(item) for item in sections],
        )
        conn.executemany(
            _INSERT_KNOWLEDGE_EVIDENCE_SQL,
            [_evidence_row(item) for item in evidence],
        )
        if record.status is KnowledgeStatus.ACTIVE and record.projection_path:
            for category_path in _parent_category_paths(record.projection_path):
                KnowledgeRepository.insert_category(
                    conn,
                    record.tenant_id,
                    record.scope,
                    record.owner_user_id,
                    record.session_id,
                    category_path,
                )

    @staticmethod
    def insert_new_documents_batch(
        conn: sqlite3.Connection,
        items: Sequence[KnowledgeNewDocumentBatchItem],
    ) -> None:
        """批量写入彼此独立的新文档及审计、幂等结果。"""

        items = tuple(items)
        if not items:
            return
        if any(
            item.record.version != 1
            or item.record.status is not KnowledgeStatus.ACTIVE
            or item.record.projection_path is not None
            for item in items
        ):
            raise KnowledgeValidationError("新文档批写只接受无投影的 active v1 记录")

        conn.executemany(
            _INSERT_KNOWLEDGE_DOCUMENT_SQL,
            [_document_row(item.record, item.parser_version) for item in items],
        )
        conn.executemany(
            _INSERT_KNOWLEDGE_SECTION_SQL,
            [_section_row(section) for item in items for section in item.sections],
        )
        conn.executemany(
            _INSERT_KNOWLEDGE_EVIDENCE_SQL,
            [_evidence_row(evidence) for item in items for evidence in item.evidence],
        )
        conn.executemany(
            """
            INSERT INTO knowledge_audit (
                event_id, tenant_id, trace_id, actor_user_id, action,
                document_id, document_version, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.audit_event_id,
                    item.record.tenant_id,
                    item.record.trace_id,
                    item.record.created_by,
                    item.audit_action,
                    item.record.document_id,
                    item.record.version,
                    _json(item.audit_details),
                    item.record.created_at,
                )
                for item in items
            ],
        )
        conn.executemany(
            """
            INSERT INTO knowledge_idempotency (
                tenant_id, actor_user_id, operation, idempotency_key,
                request_hash, document_id, document_version, created_at
            ) VALUES (?, ?, 'write', ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.record.tenant_id,
                    item.record.created_by,
                    item.idempotency_key,
                    item.request_hash,
                    item.record.document_id,
                    item.record.version,
                    item.record.created_at,
                )
                for item in items
            ],
        )

    @staticmethod
    def insert_category(
        conn: sqlite3.Connection,
        tenant_id: str,
        scope: MemoryScope,
        owner_user_id: Optional[str],
        session_id: Optional[str],
        category_path: str,
    ) -> bool:
        """写入分类事实；同一身份作用域内重复调用保持幂等。"""

        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO knowledge_categories (
                tenant_id, scope, owner_user_id, session_id,
                category_path, category_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                scope.value,
                owner_user_id or "",
                session_id or "",
                category_path,
                KnowledgeRepository.projection_key(category_path),
                _utc_now(),
            ),
        )
        return cursor.rowcount == 1

    def create_category(
        self,
        tenant_id: str,
        scope: MemoryScope,
        owner_user_id: Optional[str],
        session_id: Optional[str],
        category_path: str,
    ) -> bool:
        with self.transaction() as conn:
            return self.insert_category(
                conn,
                tenant_id,
                scope,
                owner_user_id,
                session_id,
                category_path,
            )

    def list_categories(self, tenant_id: str) -> Sequence[sqlite3.Row]:
        with self.connection() as conn:
            return self.list_categories_in_transaction(conn, tenant_id)

    @staticmethod
    def list_categories_in_transaction(
        conn: sqlite3.Connection, tenant_id: str
    ) -> Sequence[sqlite3.Row]:
        return conn.execute(
            """
            SELECT tenant_id, scope, owner_user_id, session_id,
                   category_path, category_key, created_at
            FROM knowledge_categories
            WHERE tenant_id = ?
            ORDER BY category_key, scope, owner_user_id, session_id
            """,
            (tenant_id,),
        ).fetchall()

    @staticmethod
    def delete_category_in_transaction(
        conn: sqlite3.Connection,
        tenant_id: str,
        scope: MemoryScope,
        owner_user_id: Optional[str],
        session_id: Optional[str],
        category_path: str,
        include_descendants: bool = True,
    ) -> int:
        key = KnowledgeRepository.projection_key(category_path)
        predicate = "(category_key = ? OR category_key LIKE ? ESCAPE '\\')"
        parameters: List[object] = [key, _like_prefix(key) + "%"]
        if not include_descendants:
            predicate = "category_key = ?"
            parameters = [key]
        cursor = conn.execute(
            """
            DELETE FROM knowledge_categories
            WHERE tenant_id = ? AND scope = ?
              AND owner_user_id = ? AND session_id = ? AND %s
            """ % predicate,
            (
                tenant_id,
                scope.value,
                owner_user_id or "",
                session_id or "",
                *parameters,
            ),
        )
        return int(cursor.rowcount)

    @staticmethod
    def append_audit(
        conn: sqlite3.Connection,
        event_id: str,
        tenant_id: str,
        trace_id: str,
        actor_user_id: str,
        action: str,
        document_id: str,
        document_version: int,
        details: Dict[str, object],
    ) -> None:
        conn.execute(
            """
            INSERT INTO knowledge_audit (
                event_id, tenant_id, trace_id, actor_user_id, action,
                document_id, document_version, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                tenant_id,
                trace_id,
                actor_user_id,
                action,
                document_id,
                document_version,
                _json(details),
                _utc_now(),
            ),
        )

    @staticmethod
    def enqueue_derivative_job(
        conn: sqlite3.Connection,
        tenant_id: str,
        document_id: str,
        target_version: int,
    ) -> None:
        """在事实事务内登记待同步派生版本，同文档只保留最新目标。"""

        conn.execute(
            """
            INSERT INTO knowledge_derivative_jobs(
                tenant_id, document_id, target_version, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(tenant_id, document_id) DO UPDATE SET
                target_version = excluded.target_version,
                updated_at = excluded.updated_at
            WHERE excluded.target_version >= knowledge_derivative_jobs.target_version
            """,
            (tenant_id, document_id, target_version, _utc_now()),
        )

    @staticmethod
    def enqueue_derivative_jobs_batch(
        conn: sqlite3.Connection,
        targets: Sequence[tuple],
    ) -> None:
        """为不能执行全租户同步的批次登记逐文档派生任务。"""

        now = _utc_now()
        conn.executemany(
            """
            INSERT INTO knowledge_derivative_jobs(
                tenant_id, document_id, target_version, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(tenant_id, document_id) DO UPDATE SET
                target_version = excluded.target_version,
                updated_at = excluded.updated_at
            WHERE excluded.target_version >= knowledge_derivative_jobs.target_version
            """,
            [(*target, now) for target in targets],
        )

    @staticmethod
    def enqueue_derivative_batch(
        conn: sqlite3.Connection,
        tenant_id: str,
        batch_id: str,
    ) -> None:
        """为全租户同步登记单条可恢复批次任务。"""

        conn.execute(
            """
            INSERT INTO knowledge_derivative_batches(tenant_id, batch_id, updated_at)
            VALUES (?, ?, ?)
            """,
            (tenant_id, batch_id, _utc_now()),
        )

    @staticmethod
    def get_derivative_job(
        conn: sqlite3.Connection,
        tenant_id: str,
        document_id: str,
    ) -> Optional[int]:
        row = conn.execute(
            """
            SELECT target_version FROM knowledge_derivative_jobs
            WHERE tenant_id = ? AND document_id = ?
            """,
            (tenant_id, document_id),
        ).fetchone()
        return int(row["target_version"]) if row is not None else None

    @staticmethod
    def complete_derivative_job(
        conn: sqlite3.Connection,
        tenant_id: str,
        document_id: str,
        target_version: int,
    ) -> bool:
        """仅在目标版本没有被更新时清除派生任务。"""

        cursor = conn.execute(
            """
            DELETE FROM knowledge_derivative_jobs
            WHERE tenant_id = ? AND document_id = ? AND target_version = ?
            """,
            (tenant_id, document_id, target_version),
        )
        return cursor.rowcount > 0

    @staticmethod
    def clear_derivative_jobs(
        conn: sqlite3.Connection, tenant_id: str
    ) -> None:
        conn.execute(
            "DELETE FROM knowledge_derivative_jobs WHERE tenant_id = ?",
            (tenant_id,),
        )
        conn.execute(
            "DELETE FROM knowledge_derivative_batches WHERE tenant_id = ?",
            (tenant_id,),
        )

    @staticmethod
    def complete_derivative_jobs_batch(
        conn: sqlite3.Connection,
        targets: Sequence[tuple],
    ) -> int:
        """只清除仍指向已提交索引版本的派生任务，保留并发新版本。"""

        cursor = conn.executemany(
            """
            DELETE FROM knowledge_derivative_jobs
            WHERE tenant_id = ? AND document_id = ? AND target_version = ?
            """,
            targets,
        )
        return int(cursor.rowcount)

    @staticmethod
    def complete_derivative_batch(
        conn: sqlite3.Connection,
        tenant_id: str,
        batch_id: str,
    ) -> bool:
        """仅清除已成功提交对应全租户索引的批次任务。"""

        cursor = conn.execute(
            "DELETE FROM knowledge_derivative_batches "
            "WHERE tenant_id = ? AND batch_id = ?",
            (tenant_id, batch_id),
        )
        return cursor.rowcount == 1

    @staticmethod
    def list_active_records_in_transaction(
        conn: sqlite3.Connection, tenant_id: str
    ) -> Sequence[KnowledgeDocumentRecord]:
        rows = conn.execute(
            """
            SELECT * FROM knowledge_documents
            WHERE tenant_id = ? AND status = 'active'
            ORDER BY document_id
            """,
            (tenant_id,),
        ).fetchall()
        return [_record_from_row(row) for row in rows]

    @staticmethod
    def list_evidence_for_active_in_transaction(
        conn: sqlite3.Connection, tenant_id: str
    ) -> Sequence[sqlite3.Row]:
        return conn.execute(
            _ACTIVE_EVIDENCE_SQL + " ORDER BY evidence.evidence_id",
            (tenant_id,),
        ).fetchall()

    @staticmethod
    def list_active_evidence_for_document_in_transaction(
        conn: sqlite3.Connection,
        tenant_id: str,
        document_id: str,
    ) -> Sequence[sqlite3.Row]:
        return conn.execute(
            _ACTIVE_EVIDENCE_SQL
            + " AND evidence.document_id = ? ORDER BY evidence.evidence_index",
            (tenant_id, document_id),
        ).fetchall()

    @staticmethod
    def list_evidence_ids_in_transaction(
        conn: sqlite3.Connection,
        tenant_id: str,
        document_id: str,
    ) -> Sequence[str]:
        rows = conn.execute(
            """
            SELECT evidence_id FROM knowledge_evidence
            WHERE tenant_id = ? AND document_id = ?
            """,
            (tenant_id, document_id),
        ).fetchall()
        return [str(row["evidence_id"]) for row in rows]

    @staticmethod
    def list_projection_paths_in_transaction(
        conn: sqlite3.Connection,
        tenant_id: str,
        document_id: str,
    ) -> Sequence[str]:
        rows = conn.execute(
            """
            SELECT DISTINCT projection_path FROM knowledge_documents
            WHERE tenant_id = ? AND document_id = ?
              AND projection_path IS NOT NULL
            """,
            (tenant_id, document_id),
        ).fetchall()
        return [str(row["projection_path"]) for row in rows]

    @staticmethod
    def list_stale_projection_paths_in_transaction(
        conn: sqlite3.Connection,
    ) -> Sequence[str]:
        """列出没有可投影共享事实占用的历史物理路径。"""

        rows = conn.execute(
            """
            SELECT DISTINCT history.projection_path
            FROM knowledge_documents AS history
            WHERE history.projection_path IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM knowledge_documents AS active
                  WHERE active.status = 'active'
                    AND active.projection_key = history.projection_key
                    AND active.scope = 'shared'
                    AND active.sensitivity IN ('public', 'internal')
              )
            """
        ).fetchall()
        return [str(row["projection_path"]) for row in rows]

    def list_active_records(self, tenant_id: str) -> Sequence[KnowledgeDocumentRecord]:
        with self.connection() as conn:
            return self.list_active_records_in_transaction(conn, tenant_id)

    def list_evidence_for_active(self, tenant_id: str) -> Sequence[sqlite3.Row]:
        with self.connection() as conn:
            return self.list_evidence_for_active_in_transaction(conn, tenant_id)

    def list_active_evidence_for_document(
        self, tenant_id: str, document_id: str
    ) -> Sequence[sqlite3.Row]:
        """读取一个当前有效文档的全部证据。"""

        with self.connection() as conn:
            return self.list_active_evidence_for_document_in_transaction(
                conn, tenant_id, document_id
            )

    def resolve_active_evidence(
        self, tenant_id: str, evidence_ids: Sequence[str]
    ) -> Dict[str, sqlite3.Row]:
        if not evidence_ids:
            return {}
        placeholders = ",".join("?" for _ in evidence_ids)
        with self._read_lock:
            rows = self._read_conn.execute(
                _ACTIVE_EVIDENCE_SQL
                + " AND evidence.evidence_id IN (%s)" % placeholders,
                [tenant_id] + list(evidence_ids),
            ).fetchall()
        return {str(row["evidence_id"]): row for row in rows}

    def close(self) -> None:
        """关闭检索热路径使用的持久只读连接。"""

        with self._read_lock:
            if self._read_conn is None:
                return
            self._read_conn.close()
            self._read_conn = None

    def list_evidence_ids(self, tenant_id: str, document_id: str) -> Sequence[str]:
        with self.connection() as conn:
            return self.list_evidence_ids_in_transaction(
                conn, tenant_id, document_id
            )

    def get_state(self, state_key: str) -> Optional[str]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT state_value FROM knowledge_runtime_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
        return str(row["state_value"]) if row is not None else None

    def set_state(self, state_key: str, state_value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO knowledge_runtime_state(state_key, state_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    state_value = excluded.state_value,
                    updated_at = excluded.updated_at
                """,
                (state_key, state_value, _utc_now()),
            )


_INSERT_KNOWLEDGE_DOCUMENT_SQL = """
    INSERT INTO knowledge_documents (
        tenant_id, document_id, version, status, scope,
        owner_user_id, session_id, sensitivity, collection_id,
        title, source_ref, projection_path, projection_key,
        content, content_hash, parser_version, metadata_json,
        created_by, trace_id, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_KNOWLEDGE_SECTION_SQL = """
    INSERT INTO knowledge_sections (
        tenant_id, section_id, document_id, document_version,
        section_index, heading, char_start, char_end, byte_start,
        byte_end, content_hash
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_KNOWLEDGE_EVIDENCE_SQL = """
    INSERT INTO knowledge_evidence (
        tenant_id, evidence_id, document_id, document_version,
        section_id, evidence_index, quote, char_start, char_end,
        byte_start, byte_end, quote_hash
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_ACTIVE_EVIDENCE_SQL = """
    SELECT
        evidence.tenant_id,
        evidence.evidence_id,
        evidence.document_id,
        evidence.document_version,
        evidence.section_id,
        evidence.quote,
        evidence.char_start,
        evidence.char_end,
        evidence.byte_start,
        evidence.byte_end,
        evidence.quote_hash,
        section.byte_start AS section_byte_start,
        section.byte_end AS section_byte_end,
        section.content_hash AS section_content_hash,
        document.scope,
        document.owner_user_id,
        document.session_id,
        document.sensitivity,
        document.collection_id,
        document.title,
        document.source_ref,
        document.projection_path,
        document.content,
        document.content_hash
    FROM knowledge_evidence AS evidence
    JOIN knowledge_documents AS document
      ON document.tenant_id = evidence.tenant_id
     AND document.document_id = evidence.document_id
     AND document.version = evidence.document_version
    JOIN knowledge_sections AS section
      ON section.tenant_id = evidence.tenant_id
     AND section.section_id = evidence.section_id
     AND section.document_id = evidence.document_id
     AND section.document_version = evidence.document_version
    WHERE evidence.tenant_id = ?
      AND document.status = 'active'
"""


def make_document_record(
    document_id: str,
    tenant_id: str,
    version: int,
    status: KnowledgeStatus,
    scope: MemoryScope,
    owner_user_id: Optional[str],
    session_id: Optional[str],
    sensitivity: Sensitivity,
    collection_id: str,
    title: str,
    source_ref: str,
    projection_path: Optional[str],
    content: str,
    metadata: Dict[str, object],
    created_by: str,
    trace_id: str,
) -> KnowledgeDocumentRecord:
    """生成带内容哈希和 UTC 时间的不可变记录。"""

    return KnowledgeDocumentRecord(
        document_id=document_id,
        tenant_id=tenant_id,
        version=version,
        status=status,
        scope=scope,
        owner_user_id=owner_user_id,
        session_id=session_id,
        sensitivity=sensitivity,
        collection_id=collection_id,
        title=title,
        source_ref=source_ref,
        projection_path=projection_path,
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        metadata=dict(metadata),
        created_by=created_by,
        trace_id=trace_id,
        created_at=_utc_now(),
    )


def _document_row(
    record: KnowledgeDocumentRecord, parser_version: str
) -> tuple:
    return (
        record.tenant_id,
        record.document_id,
        record.version,
        record.status.value,
        record.scope.value,
        record.owner_user_id,
        record.session_id,
        record.sensitivity.value,
        record.collection_id,
        record.title,
        record.source_ref,
        record.projection_path,
        (
            KnowledgeRepository.projection_key(record.projection_path)
            if record.projection_path
            else None
        ),
        record.content,
        record.content_hash,
        parser_version,
        _json(record.metadata),
        record.created_by,
        record.trace_id,
        record.created_at,
    )


def _section_row(section: KnowledgeSection) -> tuple:
    return (
        section.tenant_id,
        section.section_id,
        section.document_id,
        section.document_version,
        section.section_index,
        section.heading,
        section.char_start,
        section.char_end,
        section.byte_start,
        section.byte_end,
        section.content_hash,
    )


def _evidence_row(evidence: KnowledgeEvidence) -> tuple:
    return (
        evidence.tenant_id,
        evidence.evidence_id,
        evidence.document_id,
        evidence.document_version,
        evidence.section_id,
        evidence.evidence_index,
        evidence.quote,
        evidence.char_start,
        evidence.char_end,
        evidence.byte_start,
        evidence.byte_end,
        evidence.quote_hash,
    )


def _record_from_row(row: sqlite3.Row) -> KnowledgeDocumentRecord:
    return KnowledgeDocumentRecord(
        document_id=str(row["document_id"]),
        tenant_id=str(row["tenant_id"]),
        version=int(row["version"]),
        status=KnowledgeStatus(str(row["status"])),
        scope=MemoryScope(str(row["scope"])),
        owner_user_id=row["owner_user_id"],
        session_id=row["session_id"],
        sensitivity=Sensitivity(str(row["sensitivity"])),
        collection_id=str(row["collection_id"]),
        title=str(row["title"]),
        source_ref=str(row["source_ref"]),
        projection_path=row["projection_path"],
        content=str(row["content"]),
        content_hash=str(row["content_hash"]),
        metadata=json.loads(str(row["metadata_json"])),
        created_by=str(row["created_by"]),
        trace_id=str(row["trace_id"]),
        created_at=str(row["created_at"]),
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parent_category_paths(document_path: str) -> Sequence[str]:
    parts = document_path.replace("\\", "/").strip("/").split("/")[:-1]
    return tuple("/".join(parts[:index]) for index in range(1, len(parts) + 1))


def _like_prefix(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "/"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunks(values: Sequence[object], size: int) -> Iterable[Sequence[object]]:
    """按 SQLite 参数预算切分只读批量查询。"""

    for offset in range(0, len(values), size):
        yield values[offset : offset + size]
