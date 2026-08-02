"""受治理技能的 SQLite 不可变事实库。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from common.path_safety import is_link_or_reparse_point

from .contracts import (
    EvaluationPolicy,
    IdempotencyConflictError,
    PairedSampleResult,
    SkillAuditEvent,
    SkillEvaluation,
    SkillNotFoundError,
    SkillStatus,
    SkillTamperError,
    SkillValidationError,
    SkillVersion,
)


SCHEMA_VERSION = 1
_APPEND_ONLY_TABLES = (
    "governed_skill_versions",
    "governed_skill_state_events",
    "governed_skill_evaluations",
    "governed_skill_audit",
)


def utc_now() -> str:
    """生成带时区的 UTC 时间。"""

    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: object) -> str:
    """生成稳定 JSON，供持久化和指纹核验共用。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(payload: bytes) -> str:
    """计算字节内容的 SHA-256。"""

    return hashlib.sha256(payload).hexdigest()


class GovernedSkillRepository:
    """提供串行写事务、不可变版本、评测、状态和审计存储。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        if is_link_or_reparse_point(self.db_path.parent):
            raise SkillValidationError(
                "技能治理数据库目录不能是符号链接或重解析点"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if is_link_or_reparse_point(self.db_path.parent):
            raise SkillValidationError(
                "技能治理数据库目录不能是符号链接或重解析点"
            )
        if is_link_or_reparse_point(self.db_path):
            raise SkillValidationError("技能治理数据库不能是符号链接或重解析点")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=30.0, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        """初始化第一版结构，并拒绝未知的未来模式。"""

        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise SkillValidationError("技能治理数据库版本高于当前程序")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS governed_skill_versions (
                tenant_id TEXT NOT NULL,
                skill_id TEXT NOT NULL,
                name TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0),
                owner_user_id TEXT NOT NULL,
                description TEXT NOT NULL,
                applicability_json TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                validation_rules_json TEXT NOT NULL,
                contraindications_json TEXT NOT NULL,
                model_compatibility_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_by TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                rollback_of_version INTEGER,
                PRIMARY KEY (tenant_id, skill_id, version),
                UNIQUE (tenant_id, name, version)
            );

            CREATE INDEX IF NOT EXISTS idx_governed_skill_versions_name
            ON governed_skill_versions(tenant_id, name, version DESC);

            CREATE TABLE IF NOT EXISTS governed_skill_state_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                skill_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('candidate', 'active', 'superseded', 'rejected')),
                evaluation_id TEXT,
                reason TEXT NOT NULL,
                actor_user_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (tenant_id, skill_id, version)
                    REFERENCES governed_skill_versions(tenant_id, skill_id, version)
            );

            CREATE INDEX IF NOT EXISTS idx_governed_skill_state_target
            ON governed_skill_state_events(tenant_id, skill_id, version, sequence DESC);

            CREATE TABLE IF NOT EXISTS governed_skill_evaluations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluation_id TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                skill_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                validator_user_id TEXT NOT NULL,
                runner_id TEXT NOT NULL,
                runner_version TEXT NOT NULL,
                suite_path TEXT NOT NULL,
                suite_sha256 TEXT NOT NULL,
                sample_count INTEGER NOT NULL CHECK(sample_count > 0),
                baseline_model_id TEXT NOT NULL,
                candidate_model_id TEXT NOT NULL,
                baseline_passed INTEGER NOT NULL,
                candidate_passed INTEGER NOT NULL,
                regression_count INTEGER NOT NULL,
                baseline_p95_latency_ms REAL NOT NULL,
                candidate_p95_latency_ms REAL NOT NULL,
                gate_passed INTEGER NOT NULL CHECK(gate_passed IN (0, 1)),
                gate_failures_json TEXT NOT NULL,
                samples_json TEXT NOT NULL,
                policy_json TEXT NOT NULL,
                record_hash TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (tenant_id, skill_id, version)
                    REFERENCES governed_skill_versions(tenant_id, skill_id, version)
            );

            CREATE INDEX IF NOT EXISTS idx_governed_skill_eval_target
            ON governed_skill_evaluations(tenant_id, skill_id, version, sequence DESC);

            CREATE TABLE IF NOT EXISTS governed_skill_audit (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                skill_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                actor_user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                details_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_governed_skill_audit_target
            ON governed_skill_audit(tenant_id, skill_id, sequence);

            CREATE TABLE IF NOT EXISTS governed_skill_idempotency (
                tenant_id TEXT NOT NULL,
                actor_user_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                result_type TEXT NOT NULL,
                skill_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                evaluation_id TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, actor_user_id, operation, idempotency_key)
            );
            """
        )
        for table in _APPEND_ONLY_TABLES:
            self._create_append_only_triggers(table)
        self._conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        self._conn.commit()

    def _create_append_only_triggers(self, table: str) -> None:
        """用数据库触发器阻止不可变事实被更新或删除。"""

        self._conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS {table}_reject_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only table');
            END;
            CREATE TRIGGER IF NOT EXISTS {table}_reject_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only table');
            END;
            """.format(table=table)
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """用立即事务同时串行化进程内与跨连接写入。"""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def close(self) -> None:
        """释放数据库连接。"""

        with self._lock:
            self._conn.close()

    @staticmethod
    def request_hash(payload: Dict[str, object]) -> str:
        """计算幂等请求的稳定指纹。"""

        return sha256_bytes(canonical_json(payload).encode("utf-8"))

    def find_idempotent(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        actor_user_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
    ) -> Optional[sqlite3.Row]:
        """查找相同请求结果，并拒绝幂等键跨请求复用。"""

        row = conn.execute(
            """
            SELECT * FROM governed_skill_idempotency
            WHERE tenant_id = ? AND actor_user_id = ?
              AND operation = ? AND idempotency_key = ?
            """,
            (tenant_id, actor_user_id, operation, idempotency_key),
        ).fetchone()
        if row is not None and row["request_hash"] != request_hash:
            raise IdempotencyConflictError("幂等键已用于不同请求")
        return row

    def save_idempotent(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        actor_user_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        result_type: str,
        skill_id: str,
        version: int,
        evaluation_id: Optional[str] = None,
    ) -> None:
        """在业务事务内保存幂等结果引用。"""

        conn.execute(
            """
            INSERT INTO governed_skill_idempotency
            (tenant_id, actor_user_id, operation, idempotency_key, request_hash,
             result_type, skill_id, version, evaluation_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                actor_user_id,
                operation,
                idempotency_key,
                request_hash,
                result_type,
                skill_id,
                version,
                evaluation_id,
                utc_now(),
            ),
        )

    def next_version(
        self, conn: sqlite3.Connection, tenant_id: str, skill_id: str
    ) -> int:
        """在写事务中分配单调递增版本号。"""

        row = conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS max_version
            FROM governed_skill_versions
            WHERE tenant_id = ? AND skill_id = ?
            """,
            (tenant_id, skill_id),
        ).fetchone()
        return int(row["max_version"]) + 1

    def find_skill_id_by_name(
        self, conn: sqlite3.Connection, tenant_id: str, name: str
    ) -> Optional[str]:
        """按规范名称查找既有技能标识。"""

        rows = conn.execute(
            """
            SELECT DISTINCT skill_id FROM governed_skill_versions
            WHERE tenant_id = ? AND name = ?
            """,
            (tenant_id, name),
        ).fetchall()
        if len(rows) > 1:
            raise SkillTamperError("同名技能出现多个 skill_id")
        return str(rows[0]["skill_id"]) if rows else None

    def insert_version(self, conn: sqlite3.Connection, record: SkillVersion) -> None:
        """插入不可变技能正文。"""

        conn.execute(
            """
            INSERT INTO governed_skill_versions
            (tenant_id, skill_id, name, version, owner_user_id, description,
             applicability_json, steps_json, validation_rules_json,
             contraindications_json, model_compatibility_json, provenance_json,
             content_hash, created_by, trace_id, created_at, rollback_of_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.tenant_id,
                record.skill_id,
                record.name,
                record.version,
                record.owner_user_id,
                record.description,
                canonical_json(record.applicability),
                canonical_json(record.steps),
                canonical_json(record.validation_rules),
                canonical_json(record.contraindications),
                canonical_json(record.model_compatibility),
                canonical_json(record.provenance),
                record.content_hash,
                record.created_by,
                record.trace_id,
                record.created_at,
                record.rollback_of_version,
            ),
        )

    def append_state(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        skill_id: str,
        version: int,
        status: SkillStatus,
        evaluation_id: Optional[str],
        reason: str,
        actor_user_id: str,
        trace_id: str,
    ) -> None:
        """追加状态转换，不修改正文或既有状态事实。"""

        conn.execute(
            """
            INSERT INTO governed_skill_state_events
            (event_id, tenant_id, skill_id, version, status, evaluation_id,
             reason, actor_user_id, trace_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                tenant_id,
                skill_id,
                version,
                status.value,
                evaluation_id,
                reason,
                actor_user_id,
                trace_id,
                utc_now(),
            ),
        )

    def get_version(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        skill_id: str,
        version: int,
    ) -> SkillVersion:
        """读取技能正文及最后一个状态事件，并核验内容哈希。"""

        row = conn.execute(
            """
            SELECT v.*,
                (SELECT s.status FROM governed_skill_state_events AS s
                 WHERE s.tenant_id = v.tenant_id AND s.skill_id = v.skill_id
                   AND s.version = v.version
                 ORDER BY s.sequence DESC LIMIT 1) AS current_status
            FROM governed_skill_versions AS v
            WHERE v.tenant_id = ? AND v.skill_id = ? AND v.version = ?
            """,
            (tenant_id, skill_id, version),
        ).fetchone()
        if row is None:
            raise SkillNotFoundError("技能版本不存在")
        return self._row_to_version(row)

    def read_version(self, tenant_id: str, skill_id: str, version: int) -> SkillVersion:
        """在线程锁内读取单个版本。"""

        with self._lock:
            return self.get_version(self._conn, tenant_id, skill_id, version)

    def list_versions(self, tenant_id: str, skill_id: str) -> List[SkillVersion]:
        """读取一个技能的完整不可变版本链。"""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT v.*,
                    (SELECT s.status FROM governed_skill_state_events AS s
                     WHERE s.tenant_id = v.tenant_id AND s.skill_id = v.skill_id
                       AND s.version = v.version
                     ORDER BY s.sequence DESC LIMIT 1) AS current_status
                FROM governed_skill_versions AS v
                WHERE v.tenant_id = ? AND v.skill_id = ?
                ORDER BY v.version ASC
                """,
                (tenant_id, skill_id),
            ).fetchall()
            return [self._row_to_version(row) for row in rows]

    def list_candidates(self, tenant_id: str) -> List[SkillVersion]:
        """跨技能读取当前仍为候选的版本，并按创建时间倒序排列。"""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT v.*,
                    (SELECT s.status FROM governed_skill_state_events AS s
                     WHERE s.tenant_id = v.tenant_id AND s.skill_id = v.skill_id
                       AND s.version = v.version
                     ORDER BY s.sequence DESC LIMIT 1) AS current_status
                FROM governed_skill_versions AS v
                WHERE v.tenant_id = ?
                  AND (SELECT s.status FROM governed_skill_state_events AS s
                       WHERE s.tenant_id = v.tenant_id AND s.skill_id = v.skill_id
                         AND s.version = v.version
                       ORDER BY s.sequence DESC LIMIT 1) = 'candidate'
                ORDER BY v.created_at DESC, v.skill_id ASC, v.version DESC
                """,
                (tenant_id,),
            ).fetchall()
            return [self._row_to_version(row) for row in rows]

    def get_active_by_name(
        self, conn: sqlite3.Connection, tenant_id: str, name: str
    ) -> Optional[SkillVersion]:
        """查找名称对应的唯一有效版本。"""

        rows = conn.execute(
            """
            SELECT v.*,
                (SELECT s.status FROM governed_skill_state_events AS s
                 WHERE s.tenant_id = v.tenant_id AND s.skill_id = v.skill_id
                   AND s.version = v.version
                 ORDER BY s.sequence DESC LIMIT 1) AS current_status
            FROM governed_skill_versions AS v
            WHERE v.tenant_id = ? AND v.name = ?
              AND (SELECT s.status FROM governed_skill_state_events AS s
                   WHERE s.tenant_id = v.tenant_id AND s.skill_id = v.skill_id
                     AND s.version = v.version
                   ORDER BY s.sequence DESC LIMIT 1) = 'active'
            """,
            (tenant_id, name),
        ).fetchall()
        if len(rows) > 1:
            raise SkillTamperError("同名技能存在多个有效版本")
        return self._row_to_version(rows[0]) if rows else None

    def read_active_by_name(self, tenant_id: str, name: str) -> Optional[SkillVersion]:
        """在线程锁内查找有效技能。"""

        with self._lock:
            return self.get_active_by_name(self._conn, tenant_id, name)

    def list_active_versions(self, tenant_id: str) -> List[SkillVersion]:
        """读取租户当前全部有效版本，并核验每条正文哈希。"""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT v.*,
                    (SELECT s.status FROM governed_skill_state_events AS s
                     WHERE s.tenant_id = v.tenant_id AND s.skill_id = v.skill_id
                       AND s.version = v.version
                     ORDER BY s.sequence DESC LIMIT 1) AS current_status
                FROM governed_skill_versions AS v
                WHERE v.tenant_id = ?
                  AND (SELECT s.status FROM governed_skill_state_events AS s
                       WHERE s.tenant_id = v.tenant_id AND s.skill_id = v.skill_id
                         AND s.version = v.version
                       ORDER BY s.sequence DESC LIMIT 1) = 'active'
                ORDER BY v.name ASC, v.version DESC
                """,
                (tenant_id,),
            ).fetchall()
            records = [self._row_to_version(row) for row in rows]
            names = [record.name for record in records]
            if len(names) != len(set(names)):
                raise SkillTamperError("同名技能存在多个有效版本")
            return records

    def current_status(
        self, conn: sqlite3.Connection, tenant_id: str, skill_id: str, version: int
    ) -> SkillStatus:
        """读取目标版本的当前派生状态。"""

        row = conn.execute(
            """
            SELECT status FROM governed_skill_state_events
            WHERE tenant_id = ? AND skill_id = ? AND version = ?
            ORDER BY sequence DESC LIMIT 1
            """,
            (tenant_id, skill_id, version),
        ).fetchone()
        if row is None:
            raise SkillTamperError("技能版本缺少状态事件")
        return SkillStatus(row["status"])

    def version_had_status(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        skill_id: str,
        version: int,
        status: SkillStatus,
    ) -> bool:
        """判断历史事件中是否出现过指定状态。"""

        row = conn.execute(
            """
            SELECT 1 FROM governed_skill_state_events
            WHERE tenant_id = ? AND skill_id = ? AND version = ? AND status = ?
            LIMIT 1
            """,
            (tenant_id, skill_id, version, status.value),
        ).fetchone()
        return row is not None

    def active_evaluation_id(
        self, conn: sqlite3.Connection, tenant_id: str, skill_id: str, version: int
    ) -> Optional[str]:
        """读取把版本置为有效时绑定的评测标识。"""

        row = conn.execute(
            """
            SELECT evaluation_id FROM governed_skill_state_events
            WHERE tenant_id = ? AND skill_id = ? AND version = ? AND status = 'active'
            ORDER BY sequence DESC LIMIT 1
            """,
            (tenant_id, skill_id, version),
        ).fetchone()
        return str(row["evaluation_id"]) if row and row["evaluation_id"] else None

    def insert_evaluation(
        self, conn: sqlite3.Connection, evaluation: SkillEvaluation
    ) -> None:
        """追加一条完整的配对评测证据。"""

        conn.execute(
            """
            INSERT INTO governed_skill_evaluations
            (evaluation_id, tenant_id, skill_id, version, validator_user_id,
             runner_id, runner_version,
             suite_path, suite_sha256, sample_count, baseline_model_id,
             candidate_model_id, baseline_passed, candidate_passed,
             regression_count, baseline_p95_latency_ms,
             candidate_p95_latency_ms, gate_passed, gate_failures_json,
             samples_json, policy_json, record_hash, trace_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evaluation.evaluation_id,
                evaluation.tenant_id,
                evaluation.skill_id,
                evaluation.version,
                evaluation.validator_user_id,
                evaluation.runner_id,
                evaluation.runner_version,
                evaluation.suite_path,
                evaluation.suite_sha256,
                evaluation.sample_count,
                evaluation.baseline_model_id,
                evaluation.candidate_model_id,
                evaluation.baseline_passed,
                evaluation.candidate_passed,
                evaluation.regression_count,
                evaluation.baseline_p95_latency_ms,
                evaluation.candidate_p95_latency_ms,
                int(evaluation.gate_passed),
                canonical_json(evaluation.gate_failures),
                canonical_json(
                    [
                        {
                            "sample_id": sample.sample_id,
                            "baseline_success": sample.baseline_success,
                            "candidate_success": sample.candidate_success,
                            "baseline_latency_ms": sample.baseline_latency_ms,
                            "candidate_latency_ms": sample.candidate_latency_ms,
                        }
                        for sample in evaluation.samples
                    ]
                ),
                canonical_json(
                    {
                        "gate_schema_version": evaluation.gate_schema_version,
                        "minimum_sample_count": evaluation.policy.minimum_sample_count,
                        "max_candidate_p95_latency_ms": evaluation.policy.max_candidate_p95_latency_ms,
                        "max_latency_regression_ratio": evaluation.policy.max_latency_regression_ratio,
                    }
                ),
                evaluation.record_hash,
                evaluation.trace_id,
                evaluation.created_at,
            ),
        )

    def get_evaluation(
        self, conn: sqlite3.Connection, tenant_id: str, evaluation_id: str
    ) -> SkillEvaluation:
        """读取评测并核验记录哈希。"""

        row = conn.execute(
            """
            SELECT * FROM governed_skill_evaluations
            WHERE tenant_id = ? AND evaluation_id = ?
            """,
            (tenant_id, evaluation_id),
        ).fetchone()
        if row is None:
            raise SkillNotFoundError("技能评测不存在")
        return self._row_to_evaluation(row)

    def read_evaluation(self, tenant_id: str, evaluation_id: str) -> SkillEvaluation:
        """在线程锁内读取评测。"""

        with self._lock:
            return self.get_evaluation(self._conn, tenant_id, evaluation_id)

    def list_evaluations(
        self, tenant_id: str, skill_id: str, version: int
    ) -> List[SkillEvaluation]:
        """按追加顺序读取目标版本的全部评测。"""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM governed_skill_evaluations
                WHERE tenant_id = ? AND skill_id = ? AND version = ?
                ORDER BY sequence ASC
                """,
                (tenant_id, skill_id, version),
            ).fetchall()
            return [self._row_to_evaluation(row) for row in rows]

    def append_audit(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        skill_id: str,
        version: int,
        actor_user_id: str,
        action: str,
        details: Dict[str, object],
        trace_id: str,
    ) -> None:
        """追加带前序哈希的审计事件。"""

        previous = conn.execute(
            """
            SELECT event_hash FROM governed_skill_audit
            WHERE tenant_id = ? AND skill_id = ?
            ORDER BY sequence DESC LIMIT 1
            """,
            (tenant_id, skill_id),
        ).fetchone()
        previous_hash = str(previous["event_hash"]) if previous else "0" * 64
        event_id = str(uuid.uuid4())
        created_at = utc_now()
        payload = {
            "event_id": event_id,
            "tenant_id": tenant_id,
            "skill_id": skill_id,
            "version": version,
            "actor_user_id": actor_user_id,
            "action": action,
            "details": details,
            "previous_hash": previous_hash,
            "trace_id": trace_id,
            "created_at": created_at,
        }
        event_hash = sha256_bytes(canonical_json(payload).encode("utf-8"))
        conn.execute(
            """
            INSERT INTO governed_skill_audit
            (event_id, tenant_id, skill_id, version, actor_user_id, action,
             details_json, previous_hash, event_hash, trace_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                tenant_id,
                skill_id,
                version,
                actor_user_id,
                action,
                canonical_json(details),
                previous_hash,
                event_hash,
                trace_id,
                created_at,
            ),
        )

    def list_audit(self, tenant_id: str, skill_id: str) -> List[SkillAuditEvent]:
        """读取并核验完整审计哈希链。"""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM governed_skill_audit
                WHERE tenant_id = ? AND skill_id = ?
                ORDER BY sequence ASC
                """,
                (tenant_id, skill_id),
            ).fetchall()
            events = [self._row_to_audit(row) for row in rows]
        previous_hash = "0" * 64
        for event in events:
            if event.previous_hash != previous_hash:
                raise SkillTamperError("技能审计哈希链断裂")
            payload = {
                "event_id": event.event_id,
                "tenant_id": event.tenant_id,
                "skill_id": event.skill_id,
                "version": event.version,
                "actor_user_id": event.actor_user_id,
                "action": event.action,
                "details": event.details,
                "previous_hash": event.previous_hash,
                "trace_id": event.trace_id,
                "created_at": event.created_at,
            }
            expected = sha256_bytes(canonical_json(payload).encode("utf-8"))
            if expected != event.event_hash:
                raise SkillTamperError("技能审计事件哈希不匹配")
            previous_hash = event.event_hash
        return events

    @staticmethod
    def content_payload(record: SkillVersion) -> Dict[str, object]:
        """生成不含生命周期字段的技能正文规范载荷。"""

        payload = {
            "name": record.name,
            "description": record.description,
            "applicability": record.applicability,
            "steps": record.steps,
            "validation_rules": record.validation_rules,
            "contraindications": record.contraindications,
            "model_compatibility": record.model_compatibility,
            "provenance": record.provenance,
        }
        return payload

    @classmethod
    def content_hash(cls, record: SkillVersion) -> str:
        """计算结构化技能正文的稳定 SHA-256。"""

        return sha256_bytes(canonical_json(cls.content_payload(record)).encode("utf-8"))

    def _row_to_version(self, row: sqlite3.Row) -> SkillVersion:
        """把数据库行转换为领域对象并做完整性核验。"""

        if not row["current_status"]:
            raise SkillTamperError("技能版本缺少状态")
        record = SkillVersion(
            skill_id=row["skill_id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            version=int(row["version"]),
            status=SkillStatus(row["current_status"]),
            owner_user_id=row["owner_user_id"],
            description=row["description"],
            applicability=tuple(json.loads(row["applicability_json"])),
            steps=tuple(json.loads(row["steps_json"])),
            validation_rules=tuple(json.loads(row["validation_rules_json"])),
            contraindications=tuple(json.loads(row["contraindications_json"])),
            model_compatibility=tuple(json.loads(row["model_compatibility_json"])),
            provenance=tuple(json.loads(row["provenance_json"])),
            content_hash=row["content_hash"],
            created_by=row["created_by"],
            trace_id=row["trace_id"],
            created_at=row["created_at"],
            rollback_of_version=row["rollback_of_version"],
        )
        if self.content_hash(record) != record.content_hash:
            raise SkillTamperError("技能版本内容哈希不匹配")
        return record

    @staticmethod
    def evaluation_payload(evaluation: SkillEvaluation) -> Dict[str, object]:
        """生成评测记录的规范哈希载荷。"""

        payload = {
            "evaluation_id": evaluation.evaluation_id,
            "tenant_id": evaluation.tenant_id,
            "skill_id": evaluation.skill_id,
            "version": evaluation.version,
            "validator_user_id": evaluation.validator_user_id,
            "runner_id": evaluation.runner_id,
            "runner_version": evaluation.runner_version,
            "suite_path": evaluation.suite_path,
            "suite_sha256": evaluation.suite_sha256,
            "sample_count": evaluation.sample_count,
            "baseline_model_id": evaluation.baseline_model_id,
            "candidate_model_id": evaluation.candidate_model_id,
            "baseline_passed": evaluation.baseline_passed,
            "candidate_passed": evaluation.candidate_passed,
            "regression_count": evaluation.regression_count,
            "baseline_p95_latency_ms": evaluation.baseline_p95_latency_ms,
            "candidate_p95_latency_ms": evaluation.candidate_p95_latency_ms,
            "gate_passed": evaluation.gate_passed,
            "gate_failures": evaluation.gate_failures,
            "samples": [
                {
                    "sample_id": sample.sample_id,
                    "baseline_success": sample.baseline_success,
                    "candidate_success": sample.candidate_success,
                    "baseline_latency_ms": sample.baseline_latency_ms,
                    "candidate_latency_ms": sample.candidate_latency_ms,
                }
                for sample in evaluation.samples
            ],
            "policy": {
                "minimum_sample_count": evaluation.policy.minimum_sample_count,
                "max_candidate_p95_latency_ms": evaluation.policy.max_candidate_p95_latency_ms,
                "max_latency_regression_ratio": evaluation.policy.max_latency_regression_ratio,
            },
            "trace_id": evaluation.trace_id,
            "created_at": evaluation.created_at,
        }
        if evaluation.gate_schema_version > 0:
            payload["policy"]["gate_schema_version"] = (
                evaluation.gate_schema_version
            )
        return payload

    def _row_to_evaluation(self, row: sqlite3.Row) -> SkillEvaluation:
        """把评测行转换为领域对象并核验记录哈希。"""

        sample_rows = json.loads(row["samples_json"])
        policy_row = json.loads(row["policy_json"])
        gate_schema_version = int(policy_row.pop("gate_schema_version", 0))
        evaluation = SkillEvaluation(
            evaluation_id=row["evaluation_id"],
            tenant_id=row["tenant_id"],
            skill_id=row["skill_id"],
            version=int(row["version"]),
            validator_user_id=row["validator_user_id"],
            runner_id=row["runner_id"],
            runner_version=row["runner_version"],
            suite_path=row["suite_path"],
            suite_sha256=row["suite_sha256"],
            sample_count=int(row["sample_count"]),
            baseline_model_id=row["baseline_model_id"],
            candidate_model_id=row["candidate_model_id"],
            baseline_passed=int(row["baseline_passed"]),
            candidate_passed=int(row["candidate_passed"]),
            regression_count=int(row["regression_count"]),
            baseline_p95_latency_ms=float(row["baseline_p95_latency_ms"]),
            candidate_p95_latency_ms=float(row["candidate_p95_latency_ms"]),
            gate_passed=bool(row["gate_passed"]),
            gate_failures=tuple(json.loads(row["gate_failures_json"])),
            samples=tuple(PairedSampleResult(**sample) for sample in sample_rows),
            policy=EvaluationPolicy(**policy_row),
            record_hash=row["record_hash"],
            trace_id=row["trace_id"],
            created_at=row["created_at"],
            gate_schema_version=gate_schema_version,
        )
        expected = sha256_bytes(
            canonical_json(self.evaluation_payload(evaluation)).encode("utf-8")
        )
        if expected != evaluation.record_hash:
            raise SkillTamperError("技能评测记录哈希不匹配")
        return evaluation

    @staticmethod
    def _row_to_audit(row: sqlite3.Row) -> SkillAuditEvent:
        """把审计行转换为领域对象。"""

        return SkillAuditEvent(
            event_id=row["event_id"],
            tenant_id=row["tenant_id"],
            skill_id=row["skill_id"],
            version=int(row["version"]),
            actor_user_id=row["actor_user_id"],
            action=row["action"],
            details=json.loads(row["details_json"]),
            previous_hash=row["previous_hash"],
            event_hash=row["event_hash"],
            trace_id=row["trace_id"],
            created_at=row["created_at"],
        )
