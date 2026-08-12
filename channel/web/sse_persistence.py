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
import secrets
import sqlite3
import threading
import time
from typing import Any


class DurableSSEJournalStore:
    """Owner-scoped Web request journal with strict replay and execution fences.

    ``web_sse_runs`` deliberately contains both delivery and execution state.
    An SSE ``done`` event only proves that a renderer was sent a terminal
    transport payload; it does *not* prove that an Agent/tool run was durably
    settled. Keeping the two state machines together prevents a restart from
    treating an early or forged terminal event as evidence that an external
    side effect may be safely retried.
    """

    _RETENTION_SECONDS = 24 * 60 * 60
    _EXECUTION_STATES = frozenset(
        {"queued", "running", "completed", "failed_safe", "cancelled", "in_doubt"}
    )
    # A lease must comfortably exceed the documented 500 ms cross-worker clock
    # skew. Every tool execution also has a heartbeat, so this is a failure
    # detector rather than permission to overlap side effects.
    _EXECUTION_LEASE_SECONDS = 30.0
    _MAX_DISPATCH_BYTES = 1024 * 1024

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
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> None:
        """Enable WAL once per schema initializer with bounded lock backoff.

        Setting journal mode itself needs a database-wide lock. Performing it
        for every short-lived reader/writer connection turned concurrent valid
        requests into a spurious ``database is locked`` failure before SQLite's
        busy timeout was even configured.
        """

        for attempt in range(8):
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 7:
                    raise
                time.sleep(0.025 * (attempt + 1))

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            connection = self._connect()
            try:
                self._enable_wal(connection)
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
                    CREATE TABLE IF NOT EXISTS web_session_execution_fences (
                        owner_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        request_id TEXT NOT NULL UNIQUE,
                        execution_lease TEXT NOT NULL,
                        runner_id TEXT NOT NULL,
                        fence_token TEXT NOT NULL UNIQUE,
                        acquired_at REAL NOT NULL,
                        lease_expires_at REAL NOT NULL DEFAULT 0,
                        fence_epoch INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(owner_id, session_id),
                        FOREIGN KEY(request_id) REFERENCES web_sse_runs(request_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS
                    idx_web_session_execution_fences_request
                    ON web_session_execution_fences(request_id);
                    CREATE TABLE IF NOT EXISTS web_session_execution_queue (
                        queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        owner_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        request_id TEXT NOT NULL UNIQUE,
                        enqueued_at REAL NOT NULL,
                        FOREIGN KEY(request_id) REFERENCES web_sse_runs(request_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_web_session_execution_queue_head
                    ON web_session_execution_queue(owner_id, session_id, queue_id);
                    CREATE TABLE IF NOT EXISTS web_session_execution_epochs (
                        owner_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        last_fence_epoch INTEGER NOT NULL,
                        context_generation INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(owner_id, session_id)
                    );
                    -- A destructive session mutation (delete/clear) first
                    -- closes this durable admission gate, then waits for every
                    -- pre-existing execution to become terminal. This prevents
                    -- a second Web process from enqueueing a new turn during
                    -- the cancellation/quiescence window.
                    CREATE TABLE IF NOT EXISTS web_session_execution_mutations (
                        owner_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        mutation_token TEXT NOT NULL,
                        mutation_kind TEXT NOT NULL DEFAULT '',
                        context_generation_before INTEGER NOT NULL DEFAULT 0,
                        context_generation_after INTEGER,
                        detail TEXT,
                        completed_at REAL,
                        completion_detail TEXT,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(owner_id, session_id)
                    );
                    """
                )
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(web_sse_runs)"
                    ).fetchall()
                }
                # Existing durable SSE rows predate an execution outcome. They
                # must become ``in_doubt`` rather than being retroactively
                # labelled successful from an old terminal event.
                migrations = (
                    ("idempotency_key", "TEXT"),
                    ("request_digest", "TEXT NOT NULL DEFAULT ''"),
                    ("execution_state", "TEXT NOT NULL DEFAULT 'in_doubt'"),
                    ("execution_lease", "TEXT NOT NULL DEFAULT ''"),
                    ("runner_id", "TEXT NOT NULL DEFAULT ''"),
                    ("execution_detail", "TEXT"),
                    ("execution_started_at", "REAL"),
                    ("execution_finished_at", "REAL"),
                    ("dispatch_json", "TEXT NOT NULL DEFAULT ''"),
                    ("execution_fence_token", "TEXT NOT NULL DEFAULT ''"),
                    ("execution_fence_epoch", "INTEGER NOT NULL DEFAULT 0"),
                    ("cancel_requested_at", "REAL"),
                    ("cancel_request_detail", "TEXT"),
                    ("session_context_generation", "INTEGER NOT NULL DEFAULT 0"),
                )
                for name, ddl in migrations:
                    if name in columns:
                        continue
                    try:
                        connection.execute(
                            f"ALTER TABLE web_sse_runs ADD COLUMN {name} {ddl}"
                        )
                    except sqlite3.OperationalError:
                        # A concurrent process may have completed the same
                        # migration between our schema inspection and ALTER.
                        columns = {
                            str(row["name"])
                            for row in connection.execute(
                                "PRAGMA table_info(web_sse_runs)"
                            ).fetchall()
                        }
                        if name not in columns:
                            raise
                    else:
                        columns.add(name)
                fence_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(web_session_execution_fences)"
                    ).fetchall()
                }
                for name, ddl in (
                    ("lease_expires_at", "REAL NOT NULL DEFAULT 0"),
                    ("fence_epoch", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if name in fence_columns:
                        continue
                    try:
                        connection.execute(
                            f"ALTER TABLE web_session_execution_fences ADD COLUMN {name} {ddl}"
                        )
                    except sqlite3.OperationalError:
                        fence_columns = {
                            str(row["name"])
                            for row in connection.execute(
                                "PRAGMA table_info(web_session_execution_fences)"
                            ).fetchall()
                        }
                        if name not in fence_columns:
                            raise
                    else:
                        fence_columns.add(name)
                epoch_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(web_session_execution_epochs)"
                    ).fetchall()
                }
                if "context_generation" not in epoch_columns:
                    try:
                        connection.execute(
                            "ALTER TABLE web_session_execution_epochs "
                            "ADD COLUMN context_generation INTEGER NOT NULL DEFAULT 0"
                        )
                    except sqlite3.OperationalError:
                        epoch_columns = {
                            str(row["name"])
                            for row in connection.execute(
                                "PRAGMA table_info(web_session_execution_epochs)"
                            ).fetchall()
                        }
                        if "context_generation" not in epoch_columns:
                            raise
                mutation_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(web_session_execution_mutations)"
                    ).fetchall()
                }
                for name, ddl in (
                    ("mutation_kind", "TEXT NOT NULL DEFAULT ''"),
                    ("context_generation_before", "INTEGER NOT NULL DEFAULT 0"),
                    ("context_generation_after", "INTEGER"),
                    ("completed_at", "REAL"),
                    ("completion_detail", "TEXT"),
                ):
                    if name in mutation_columns:
                        continue
                    try:
                        connection.execute(
                            f"ALTER TABLE web_session_execution_mutations "
                            f"ADD COLUMN {name} {ddl}"
                        )
                    except sqlite3.OperationalError:
                        mutation_columns = {
                            str(row["name"])
                            for row in connection.execute(
                                "PRAGMA table_info(web_session_execution_mutations)"
                            ).fetchall()
                        }
                        if name not in mutation_columns:
                            raise
                    else:
                        mutation_columns.add(name)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO web_session_execution_epochs(
                        owner_id, session_id, last_fence_epoch
                    )
                    SELECT owner_id, session_id, COALESCE(MAX(fence_epoch), 0)
                    FROM web_session_execution_fences
                    GROUP BY owner_id, session_id
                    """
                )
                connection.execute(
                    """
                    UPDATE web_session_execution_epochs
                    SET last_fence_epoch = MAX(
                        last_fence_epoch,
                        COALESCE((
                            SELECT MAX(fence_epoch)
                            FROM web_session_execution_fences AS fence
                            WHERE fence.owner_id = web_session_execution_epochs.owner_id
                              AND fence.session_id = web_session_execution_epochs.session_id
                        ), 0)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    web_sse_runs_owner_session_idempotency
                    ON web_sse_runs(owner_id, session_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL AND idempotency_key != ''
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

    @staticmethod
    def validate_idempotency_key(idempotency_key: str) -> None:
        """Reject keys that cannot be safely used as durable request identity."""

        if (
            not isinstance(idempotency_key, str)
            or not 8 <= len(idempotency_key) <= 128
            or any(ord(character) < 33 or ord(character) > 126 for character in idempotency_key)
        ):
            raise ValueError(
                "idempotency_key must be 8-128 printable ASCII characters"
            )

    @staticmethod
    def _validate_request_digest(request_digest: str) -> None:
        if (
            not isinstance(request_digest, str)
            or len(request_digest) != 64
            or any(character not in "0123456789abcdef" for character in request_digest)
        ):
            raise ValueError("invalid Web request digest")

    @staticmethod
    def _validate_runner_id(runner_id: str) -> None:
        if (
            not isinstance(runner_id, str)
            or not runner_id
            or len(runner_id) > 128
            or any(ord(character) < 33 or ord(character) > 126 for character in runner_id)
        ):
            raise ValueError("invalid Web execution runner")

    @staticmethod
    def _validate_session_mutation_kind(mutation_kind: str) -> None:
        if mutation_kind not in {"clear_context", "delete_session"}:
            raise ValueError("invalid destructive Web session mutation kind")

    @staticmethod
    def _validate_session_mutation_token(mutation_token: str) -> None:
        if (
            not isinstance(mutation_token, str)
            or not 16 <= len(mutation_token) <= 128
            or any(ord(character) < 33 or ord(character) > 126 for character in mutation_token)
        ):
            raise ValueError("invalid destructive Web session mutation token")

    @staticmethod
    def _detail(detail: object | None) -> str | None:
        if detail is None:
            return None
        text = str(detail).replace("\x00", " ").replace("\r", " ").replace("\n", " ")
        return text[:512]

    @staticmethod
    def _row_value(row: sqlite3.Row, name: str, default: Any = None) -> Any:
        return row[name] if name in row.keys() else default

    @classmethod
    def _lease_seconds(cls) -> float:
        value = float(cls._EXECUTION_LEASE_SECONDS)
        if value < 5.0:
            raise RuntimeError("Web execution lease must be at least five seconds")
        return value

    @classmethod
    def _canonical_dispatch_payload(cls, payload: dict[str, Any] | None) -> str:
        if payload is None:
            return ""
        if not isinstance(payload, dict):
            raise ValueError("Web dispatch payload must be an object")
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Web dispatch payload is not strict JSON") from exc
        if len(encoded.encode("utf-8")) > cls._MAX_DISPATCH_BYTES:
            raise ValueError("Web dispatch payload is too large")
        return encoded

    @classmethod
    def _decode_dispatch_payload(cls, row: sqlite3.Row) -> dict[str, Any]:
        raw = cls._row_value(row, "dispatch_json", "")
        if not isinstance(raw, str) or not raw:
            raise RuntimeError("queued Web request has no durable dispatch envelope")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("queued Web request has malformed dispatch envelope") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("queued Web request dispatch envelope is not an object")
        cls._canonical_dispatch_payload(payload)
        return payload

    @staticmethod
    def _execution_record(row: sqlite3.Row) -> dict[str, Any]:
        value = DurableSSEJournalStore._row_value
        cancellation_requested_at = value(row, "cancel_requested_at")
        return {
            "request_id": str(row["request_id"]),
            "owner_id": str(row["owner_id"]),
            "session_id": str(row["session_id"]),
            "state": str(row["state"]),
            "execution_state": str(row["execution_state"]),
            "execution_detail": value(row, "execution_detail"),
            # A running request is not "cancelled" merely because another
            # process accepted a stop request. This durable intent is exposed
            # separately until the active fence holder reaches a safe point and
            # acknowledges a terminal cancellation.
            "cancel_requested": cancellation_requested_at is not None,
            "cancel_requested_at": (
                float(cancellation_requested_at)
                if cancellation_requested_at is not None
                else None
            ),
            "cancel_request_detail": value(row, "cancel_request_detail"),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "execution_started_at": (
                float(value(row, "execution_started_at"))
                if value(row, "execution_started_at") is not None
                else None
            ),
            "execution_finished_at": (
                float(value(row, "execution_finished_at"))
                if value(row, "execution_finished_at") is not None
                else None
            ),
            "execution_fence_epoch": int(value(row, "execution_fence_epoch", 0) or 0),
            "session_context_generation": int(
                value(row, "session_context_generation", 0) or 0
            ),
        }

    def begin(self, request_id: str, owner_id: str, session_id: str) -> None:
        """Create a legacy journal request before its worker starts.

        New authenticated Web requests must call ``claim_execution`` instead.
        ``begin`` remains for compatibility with focused SSE tests
        and creates a deliberately unresolved execution claim.
        """

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
                        (request_id, owner_id, session_id, state,
                         execution_state, execution_lease, runner_id,
                         created_at, updated_at, execution_started_at)
                    VALUES (?, ?, ?, 'running', 'running', ?, 'legacy', ?, ?, ?)
                    """,
                    (
                        request_id,
                        owner_id,
                        session_id,
                        secrets.token_urlsafe(32),
                        now,
                        now,
                        now,
                    ),
                )
            elif row["owner_id"] != owner_id or row["session_id"] != session_id:
                raise PermissionError("SSE request identity conflict")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _expire_execution_fences_locked(
        self,
        connection: sqlite3.Connection,
        now: float,
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """Fence expired holders before another worker can claim their queue."""

        clauses = ["lease_expires_at <= ?"]
        params: list[Any] = [now]
        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(owner_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        rows = connection.execute(
            """
            SELECT owner_id, session_id, request_id, execution_lease, runner_id
            FROM web_session_execution_fences
            WHERE """ + " AND ".join(clauses),
            tuple(params),
        ).fetchall()
        for fence in rows:
            connection.execute(
                """
                UPDATE web_sse_runs
                SET execution_state = 'in_doubt',
                    execution_detail = ?, updated_at = ?, execution_finished_at = ?
                WHERE request_id = ? AND owner_id = ? AND session_id = ?
                  AND execution_state = 'running'
                  AND execution_lease = ? AND runner_id = ?
                """,
                (
                    "execution lease expired; external side effect is unconfirmed",
                    now,
                    now,
                    fence["request_id"],
                    fence["owner_id"],
                    fence["session_id"],
                    fence["execution_lease"],
                    fence["runner_id"],
                ),
            )
            connection.execute(
                """
                DELETE FROM web_session_execution_fences
                WHERE owner_id = ? AND session_id = ? AND request_id = ?
                  AND execution_lease = ? AND runner_id = ?
                """,
                (
                    fence["owner_id"],
                    fence["session_id"],
                    fence["request_id"],
                    fence["execution_lease"],
                    fence["runner_id"],
                ),
            )
        return len(rows)

    @staticmethod
    def _session_mutation_locked(
        connection: sqlite3.Connection, owner_id: str, session_id: str
    ) -> sqlite3.Row | None:
        """Return the one durable destructive-mutation closure for a session."""

        return connection.execute(
            """
            SELECT mutation_token, mutation_kind, context_generation_before,
                   context_generation_after, detail, completed_at, completion_detail,
                   created_at
            FROM web_session_execution_mutations
            WHERE owner_id = ? AND session_id = ?
            """,
            (owner_id, session_id),
        ).fetchone()

    @staticmethod
    def _session_context_generation_locked(
        connection: sqlite3.Connection,
        owner_id: str,
        session_id: str,
        *,
        create: bool = False,
    ) -> int:
        """Read (and in write transactions initialize) the session context epoch."""

        row = connection.execute(
            """
            SELECT context_generation FROM web_session_execution_epochs
            WHERE owner_id = ? AND session_id = ?
            """,
            (owner_id, session_id),
        ).fetchone()
        if row is not None:
            return int(row["context_generation"] or 0)
        if create:
            connection.execute(
                """
                INSERT OR IGNORE INTO web_session_execution_epochs(
                    owner_id, session_id, last_fence_epoch, context_generation
                ) VALUES (?, ?, 0, 0)
                """,
                (owner_id, session_id),
            )
        return 0

    def get_delete_session_mutation(
        self, owner_id: str, session_id: str
    ) -> dict[str, Any] | None:
        """Return an owner-bound delete tombstone/receipt for safe HTTP retry.

        This deliberately exposes no result for another principal's locator.
        A row without ``completed_at`` is *not* a completed delete: callers must
        reconcile it through the normal quiescence and owner-checked
        conversation-store operation before returning success.
        """

        self._validate_identity("session-mutation", owner_id, session_id)
        self._ensure_schema()
        connection = self._connect()
        try:
            mutation = self._session_mutation_locked(connection, owner_id, session_id)
            if mutation is None or mutation["mutation_kind"] != "delete_session":
                return None
            completed_at = mutation["completed_at"]
            return {
                "mutation_token": str(mutation["mutation_token"]),
                "mutation_kind": "delete_session",
                "completed_at": float(completed_at) if completed_at is not None else None,
                "completion_detail": self._row_value(mutation, "completion_detail"),
                "created_at": float(mutation["created_at"]),
            }
        finally:
            connection.close()

    def record_delete_session_completion(
        self,
        owner_id: str,
        session_id: str,
        mutation_token: str,
        *,
        detail: object | None = None,
    ) -> float:
        """Record the post-delete receipt before returning an idempotent success.

        The receipt is accepted only under the exact durable delete mutation and
        only after rechecking that no queued/running/in_doubt execution exists.
        This prevents a response-loss retry from converting a merely *pending*
        deletion into ``already_deleted``.
        """

        self._validate_identity("session-mutation", owner_id, session_id)
        self._validate_session_mutation_token(mutation_token)
        self._ensure_schema()
        now = time.time()
        completion_detail = self._detail(detail) or "Web session deletion completed"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            mutation = self._session_mutation_locked(connection, owner_id, session_id)
            if (
                mutation is None
                or mutation["mutation_token"] != mutation_token
                or mutation["mutation_kind"] != "delete_session"
            ):
                raise RuntimeError("delete-session mutation fence is missing or stale")
            self._expire_execution_fences_locked(
                connection, now, owner_id=owner_id, session_id=session_id
            )
            pending = connection.execute(
                """
                SELECT 1 FROM web_sse_runs
                WHERE owner_id = ? AND session_id = ?
                  AND execution_state IN ('queued', 'running', 'in_doubt')
                LIMIT 1
                """,
                (owner_id, session_id),
            ).fetchone()
            if pending is not None:
                raise RuntimeError(
                    "delete-session completion cannot be recorded while execution is pending"
                )
            completed_at = mutation["completed_at"]
            if completed_at is not None:
                connection.execute("COMMIT")
                return float(completed_at)
            cursor = connection.execute(
                """
                UPDATE web_session_execution_mutations
                SET completed_at = ?, completion_detail = ?
                WHERE owner_id = ? AND session_id = ? AND mutation_token = ?
                  AND mutation_kind = 'delete_session' AND completed_at IS NULL
                """,
                (
                    now,
                    completion_detail,
                    owner_id,
                    session_id,
                    mutation_token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("delete-session completion receipt was rejected")
            connection.execute("COMMIT")
            return now
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def _execution_delivery_status_locked(
        self, connection: sqlite3.Connection, run: sqlite3.Row
    ) -> str:
        """Return whether a run may still deliver into its owner's session.

        A destructive mutation closes presentation as well as admission: an old
        response must not leak through a durable replay or an in-memory SSE
        journal while clear/delete is waiting to converge. After a successful
        clear, the request's recorded context generation must equal the current
        session generation before any event may be delivered or appended.
        """

        owner_id = str(run["owner_id"])
        session_id = str(run["session_id"])
        if self._session_mutation_locked(connection, owner_id, session_id) is not None:
            return "mutation_pending"
        current_generation = self._session_context_generation_locked(
            connection, owner_id, session_id, create=False
        )
        run_generation = int(
            self._row_value(run, "session_context_generation", 0) or 0
        )
        if run_generation != current_generation:
            return "stale_context"
        return "current"

    def execution_delivery_status(self, request_id: str, owner_id: str) -> str | None:
        """Return a request's owner-authorized SSE delivery status.

        ``None`` intentionally conflates an unknown request with an owner
        mismatch so callers do not gain a request-existence oracle. This is a
        read-only observation; it never creates an epoch while an SSE consumer
        is deciding whether to emit a locally buffered event.
        """

        self._validate_identity(request_id, owner_id)
        self._ensure_schema()
        connection = self._connect()
        try:
            run = connection.execute(
                "SELECT * FROM web_sse_runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if run is None or run["owner_id"] != owner_id:
                return None
            return self._execution_delivery_status_locked(connection, run)
        finally:
            connection.close()

    def _claim_next_queued_locked(
        self,
        connection: sqlite3.Connection,
        runner_id: str,
        now: float,
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        self._expire_execution_fences_locked(
            connection, now, owner_id=owner_id, session_id=session_id
        )
        if owner_id is not None and session_id is not None:
            if self._session_mutation_locked(connection, owner_id, session_id) is not None:
                # A destructive session mutation has already closed admission.
                # Existing work is cancelled/reaped by that mutation; no worker
                # may revive a queued request while the caller waits for it.
                return None
            holder = connection.execute(
                """
                SELECT 1 FROM web_session_execution_fences
                WHERE owner_id = ? AND session_id = ?
                """,
                (owner_id, session_id),
            ).fetchone()
            if holder is not None:
                return None
            candidate = connection.execute(
                """
                SELECT queue.queue_id, run.*
                FROM web_session_execution_queue AS queue
                JOIN web_sse_runs AS run ON run.request_id = queue.request_id
                WHERE queue.owner_id = ? AND queue.session_id = ?
                  AND run.execution_state = 'queued'
                ORDER BY queue.queue_id
                LIMIT 1
                """,
                (owner_id, session_id),
            ).fetchone()
        else:
            candidate = connection.execute(
                """
                SELECT queue.queue_id, run.*
                FROM web_session_execution_queue AS queue
                JOIN web_sse_runs AS run ON run.request_id = queue.request_id
                WHERE run.execution_state = 'queued'
                  AND NOT EXISTS (
                      SELECT 1 FROM web_session_execution_fences AS fence
                      WHERE fence.owner_id = queue.owner_id
                        AND fence.session_id = queue.session_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM web_session_execution_mutations AS mutation
                      WHERE mutation.owner_id = queue.owner_id
                        AND mutation.session_id = queue.session_id
                  )
                ORDER BY queue.queue_id
                LIMIT 1
                """
            ).fetchone()
        if candidate is None:
            return None

        chosen_owner = str(candidate["owner_id"])
        chosen_session = str(candidate["session_id"])
        epoch_row = connection.execute(
            """
            SELECT last_fence_epoch FROM web_session_execution_epochs
            WHERE owner_id = ? AND session_id = ?
            """,
            (chosen_owner, chosen_session),
        ).fetchone()
        if epoch_row is None:
            epoch = 1
            connection.execute(
                """
                INSERT INTO web_session_execution_epochs(
                    owner_id, session_id, last_fence_epoch, context_generation
                ) VALUES (?, ?, ?, 0)
                """,
                (chosen_owner, chosen_session, epoch),
            )
        else:
            epoch = int(epoch_row["last_fence_epoch"]) + 1
            connection.execute(
                """
                UPDATE web_session_execution_epochs
                SET last_fence_epoch = ?
                WHERE owner_id = ? AND session_id = ?
                """,
                (epoch, chosen_owner, chosen_session),
            )
        lease_token = secrets.token_urlsafe(32)
        fence_token = f"{epoch}.{secrets.token_urlsafe(24)}"
        lease_expires_at = now + self._lease_seconds()
        cursor = connection.execute(
            """
            UPDATE web_sse_runs
            SET execution_state = 'running', execution_lease = ?, runner_id = ?,
                execution_fence_token = ?, execution_fence_epoch = ?,
                execution_detail = NULL, updated_at = ?, execution_started_at = ?,
                execution_finished_at = NULL
            WHERE request_id = ? AND owner_id = ? AND session_id = ?
              AND execution_state = 'queued'
            """,
            (
                lease_token,
                runner_id,
                fence_token,
                epoch,
                now,
                now,
                candidate["request_id"],
                chosen_owner,
                chosen_session,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("queued Web request could not be claimed")
        connection.execute(
            """
            DELETE FROM web_session_execution_queue
            WHERE queue_id = ? AND request_id = ?
            """,
            (candidate["queue_id"], candidate["request_id"]),
        )
        connection.execute(
            """
            INSERT INTO web_session_execution_fences(
                owner_id, session_id, request_id, execution_lease, runner_id,
                fence_token, acquired_at, lease_expires_at, fence_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chosen_owner,
                chosen_session,
                candidate["request_id"],
                lease_token,
                runner_id,
                fence_token,
                now,
                lease_expires_at,
                epoch,
            ),
        )
        run = connection.execute(
            "SELECT * FROM web_sse_runs WHERE request_id = ?",
            (candidate["request_id"],),
        ).fetchone()
        result = self._execution_record(run)
        result.update(
            {
                "claim_status": "claimed",
                "lease_token": lease_token,
                "runner_id": runner_id,
                "session_fence_token": fence_token,
                "session_fence_epoch": epoch,
                "lease_expires_at": lease_expires_at,
                "dispatch_payload": self._decode_dispatch_payload(run)
                if self._row_value(run, "dispatch_json", "")
                else None,
            }
        )
        return result

    def claim_execution(
        self,
        request_id: str,
        owner_id: str,
        session_id: str,
        idempotency_key: str,
        request_digest: str,
        runner_id: str,
        dispatch_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Durably enqueue a Web request and claim only the session head."""

        self._validate_identity(request_id, owner_id, session_id)
        self.validate_idempotency_key(idempotency_key)
        self._validate_request_digest(request_digest)
        self._validate_runner_id(runner_id)
        dispatch_json = self._canonical_dispatch_payload(dispatch_payload)
        self._ensure_schema()
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            mutation = self._session_mutation_locked(connection, owner_id, session_id)
            context_generation = self._session_context_generation_locked(
                connection, owner_id, session_id, create=True
            )
            by_request = connection.execute(
                "SELECT * FROM web_sse_runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if by_request is not None:
                if not (
                    by_request["owner_id"] == owner_id
                    and by_request["session_id"] == session_id
                    and by_request["idempotency_key"] == idempotency_key
                    and by_request["request_digest"] == request_digest
                ):
                    raise ValueError("Web request id collision")
                stored_dispatch = self._row_value(by_request, "dispatch_json", "")
                if dispatch_json and stored_dispatch != dispatch_json:
                    raise ValueError("Web request id was reused with another dispatch envelope")
                result = self._execution_record(by_request)
                if mutation is not None:
                    # An exact retry may learn that a destructive mutation is
                    # pending, but it must not attach/replay a prior response or
                    # dispatch another queued request into that closed session.
                    result["claim_status"] = "mutation_pending"
                    result["mutation_pending"] = True
                    result["delivery_status"] = "mutation_pending"
                    connection.execute("COMMIT")
                    return result
                if result["session_context_generation"] != context_generation:
                    result["claim_status"] = "stale_context"
                    result["stale_context"] = True
                    result["delivery_status"] = "stale_context"
                    connection.execute("COMMIT")
                    return result
                dispatched = self._claim_next_queued_locked(
                    connection, runner_id, now, owner_id=owner_id, session_id=session_id
                )
                if dispatched is not None and dispatched["request_id"] == request_id:
                    dispatched["duplicate"] = True
                    connection.execute("COMMIT")
                    return dispatched
                result["claim_status"] = "duplicate"
                if dispatched is not None:
                    result["dispatch_claim"] = dispatched
                connection.execute("COMMIT")
                return result

            existing = connection.execute(
                """
                SELECT * FROM web_sse_runs
                WHERE owner_id = ? AND session_id = ? AND idempotency_key = ?
                """,
                (owner_id, session_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ValueError("idempotency_key was already used with a different request")
                stored_dispatch = self._row_value(existing, "dispatch_json", "")
                if dispatch_json and stored_dispatch != dispatch_json:
                    raise ValueError("idempotency_key was already used with another dispatch envelope")
                result = self._execution_record(existing)
                if mutation is not None:
                    result["claim_status"] = "mutation_pending"
                    result["mutation_pending"] = True
                    result["delivery_status"] = "mutation_pending"
                    connection.execute("COMMIT")
                    return result
                if result["session_context_generation"] != context_generation:
                    result["claim_status"] = "stale_context"
                    result["stale_context"] = True
                    result["delivery_status"] = "stale_context"
                    connection.execute("COMMIT")
                    return result
                dispatched = self._claim_next_queued_locked(
                    connection, runner_id, now, owner_id=owner_id, session_id=session_id
                )
                if dispatched is not None and dispatched["request_id"] == existing["request_id"]:
                    dispatched["duplicate"] = True
                    connection.execute("COMMIT")
                    return dispatched
                result["claim_status"] = "duplicate"
                if dispatched is not None:
                    result["dispatch_claim"] = dispatched
                connection.execute("COMMIT")
                return result

            if mutation is not None:
                raise RuntimeError(
                    "Web session is unavailable while a destructive mutation is pending"
                )

            connection.execute(
                """
                INSERT INTO web_sse_runs(
                    request_id, owner_id, session_id, state, idempotency_key,
                    request_digest, execution_state, execution_lease, runner_id,
                    dispatch_json, session_context_generation, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?, 'queued', '', '', ?, ?, ?, ?)
                """,
                (
                    request_id,
                    owner_id,
                    session_id,
                    idempotency_key,
                    request_digest,
                    dispatch_json,
                    context_generation,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO web_session_execution_queue(
                    owner_id, session_id, request_id, enqueued_at
                ) VALUES (?, ?, ?, ?)
                """,
                (owner_id, session_id, request_id, now),
            )
            dispatched = self._claim_next_queued_locked(
                connection, runner_id, now, owner_id=owner_id, session_id=session_id
            )
            run = connection.execute(
                "SELECT * FROM web_sse_runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if dispatched is not None and dispatched["request_id"] == request_id:
                connection.execute("COMMIT")
                return dispatched
            result = self._execution_record(run)
            result["claim_status"] = "queued"
            position = connection.execute(
                """
                SELECT COUNT(*) AS position
                FROM web_session_execution_queue
                WHERE owner_id = ? AND session_id = ? AND queue_id <= (
                    SELECT queue_id FROM web_session_execution_queue
                    WHERE request_id = ?
                )
                """,
                (owner_id, session_id, request_id),
            ).fetchone()
            result["queue_position"] = int(position["position"]) if position else 1
            if dispatched is not None:
                result["dispatch_claim"] = dispatched
            connection.execute("COMMIT")
            return result
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def claim_next_queued_execution(
        self,
        runner_id: str,
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Claim one durable queue head for any healthy Web worker."""

        self._validate_runner_id(runner_id)
        if (owner_id is None) != (session_id is None):
            raise ValueError("owner_id and session_id must be supplied together")
        if owner_id is not None and session_id is not None:
            self._validate_identity("queue-claim", owner_id, session_id)
        self._ensure_schema()
        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            claim = self._claim_next_queued_locked(
                connection,
                runner_id,
                current,
                owner_id=owner_id,
                session_id=session_id,
            )
            connection.execute("COMMIT")
            return claim
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def load_execution_dispatch(
        self,
        request_id: str,
        owner_id: str,
        session_id: str,
        lease_token: str,
        runner_id: str,
        fence_token: str,
    ) -> dict[str, Any]:
        """Load an immutable envelope only for the current renewable holder."""

        self.verify_session_execution_fence(
            request_id,
            owner_id,
            session_id,
            lease_token,
            runner_id,
            fence_token,
        )
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM web_sse_runs
                WHERE request_id = ? AND owner_id = ? AND session_id = ?
                  AND execution_state = 'running' AND execution_lease = ?
                  AND runner_id = ? AND execution_fence_token = ?
                """,
                (request_id, owner_id, session_id, lease_token, runner_id, fence_token),
            ).fetchone()
            if row is None:
                raise RuntimeError("Web execution dispatch is no longer owned")
            return self._decode_dispatch_payload(row)
        finally:
            connection.close()

    def verify_execution_claim(
        self,
        request_id: str,
        owner_id: str,
        session_id: str,
        lease_token: str,
        runner_id: str,
    ) -> dict[str, Any]:
        """Return only a still-running exact server claim, otherwise fail closed."""

        self._validate_identity(request_id, owner_id, session_id)
        self._validate_runner_id(runner_id)
        if not isinstance(lease_token, str) or not lease_token or len(lease_token) > 256:
            raise ValueError("invalid Web execution lease")
        self._ensure_schema()
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM web_sse_runs
                WHERE request_id = ? AND owner_id = ? AND session_id = ?
                  AND execution_lease = ? AND runner_id = ?
                """,
                (request_id, owner_id, session_id, lease_token, runner_id),
            ).fetchone()
            if row is None:
                raise PermissionError("Web execution claim was not found")
            record = self._execution_record(row)
            if record["execution_state"] != "running":
                raise RuntimeError(
                    "Web execution is not runnable: "
                    f"{record['execution_state']}"
                )
            return record
        finally:
            connection.close()

    def verify_session_execution_fence(
        self,
        request_id: str,
        owner_id: str,
        session_id: str,
        lease_token: str,
        runner_id: str,
        fence_token: str,
        *,
        now: float | None = None,
    ) -> float:
        """Atomically verify and renew the exact current session lease."""

        self._validate_identity(request_id, owner_id, session_id)
        self._validate_runner_id(runner_id)
        if (
            not isinstance(lease_token, str)
            or not lease_token
            or len(lease_token) > 256
            or not isinstance(fence_token, str)
            or not fence_token
            or len(fence_token) > 256
        ):
            raise ValueError("invalid Web session execution fence")
        self._ensure_schema()
        current = time.time() if now is None else float(now)
        deadline = current + self._lease_seconds()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_execution_fences_locked(
                connection, current, owner_id=owner_id, session_id=session_id
            )
            cursor = connection.execute(
                """
                UPDATE web_session_execution_fences
                SET lease_expires_at = ?
                WHERE owner_id = ? AND session_id = ? AND request_id = ?
                  AND execution_lease = ? AND runner_id = ? AND fence_token = ?
                  AND lease_expires_at > ?
                  AND EXISTS (
                      SELECT 1 FROM web_sse_runs AS run
                      WHERE run.request_id = web_session_execution_fences.request_id
                        AND run.owner_id = web_session_execution_fences.owner_id
                        AND run.session_id = web_session_execution_fences.session_id
                        AND run.execution_state = 'running'
                        AND run.execution_lease = web_session_execution_fences.execution_lease
                        AND run.runner_id = web_session_execution_fences.runner_id
                        AND run.execution_fence_token = web_session_execution_fences.fence_token
                  )
                """,
                (
                    deadline,
                    owner_id,
                    session_id,
                    request_id,
                    lease_token,
                    runner_id,
                    fence_token,
                    current,
                ),
            )
            connection.execute("COMMIT")
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Web session execution fence is no longer owned by this worker"
                )
            return deadline
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def cancellation_requested_for_fence(
        self,
        request_id: str,
        owner_id: str,
        session_id: str,
        lease_token: str,
        runner_id: str,
        fence_token: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Return a durable cancel intent only for the exact live fence holder.

        A caller must first renew its fence, then use this observation at every
        safe checkpoint.  The separate read intentionally reports a request as
        *pending* rather than changing an active run to ``cancelled`` behind a
        worker that may already be inside an external side effect.
        """

        self._validate_identity(request_id, owner_id, session_id)
        self._validate_runner_id(runner_id)
        if (
            not isinstance(lease_token, str)
            or not lease_token
            or len(lease_token) > 256
            or not isinstance(fence_token, str)
            or not fence_token
            or len(fence_token) > 256
        ):
            raise ValueError("invalid Web session execution fence")
        self._ensure_schema()
        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT run.cancel_requested_at
                FROM web_sse_runs AS run
                JOIN web_session_execution_fences AS fence
                  ON fence.request_id = run.request_id
                 AND fence.owner_id = run.owner_id
                 AND fence.session_id = run.session_id
                WHERE run.request_id = ? AND run.owner_id = ? AND run.session_id = ?
                  AND run.execution_state = 'running'
                  AND run.execution_lease = ? AND run.runner_id = ?
                  AND run.execution_fence_token = ?
                  AND fence.execution_lease = ? AND fence.runner_id = ?
                  AND fence.fence_token = ? AND fence.lease_expires_at > ?
                """,
                (
                    request_id,
                    owner_id,
                    session_id,
                    lease_token,
                    runner_id,
                    fence_token,
                    lease_token,
                    runner_id,
                    fence_token,
                    current,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "Web session execution fence is no longer owned by this worker"
                )
            return row["cancel_requested_at"] is not None
        finally:
            connection.close()

    @staticmethod
    def _terminal_event_type(payload_json: str) -> str:
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            return ""
        return str(payload.get("type") or "") if isinstance(payload, dict) else ""

    def _append_cancelled_terminal_locked(
        self, connection: sqlite3.Connection, request_id: str, now: float
    ) -> bool:
        """Atomically persist the one durable cancellation marker.

        The marker is committed in the same transaction as the terminal
        execution state.  A late holder that still knows an old lease/fence can
        no longer race a ``done`` payload in between a cancellation acknowledgement
        and its UI delivery.
        """

        prior = connection.execute(
            """
            SELECT event_id, payload_json FROM web_sse_events
            WHERE request_id = ? ORDER BY event_id DESC LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        if prior is not None and self._terminal_event_type(prior["payload_json"]) in {
            "done",
            "error",
            "cancelled",
        }:
            connection.execute(
                """
                UPDATE web_sse_runs SET state = 'completed', updated_at = ?
                WHERE request_id = ?
                """,
                (now, request_id),
            )
            return False
        event_id = int(prior["event_id"]) + 1 if prior is not None else 1
        payload_json = json.dumps(
            {
                "type": "cancelled",
                "content": "",
                "request_id": request_id,
                "timestamp": now,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        connection.execute(
            """
            INSERT INTO web_sse_events(request_id, event_id, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (request_id, event_id, payload_json, now),
        )
        connection.execute(
            """
            UPDATE web_sse_runs SET state = 'completed', updated_at = ?
            WHERE request_id = ?
            """,
            (now, request_id),
        )
        return True

    def _append_mutation_superseded_terminal_locked(
        self,
        connection: sqlite3.Connection,
        request_id: str,
        now: float,
        detail: str,
    ) -> bool:
        """Close an undelivered response without inventing a post-mutation success.

        A durable execution may be ``completed`` while its transport remains
        ``running`` until WebChannel appends ``done``.  Once a clear/delete
        mutation begins, that old success can no longer be delivered into the
        mutated session.  The same transaction records a truthful error marker
        and closes transport so an old worker's late ``done`` conflicts.
        """

        prior = connection.execute(
            """
            SELECT event_id, payload_json FROM web_sse_events
            WHERE request_id = ? ORDER BY event_id DESC LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        if prior is not None and self._terminal_event_type(prior["payload_json"]) in {
            "done",
            "error",
            "cancelled",
        }:
            connection.execute(
                """
                UPDATE web_sse_runs SET state = 'completed', updated_at = ?
                WHERE request_id = ?
                """,
                (now, request_id),
            )
            return False
        event_id = int(prior["event_id"]) + 1 if prior is not None else 1
        payload_json = json.dumps(
            {
                "type": "error",
                "content": detail,
                "request_id": request_id,
                "timestamp": now,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        connection.execute(
            """
            INSERT INTO web_sse_events(request_id, event_id, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (request_id, event_id, payload_json, now),
        )
        connection.execute(
            """
            UPDATE web_sse_runs SET state = 'completed', updated_at = ?
            WHERE request_id = ?
            """,
            (now, request_id),
        )
        return True

    def _supersede_undelivered_terminal_runs_locked(
        self,
        connection: sqlite3.Connection,
        owner_id: str,
        session_id: str,
        now: float,
        mutation_kind: str,
    ) -> int:
        """Terminalize old delivery gaps before a clear/delete changes context."""

        rows = connection.execute(
            """
            SELECT request_id, execution_state
            FROM web_sse_runs
            WHERE owner_id = ? AND session_id = ? AND state = 'running'
              AND execution_state IN ('completed', 'failed_safe', 'cancelled')
            ORDER BY created_at, request_id
            """,
            (owner_id, session_id),
        ).fetchall()
        superseded = 0
        for row in rows:
            request_id = str(row["request_id"])
            execution_state = str(row["execution_state"])
            if execution_state == "cancelled":
                if self._append_cancelled_terminal_locked(connection, request_id, now):
                    superseded += 1
                continue
            if self._append_mutation_superseded_terminal_locked(
                connection,
                request_id,
                now,
                "Response delivery was superseded by "
                f"{mutation_kind} before it reached the client",
            ):
                superseded += 1
        return superseded

    def finish_execution(
        self,
        request_id: str,
        owner_id: str,
        session_id: str,
        lease_token: str,
        runner_id: str,
        *,
        outcome: str,
        detail: object | None = None,
        fence_token: str | None = None,
    ) -> None:
        """Settle exactly one current lease and atomically release its session."""

        if outcome not in {"completed", "failed_safe", "cancelled", "in_doubt"}:
            raise ValueError("invalid Web execution outcome")
        self._validate_identity(request_id, owner_id, session_id)
        self._validate_runner_id(runner_id)
        if not isinstance(lease_token, str) or not lease_token or len(lease_token) > 256:
            raise ValueError("invalid Web execution lease")
        self._ensure_schema()
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_execution_fences_locked(
                connection, now, owner_id=owner_id, session_id=session_id
            )
            run = connection.execute(
                """
                SELECT * FROM web_sse_runs
                WHERE request_id = ? AND owner_id = ? AND session_id = ?
                """,
                (request_id, owner_id, session_id),
            ).fetchone()
            if run is None:
                connection.execute("COMMIT")
                raise RuntimeError("Web execution completion was rejected (missing request)")
            authenticated = run["idempotency_key"] not in (None, "")
            if authenticated:
                if not isinstance(fence_token, str) or not fence_token:
                    connection.execute("COMMIT")
                    raise RuntimeError("authenticated Web completion requires its fence token")
                holder = connection.execute(
                    """
                    SELECT 1 FROM web_session_execution_fences
                    WHERE owner_id = ? AND session_id = ? AND request_id = ?
                      AND execution_lease = ? AND runner_id = ? AND fence_token = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        owner_id,
                        session_id,
                        request_id,
                        lease_token,
                        runner_id,
                        fence_token,
                        now,
                    ),
                ).fetchone()
                if holder is None or run["execution_fence_token"] != fence_token:
                    connection.execute("COMMIT")
                    raise RuntimeError(
                        "Web execution completion was rejected (stale or missing fence)"
                    )
            cancel_requested = self._row_value(run, "cancel_requested_at") is not None
            effective_outcome = outcome
            effective_detail = self._detail(detail)
            if cancel_requested and outcome == "completed":
                # A user cancellation accepted by any process wins over a
                # completion that has not yet crossed the durable terminal
                # boundary.  The response may contain partial work, but it can
                # never be delivered as a completed request after this point.
                effective_outcome = "cancelled"
                effective_detail = (
                    self._row_value(run, "cancel_request_detail")
                    or "cancellation requested before durable completion"
                )
            cursor = connection.execute(
                """
                UPDATE web_sse_runs
                SET execution_state = ?, execution_detail = ?, updated_at = ?,
                    execution_finished_at = ?
                WHERE request_id = ? AND owner_id = ? AND session_id = ?
                  AND execution_lease = ? AND runner_id = ?
                  AND execution_fence_token = ?
                  AND execution_state = 'running'
                """,
                (
                    effective_outcome,
                    effective_detail,
                    now,
                    now,
                    request_id,
                    owner_id,
                    session_id,
                    lease_token,
                    runner_id,
                    fence_token if authenticated else run["execution_fence_token"],
                ),
            )
            if cursor.rowcount != 1:
                connection.execute("COMMIT")
                raise RuntimeError(
                    "Web execution completion was rejected (stale or missing lease)"
                )
            if effective_outcome == "cancelled":
                self._append_cancelled_terminal_locked(connection, request_id, now)
            connection.execute(
                """
                DELETE FROM web_session_execution_fences
                WHERE owner_id = ? AND session_id = ? AND request_id = ?
                  AND execution_lease = ? AND runner_id = ?
                """,
                (owner_id, session_id, request_id, lease_token, runner_id),
            )
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def reject_execution_before_agent(
        self,
        request_id: str,
        owner_id: str,
        session_id: str,
        lease_token: str,
        runner_id: str,
        *,
        detail: object | None = None,
    ) -> bool:
        """Reject an authenticated claim before Agent/tool execution begins.

        This deliberately does not accept a fence token and must only be called
        by the server before it has entered the Agent executor.  It can never
        report success or take over another lease: request, owner, session,
        lease, runner, and a still-live durable fence must all match.
        """

        self._validate_identity(request_id, owner_id, session_id)
        self._validate_runner_id(runner_id)
        if not isinstance(lease_token, str) or not lease_token or len(lease_token) > 256:
            raise ValueError("invalid Web execution lease")
        self._ensure_schema()
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_execution_fences_locked(
                connection, now, owner_id=owner_id, session_id=session_id
            )
            run = connection.execute(
                """
                SELECT * FROM web_sse_runs
                WHERE request_id = ? AND owner_id = ? AND session_id = ?
                  AND execution_state = 'running' AND execution_lease = ?
                  AND runner_id = ?
                """,
                (request_id, owner_id, session_id, lease_token, runner_id),
            ).fetchone()
            if run is None or run["idempotency_key"] in (None, ""):
                connection.execute("COMMIT")
                return False
            holder = connection.execute(
                """
                SELECT 1 FROM web_session_execution_fences
                WHERE owner_id = ? AND session_id = ? AND request_id = ?
                  AND execution_lease = ? AND runner_id = ?
                  AND lease_expires_at > ?
                """,
                (owner_id, session_id, request_id, lease_token, runner_id, now),
            ).fetchone()
            if holder is None:
                connection.execute("COMMIT")
                return False
            cursor = connection.execute(
                """
                UPDATE web_sse_runs
                SET execution_state = 'failed_safe', execution_detail = ?,
                    updated_at = ?, execution_finished_at = ?
                WHERE request_id = ? AND owner_id = ? AND session_id = ?
                  AND execution_state = 'running' AND execution_lease = ?
                  AND runner_id = ?
                """,
                (
                    self._detail(detail) or "rejected before Agent execution",
                    now,
                    now,
                    request_id,
                    owner_id,
                    session_id,
                    lease_token,
                    runner_id,
                ),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    DELETE FROM web_session_execution_fences
                    WHERE owner_id = ? AND session_id = ? AND request_id = ?
                      AND execution_lease = ? AND runner_id = ?
                    """,
                    (owner_id, session_id, request_id, lease_token, runner_id),
                )
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def mark_interrupted_execution(
        self, request_id: str, owner_id: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        """Fence only an expired or unheld request; do not kill a live peer worker."""

        self._validate_identity(request_id, owner_id)
        self._ensure_schema()
        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM web_sse_runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None or row["owner_id"] != owner_id:
                connection.execute("COMMIT")
                return None
            if row["execution_state"] == "running":
                fence = connection.execute(
                    """
                    SELECT lease_expires_at FROM web_session_execution_fences
                    WHERE owner_id = ? AND session_id = ? AND request_id = ?
                      AND execution_lease = ? AND runner_id = ?
                    """,
                    (
                        row["owner_id"],
                        row["session_id"],
                        request_id,
                        row["execution_lease"],
                        row["runner_id"],
                    ),
                ).fetchone()
                if fence is None or float(fence["lease_expires_at"]) <= current:
                    self._expire_execution_fences_locked(
                        connection,
                        current,
                        owner_id=str(row["owner_id"]),
                        session_id=str(row["session_id"]),
                    )
                    connection.execute(
                        """
                        UPDATE web_sse_runs
                        SET execution_state = 'in_doubt', execution_detail = ?,
                            updated_at = ?, execution_finished_at = ?
                        WHERE request_id = ? AND execution_state = 'running'
                        """,
                        (
                            "worker unavailable during durable recovery",
                            current,
                            current,
                            request_id,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM web_sse_runs WHERE request_id = ?", (request_id,)
                    ).fetchone()
            connection.execute("COMMIT")
            return self._execution_record(row)
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def request_execution_cancellation(
        self, request_id: str, owner_id: str, *, detail: object | None = None
    ) -> dict[str, Any] | None:
        """Durably request one owner-scoped cancellation without false success.

        A queued request is terminally cancelled in this transaction.  A running
        request remains ``running`` and receives a persistent intent that the
        live fence holder must acknowledge at its next safe checkpoint.  The
        caller can therefore distinguish ``cancelled`` from merely
        ``requested`` even when it contacted a different Web process.
        """

        self._validate_identity(request_id, owner_id)
        self._ensure_schema()
        now = time.time()
        cancellation_detail = self._detail(detail) or "cancelled by authenticated Web request"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM web_sse_runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None or row["owner_id"] != owner_id:
                connection.execute("COMMIT")
                return None
            execution_state = str(row["execution_state"])
            if execution_state == "queued":
                cursor = connection.execute(
                    """
                    UPDATE web_sse_runs
                    SET execution_state = 'cancelled', execution_detail = ?,
                        cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        cancel_request_detail = COALESCE(cancel_request_detail, ?),
                        updated_at = ?, execution_finished_at = ?
                    WHERE request_id = ? AND owner_id = ?
                      AND execution_state = 'queued'
                    """,
                    (
                        cancellation_detail,
                        now,
                        cancellation_detail,
                        now,
                        now,
                        request_id,
                        owner_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("queued Web cancellation lost its state transition")
                connection.execute(
                    "DELETE FROM web_session_execution_queue WHERE request_id = ?",
                    (request_id,),
                )
                self._append_cancelled_terminal_locked(connection, request_id, now)
                cancellation_state = "cancelled"
                cancellation_accepted = True
            elif execution_state == "running":
                connection.execute(
                    """
                    UPDATE web_sse_runs
                    SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        cancel_request_detail = COALESCE(cancel_request_detail, ?),
                        updated_at = ?
                    WHERE request_id = ? AND owner_id = ?
                      AND execution_state = 'running'
                    """,
                    (now, cancellation_detail, now, request_id, owner_id),
                )
                cancellation_state = "requested"
                cancellation_accepted = True
            elif execution_state == "cancelled":
                cancellation_state = "cancelled"
                cancellation_accepted = True
            else:
                cancellation_state = "terminal"
                cancellation_accepted = False
            row = connection.execute(
                "SELECT * FROM web_sse_runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            connection.execute("COMMIT")
            record = self._execution_record(row)
            record["cancellation_state"] = cancellation_state
            record["cancellation_accepted"] = cancellation_accepted
            return record
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def _request_session_cancellation_locked(
        self,
        connection: sqlite3.Connection,
        owner_id: str,
        session_id: str,
        now: float,
        cancellation_detail: str,
    ) -> dict[str, int]:
        """Cancel queued work and persist intent for running work in one transaction.

        The caller owns ``BEGIN IMMEDIATE``.  Keeping this logic private makes a
        destructive session mutation able to close admission and request every
        cancellation in the *same* transaction; a second process cannot claim a
        queued request in between those two state changes.
        """

        rows = connection.execute(
            """
            SELECT * FROM web_sse_runs
            WHERE owner_id = ? AND session_id = ?
              AND execution_state IN ('queued', 'running')
            ORDER BY created_at, request_id
            """,
            (owner_id, session_id),
        ).fetchall()
        cancelled = 0
        cancellation_requested = 0
        for row in rows:
            request_id = str(row["request_id"])
            if row["execution_state"] == "queued":
                cursor = connection.execute(
                    """
                    UPDATE web_sse_runs
                    SET execution_state = 'cancelled', execution_detail = ?,
                        cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        cancel_request_detail = COALESCE(cancel_request_detail, ?),
                        updated_at = ?, execution_finished_at = ?
                    WHERE request_id = ? AND owner_id = ? AND session_id = ?
                      AND execution_state = 'queued'
                    """,
                    (
                        cancellation_detail,
                        now,
                        cancellation_detail,
                        now,
                        now,
                        request_id,
                        owner_id,
                        session_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("queued Web session cancellation lost its state transition")
                connection.execute(
                    "DELETE FROM web_session_execution_queue WHERE request_id = ?",
                    (request_id,),
                )
                self._append_cancelled_terminal_locked(connection, request_id, now)
                cancelled += 1
            else:
                connection.execute(
                    """
                    UPDATE web_sse_runs
                    SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        cancel_request_detail = COALESCE(cancel_request_detail, ?),
                        updated_at = ?
                    WHERE request_id = ? AND owner_id = ? AND session_id = ?
                      AND execution_state = 'running'
                    """,
                    (
                        now,
                        cancellation_detail,
                        now,
                        request_id,
                        owner_id,
                        session_id,
                    ),
                )
                cancellation_requested += 1
        return {
            "cancelled": cancelled,
            "cancellation_requested": cancellation_requested,
        }

    def request_session_cancellation(
        self, owner_id: str, session_id: str, *, detail: object | None = None
    ) -> dict[str, int]:
        """Persist cancellation intent for every active request in one session.

        ``cancelled`` counts requests made terminal before Agent execution;
        ``cancellation_requested`` counts active fence holders that still need
        to observe the intent.  Neither count is inferred from local memory.
        """

        self._validate_identity("session-cancel", owner_id, session_id)
        self._ensure_schema()
        now = time.time()
        cancellation_detail = self._detail(detail) or "cancelled by authenticated Web session request"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = self._request_session_cancellation_locked(
                connection,
                owner_id,
                session_id,
                now,
                cancellation_detail,
            )
            connection.execute("COMMIT")
            return result
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def begin_session_mutation(
        self,
        owner_id: str,
        session_id: str,
        *,
        mutation_kind: str,
        detail: object | None = None,
    ) -> dict[str, Any]:
        """Close durable admission and request cancellation for a destructive mutation.

        ``delete_session`` keeps this closure as a tombstone.  ``clear_context``
        must later call :meth:`release_session_mutation` with the returned exact
        token, and only after :meth:`session_mutation_quiescence` proves that no
        queued or running request remains.  A retry of the same mutation kind
        returns the existing token; a different concurrent mutation is rejected
        instead of racing clear against delete.
        """

        self._validate_identity("session-mutation", owner_id, session_id)
        self._validate_session_mutation_kind(mutation_kind)
        self._ensure_schema()
        now = time.time()
        mutation_detail = self._detail(detail) or "destructive Web session mutation requested"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            # First resolve a dead holder to an explicit in_doubt outcome.  It
            # must not remain a permanently invisible blocker merely because a
            # process died before it could observe the mutation cancellation.
            expired = self._expire_execution_fences_locked(
                connection, now, owner_id=owner_id, session_id=session_id
            )
            context_generation_before = self._session_context_generation_locked(
                connection, owner_id, session_id, create=True
            )
            existing = self._session_mutation_locked(connection, owner_id, session_id)
            if existing is None:
                mutation_token = secrets.token_urlsafe(32)
                context_generation_after = None
                connection.execute(
                    """
                    INSERT INTO web_session_execution_mutations(
                        owner_id, session_id, mutation_token, mutation_kind,
                        context_generation_before, context_generation_after,
                        detail, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner_id,
                        session_id,
                        mutation_token,
                        mutation_kind,
                        context_generation_before,
                        context_generation_after,
                        mutation_detail,
                        now,
                    ),
                )
                created = True
            else:
                existing_kind = str(existing["mutation_kind"] or "")
                if existing_kind != mutation_kind:
                    raise RuntimeError(
                        "a different destructive Web session mutation is already pending"
                    )
                mutation_token = str(existing["mutation_token"])
                context_generation_before = int(
                    existing["context_generation_before"] or 0
                )
                context_generation_after = (
                    int(existing["context_generation_after"])
                    if existing["context_generation_after"] is not None
                    else None
                )
                created = False
            cancellation = self._request_session_cancellation_locked(
                connection,
                owner_id,
                session_id,
                now,
                mutation_detail,
            )
            superseded_terminal_deliveries = (
                self._supersede_undelivered_terminal_runs_locked(
                    connection,
                    owner_id,
                    session_id,
                    now,
                    mutation_kind,
                )
            )
            connection.execute("COMMIT")
            return {
                "mutation_token": mutation_token,
                "mutation_kind": mutation_kind,
                "created": created,
                "context_generation_before": context_generation_before,
                "context_generation_after": context_generation_after,
                "completed_at": (
                    float(existing["completed_at"])
                    if existing is not None and existing["completed_at"] is not None
                    else None
                ),
                "expired_execution_fences": expired,
                "superseded_terminal_deliveries": superseded_terminal_deliveries,
                **cancellation,
            }
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def advance_session_context_generation(
        self,
        owner_id: str,
        session_id: str,
        mutation_token: str,
    ) -> int:
        """Commit the new clear-context generation before reopening admission.

        This is intentionally separate from the conversation-store transaction.
        If either store crashes, the mutation closure remains and a retry can
        reconcile rather than replay an old idempotency key into fresh context.
        """

        self._validate_identity("session-mutation", owner_id, session_id)
        self._validate_session_mutation_token(mutation_token)
        self._ensure_schema()
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            mutation = self._session_mutation_locked(connection, owner_id, session_id)
            if (
                mutation is None
                or mutation["mutation_token"] != mutation_token
                or mutation["mutation_kind"] != "clear_context"
            ):
                raise RuntimeError("destructive Web session mutation fence is missing or stale")
            self._expire_execution_fences_locked(
                connection, now, owner_id=owner_id, session_id=session_id
            )
            pending = connection.execute(
                """
                SELECT 1 FROM web_sse_runs
                WHERE owner_id = ? AND session_id = ?
                  AND execution_state IN ('queued', 'running', 'in_doubt')
                LIMIT 1
                """,
                (owner_id, session_id),
            ).fetchone()
            if pending is not None:
                raise RuntimeError(
                    "clear-context generation cannot advance while execution is pending"
                )
            before = int(mutation["context_generation_before"] or 0)
            after_value = mutation["context_generation_after"]
            current = self._session_context_generation_locked(
                connection, owner_id, session_id, create=True
            )
            if after_value is not None:
                after = int(after_value)
                if current != after:
                    raise RuntimeError("clear-context generation is inconsistent")
                connection.execute("COMMIT")
                return after
            if current != before:
                raise RuntimeError("clear-context generation changed outside its mutation")
            after = before + 1
            cursor = connection.execute(
                """
                UPDATE web_session_execution_epochs
                SET context_generation = ?
                WHERE owner_id = ? AND session_id = ? AND context_generation = ?
                """,
                (after, owner_id, session_id, before),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("clear-context generation update was rejected")
            cursor = connection.execute(
                """
                UPDATE web_session_execution_mutations
                SET context_generation_after = ?
                WHERE owner_id = ? AND session_id = ? AND mutation_token = ?
                  AND mutation_kind = 'clear_context'
                  AND context_generation_after IS NULL
                """,
                (after, owner_id, session_id, mutation_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("clear-context generation receipt was rejected")
            connection.execute("COMMIT")
            return after
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def session_mutation_quiescence(
        self,
        owner_id: str,
        session_id: str,
        mutation_token: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Return whether the exact durable mutation has no live/queued work.

        A lease that expires while the caller is waiting is terminalized as
        ``in_doubt`` in this transaction.  ``in_doubt`` remains a destructive
        mutation blocker: lease expiry revokes journal authority, but does not
        prove that a paused remote process, native call, or external tool has
        stopped. An explicit reconciliation path is required before an operator
        can mutate that session.
        """

        self._validate_identity("session-mutation", owner_id, session_id)
        self._validate_session_mutation_token(mutation_token)
        self._ensure_schema()
        current = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            mutation = self._session_mutation_locked(connection, owner_id, session_id)
            if mutation is None or mutation["mutation_token"] != mutation_token:
                raise RuntimeError("destructive Web session mutation fence is missing or stale")
            expired = self._expire_execution_fences_locked(
                connection, current, owner_id=owner_id, session_id=session_id
            )
            pending = connection.execute(
                """
                SELECT request_id, execution_state
                FROM web_sse_runs
                WHERE owner_id = ? AND session_id = ?
                  AND execution_state IN ('queued', 'running', 'in_doubt')
                ORDER BY created_at, request_id
                """,
                (owner_id, session_id),
            ).fetchall()
            connection.execute("COMMIT")
            return {
                "mutation_token": mutation_token,
                "mutation_kind": str(mutation["mutation_kind"] or ""),
                "context_generation_before": int(
                    mutation["context_generation_before"] or 0
                ),
                "context_generation_after": (
                    int(mutation["context_generation_after"])
                    if mutation["context_generation_after"] is not None
                    else None
                ),
                "completed_at": (
                    float(mutation["completed_at"])
                    if mutation["completed_at"] is not None
                    else None
                ),
                "quiescent": not pending,
                "pending_request_ids": [str(row["request_id"]) for row in pending],
                "pending_execution_states": [
                    str(row["execution_state"]) for row in pending
                ],
                "expired_execution_fences": expired,
            }
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def release_session_mutation(
        self,
        owner_id: str,
        session_id: str,
        mutation_token: str,
        *,
        mutation_kind: str,
    ) -> None:
        """Reopen admission only after the exact clear mutation is quiescent."""

        self._validate_identity("session-mutation", owner_id, session_id)
        self._validate_session_mutation_token(mutation_token)
        self._validate_session_mutation_kind(mutation_kind)
        if mutation_kind != "clear_context":
            # A deletion closure is an intentional durable tombstone.  No
            # generic recovery path may reopen it after the session data has
            # been destroyed.
            raise ValueError("only clear_context mutations may reopen Web session admission")
        self._ensure_schema()
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            mutation = self._session_mutation_locked(connection, owner_id, session_id)
            if (
                mutation is None
                or mutation["mutation_token"] != mutation_token
                or mutation["mutation_kind"] != mutation_kind
            ):
                raise RuntimeError("destructive Web session mutation fence is missing or stale")
            # A pending/in_doubt execution is the primary reason reopening is
            # unsafe. Check it before the clear receipt so diagnostics cannot
            # hide an unresolved remote side effect behind a later prerequisite.
            self._expire_execution_fences_locked(
                connection, now, owner_id=owner_id, session_id=session_id
            )
            pending = connection.execute(
                """
                SELECT 1 FROM web_sse_runs
                WHERE owner_id = ? AND session_id = ?
                  AND execution_state IN ('queued', 'running', 'in_doubt')
                LIMIT 1
                """,
                (owner_id, session_id),
            ).fetchone()
            if pending is not None:
                raise RuntimeError(
                    "destructive Web session mutation cannot reopen while execution is pending"
                )
            context_before = int(mutation["context_generation_before"] or 0)
            context_after = mutation["context_generation_after"]
            if context_after is None:
                raise RuntimeError("clear-context generation was not durably advanced")
            if self._session_context_generation_locked(
                connection, owner_id, session_id, create=True
            ) != int(context_after):
                raise RuntimeError("clear-context generation is inconsistent")
            if int(context_after) != context_before + 1:
                raise RuntimeError("clear-context generation advance is invalid")
            cursor = connection.execute(
                """
                DELETE FROM web_session_execution_mutations
                WHERE owner_id = ? AND session_id = ? AND mutation_token = ?
                  AND mutation_kind = ?
                """,
                (owner_id, session_id, mutation_token, mutation_kind),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("destructive Web session mutation release was rejected")
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def cancel_queued_execution(
        self, request_id: str, owner_id: str, *, detail: object | None = None
    ) -> dict[str, Any] | None:
        """Compatibility wrapper for the durable cancellation protocol.

        Older callers only supplied queued request IDs.  Returning a running
        record with ``cancellation_state='requested'`` is intentional: it
        prevents cross-instance callers from treating an absent local token as
        proof that no cancellation was requested.
        """

        return self.request_execution_cancellation(request_id, owner_id, detail=detail)

    def cancel_queued_session(
        self, owner_id: str, session_id: str, *, detail: object | None = None
    ) -> int:
        """Compatibility wrapper returning only immediately terminal cancels."""

        return self.request_session_cancellation(
            owner_id, session_id, detail=detail
        )["cancelled"]

    def append(
        self,
        request_id: str,
        event_id: int,
        payload: dict[str, Any],
        *,
        lease_token: str | None = None,
        runner_id: str | None = None,
        fence_token: str | None = None,
    ) -> None:
        """Persist exactly the next event under the current execution authority."""

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
                "SELECT * FROM web_sse_runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if run is None:
                raise LookupError("unknown SSE request")
            delivery_status = self._execution_delivery_status_locked(connection, run)
            if delivery_status != "current":
                connection.execute("COMMIT")
                raise RuntimeError(
                    "Web SSE append is forbidden while durable delivery status is "
                    f"{delivery_status}"
                )
            authenticated = run["idempotency_key"] not in (None, "")
            event_type = str(payload.get("type") or "")
            if authenticated:
                if not all(
                    isinstance(value, str) and value
                    for value in (lease_token, runner_id, fence_token)
                ):
                    connection.execute("COMMIT")
                    raise PermissionError("authenticated Web SSE append requires an execution fence")
                if run["execution_state"] == "running":
                    self._expire_execution_fences_locked(
                        connection,
                        now,
                        owner_id=str(run["owner_id"]),
                        session_id=str(run["session_id"]),
                    )
                    run = connection.execute(
                        "SELECT * FROM web_sse_runs WHERE request_id = ?", (request_id,)
                    ).fetchone()
                    holder = connection.execute(
                        """
                        SELECT 1 FROM web_session_execution_fences
                        WHERE owner_id = ? AND session_id = ? AND request_id = ?
                          AND execution_lease = ? AND runner_id = ? AND fence_token = ?
                          AND lease_expires_at > ?
                        """,
                        (
                            run["owner_id"],
                            run["session_id"],
                            request_id,
                            lease_token,
                            runner_id,
                            fence_token,
                            now,
                        ),
                    ).fetchone()
                    if holder is None:
                        connection.execute("COMMIT")
                        raise RuntimeError("stale worker cannot append Web SSE events")
                else:
                    allowed_post_settlement = {
                        "completed": {"done", "error", "voice_attach"},
                        # A cancellation marker must be the only new terminal
                        # delivery after a durable cancellation. In particular,
                        # an old fence can never append a late success ``done``.
                        "cancelled": {"cancelled"},
                        "failed_safe": {"error"},
                        "in_doubt": {"error"},
                    }.get(str(run["execution_state"]))
                    if allowed_post_settlement is None:
                        connection.execute("COMMIT")
                        raise RuntimeError(
                            "Web SSE append is forbidden for execution state "
                            f"{run['execution_state']}"
                        )
                    if event_type not in allowed_post_settlement:
                        connection.execute("COMMIT")
                        raise RuntimeError("forbidden post-settlement Web SSE payload is not permitted")
                    if not (
                        run["execution_lease"] == lease_token
                        and run["runner_id"] == runner_id
                        and run["execution_fence_token"] == fence_token
                    ):
                        connection.execute("COMMIT")
                        raise RuntimeError("stale worker cannot append terminal Web SSE events")
            if (
                event_type == "done"
                and authenticated
                and run["execution_state"] != "completed"
            ):
                connection.execute("COMMIT")
                raise RuntimeError(
                    "Web SSE done is forbidden before durable execution completion"
                )
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
            elif run["state"] == "completed":
                connection.execute("COMMIT")
                raise RuntimeError("terminal Web SSE delivery was already recorded")
            else:
                connection.execute(
                    """
                    INSERT INTO web_sse_events(request_id, event_id, payload_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (request_id, event_id, payload_json, now),
                )
            terminal = event_type in {"done", "error", "cancelled"}
            connection.execute(
                """
                UPDATE web_sse_runs
                SET state = CASE WHEN ? THEN 'completed' ELSE state END,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (1 if terminal else 0, now, request_id),
            )
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def events_after(
        self, request_id: str, owner_id: str, event_id: int
    ) -> list[tuple[int, dict[str, Any]]]:
        """Return durable events newer than an SSE cursor for the owner."""

        self._validate_identity(request_id, owner_id)
        if not isinstance(event_id, int) or event_id < 0:
            raise ValueError("invalid SSE event cursor")
        self._ensure_schema()
        connection = self._connect()
        try:
            run = connection.execute(
                "SELECT * FROM web_sse_runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if run is None or run["owner_id"] != owner_id:
                return []
            if self._execution_delivery_status_locked(connection, run) != "current":
                return []
            rows = connection.execute(
                """
                SELECT event_id, payload_json FROM web_sse_events
                WHERE request_id = ? AND event_id > ? ORDER BY event_id
                """,
                (request_id, event_id),
            ).fetchall()
            events: list[tuple[int, dict[str, Any]]] = []
            for row in rows:
                payload = json.loads(row["payload_json"])
                if not isinstance(payload, dict):
                    raise ValueError("stored SSE payload is not an object")
                events.append((int(row["event_id"]), payload))
            return events
        finally:
            connection.close()

    def replay(self, request_id: str, owner_id: str) -> dict[str, Any] | None:
        """Return owner-authorized events without manufacturing a terminal state."""

        self._validate_identity(request_id, owner_id)
        self._ensure_schema()
        connection = self._connect()
        try:
            run = connection.execute(
                "SELECT * FROM web_sse_runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if run is None or run["owner_id"] != owner_id:
                return None
            result = self._execution_record(run)
            delivery_status = self._execution_delivery_status_locked(connection, run)
            result["delivery_status"] = delivery_status
            if delivery_status != "current":
                result[delivery_status] = True
                result["events"] = []
                result["queue_position"] = None
                return result
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
            result["events"] = events
            queue = connection.execute(
                """
                SELECT COUNT(*) AS position
                FROM web_session_execution_queue
                WHERE owner_id = ? AND session_id = ? AND queue_id <= (
                    SELECT queue_id FROM web_session_execution_queue
                    WHERE request_id = ?
                )
                """,
                (run["owner_id"], run["session_id"], request_id),
            ).fetchone()
            result["queue_position"] = int(queue["position"]) if queue and queue["position"] else None
            return result
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
                WHERE execution_state IN ('completed', 'failed_safe', 'cancelled')
                  AND updated_at < ?
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


_DEFAULT_STORE_LOCK = threading.Lock()
_DEFAULT_STORE: DurableSSEJournalStore | None = None


def get_durable_web_request_store() -> DurableSSEJournalStore:
    """Return the process-wide data-root-bound Web request store.

    The function lives in this dependency-light module so both WebChannel and
    AgentBridge use the same persistence implementation without importing one
    another's runtime classes.
    """

    from config import get_data_root

    global _DEFAULT_STORE
    path = os.path.realpath(
        os.path.join(get_data_root(), "web_sse_journal.sqlite3")
    )
    with _DEFAULT_STORE_LOCK:
        if _DEFAULT_STORE is None or _DEFAULT_STORE.path != path:
            _DEFAULT_STORE = DurableSSEJournalStore(path)
        return _DEFAULT_STORE
