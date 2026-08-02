"""受治理记忆核心的 SQLite 持久化实现。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .contracts import (
    AuditEvent,
    IdempotencyConflictError,
    MemoryNotFoundError,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    Sensitivity,
)


def _utc_now() -> str:
    """生成带时区的 UTC 时间。"""

    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> str:
    """生成稳定 JSON，供指纹和持久化共用。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class GovernedMemoryRepository:
    """提供事务、版本、幂等和审计能力的记忆仓库。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        """以可重复执行的方式初始化数据库结构。"""

        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS governed_memory_versions (
                memory_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0),
                status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'revoked')),
                scope TEXT NOT NULL CHECK(scope IN ('shared', 'user', 'session')),
                owner_user_id TEXT,
                session_id TEXT,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                sensitivity TEXT NOT NULL CHECK(sensitivity IN ('public', 'internal', 'private', 'restricted')),
                metadata_json TEXT NOT NULL,
                created_by TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, memory_id, version)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_governed_memory_active
            ON governed_memory_versions(tenant_id, memory_id)
            WHERE status = 'active';

            CREATE INDEX IF NOT EXISTS idx_governed_memory_visibility
            ON governed_memory_versions(tenant_id, status, scope, owner_user_id, session_id);

            CREATE TABLE IF NOT EXISTS governed_memory_idempotency (
                tenant_id TEXT NOT NULL,
                actor_user_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, actor_user_id, operation, idempotency_key)
            );

            CREATE TABLE IF NOT EXISTS governed_memory_audit (
                event_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                actor_user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_governed_memory_audit_target
            ON governed_memory_audit(tenant_id, memory_id, created_at);

            CREATE TABLE IF NOT EXISTS governed_memory_derivative_jobs (
                tenant_id TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                target_version INTEGER NOT NULL CHECK(target_version > 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, memory_id)
            );
            """
        )
        self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """以立即事务串行化同一进程内的写入。"""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    @staticmethod
    def request_hash(payload: Dict[str, object]) -> str:
        """计算幂等请求的稳定指纹。"""

        raw = _canonical_json(payload).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def find_idempotent_result(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        actor_user_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
    ) -> Optional[MemoryRecord]:
        """返回已提交结果，并拒绝幂等键复用到不同请求。"""

        row = conn.execute(
            """
            SELECT request_hash, memory_id, version
            FROM governed_memory_idempotency
            WHERE tenant_id = ? AND actor_user_id = ?
              AND operation = ? AND idempotency_key = ?
            """,
            (tenant_id, actor_user_id, operation, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflictError("幂等键已用于不同请求")
        return self.get_version(conn, tenant_id, row["memory_id"], row["version"])

    def save_idempotent_result(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        actor_user_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        record: MemoryRecord,
    ) -> None:
        """在业务事务中保存幂等结果。"""

        conn.execute(
            """
            INSERT INTO governed_memory_idempotency
            (tenant_id, actor_user_id, operation, idempotency_key,
             request_hash, memory_id, version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                actor_user_id,
                operation,
                idempotency_key,
                request_hash,
                record.memory_id,
                record.version,
                _utc_now(),
            ),
        )

    def get_active(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        memory_id: str,
    ) -> MemoryRecord:
        """按租户读取当前有效版本。"""

        row = conn.execute(
            """
            SELECT * FROM governed_memory_versions
            WHERE tenant_id = ? AND memory_id = ? AND status = 'active'
            """,
            (tenant_id, memory_id),
        ).fetchone()
        if row is None:
            raise MemoryNotFoundError("记忆不存在或已撤销")
        return self._row_to_record(row)

    def get_version(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        memory_id: str,
        version: int,
    ) -> MemoryRecord:
        """按租户和版本读取历史记录。"""

        row = conn.execute(
            """
            SELECT * FROM governed_memory_versions
            WHERE tenant_id = ? AND memory_id = ? AND version = ?
            """,
            (tenant_id, memory_id, version),
        ).fetchone()
        if row is None:
            raise MemoryNotFoundError("记忆版本不存在")
        return self._row_to_record(row)

    def get_latest(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        memory_id: str,
    ) -> MemoryRecord:
        """读取最新版本，包括已经撤销的记忆。"""

        row = conn.execute(
            """
            SELECT * FROM governed_memory_versions
            WHERE tenant_id = ? AND memory_id = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (tenant_id, memory_id),
        ).fetchone()
        if row is None:
            raise MemoryNotFoundError("记忆不存在")
        return self._row_to_record(row)

    def read_active(self, tenant_id: str, memory_id: str) -> MemoryRecord:
        """在线程锁保护下读取当前有效版本。"""

        with self._lock:
            return self.get_active(self._conn, tenant_id, memory_id)

    def read_latest(self, tenant_id: str, memory_id: str) -> MemoryRecord:
        """在线程锁保护下读取最新版本。"""

        with self._lock:
            return self.get_latest(self._conn, tenant_id, memory_id)

    def list_versions(self, tenant_id: str, memory_id: str) -> List[MemoryRecord]:
        """列出同一租户内的全部历史版本。"""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM governed_memory_versions
                WHERE tenant_id = ? AND memory_id = ?
                ORDER BY version ASC
                """,
                (tenant_id, memory_id),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_active_records(self, tenant_id: str) -> List[MemoryRecord]:
        """列出租户内全部当前有效记忆。"""

        with self._lock:
            return self.list_active_records_in_transaction(
                self._conn, tenant_id
            )

    def count_derivative_jobs(self, tenant_id: str) -> int:
        """返回租户尚未完成的派生任务数量。"""

        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS job_count
                FROM governed_memory_derivative_jobs
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
        return int(row["job_count"])

    @staticmethod
    def list_active_records_in_transaction(
        conn: sqlite3.Connection, tenant_id: str
    ) -> List[MemoryRecord]:
        """在调用方事务中列出租户的全部有效记忆。"""

        rows = conn.execute(
            """
            SELECT * FROM governed_memory_versions
            WHERE tenant_id = ? AND status = 'active'
            ORDER BY memory_id ASC
            """,
            (tenant_id,),
        ).fetchall()
        return [GovernedMemoryRepository._row_to_record(row) for row in rows]

    def next_version(self, conn: sqlite3.Connection, tenant_id: str, memory_id: str) -> int:
        """计算同一记忆的下一个单调版本号。"""

        row = conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS max_version
            FROM governed_memory_versions
            WHERE tenant_id = ? AND memory_id = ?
            """,
            (tenant_id, memory_id),
        ).fetchone()
        return int(row["max_version"]) + 1

    def supersede_active(self, conn: sqlite3.Connection, tenant_id: str, memory_id: str) -> None:
        """把当前有效版本标记为已替代。"""

        conn.execute(
            """
            UPDATE governed_memory_versions
            SET status = 'superseded'
            WHERE tenant_id = ? AND memory_id = ? AND status = 'active'
            """,
            (tenant_id, memory_id),
        )

    def insert_record(self, conn: sqlite3.Connection, record: MemoryRecord) -> None:
        """插入一个不可变版本。"""

        conn.execute(
            """
            INSERT INTO governed_memory_versions
            (memory_id, tenant_id, version, status, scope, owner_user_id,
             session_id, content, content_hash, source_type, source_ref,
             sensitivity, metadata_json, created_by, trace_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.memory_id,
                record.tenant_id,
                record.version,
                record.status.value,
                record.scope.value,
                record.owner_user_id,
                record.session_id,
                record.content,
                record.content_hash,
                record.source_type,
                record.source_ref,
                record.sensitivity.value,
                _canonical_json(record.metadata),
                record.created_by,
                record.trace_id,
                record.created_at,
            ),
        )

    def append_audit(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        trace_id: str,
        actor_user_id: str,
        action: str,
        memory_id: str,
        version: int,
        details: Dict[str, object],
    ) -> None:
        """在业务事务内写入审计事件。"""

        conn.execute(
            """
            INSERT INTO governed_memory_audit
            (event_id, tenant_id, trace_id, actor_user_id, action,
             memory_id, version, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                tenant_id,
                trace_id,
                actor_user_id,
                action,
                memory_id,
                version,
                _canonical_json(details),
                _utc_now(),
            ),
        )

    @staticmethod
    def enqueue_derivative_job(
        conn: sqlite3.Connection,
        tenant_id: str,
        memory_id: str,
        target_version: int,
    ) -> None:
        """在事实事务内登记待同步版本，只允许目标版本单调前进。"""

        conn.execute(
            """
            INSERT INTO governed_memory_derivative_jobs(
                tenant_id, memory_id, target_version, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(tenant_id, memory_id) DO UPDATE SET
                target_version = excluded.target_version,
                updated_at = excluded.updated_at
            WHERE excluded.target_version >=
                  governed_memory_derivative_jobs.target_version
            """,
            (tenant_id, memory_id, target_version, _utc_now()),
        )

    @staticmethod
    def get_derivative_job(
        conn: sqlite3.Connection,
        tenant_id: str,
        memory_id: str,
    ) -> Optional[int]:
        """读取一个记忆当前等待同步的目标版本。"""

        row = conn.execute(
            """
            SELECT target_version FROM governed_memory_derivative_jobs
            WHERE tenant_id = ? AND memory_id = ?
            """,
            (tenant_id, memory_id),
        ).fetchone()
        return int(row["target_version"]) if row is not None else None

    @staticmethod
    def complete_derivative_job(
        conn: sqlite3.Connection,
        tenant_id: str,
        memory_id: str,
        target_version: int,
    ) -> bool:
        """仅在任务仍指向已核验版本时清除任务。"""

        cursor = conn.execute(
            """
            DELETE FROM governed_memory_derivative_jobs
            WHERE tenant_id = ? AND memory_id = ? AND target_version = ?
            """,
            (tenant_id, memory_id, target_version),
        )
        return cursor.rowcount > 0

    @staticmethod
    def clear_derivative_jobs(
        conn: sqlite3.Connection, tenant_id: str
    ) -> None:
        """在完整派生重建成功后清除该租户的待同步任务。"""

        conn.execute(
            "DELETE FROM governed_memory_derivative_jobs WHERE tenant_id = ?",
            (tenant_id,),
        )

    def list_audit(self, tenant_id: str, memory_id: str) -> List[AuditEvent]:
        """读取目标记忆的审计轨迹。"""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM governed_memory_audit
                WHERE tenant_id = ? AND memory_id = ?
                ORDER BY version ASC, created_at ASC
                """,
                (tenant_id, memory_id),
            ).fetchall()
        return [
            AuditEvent(
                event_id=row["event_id"],
                tenant_id=row["tenant_id"],
                trace_id=row["trace_id"],
                actor_user_id=row["actor_user_id"],
                action=row["action"],
                memory_id=row["memory_id"],
                version=row["version"],
                details=json.loads(row["details_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def close(self) -> None:
        """释放数据库连接。"""

        with self._lock:
            self._conn.close()

    @staticmethod
    def make_record(
        *,
        memory_id: str,
        tenant_id: str,
        version: int,
        status: MemoryStatus,
        scope: MemoryScope,
        owner_user_id: Optional[str],
        session_id: Optional[str],
        content: str,
        source_type: str,
        source_ref: str,
        sensitivity: Sensitivity,
        metadata: Dict[str, object],
        created_by: str,
        trace_id: str,
    ) -> MemoryRecord:
        """构造内容哈希完整的记忆版本。"""

        return MemoryRecord(
            memory_id=memory_id,
            tenant_id=tenant_id,
            version=version,
            status=status,
            scope=scope,
            owner_user_id=owner_user_id,
            session_id=session_id,
            content=content,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            source_type=source_type,
            source_ref=source_ref,
            sensitivity=sensitivity,
            metadata=dict(metadata),
            created_by=created_by,
            trace_id=trace_id,
            created_at=_utc_now(),
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
        """把数据库行转换为领域对象。"""

        return MemoryRecord(
            memory_id=row["memory_id"],
            tenant_id=row["tenant_id"],
            version=row["version"],
            status=MemoryStatus(row["status"]),
            scope=MemoryScope(row["scope"]),
            owner_user_id=row["owner_user_id"],
            session_id=row["session_id"],
            content=row["content"],
            content_hash=row["content_hash"],
            source_type=row["source_type"],
            source_ref=row["source_ref"],
            sensitivity=Sensitivity(row["sensitivity"]),
            metadata=json.loads(row["metadata_json"]),
            created_by=row["created_by"],
            trace_id=row["trace_id"],
            created_at=row["created_at"],
        )
