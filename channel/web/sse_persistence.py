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
        {"running", "completed", "failed_safe", "cancelled", "in_doubt"}
    )

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
                        PRIMARY KEY(owner_id, session_id),
                        FOREIGN KEY(request_id) REFERENCES web_sse_runs(request_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS
                    idx_web_session_execution_fences_request
                    ON web_session_execution_fences(request_id);
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
                    (
                        "execution_state",
                        "TEXT NOT NULL DEFAULT 'in_doubt'",
                    ),
                    ("execution_lease", "TEXT NOT NULL DEFAULT ''"),
                    ("runner_id", "TEXT NOT NULL DEFAULT ''"),
                    ("execution_detail", "TEXT"),
                    ("execution_started_at", "REAL"),
                    ("execution_finished_at", "REAL"),
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
    def _detail(detail: object | None) -> str | None:
        if detail is None:
            return None
        text = str(detail).replace("\x00", " ").replace("\r", " ").replace("\n", " ")
        return text[:512]

    @staticmethod
    def _execution_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "request_id": str(row["request_id"]),
            "owner_id": str(row["owner_id"]),
            "session_id": str(row["session_id"]),
            "state": str(row["state"]),
            "execution_state": str(row["execution_state"]),
            "execution_detail": row["execution_detail"],
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "execution_started_at": (
                float(row["execution_started_at"])
                if row["execution_started_at"] is not None
                else None
            ),
            "execution_finished_at": (
                float(row["execution_finished_at"])
                if row["execution_finished_at"] is not None
                else None
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

    def claim_execution(
        self,
        request_id: str,
        owner_id: str,
        session_id: str,
        idempotency_key: str,
        request_digest: str,
        runner_id: str,
    ) -> dict[str, Any]:
        """Atomically claim one authenticated Web request.

        The first request receives a random server-only lease. A retry with
        exactly the same owner, session, idempotency key, and semantic request
        digest returns the original request id but cannot create a second
        worker. A mismatched payload is a hard conflict, not a convenient way
        to overwrite evidence for an earlier request.
        """

        self._validate_identity(request_id, owner_id, session_id)
        self.validate_idempotency_key(idempotency_key)
        self._validate_request_digest(request_digest)
        self._validate_runner_id(runner_id)
        self._ensure_schema()
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            by_request = connection.execute(
                """
                SELECT * FROM web_sse_runs WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if by_request is not None:
                if (
                    by_request["owner_id"] == owner_id
                    and by_request["session_id"] == session_id
                    and by_request["idempotency_key"] == idempotency_key
                    and by_request["request_digest"] == request_digest
                ):
                    connection.execute("COMMIT")
                    result = self._execution_record(by_request)
                    result["claim_status"] = "duplicate"
                    return result
                raise ValueError("Web request id collision")

            existing = connection.execute(
                """
                SELECT * FROM web_sse_runs
                WHERE owner_id = ? AND session_id = ? AND idempotency_key = ?
                """,
                (owner_id, session_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ValueError(
                        "idempotency_key was already used with a different request"
                    )
                connection.execute("COMMIT")
                result = self._execution_record(existing)
                result["claim_status"] = "duplicate"
                return result

            # This reservation is intentionally part of the same transaction as
            # the request claim.  A new idempotency key is not permission to
            # start another mutable Agent turn for the same Web session.
            # Returning a direct rejection here, rather than after a worker has
            # been started, prevents the Web UI from showing a false initial
            # success for a request that will never be allowed to execute.
            holder = connection.execute(
                """
                SELECT 1 FROM web_session_execution_fences
                WHERE owner_id = ? AND session_id = ?
                """,
                (owner_id, session_id),
            ).fetchone()
            if holder is not None:
                connection.execute("COMMIT")
                return {
                    "claim_status": "session_busy",
                    "owner_id": owner_id,
                    "session_id": session_id,
                    "execution_state": "session_busy",
                }

            lease_token = secrets.token_urlsafe(32)
            session_fence_token = secrets.token_urlsafe(32)
            connection.execute(
                """
                INSERT INTO web_sse_runs(
                    request_id, owner_id, session_id, state, idempotency_key,
                    request_digest, execution_state, execution_lease, runner_id,
                    created_at, updated_at, execution_started_at
                ) VALUES (?, ?, ?, 'running', ?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    owner_id,
                    session_id,
                    idempotency_key,
                    request_digest,
                    lease_token,
                    runner_id,
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO web_session_execution_fences(
                    owner_id, session_id, request_id, execution_lease,
                    runner_id, fence_token, acquired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    session_id,
                    request_id,
                    lease_token,
                    runner_id,
                    session_fence_token,
                    now,
                ),
            )
            connection.execute("COMMIT")
            return {
                "claim_status": "claimed",
                "request_id": request_id,
                "owner_id": owner_id,
                "session_id": session_id,
                "state": "running",
                "execution_state": "running",
                "execution_detail": None,
                "lease_token": lease_token,
                "runner_id": runner_id,
                "session_fence_token": session_fence_token,
                "created_at": now,
                "updated_at": now,
                "execution_started_at": now,
                "execution_finished_at": None,
            }
        except Exception:
            connection.rollback()
            raise
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
    ) -> None:
        """Fail closed unless this worker still owns the session fence."""

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
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT run.execution_state
                FROM web_session_execution_fences AS fence
                JOIN web_sse_runs AS run ON run.request_id = fence.request_id
                WHERE fence.owner_id = ? AND fence.session_id = ?
                  AND fence.request_id = ? AND fence.execution_lease = ?
                  AND fence.runner_id = ? AND fence.fence_token = ?
                """,
                (
                    owner_id,
                    session_id,
                    request_id,
                    lease_token,
                    runner_id,
                    fence_token,
                ),
            ).fetchone()
            if row is None or str(row["execution_state"]) != "running":
                raise RuntimeError(
                    "Web session execution fence is no longer owned by this worker"
                )
        finally:
            connection.close()

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
    ) -> None:
        """Settle an exact active claim without allowing stale writers to win."""

        if outcome not in self._EXECUTION_STATES - {"running"}:
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
            cursor = connection.execute(
                """
                UPDATE web_sse_runs
                SET execution_state = ?, execution_detail = ?, updated_at = ?,
                    execution_finished_at = ?
                WHERE request_id = ? AND owner_id = ? AND session_id = ?
                  AND execution_lease = ? AND runner_id = ?
                  AND execution_state = 'running'
                """,
                (
                    outcome,
                    self._detail(detail),
                    now,
                    now,
                    request_id,
                    owner_id,
                    session_id,
                    lease_token,
                    runner_id,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeError(
                    "Web execution completion was rejected (stale or missing lease)"
                )
            # The request lease is also the only authority permitted to release
            # its per-session fence.  Keep this deletion in the same transaction
            # as the terminal state write: a crash cannot leave a successful
            # request looking settled while another worker is permanently
            # excluded, nor can a different request release this worker's fence.
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

    def mark_interrupted_execution(
        self, request_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        """Fence an unavailable worker rather than automatically resuming it."""

        self._validate_identity(request_id, owner_id)
        self._ensure_schema()
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM web_sse_runs WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None or row["owner_id"] != owner_id:
                connection.execute("COMMIT")
                return None
            if row["execution_state"] == "running":
                connection.execute(
                    """
                    UPDATE web_sse_runs
                    SET execution_state = 'in_doubt',
                        execution_detail = ?,
                        updated_at = ?, execution_finished_at = ?
                    WHERE request_id = ? AND owner_id = ?
                      AND execution_state = 'running'
                    """,
                    (
                        "worker unavailable during durable recovery",
                        now,
                        now,
                        request_id,
                        owner_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM web_sse_runs WHERE request_id = ?",
                    (request_id,),
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
                """
                SELECT request_id, idempotency_key, execution_state
                FROM web_sse_runs WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if run is None:
                raise LookupError("unknown SSE request")
            # A legacy journal may be used by focused transport tests and older
            # non-HTTP callers.  New authenticated Web requests always have an
            # idempotency key, and their success-looking terminal delivery
            # event is forbidden until the *separate* Agent/tool execution
            # state is durably known.  Without this fence a crash between
            # `agent_end` and message persistence could replay a `done` as a
            # fabricated success.
            if (
                str(payload.get("type") or "") == "done"
                and run["idempotency_key"] not in (None, "")
                and run["execution_state"] not in {"completed", "cancelled"}
            ):
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
                SELECT request_id, owner_id, session_id, state, idempotency_key,
                       request_digest, execution_state, execution_lease, runner_id,
                       execution_detail, created_at, updated_at,
                       execution_started_at, execution_finished_at
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
            result = self._execution_record(run)
            result["events"] = events
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
