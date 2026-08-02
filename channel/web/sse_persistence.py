"""Crash-visible durable replay journal for Web SSE requests.

The live Web channel still owns delivery and cancellation. This store only
records the already-emitted event sequence so a process restart cannot turn a
partially executed request into a fabricated successful completion. SQLite is
used with a short transaction per append: the producer either durably records
an event before it is exposed to an SSE consumer, or the consumer receives an
explicit `unconfirmed` error from the in-memory journal.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any


class DurableSSEJournalStore:
    """Owner-scoped append-only SSE journal with strict sequence invariants."""

    _RETENTION_SECONDS = 24 * 60 * 60

    def __init__(self, path: str):
        self.path = os.path.realpath(path)
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        connection = sqlite3.connect(
            self.path, timeout=5, isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            connection = self._connect()
            try:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS web_sse_runs (
                        request_id TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('running', 'completed')),
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS web_sse_events (
                        request_id TEXT NOT NULL,
                        event_id INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(request_id, event_id),
                        FOREIGN KEY(request_id) REFERENCES web_sse_runs(request_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_web_sse_runs_owner_updated
                        ON web_sse_runs(owner_id, updated_at);
                    """
                )
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    # Windows ACLs are configured by deployment; never fail a
                    # valid local data root just because POSIX chmod is absent.
                    pass
                self._initialized = True
            finally:
                connection.close()

    @staticmethod
    def _validate_identity(request_id: str, owner_id: str, session_id: str = "") -> None:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("invalid SSE request id")
        if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 256:
            raise ValueError("invalid SSE owner")
        if not isinstance(session_id, str) or len(session_id) > 512:
            raise ValueError("invalid SSE session")

    def begin(self, request_id: str, owner_id: str, session_id: str) -> None:
        """Create a request before its worker starts; owner changes are denied."""

        self._validate_identity(request_id, owner_id, session_id)
        self._ensure_schema()
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_id, session_id FROM web_sse_runs WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO web_sse_runs
                        (request_id, owner_id, session_id, state, created_at, updated_at)
                    VALUES (?, ?, ?, 'running', ?, ?)
                    """,
                    (request_id, owner_id, session_id, now, now),
                )
            elif row["owner_id"] != owner_id or row["session_id"] != session_id:
                raise PermissionError("SSE request identity conflict")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def append(self, request_id: str, event_id: int, payload: dict[str, Any]) -> None:
        """Persist exactly the next event, accepting only byte-identical retries."""

        if not isinstance(event_id, int) or event_id < 1:
            raise ValueError("invalid SSE event id")
        try:
            payload_json = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("SSE event is not strict JSON") from exc
        self._ensure_schema()
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT request_id FROM web_sse_runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if run is None:
                raise LookupError("unknown SSE request")
            prior = connection.execute(
                """
                SELECT event_id, payload_json FROM web_sse_events
                WHERE request_id = ? ORDER BY event_id DESC LIMIT 1
                """,
                (request_id,),
            ).fetchone()
            previous_id = int(prior["event_id"]) if prior is not None else 0
            if event_id <= previous_id:
                duplicate = connection.execute(
                    """
                    SELECT payload_json FROM web_sse_events
                    WHERE request_id = ? AND event_id = ?
                    """,
                    (request_id, event_id),
                ).fetchone()
                if duplicate is None or duplicate["payload_json"] != payload_json:
                    raise ValueError("conflicting SSE event retry")
            elif event_id != previous_id + 1:
                raise ValueError("non-contiguous SSE event")
            else:
                connection.execute(
                    """
                    INSERT INTO web_sse_events(request_id, event_id, payload_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (request_id, event_id, payload_json, now),
                )
            terminal = str(payload.get("type") or "") in {"done", "error"}
            if terminal:
                connection.execute(
                    """
                    UPDATE web_sse_runs
                    SET state = 'completed', updated_at = ?
                    WHERE request_id = ?
                    """,
                    (now, request_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE web_sse_runs
                    SET updated_at = ?
                    WHERE request_id = ?
                    """,
                    (now, request_id),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def replay(self, request_id: str, owner_id: str) -> dict[str, Any] | None:
        """Return owner-authorized events without manufacturing a terminal state."""

        self._validate_identity(request_id, owner_id)
        self._ensure_schema()
        connection = self._connect()
        try:
            run = connection.execute(
                """
                SELECT request_id, owner_id, session_id, state, created_at, updated_at
                FROM web_sse_runs WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if run is None or run["owner_id"] != owner_id:
                return None
            rows = connection.execute(
                """
                SELECT event_id, payload_json FROM web_sse_events
                WHERE request_id = ? ORDER BY event_id
                """,
                (request_id,),
            ).fetchall()
            events = []
            for row in rows:
                payload = json.loads(row["payload_json"])
                if not isinstance(payload, dict):
                    raise ValueError("stored SSE payload is not an object")
                events.append((int(row["event_id"]), payload))
            return {
                "request_id": run["request_id"],
                "owner_id": run["owner_id"],
                "session_id": run["session_id"],
                "state": run["state"],
                "created_at": float(run["created_at"]),
                "updated_at": float(run["updated_at"]),
                "events": events,
            }
        finally:
            connection.close()

    def reap(self, *, now: float | None = None) -> int:
        """Delete only old terminal runs; a running request remains evidence."""

        self._ensure_schema()
        cutoff = (time.time() if now is None else now) - self._RETENTION_SECONDS
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM web_sse_runs
                WHERE state = 'completed' AND updated_at < ?
                """,
                (cutoff,),
            )
            connection.execute("COMMIT")
            return int(cursor.rowcount)
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
