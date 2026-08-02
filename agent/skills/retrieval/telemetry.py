"""只保存脱敏结构和指标的技能影子遥测库。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from common.path_safety import is_link_or_reparse_point

from .contracts import ShadowCandidate


_SCHEMA_VERSION = 2


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ShadowTelemetryRepository:
    """持久化不含任务、参数值和工具结果正文的运行证据。"""

    def __init__(self, db_path: Path, key_path: Path):
        self.db_path = Path(db_path)
        self.key_path = Path(key_path)
        for path in (self.db_path.parent, self.db_path, self.key_path):
            if is_link_or_reparse_point(path):
                raise ValueError("影子遥测路径不能是符号链接或重解析点")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._key = self._load_or_create_key()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=30.0, check_same_thread=False
        )
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._initialize_schema()
        except Exception:
            self._conn.close()
            raise

    def _load_or_create_key(self) -> bytes:
        """创建本机随机 HMAC 密钥；竞争创建时读取胜出者。"""

        try:
            with self.key_path.open("xb") as handle:
                key = os.urandom(32)
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
            return key
        except FileExistsError:
            key = self.key_path.read_bytes()
        if len(key) != 32:
            raise ValueError("技能影子 HMAC 密钥长度无效")
        return key

    def _initialize_schema(self) -> None:
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if current > _SCHEMA_VERSION:
            raise ValueError("技能影子遥测数据库版本高于当前程序")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS skill_shadow_runs (
                run_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                task_hmac TEXT NOT NULL,
                task_char_count INTEGER NOT NULL CHECK(task_char_count >= 0),
                task_byte_count INTEGER NOT NULL CHECK(task_byte_count >= 0),
                actor_hmac TEXT NOT NULL,
                session_hmac TEXT,
                model_id TEXT NOT NULL,
                retriever_version TEXT NOT NULL,
                index_generation TEXT NOT NULL,
                top_k INTEGER NOT NULL CHECK(top_k > 0),
                retrieval_latency_ms REAL NOT NULL CHECK(retrieval_latency_ms >= 0),
                status TEXT NOT NULL,
                tool_count INTEGER NOT NULL DEFAULT 0 CHECK(tool_count >= 0),
                final_type TEXT,
                final_char_count INTEGER,
                final_hmac TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS skill_shadow_candidates (
                run_id TEXT NOT NULL,
                rank INTEGER NOT NULL CHECK(rank > 0),
                skill_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0),
                content_hash TEXT NOT NULL,
                score REAL NOT NULL,
                bm25_score REAL NOT NULL,
                query_coverage REAL NOT NULL,
                fact_verified INTEGER NOT NULL CHECK(fact_verified = 1),
                model_compatible INTEGER NOT NULL,
                PRIMARY KEY (run_id, rank),
                FOREIGN KEY (run_id) REFERENCES skill_shadow_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS skill_shadow_tool_events (
                run_id TEXT NOT NULL,
                call_hmac TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_shape_json TEXT NOT NULL,
                status TEXT,
                latency_ms REAL,
                result_type TEXT,
                result_length INTEGER,
                result_hmac TEXT,
                error_class TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                PRIMARY KEY (run_id, call_hmac),
                FOREIGN KEY (run_id) REFERENCES skill_shadow_runs(run_id)
            );
            """
        )
        self._ensure_injection_columns()
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA user_version=%d" % _SCHEMA_VERSION)
        self._conn.commit()

    def _ensure_injection_columns(self) -> None:
        """幂等迁移生产注入状态列，兼容已有 v1 遥测库。"""

        migrations = {
            "skill_shadow_runs": {
                "injection_requested": (
                    "INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(injection_requested IN (0, 1))"
                ),
                "injection_status": "TEXT NOT NULL DEFAULT 'not_requested'",
                "injected_count": (
                    "INTEGER NOT NULL DEFAULT 0 CHECK(injected_count >= 0)"
                ),
            },
            "skill_shadow_candidates": {
                "projection_verified": (
                    "INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(projection_verified IN (0, 1))"
                ),
                "injected": (
                    "INTEGER NOT NULL DEFAULT 0 CHECK(injected IN (0, 1))"
                ),
            },
        }
        for table, columns in migrations.items():
            existing = {
                str(row[1])
                for row in self._conn.execute("PRAGMA table_info(%s)" % table)
            }
            for name, declaration in columns.items():
                if name not in existing:
                    self._conn.execute(
                        "ALTER TABLE %s ADD COLUMN %s %s"
                        % (table, name, declaration)
                    )

    def digest(self, value: bytes, domain: str = "generic") -> str:
        """用本机随机密钥生成不可直接字典反查的稳定摘要。"""

        if not isinstance(domain, str) or not domain:
            raise ValueError("HMAC 域不能为空")
        message = domain.encode("utf-8") + b"\0" + value
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def summarize_value(
        self, value: Any, domain: str = "value"
    ) -> Dict[str, object]:
        """只返回值类型、序列化长度和 HMAC，不返回值本身。"""

        value_type = type(value).__name__
        if isinstance(value, bytes):
            payload = value
            length = len(value)
        elif isinstance(value, str):
            payload = value.encode("utf-8", errors="replace")
            length = len(value)
        else:
            try:
                payload = json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=lambda item: "<%s>" % type(item).__name__,
                ).encode("utf-8")
            except (TypeError, ValueError):
                payload = ("<%s>" % value_type).encode("ascii")
            length = len(payload)
        return {
            "type": value_type,
            "length": length,
            "hmac": self.digest(payload, domain),
        }

    def create_run(
        self,
        *,
        tenant_id: str,
        task: str,
        actor_user_id: str,
        session_id: Optional[str],
        model_id: str,
        retriever_version: str,
        index_generation: str,
        top_k: int,
        retrieval_latency_ms: float,
        candidates: Sequence[ShadowCandidate],
    ) -> str:
        """原子写入一次运行及其已核验候选。"""

        run_id = str(uuid.uuid4())
        task_bytes = task.encode("utf-8")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO skill_shadow_runs (
                        run_id, tenant_id, task_hmac, task_char_count,
                        task_byte_count, actor_hmac, session_hmac, model_id,
                        retriever_version, index_generation, top_k,
                        retrieval_latency_ms, status, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
                    """,
                    (
                        run_id,
                        tenant_id,
                        self.digest(task_bytes, "task"),
                        len(task),
                        len(task_bytes),
                        self.digest(actor_user_id.encode("utf-8"), "actor"),
                        self.digest(session_id.encode("utf-8"), "session")
                        if session_id
                        else None,
                        model_id,
                        retriever_version,
                        index_generation,
                        top_k,
                        float(retrieval_latency_ms),
                        _utc_now(),
                    ),
                )
                self._conn.executemany(
                    """
                    INSERT INTO skill_shadow_candidates (
                        run_id, rank, skill_id, version, content_hash,
                        score, bm25_score, query_coverage, fact_verified,
                        model_compatible
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    [
                        (
                            run_id,
                            item.rank,
                            item.skill_id,
                            item.version,
                            item.content_hash,
                            item.score,
                            item.bm25_score,
                            item.query_coverage,
                            int(item.model_compatible),
                        )
                        for item in candidates
                    ],
                )
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()
        return run_id

    def record_injection(
        self,
        run_id: str,
        status: str,
        candidates: Sequence[ShadowCandidate],
    ) -> None:
        """原子记录实际注入候选，不保存任务或技能正文。"""

        allowed_statuses = {
            "injected",
            "no_match",
            "no_eligible_candidate",
        }
        normalized_status = str(status)
        if normalized_status not in allowed_statuses:
            raise ValueError("生产技能注入状态无效")
        selected = {
            (
                str(candidate.skill_id),
                int(candidate.version),
                str(candidate.content_hash),
            )
            for candidate in candidates
        }
        if len(selected) != len(candidates):
            raise ValueError("生产技能注入候选不能重复")
        if normalized_status == "injected" and not selected:
            raise ValueError("injected 状态必须包含候选")
        if normalized_status != "injected" and selected:
            raise ValueError("非 injected 状态不能包含候选")

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._require_running(run_id)
                row = self._conn.execute(
                    """
                    SELECT injection_status FROM skill_shadow_runs
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
                if row["injection_status"] != "not_requested":
                    raise ValueError("生产技能注入状态已经记录")
                available = {
                    (str(row[0]), int(row[1]), str(row[2]))
                    for row in self._conn.execute(
                        """
                        SELECT skill_id, version, content_hash
                        FROM skill_shadow_candidates WHERE run_id = ?
                        """,
                        (run_id,),
                    )
                }
                if not selected.issubset(available):
                    raise ValueError("生产技能注入候选不属于本次检索结果")
                for skill_id, version, content_hash in selected:
                    cursor = self._conn.execute(
                        """
                        UPDATE skill_shadow_candidates
                        SET projection_verified = 1, injected = 1
                        WHERE run_id = ? AND skill_id = ?
                          AND version = ? AND content_hash = ?
                        """,
                        (run_id, skill_id, version, content_hash),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("生产技能注入候选状态写入失败")
                self._conn.execute(
                    """
                    UPDATE skill_shadow_runs
                    SET injection_requested = 1, injection_status = ?,
                        injected_count = ?
                    WHERE run_id = ?
                    """,
                    (normalized_status, len(selected), run_id),
                )
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def record_tool_use(
        self, run_id: str, tool_call_id: str, tool_name: str, arguments: Any
    ) -> None:
        """保存工具参数的键、类型、长度和 HMAC。"""

        if isinstance(arguments, dict):
            shape = {
                str(key): self.summarize_value(
                    value, "tool-argument:%s" % str(key)
                )
                for key, value in sorted(arguments.items(), key=lambda item: str(item[0]))
            }
        else:
            shape = {
                "$arguments": self.summarize_value(
                    arguments, "tool-arguments"
                )
            }
        call_hmac = self.digest(
            str(tool_call_id).encode("utf-8"), "tool-call"
        )
        with self._lock:
            try:
                self._require_running(run_id)
                self._conn.execute(
                    """
                    INSERT INTO skill_shadow_tool_events (
                        run_id, call_hmac, tool_name, arguments_shape_json, started_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        call_hmac,
                        str(tool_name),
                        json.dumps(shape, sort_keys=True, separators=(",", ":")),
                        _utc_now(),
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def record_tool_result(
        self,
        run_id: str,
        tool_call_id: str,
        status: str,
        latency_ms: float,
        result: Any,
    ) -> None:
        """补齐工具结果的类型、长度和 HMAC，不保存正文或错误消息。"""

        summary = self.summarize_value(result, "tool-result")
        normalized_status = str(status or "unknown")[:32]
        if normalized_status == "success":
            error_class = None
        elif normalized_status == "critical_error":
            error_class = "critical"
        else:
            error_class = "tool_error"
        call_hmac = self.digest(
            str(tool_call_id).encode("utf-8"), "tool-call"
        )
        with self._lock:
            try:
                self._require_running(run_id)
                cursor = self._conn.execute(
                    """
                    UPDATE skill_shadow_tool_events
                    SET status = ?, latency_ms = ?, result_type = ?,
                        result_length = ?, result_hmac = ?, error_class = ?,
                        finished_at = ?
                    WHERE run_id = ? AND call_hmac = ?
                    """,
                    (
                        normalized_status,
                        max(0.0, float(latency_ms)),
                        summary["type"],
                        summary["length"],
                        summary["hmac"],
                        error_class,
                        _utc_now(),
                        run_id,
                        call_hmac,
                    ),
                )
                if cursor.rowcount != 1:
                    raise KeyError("技能影子工具调用不存在")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def finish_run(self, run_id: str, status: str, final_response: Any) -> None:
        """结束运行并记录最终响应的结构摘要。"""

        summary = self.summarize_value(final_response or "", "final-response")
        with self._lock:
            try:
                self._require_running(run_id)
                cursor = self._conn.execute(
                    """
                    UPDATE skill_shadow_runs
                    SET status = ?, tool_count = (
                            SELECT COUNT(*) FROM skill_shadow_tool_events
                            WHERE skill_shadow_tool_events.run_id = skill_shadow_runs.run_id
                        ),
                        final_type = ?, final_char_count = ?, final_hmac = ?,
                        finished_at = ?
                    WHERE run_id = ? AND status = 'running'
                    """,
                    (
                        str(status)[:32],
                        summary["type"],
                        summary["length"],
                        summary["hmac"],
                        _utc_now(),
                        run_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("技能影子运行已封存")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _require_running(self, run_id: str) -> None:
        """只允许尚未封存的运行继续写入。"""

        row = self._conn.execute(
            "SELECT status FROM skill_shadow_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError("技能影子运行不存在")
        if row["status"] != "running":
            raise ValueError("技能影子运行已封存")

    def export_evidence(self, run_id: str) -> bytes:
        """导出可哈希、可解析且不含原始敏感值的规范证据。"""

        with self._lock:
            run = self._conn.execute(
                "SELECT * FROM skill_shadow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError("技能影子运行不存在")
            candidates = self._conn.execute(
                """
                SELECT rank, skill_id, version, content_hash, score,
                       bm25_score, query_coverage, fact_verified,
                       model_compatible, projection_verified, injected
                FROM skill_shadow_candidates WHERE run_id = ? ORDER BY rank
                """,
                (run_id,),
            ).fetchall()
            tools = self._conn.execute(
                """
                SELECT call_hmac, tool_name, arguments_shape_json, status,
                       latency_ms, result_type, result_length, result_hmac,
                       error_class
                FROM skill_shadow_tool_events
                WHERE run_id = ? ORDER BY rowid
                """,
                (run_id,),
            ).fetchall()
        run_fields = (
            "run_id",
            "tenant_id",
            "task_hmac",
            "task_char_count",
            "task_byte_count",
            "actor_hmac",
            "session_hmac",
            "model_id",
            "retriever_version",
            "index_generation",
            "top_k",
            "retrieval_latency_ms",
            "status",
            "tool_count",
            "injection_requested",
            "injection_status",
            "injected_count",
            "final_type",
            "final_char_count",
            "final_hmac",
        )
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "run": {field: run[field] for field in run_fields},
            "candidates": [dict(row) for row in candidates],
            "tools": [
                {
                    **dict(row),
                    "arguments_shape": json.loads(row["arguments_shape_json"]),
                }
                for row in tools
            ],
        }
        for tool in payload["tools"]:
            tool.pop("arguments_shape_json", None)
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def prune(self, retention_days: int = 30) -> int:
        """删除超过保留期的影子遥测，索引和治理事实不受影响。"""

        if retention_days <= 0:
            raise ValueError("retention_days 必须大于零")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._lock:
            try:
                run_ids = [
                    row[0]
                    for row in self._conn.execute(
                        "SELECT run_id FROM skill_shadow_runs WHERE started_at < ?",
                        (cutoff,),
                    )
                ]
                self._conn.executemany(
                    "DELETE FROM skill_shadow_tool_events WHERE run_id = ?",
                    [(run_id,) for run_id in run_ids],
                )
                self._conn.executemany(
                    "DELETE FROM skill_shadow_candidates WHERE run_id = ?",
                    [(run_id,) for run_id in run_ids],
                )
                self._conn.executemany(
                    "DELETE FROM skill_shadow_runs WHERE run_id = ?",
                    [(run_id,) for run_id in run_ids],
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return len(run_ids)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
