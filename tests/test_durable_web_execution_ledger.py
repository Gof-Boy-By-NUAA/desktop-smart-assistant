"""Adversarial tests for durable authenticated Web Agent execution claims."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.protocol.cancel import AgentCancelledError, get_cancel_registry
from bridge.agent_bridge import AgentBridge, _DurableWebExecutionFenceGuard
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.web import web_channel
from channel.web.sse_persistence import DurableSSEJournalStore


OWNER = "web:" + "a" * 32
SESSION = "durable-web-session"
KEY = "web-request-key-0001"
RUNNER = "web-runner-0001"


def _digest(fill: str = "a") -> str:
    return fill * 64


def _claim(
    store: DurableSSEJournalStore,
    request_id: str = "web-request-1",
    *,
    owner: str = OWNER,
    session: str = SESSION,
    key: str = KEY,
    digest: str = _digest(),
    runner: str = RUNNER,
    dispatch_payload: dict | None = None,
) -> dict:
    if dispatch_payload is None:
        dispatch_payload = {
            "prompt": "durable dispatch",
            "is_voice_input": False,
        }
    return store.claim_execution(
        request_id,
        owner,
        session,
        key,
        digest,
        runner,
        dispatch_payload,
    )


def _append_for_claim(store: DurableSSEJournalStore, claim: dict, event_id: int, payload: dict) -> None:
    store.append(
        claim["request_id"],
        event_id,
        payload,
        lease_token=claim["lease_token"],
        runner_id=claim["runner_id"],
        fence_token=claim["session_fence_token"],
    )


def _claim_web_request_in_child(path: str, start, results, index: int) -> None:
    try:
        store = DurableSSEJournalStore(path)
        start.wait(timeout=10)
        claim = _claim(
            store,
            f"web-process-request-{index}",
            runner=f"web-process-{multiprocessing.current_process().pid}",
        )
        results.put((claim["claim_status"], claim["request_id"]))
    except Exception as exc:  # pragma: no cover - asserted by parent
        results.put((f"error:{type(exc).__name__}:{exc}", ""))


def _acquire_session_fence_in_child(path: str, start, results, index: int) -> None:
    try:
        store = DurableSSEJournalStore(path)
        start.wait(timeout=10)
        claim = _claim(
            store,
            f"web-session-fence-request-{index}",
            key=f"web-session-fence-key-{index:04d}",
            runner=f"web-session-fence-runner-{multiprocessing.current_process().pid}",
        )
        results.put(claim["claim_status"])
    except Exception as exc:  # pragma: no cover - asserted by parent
        results.put(f"error:{type(exc).__name__}:{exc}")


def _request_running_cancellation_in_child(path: str, request_id: str, ready, results) -> None:
    """Write cancellation intent from a genuinely separate spawned process."""

    try:
        store = DurableSSEJournalStore(path)
        if not ready.wait(timeout=20):
            raise TimeoutError("parent did not make the running claim ready")
        record = store.request_execution_cancellation(
            request_id,
            OWNER,
            detail="cancelled by spawned peer",
        )
        results.put(record)
    except Exception as exc:  # pragma: no cover - asserted by parent
        results.put({"error": f"{type(exc).__name__}:{exc}"})


def _write_durable_sse_from_child(path: str, first_ready, write_tail, results) -> None:
    """Use the production writer journal in a spawned peer process."""
    try:
        store = DurableSSEJournalStore(path)
        claim = _claim(
            store,
            "cross-process-live-sse",
            key="cross-process-live-sse-key-0001",
            runner=f"cross-process-writer-{multiprocessing.current_process().pid}",
        )
        if claim["claim_status"] != "claimed":
            raise AssertionError(f"writer did not receive active claim: {claim!r}")
        writer = _web_instance()
        journal = writer._new_durable_sse_journal(store, claim)
        journal.put({"type": "delta", "content": "written-by-spawned-peer"})
        results.put(("ready", claim["request_id"]))
        first_ready.set()
        if not write_tail.wait(timeout=20):
            raise TimeoutError("parent never requested durable tail")
        journal.put({"type": "delta", "content": "durable-tail-from-spawned-peer"})
        results.put(("done", claim["request_id"]))
    except Exception as exc:  # pragma: no cover - asserted by parent
        results.put(("error", f"{type(exc).__name__}:{exc}"))
        first_ready.set()


def test_same_owner_session_key_is_durable_single_worker_and_payload_immutable(tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))

    first = _claim(store)
    duplicate = _claim(store, "web-request-2")

    assert first["claim_status"] == "claimed"
    assert duplicate["claim_status"] == "duplicate"
    assert duplicate["request_id"] == "web-request-1"
    assert duplicate["execution_state"] == "running"
    assert "lease_token" not in duplicate
    assert "session_fence_token" not in duplicate

    with pytest.raises(ValueError, match="different request"):
        _claim(store, "web-request-3", digest=_digest("b"))

    # Same spelling from another owner is a separate authenticated request, not
    # an accidental global lockout.
    other = _claim(
        store,
        "web-request-other-owner",
        owner="web:" + "b" * 32,
    )
    assert other["claim_status"] == "claimed"


def test_concurrent_store_instances_claim_only_one_worker(tmp_path):
    path = str(tmp_path / "web.sqlite3")
    start = threading.Barrier(2)

    def attempt(index: int) -> dict:
        store = DurableSSEJournalStore(path)
        start.wait(timeout=5)
        return _claim(store, f"web-request-{index}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))

    assert sorted(item["claim_status"] for item in results) == [
        "claimed",
        "duplicate",
    ]
    assert {item["request_id"] for item in results} == {
        next(item["request_id"] for item in results if item["claim_status"] == "claimed")
    }


def test_spawned_peer_cancellation_is_observed_before_tool_and_blocks_late_done(tmp_path):
    """A cancel received by process B must fence process A without fake success."""

    path = str(tmp_path / "web.sqlite3")
    store = DurableSSEJournalStore(path)
    claim = _claim(
        store,
        "spawned-cross-process-cancel",
        key="spawned-cross-process-cancel-key-0001",
    )
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    results = context.Queue()
    peer = context.Process(
        target=_request_running_cancellation_in_child,
        args=(path, claim["request_id"], ready, results),
    )
    peer.start()
    try:
        ready.set()
        record = results.get(timeout=20)
        assert record.get("error") is None
        assert record["cancellation_state"] == "requested"
        assert record["cancellation_accepted"] is True
        assert record["execution_state"] == "running"

        cancel_event = threading.Event()
        guard = _DurableWebExecutionFenceGuard(store, claim, cancel_event)
        guard.verify_now()
        assert cancel_event.is_set(), "process A did not observe process B cancellation"
        with pytest.raises(AgentCancelledError, match="cancellation requested"):
            with guard.tool_scope():
                pytest.fail("cancelled worker entered tool scope")

        # The cancellation request wins if the live worker attempts to settle a
        # stale completion after it has been requested.
        store.finish_execution(
            claim["request_id"],
            OWNER,
            SESSION,
            claim["lease_token"],
            RUNNER,
            outcome="completed",
            fence_token=claim["session_fence_token"],
        )
        replay = store.replay(claim["request_id"], OWNER)
        assert replay is not None
        assert replay["execution_state"] == "cancelled"
        assert replay["cancel_requested"] is True
        assert [payload["type"] for _event_id, payload in replay["events"]] == [
            "cancelled"
        ]
        with pytest.raises(RuntimeError, match="forbidden|terminal"):
            _append_for_claim(
                store,
                claim,
                2,
                {"type": "done", "content": "late false success"},
            )
    finally:
        peer.join(timeout=20)
        if peer.is_alive():
            peer.terminate()
            peer.join(timeout=10)
    assert peer.exitcode == 0


def test_two_processes_claim_one_authenticated_web_request_only(tmp_path):
    path = str(tmp_path / "web.sqlite3")
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_claim_web_request_in_child,
        args=(path, start, results, 1),
    )
    second = context.Process(
        target=_claim_web_request_in_child,
        args=(path, start, results, 2),
    )
    first.start()
    second.start()
    start.set()
    first.join(timeout=20)
    second.join(timeout=20)

    assert first.exitcode == 0
    assert second.exitcode == 0
    records = [results.get(timeout=5) for _ in range(2)]
    assert sorted(status for status, _request_id in records) == [
        "claimed",
        "duplicate",
    ]
    assert len({request_id for _status, request_id in records}) == 1


def test_distinct_requests_for_one_session_have_one_non_reentrant_fence(tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    first = _claim(
        store,
        "session-fence-first",
        key="web-session-fence-key-0001",
    )
    second = _claim(
        store,
        "session-fence-second",
        key="web-session-fence-key-0002",
    )

    assert first["claim_status"] == "claimed"
    assert second["claim_status"] == "queued"
    assert second["execution_state"] == "queued"
    assert second["queue_position"] == 1
    assert "lease_token" not in second
    assert "session_fence_token" not in second

    store.verify_session_execution_fence(
        first["request_id"],
        OWNER,
        SESSION,
        first["lease_token"],
        RUNNER,
        first["session_fence_token"],
    )
    with pytest.raises(RuntimeError, match="no longer owned"):
        store.verify_session_execution_fence(
            first["request_id"],
            OWNER,
            SESSION,
            first["lease_token"],
            RUNNER,
            "forged-session-fence",
        )

    store.finish_execution(
        first["request_id"],
        OWNER,
        SESSION,
        first["lease_token"],
        RUNNER,
        outcome="completed",
        fence_token=first["session_fence_token"],
    )
    promoted = store.claim_next_queued_execution(RUNNER)
    assert promoted is not None
    assert promoted["request_id"] == second["request_id"]
    assert promoted["session_fence_epoch"] > first["session_fence_epoch"]

    third = _claim(
        store,
        "session-fence-third",
        key="web-session-fence-key-0003",
    )
    assert third["claim_status"] == "queued"
    store.finish_execution(
        promoted["request_id"],
        OWNER,
        SESSION,
        promoted["lease_token"],
        RUNNER,
        outcome="completed",
        fence_token=promoted["session_fence_token"],
    )
    third_promoted = store.claim_next_queued_execution(RUNNER)
    assert third_promoted is not None
    assert third_promoted["request_id"] == third["request_id"]


def test_two_processes_acquire_one_fence_for_distinct_session_requests(tmp_path):
    path = str(tmp_path / "web.sqlite3")
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_acquire_session_fence_in_child,
        args=(path, start, results, 1),
    )
    second = context.Process(
        target=_acquire_session_fence_in_child,
        args=(path, start, results, 2),
    )
    first.start()
    second.start()
    start.set()
    first.join(timeout=20)
    second.join(timeout=20)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert sorted(results.get(timeout=5) for _ in range(2)) == [
        "claimed",
        "queued",
    ]


def test_crash_recovery_keeps_session_fenced_instead_of_reassigning_it(tmp_path):
    path = str(tmp_path / "web.sqlite3")
    first_store = DurableSSEJournalStore(path)
    first = _claim(
        first_store,
        "session-fence-crash-first",
        key="web-session-crash-key-0001",
    )
    queued = _claim(
        first_store,
        "session-fence-crash-second",
        key="web-session-crash-key-0002",
    )
    assert first["claim_status"] == "claimed"
    assert queued["claim_status"] == "queued"

    recovered_store = DurableSSEJournalStore(path)
    # A live lease is not stolen simply because this process cannot see the
    # original worker. Only an actual expired lease becomes uncertain.
    live = recovered_store.mark_interrupted_execution(first["request_id"], OWNER)
    assert live is not None
    assert live["execution_state"] == "running"

    expired_at = first["lease_expires_at"] + 0.6
    recovered = recovered_store.mark_interrupted_execution(
        first["request_id"], OWNER, now=expired_at
    )
    assert recovered is not None
    assert recovered["execution_state"] == "in_doubt"

    promoted = recovered_store.claim_next_queued_execution(RUNNER, now=expired_at)
    assert promoted is not None
    assert promoted["request_id"] == queued["request_id"]
    with pytest.raises(RuntimeError, match="rejected"):
        recovered_store.finish_execution(
            first["request_id"],
            OWNER,
            SESSION,
            first["lease_token"],
            RUNNER,
            outcome="completed",
            fence_token=first["session_fence_token"],
        )


def test_stale_or_forged_lease_cannot_settle_a_web_execution(tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store)

    with pytest.raises(RuntimeError, match="rejected"):
        store.finish_execution(
            claim["request_id"],
            OWNER,
            SESSION,
            "forged-lease",
            RUNNER,
            outcome="completed",
            fence_token=claim["session_fence_token"],
        )
    assert store.replay(claim["request_id"], OWNER)["execution_state"] == "running"

    store.finish_execution(
        claim["request_id"],
        OWNER,
        SESSION,
        claim["lease_token"],
        RUNNER,
        outcome="completed",
        fence_token=claim["session_fence_token"],
    )
    assert store.replay(claim["request_id"], OWNER)["execution_state"] == "completed"


def test_recovery_fences_a_running_claim_even_if_sse_has_done(tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    request_id = "legacy-sse-done-before-execution-fence"
    # Simulate an SSE record emitted by the predecessor implementation. New
    # authenticated runs cannot append this early `done` anymore, but old rows
    # must still be recovered as uncertain rather than retroactively trusted.
    store.begin(request_id, OWNER, SESSION)
    store.append(request_id, 1, {"type": "done", "content": "shown"})

    replay_before = store.replay(request_id, OWNER)
    assert replay_before["state"] == "completed"
    assert replay_before["execution_state"] == "running"

    recovered = store.mark_interrupted_execution(request_id, OWNER)
    assert recovered is not None
    assert recovered["execution_state"] == "in_doubt"
    assert "unavailable" in recovered["execution_detail"]


def test_authenticated_done_requires_a_durable_terminal_execution(tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "done-before-execution")

    with pytest.raises(RuntimeError, match="forbidden before durable execution"):
        _append_for_claim(
            store,
            claim,
            1,
            {"type": "done", "content": "false success"},
        )

    store.finish_execution(
        claim["request_id"],
        OWNER,
        SESSION,
        claim["lease_token"],
        RUNNER,
        outcome="completed",
        fence_token=claim["session_fence_token"],
    )
    _append_for_claim(
        store,
        claim,
        1,
        {"type": "done", "content": "durably complete"},
    )
    replay = store.replay(claim["request_id"], OWNER)
    assert replay["execution_state"] == "completed"
    assert replay["events"] == [
        (1, {"type": "done", "content": "durably complete"})
    ]


def test_expired_worker_cannot_append_or_settle_after_successor_claim(tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    first = _claim(store, "stale-first", key="stale-key-0001")
    queued = _claim(store, "stale-second", key="stale-key-0002")
    assert queued["claim_status"] == "queued"

    promoted = store.claim_next_queued_execution(
        RUNNER, now=first["lease_expires_at"] + 0.6
    )
    assert promoted is not None
    assert promoted["request_id"] == queued["request_id"]
    assert store.replay(first["request_id"], OWNER)["execution_state"] == "in_doubt"

    with pytest.raises(RuntimeError, match="forbidden|stale"):
        _append_for_claim(
            store,
            first,
            1,
            {"type": "delta", "content": "stale side effect evidence"},
        )
    with pytest.raises(RuntimeError, match="rejected"):
        store.finish_execution(
            first["request_id"],
            OWNER,
            SESSION,
            first["lease_token"],
            RUNNER,
            outcome="completed",
            fence_token=first["session_fence_token"],
        )


def test_queued_cancellation_is_owner_scoped_and_does_not_start_a_worker(tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    first = _claim(store, "cancel-first", key="cancel-key-0001")
    queued = _claim(store, "cancel-second", key="cancel-key-0002")
    other_owner = "web:" + "b" * 32

    assert store.cancel_queued_execution(queued["request_id"], other_owner) is None
    cancelled = store.cancel_queued_execution(queued["request_id"], OWNER)
    assert cancelled is not None
    assert cancelled["execution_state"] == "cancelled"
    assert store.claim_next_queued_execution(RUNNER) is None

    store.finish_execution(
        first["request_id"],
        OWNER,
        SESSION,
        first["lease_token"],
        RUNNER,
        outcome="completed",
        fence_token=first["session_fence_token"],
    )
    assert store.claim_next_queued_execution(RUNNER) is None


def test_authenticated_cancel_api_reports_running_request_as_pending_until_acknowledged(
    monkeypatch, tmp_path
):
    """REST success means a durable intent exists, not that the worker stopped."""

    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "cancel-api-running", key="cancel-api-running-key-0001")
    instance = _web_instance()
    instance.request_owners[claim["request_id"]] = OWNER
    instance.request_to_session[claim["request_id"]] = SESSION
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)
    monkeypatch.setattr(web_channel, "_require_web_session", lambda *_args: None)
    monkeypatch.setattr(
        web_channel.web,
        "data",
        lambda: json.dumps(
            {"request_id": claim["request_id"], "session_id": SESSION, "lang": "en"}
        ).encode(),
    )

    response = json.loads(WebChannel.cancel_request(instance, owner_id=OWNER))
    assert response == {
        "status": "success",
        "cancelled": 0,
        "cancellation_requested": 1,
        "cancellation_accepted": 1,
    }
    replay = store.replay(claim["request_id"], OWNER)
    assert replay is not None
    assert replay["execution_state"] == "running"
    assert replay["cancel_requested"] is True
    assert replay["events"] == []

    # A duplicate click remains an acknowledged pending intent, not a fake
    # completion, until the actual fence holder settles the request.
    duplicate = json.loads(WebChannel.cancel_request(instance, owner_id=OWNER))
    assert duplicate["status"] == "success"
    assert duplicate["cancelled"] == 0
    assert duplicate["cancellation_requested"] == 1

    store.finish_execution(
        claim["request_id"],
        OWNER,
        SESSION,
        claim["lease_token"],
        RUNNER,
        outcome="completed",
        fence_token=claim["session_fence_token"],
    )
    assert store.replay(claim["request_id"], OWNER)["execution_state"] == "cancelled"

    # Repeating the same request ID is idempotently "already cancelled".
    repeated_terminal = json.loads(WebChannel.cancel_request(instance, owner_id=OWNER))
    assert repeated_terminal["status"] == "success"
    assert repeated_terminal["cancelled"] == 1
    assert repeated_terminal["cancellation_requested"] == 0

    # A session-wide request with no queued/running target is distinct and must
    # not claim cancellation success.
    monkeypatch.setattr(
        web_channel.web,
        "data",
        lambda: json.dumps({"session_id": SESSION, "lang": "en"}).encode(),
    )
    no_target = json.loads(WebChannel.cancel_request(instance, owner_id=OWNER))
    assert no_target == {
        "status": "error",
        "cancelled": 0,
        "cancellation_requested": 0,
        "cancellation_accepted": 0,
        "message": "no active owned request accepted cancellation",
    }


def test_authenticated_web_slash_cancel_uses_durable_pending_session_intent(
    monkeypatch, tmp_path
):
    """Typed /cancel cannot bypass the cross-process durable cancel protocol."""

    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "slash-cancel-running", key="slash-cancel-key-0001")
    instance = _web_instance()
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)
    monkeypatch.setattr(web_channel, "_require_web_session", lambda *_args: None)
    monkeypatch.setattr(
        web_channel.web,
        "data",
        lambda: json.dumps(
            {
                "session_id": SESSION,
                "message": "/cancel",
                "stream": True,
                "lang": "en",
            }
        ).encode(),
    )

    response = json.loads(WebChannel.post_message(instance, owner_id=OWNER))
    assert response["status"] == "success"
    assert response["stream"] is False
    assert response["cancelled"] == 0
    assert response["cancellation_requested"] == 1
    assert response["cancellation_accepted"] == 1
    assert "Cancellation requested" in response["inline_reply"]
    replay = store.replay(claim["request_id"], OWNER)
    assert replay is not None
    assert replay["execution_state"] == "running"
    assert replay["cancel_requested"] is True


def test_session_cancel_terminalizes_queued_and_marks_running_intent(tmp_path):
    """Session cancellation does not hide the running/queued distinction."""

    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    running = _claim(store, "session-cancel-running", key="session-cancel-key-0001")
    queued = _claim(store, "session-cancel-queued", key="session-cancel-key-0002")

    result = store.request_session_cancellation(OWNER, SESSION, detail="stop this session")
    assert result == {"cancelled": 1, "cancellation_requested": 1}
    running_replay = store.replay(running["request_id"], OWNER)
    queued_replay = store.replay(queued["request_id"], OWNER)
    assert running_replay["execution_state"] == "running"
    assert running_replay["cancel_requested"] is True
    assert queued_replay["execution_state"] == "cancelled"
    assert [payload["type"] for _event_id, payload in queued_replay["events"]] == [
        "cancelled"
    ]


def test_additive_schema_migration_keeps_legacy_columns_readable(tmp_path):
    path = tmp_path / "legacy-web.sqlite3"
    now = 1_700_000_000.0
    legacy = sqlite3.connect(path)
    try:
        legacy.executescript(
            """
            CREATE TABLE web_sse_runs (
                request_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('running', 'completed')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE web_sse_events (
                request_id TEXT NOT NULL,
                event_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(request_id, event_id),
                FOREIGN KEY(request_id) REFERENCES web_sse_runs(request_id)
                    ON DELETE CASCADE
            );
            """
        )
        legacy.execute(
            """
            INSERT INTO web_sse_runs(
                request_id, owner_id, session_id, state, created_at, updated_at
            ) VALUES (?, ?, ?, 'completed', ?, ?)
            """,
            ("legacy-row", OWNER, SESSION, now, now),
        )
        legacy.commit()
    finally:
        legacy.close()

    store = DurableSSEJournalStore(str(path))
    migrated = store.replay("legacy-row", OWNER)
    assert migrated is not None
    assert migrated["execution_state"] == "in_doubt"

    # A rollback binary that only knows the old projection can still read
    # newly claimed rows, and old INSERT column lists remain additive. This is
    # schema compatibility only; live cross-version execution stays forbidden
    # by the deployment contract because the old binary cannot honor the new
    # execution fence.
    fresh = _claim(store, "new-row-after-migration")
    rollback_reader = sqlite3.connect(path)
    try:
        row = rollback_reader.execute(
            """
            SELECT request_id, owner_id, session_id, state, created_at, updated_at
            FROM web_sse_runs WHERE request_id = ?
            """,
            (fresh["request_id"],),
        ).fetchone()
        assert row[:4] == (fresh["request_id"], OWNER, SESSION, "running")
        rollback_reader.execute(
            """
            INSERT INTO web_sse_runs(
                request_id, owner_id, session_id, state, created_at, updated_at
            ) VALUES (?, ?, ?, 'running', ?, ?)
            """,
            ("legacy-writer-row", OWNER, SESSION, now, now),
        )
        rollback_reader.commit()
    finally:
        rollback_reader.close()

    legacy_writer_row = store.replay("legacy-writer-row", OWNER)
    assert legacy_writer_row is not None
    assert legacy_writer_row["execution_state"] == "in_doubt"


class _SystemExitAfterSideEffectAgent:
    def __init__(self, side_effects: list[str]):
        self.tools = []
        self.side_effects = side_effects
        self._last_run_new_messages = []

    def run_stream(self, **_kwargs):
        self.side_effects.append("external-tool-side-effect")
        raise SystemExit("crash after external tool")


class _CompletedAgent:
    def __init__(self, *, generated_messages=None):
        self.tools = []
        self._last_run_new_messages = list(
            generated_messages
            if generated_messages is not None
            else [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "durably completed"}],
                }
            ]
        )

    def run_stream(self, **_kwargs):
        return "durably completed"


class _EmptyAfterToolAgent(_CompletedAgent):
    def run_stream(self, **_kwargs):
        return ""


class _CancelledDuringPostProcessAgent(_CompletedAgent):
    """Model executor completed, then automatic post-process guard cancelled."""

    def run_stream(self, **kwargs):
        cancel_event = kwargs.get("cancel_event")
        assert cancel_event is not None
        cancel_event.set()
        raise AgentCancelledError("post-process guard observed cancellation")


def _bridge_for_execution_test(agent) -> AgentBridge:
    bridge = object.__new__(AgentBridge)
    bridge._session_run_locks = {}
    bridge._session_run_locks_guard = threading.Lock()
    bridge._agents_lock = threading.RLock()
    bridge.agents = {}
    bridge.get_agent = lambda **_kwargs: agent
    bridge._pre_persist_user_message = lambda *_args, **_kwargs: True
    bridge._persist_messages = lambda *_args, **_kwargs: True
    bridge._schedule_mcp_hot_reload = lambda *_args, **_kwargs: None
    return bridge


def _web_execution_context(claim: dict) -> Context:
    return Context(
        ContextType.TEXT,
        "perform external action",
        {
            "session_id": SESSION,
            "request_id": claim["request_id"],
            "session_owner_id": OWNER,
            "channel_type": "web",
            "_web_execution_lease": claim["lease_token"],
            "_web_execution_runner_id": RUNNER,
            "_web_session_execution_fence": claim["session_fence_token"],
        },
    )


def test_agentbridge_baseexception_after_side_effect_leaves_in_doubt(
    monkeypatch, tmp_path
):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "agentbridge-crash")
    side_effects: list[str] = []
    agent = _SystemExitAfterSideEffectAgent(side_effects)
    bridge = _bridge_for_execution_test(agent)
    monkeypatch.setattr(
        bridge, "_get_durable_web_execution_store", lambda: store
    )
    context = _web_execution_context(claim)

    with pytest.raises(SystemExit, match="crash after external tool"):
        bridge.agent_reply("perform external action", context=context)

    assert side_effects == ["external-tool-side-effect"]
    replay = store.replay(claim["request_id"], OWNER)
    assert replay["execution_state"] == "in_doubt"
    assert get_cancel_registry().has_active(SESSION) is False


def test_agentbridge_post_process_cancellation_is_a_known_cancelled_terminal(
    monkeypatch, tmp_path
):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "agentbridge-post-process-cancel")
    bridge = _bridge_for_execution_test(_CancelledDuringPostProcessAgent())
    monkeypatch.setattr(
        bridge, "_get_durable_web_execution_store", lambda: store
    )

    reply = bridge.agent_reply(
        "perform external action", context=_web_execution_context(claim)
    )

    assert reply.type == ReplyType.ERROR
    replay = store.replay(claim["request_id"], OWNER)
    assert replay is not None
    assert replay["execution_state"] == "cancelled"
    assert [payload["type"] for _event_id, payload in replay["events"]] == [
        "cancelled"
    ]
    assert get_cancel_registry().has_active(SESSION) is False


def test_agentbridge_marks_completed_only_after_normal_run_returns(monkeypatch, tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "agentbridge-completed")
    bridge = _bridge_for_execution_test(_CompletedAgent())
    monkeypatch.setattr(
        bridge, "_get_durable_web_execution_store", lambda: store
    )

    reply = bridge.agent_reply(
        "perform external action", context=_web_execution_context(claim)
    )

    assert reply.type == ReplyType.TEXT
    assert reply.content == "durably completed"
    assert store.replay(claim["request_id"], OWNER)["execution_state"] == "completed"


def test_agentbridge_rejects_forged_session_fence_before_agent_run(
    monkeypatch, tmp_path
):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "agentbridge-forged-session-fence")
    agent = _CompletedAgent()
    agent_runs: list[bool] = []
    agent.run_stream = lambda **_kwargs: agent_runs.append(True) or "unexpected"
    bridge = _bridge_for_execution_test(agent)
    monkeypatch.setattr(
        bridge, "_get_durable_web_execution_store", lambda: store
    )
    context = _web_execution_context(claim)
    context["_web_session_execution_fence"] = "forged-session-fence"

    reply = bridge.agent_reply("must not reach Agent", context=context)

    assert reply.type == ReplyType.ERROR
    assert agent_runs == []
    assert store.replay(claim["request_id"], OWNER)["execution_state"] == "failed_safe"


def test_agentbridge_persistence_failure_after_agent_run_is_in_doubt(
    monkeypatch, tmp_path
):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "agentbridge-persistence-failure")
    agent = _CompletedAgent(
        generated_messages=[
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "effect may be complete"}],
            }
        ]
    )
    bridge = _bridge_for_execution_test(agent)
    bridge._persist_messages = lambda *_args, **_kwargs: False
    monkeypatch.setattr(
        bridge, "_get_durable_web_execution_store", lambda: store
    )

    reply = bridge.agent_reply(
        "perform external action", context=_web_execution_context(claim)
    )

    assert reply.type == ReplyType.ERROR
    assert store.replay(claim["request_id"], OWNER)["execution_state"] == "in_doubt"


def test_agentbridge_empty_response_after_execution_is_in_doubt(monkeypatch, tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "agentbridge-empty-response")
    bridge = _bridge_for_execution_test(_EmptyAfterToolAgent())
    monkeypatch.setattr(
        bridge, "_get_durable_web_execution_store", lambda: store
    )

    reply = bridge.agent_reply(
        "perform external action", context=_web_execution_context(claim)
    )

    assert reply.type == ReplyType.ERROR
    assert (
        store.replay(claim["request_id"], OWNER)["execution_state"] == "in_doubt"
    )


def test_agentbridge_text_without_assistant_delta_is_in_doubt(monkeypatch, tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "agentbridge-missing-assistant-delta")
    bridge = _bridge_for_execution_test(
        _CompletedAgent(
            generated_messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "only user delta"}],
                }
            ]
        )
    )
    monkeypatch.setattr(
        bridge, "_get_durable_web_execution_store", lambda: store
    )

    reply = bridge.agent_reply(
        "perform external action", context=_web_execution_context(claim)
    )

    assert reply.type == ReplyType.ERROR
    assert (
        store.replay(claim["request_id"], OWNER)["execution_state"] == "in_doubt"
    )


WebChannel = dict(
    zip(
        web_channel.WebChannel.__code__.co_freevars,
        (cell.cell_contents for cell in web_channel.WebChannel.__closure__),
    )
)["cls"]


def _web_instance() -> SimpleNamespace:
    instance = SimpleNamespace(
        NOT_SUPPORT_REPLYTYPE=[],
        request_to_session={},
        request_owners={},
        session_queues={},
        sse_queues={},
        sse_last_active={},
        msg_id_counter=0,
        _sse_stream_lock=threading.RLock(),
        _sse_stream_generations={},
    )
    request_ids = iter(("web-post-1", "web-post-2", "web-post-3"))
    instance._generate_request_id = lambda: next(request_ids)
    instance._generate_msg_id = lambda: "msg-1"
    instance._compose_context = lambda _type, prompt, **_kwargs: Context(
        ContextType.TEXT, prompt, {"channel_type": "web"}
    )
    instance._make_sse_callback = lambda _request_id: (lambda _event: None)
    instance._fetch_latest_pair_seqs = lambda *_args, **_kwargs: {
        "user_seq": None,
        "bot_seq": None,
    }
    instance._maybe_dispatch_auto_tts = lambda *_args, **_kwargs: None
    instance.produce = lambda _context: None
    instance._settle_claimed_web_execution = (
        lambda durable_store, claim, **kwargs: WebChannel._settle_claimed_web_execution(
            instance, durable_store, claim, **kwargs
        )
    )
    instance._new_durable_sse_journal = (
        lambda durable_store, claim: WebChannel._new_durable_sse_journal(
            instance, durable_store, claim
        )
    )
    instance._attach_durable_sse_observer = (
        lambda durable_store, request_id, owner_id, **kwargs:
        WebChannel._attach_durable_sse_observer(
            instance, durable_store, request_id, owner_id, **kwargs
        )
    )
    instance._launch_claimed_web_execution = (
        lambda durable_store, claim: WebChannel._launch_claimed_web_execution(
            instance, durable_store, claim
        )
    )
    instance._wake_durable_web_dispatcher = lambda: None
    instance._drop_sse_request = lambda request_id: WebChannel._drop_sse_request(
        instance, request_id
    )
    return instance


class _NoStartThread:
    started: list[tuple] = []

    def __init__(self, *, target, args=(), **kwargs):
        self.target = target
        self.args = args
        self.kwargs = kwargs

    def start(self):
        # Preflight is a lease heartbeat, not an Agent worker.  Tests count
        # only runnable Agent dispatches to prove no duplicate side effect.
        if self.kwargs.get("name") != "smart-assistant-web-preflight-lease":
            self.started.append((self.target, self.args))

    def join(self, *_args, **_kwargs):
        return None


def test_authenticated_post_retries_one_request_and_rejects_missing_or_changed_key(
    monkeypatch, tmp_path
):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    instance = _web_instance()
    claimed_sessions: list[tuple[str, str]] = []
    _NoStartThread.started = []
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)
    monkeypatch.setattr(
        web_channel,
        "_claim_web_session",
        lambda session, owner: claimed_sessions.append((session, owner)),
    )
    monkeypatch.setattr(web_channel, "_web_identity", lambda owner: owner)
    monkeypatch.setattr(
        web_channel,
        "conf",
        lambda: {"single_chat_prefix": [""]},
    )
    monkeypatch.setattr(web_channel.threading, "Thread", _NoStartThread)

    missing_key = {
        "session_id": SESSION,
        "message": "call a tool",
        "stream": True,
    }
    monkeypatch.setattr(
        web_channel.web, "data", lambda: json.dumps(missing_key).encode()
    )
    rejected = json.loads(WebChannel.post_message(instance, owner_id=OWNER))
    assert rejected["status"] == "error"
    assert claimed_sessions == []
    assert _NoStartThread.started == []

    malformed_voice = {
        "session_id": SESSION,
        "message": "malformed voice flag",
        "stream": True,
        "idempotency_key": KEY,
        "is_voice": "false",
    }
    monkeypatch.setattr(
        web_channel.web, "data", lambda: json.dumps(malformed_voice).encode()
    )
    malformed = json.loads(WebChannel.post_message(instance, owner_id=OWNER))
    assert malformed["status"] == "error"
    assert claimed_sessions == []
    assert _NoStartThread.started == []

    invalid_attachment = {
        "session_id": SESSION,
        "message": "invalid attachment must not claim a session",
        "stream": True,
        "idempotency_key": KEY,
        "attachments": [
            {"file_type": "workspace_ref", "file_path": 42},
        ],
    }
    monkeypatch.setattr(
        web_channel.web, "data", lambda: json.dumps(invalid_attachment).encode()
    )
    invalid = json.loads(WebChannel.post_message(instance, owner_id=OWNER))
    assert invalid["status"] == "error"
    assert claimed_sessions == []
    assert _NoStartThread.started == []

    body = {
        "session_id": SESSION,
        "message": "call a tool",
        "stream": True,
        "idempotency_key": KEY,
    }
    monkeypatch.setattr(web_channel.web, "data", lambda: json.dumps(body).encode())
    first = json.loads(WebChannel.post_message(instance, owner_id=OWNER))
    second = json.loads(WebChannel.post_message(instance, owner_id=OWNER))
    assert first["status"] == second["status"] == "success"
    assert first["request_id"] == second["request_id"] == "web-post-1"
    assert second["duplicate"] is True
    assert len(_NoStartThread.started) == 1

    changed = dict(body, message="different external action")
    monkeypatch.setattr(
        web_channel.web, "data", lambda: json.dumps(changed).encode()
    )
    conflict = json.loads(WebChannel.post_message(instance, owner_id=OWNER))
    assert conflict["status"] == "error"
    assert len(_NoStartThread.started) == 1


def test_stream_response_hides_locally_buffered_old_done_when_mutation_closes_delivery(
    monkeypatch, tmp_path
):
    """A live journal cannot bypass durable mutation/generation visibility."""

    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(
        store,
        "stream-old-context-response",
        key="stream-old-context-key-0001",
    )
    store.finish_execution(
        claim["request_id"],
        OWNER,
        SESSION,
        claim["lease_token"],
        RUNNER,
        outcome="completed",
        fence_token=claim["session_fence_token"],
    )
    _append_for_claim(
        store,
        claim,
        1,
        {"type": "done", "content": "OLD_CONTEXT_SUCCESS"},
    )

    instance = _web_instance()
    attached = WebChannel._attach_durable_sse_observer(
        instance, store, claim["request_id"], OWNER
    )
    assert attached is not None
    assert claim["request_id"] in instance.sse_queues

    store.begin_session_mutation(
        OWNER,
        SESSION,
        mutation_kind="clear_context",
        detail="clear before live SSE can emit old done",
    )
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)

    wire = b"".join(
        WebChannel.stream_response(instance, claim["request_id"], owner_id=OWNER)
    )

    assert b"OLD_CONTEXT_SUCCESS" not in wire
    assert b'"type": "done"' not in wire
    assert b'"type": "error"' in wire
    assert b"session mutation is in progress" in wire
    assert claim["request_id"] not in instance.sse_queues


def test_authenticated_post_rejects_exact_retry_while_session_mutation_is_pending(
    monkeypatch, tmp_path
):
    """A mutation-pending idempotency retry must not attach SSE or report success."""

    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    prompt = "do not replay this old request"
    store.claim_execution(
        "web-post-1",
        OWNER,
        SESSION,
        KEY,
        web_channel._web_request_digest(prompt, False),
        RUNNER,
        {"prompt": prompt, "is_voice_input": False},
    )
    store.begin_session_mutation(
        OWNER,
        SESSION,
        mutation_kind="clear_context",
        detail="block exact HTTP retry",
    )
    instance = _web_instance()
    body = {
        "session_id": SESSION,
        "message": prompt,
        "stream": True,
        "idempotency_key": KEY,
    }
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)
    monkeypatch.setattr(web_channel, "conf", lambda: {"single_chat_prefix": [""]})
    monkeypatch.setattr(web_channel.web, "data", lambda: json.dumps(body).encode())

    response = json.loads(WebChannel.post_message(instance, owner_id=OWNER))

    assert response == {
        "status": "error",
        "request_id": "web-post-1",
        "stream": False,
        "execution_state": "running",
        "mutation_pending": True,
        "message": "session mutation is in progress; prior request cannot be replayed",
    }
    assert instance.sse_queues == {}
    assert instance.request_to_session == {}


def test_authenticated_post_queues_distinct_key_while_session_turn_is_reserved(
    monkeypatch, tmp_path
):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    instance = _web_instance()
    _NoStartThread.started = []
    current_payload = {
        "session_id": SESSION,
        "message": "first external action",
        "stream": True,
        "idempotency_key": "web-session-reservation-key-0001",
    }
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)
    monkeypatch.setattr(web_channel, "_claim_web_session", lambda *_args: None)
    monkeypatch.setattr(web_channel, "_web_identity", lambda owner: owner)
    monkeypatch.setattr(web_channel, "conf", lambda: {"single_chat_prefix": [""]})
    monkeypatch.setattr(web_channel.web, "data", lambda: json.dumps(current_payload).encode())
    monkeypatch.setattr(web_channel.threading, "Thread", _NoStartThread)

    accepted = json.loads(WebChannel.post_message(instance, owner_id=OWNER))
    assert accepted["status"] == "success"
    assert accepted["execution_state"] == "running"
    assert len(_NoStartThread.started) == 1
    assert store.replay(accepted["request_id"], OWNER)["execution_state"] == "running"

    current_payload = {
        "session_id": SESSION,
        "message": "second external action",
        "stream": True,
        "idempotency_key": "web-session-reservation-key-0002",
    }
    queued = json.loads(WebChannel.post_message(instance, owner_id=OWNER))

    assert queued == {
        "status": "success",
        "request_id": "web-post-2",
        "stream": True,
        "duplicate": False,
        "execution_state": "queued",
        "queued": True,
        "queue_position": 1,
    }
    assert len(_NoStartThread.started) == 1
    assert store.replay(queued["request_id"], OWNER)["execution_state"] == "queued"


def test_web_plugin_hook_never_runs_before_its_durable_execution_claim(
    monkeypatch, tmp_path
):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    instance = _web_instance()
    observed_states: list[str] = []
    _NoStartThread.started = []
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)
    monkeypatch.setattr(web_channel, "_claim_web_session", lambda *_args: None)
    monkeypatch.setattr(web_channel, "_web_identity", lambda owner: owner)
    monkeypatch.setattr(
        web_channel,
        "conf",
        lambda: {"single_chat_prefix": [""]},
    )
    monkeypatch.setattr(web_channel.threading, "Thread", _NoStartThread)

    def filtered_context(_type, _prompt, **_kwargs):
        replay = store.replay("web-post-1", OWNER)
        assert replay is not None
        observed_states.append(replay["execution_state"])
        return None

    instance._compose_context = filtered_context
    payload = {
        "session_id": SESSION,
        "message": "extension hook sees durable claim first",
        "stream": True,
        "idempotency_key": KEY,
    }
    monkeypatch.setattr(
        web_channel.web, "data", lambda: json.dumps(payload).encode()
    )

    response = json.loads(WebChannel.post_message(instance, owner_id=OWNER))

    assert response["status"] == "error"
    assert observed_states == ["running"]
    assert _NoStartThread.started == []
    assert (
        store.replay("web-post-1", OWNER)["execution_state"] == "failed_safe"
    )


def test_session_claim_failure_is_recorded_as_failed_safe_before_worker_start(
    monkeypatch, tmp_path
):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    instance = _web_instance()
    _NoStartThread.started = []
    payload = {
        "session_id": SESSION,
        "message": "session-store failure",
        "stream": True,
        "idempotency_key": KEY,
    }
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)

    def fail_session_claim(*_args):
        raise RuntimeError("session store down")

    monkeypatch.setattr(
        web_channel,
        "_claim_web_session",
        fail_session_claim,
    )
    monkeypatch.setattr(web_channel, "_web_identity", lambda owner: owner)
    monkeypatch.setattr(
        web_channel, "conf", lambda: {"single_chat_prefix": [""]}
    )
    monkeypatch.setattr(
        web_channel.web, "data", lambda: json.dumps(payload).encode()
    )
    monkeypatch.setattr(web_channel.threading, "Thread", _NoStartThread)

    response = json.loads(WebChannel.post_message(instance, owner_id=OWNER))

    assert response["status"] == "error"
    assert _NoStartThread.started == []
    assert (
        store.replay("web-post-1", OWNER)["execution_state"] == "failed_safe"
    )


def test_concurrent_authenticated_web_posts_start_one_worker(monkeypatch, tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    first = _web_instance()
    second = _web_instance()
    first._generate_request_id = lambda: "web-race-first"
    second._generate_request_id = lambda: "web-race-second"
    _NoStartThread.started = []
    real_thread = threading.Thread
    barrier = threading.Barrier(2)
    results: list[dict] = []
    failures: list[BaseException] = []
    payload = {
        "session_id": SESSION,
        "message": "one external operation despite POST race",
        "stream": True,
        "idempotency_key": KEY,
    }
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)
    monkeypatch.setattr(web_channel, "_claim_web_session", lambda *_args: None)
    monkeypatch.setattr(web_channel, "_web_identity", lambda owner: owner)
    monkeypatch.setattr(
        web_channel, "conf", lambda: {"single_chat_prefix": [""]}
    )
    monkeypatch.setattr(web_channel, "web", SimpleNamespace(
        data=lambda: json.dumps(payload).encode()
    ))
    monkeypatch.setattr(web_channel.threading, "Thread", _NoStartThread)

    def post(instance):
        try:
            barrier.wait(timeout=5)
            results.append(
                json.loads(WebChannel.post_message(instance, owner_id=OWNER))
            )
        except BaseException as exc:  # asserted below after both join
            failures.append(exc)

    workers = [
        real_thread(target=post, args=(first,)),
        real_thread(target=post, args=(second,)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert failures == []
    assert all(not worker.is_alive() for worker in workers)
    assert sorted(result["status"] for result in results) == ["success", "success"]
    assert sum(bool(result.get("duplicate")) for result in results) == 1
    assert len(_NoStartThread.started) == 1
    assert {result["request_id"] for result in results} == {
        next(result["request_id"] for result in results if not result.get("duplicate"))
    }


def test_duplicate_in_doubt_web_post_is_error_not_false_success(monkeypatch, tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    instance = _web_instance()
    _NoStartThread.started = []
    payload = {
        "session_id": SESSION,
        "message": "do not retry an uncertain tool operation",
        "stream": True,
        "idempotency_key": KEY,
    }
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)
    monkeypatch.setattr(web_channel, "_claim_web_session", lambda *_args: None)
    monkeypatch.setattr(web_channel, "_web_identity", lambda owner: owner)
    monkeypatch.setattr(
        web_channel, "conf", lambda: {"single_chat_prefix": [""]}
    )
    monkeypatch.setattr(
        web_channel.web, "data", lambda: json.dumps(payload).encode()
    )
    monkeypatch.setattr(web_channel.threading, "Thread", _NoStartThread)

    first = json.loads(WebChannel.post_message(instance, owner_id=OWNER))
    store.mark_interrupted_execution(
        first["request_id"],
        OWNER,
        now=time.time() + store._lease_seconds() + 1.0,
    )
    retry = json.loads(WebChannel.post_message(instance, owner_id=OWNER))

    assert first["status"] == "success"
    assert retry == {
        "status": "error",
        "request_id": first["request_id"],
        "stream": False,
        "duplicate": True,
        "execution_state": "in_doubt",
        "message": "prior request did not reach a safely retryable terminal outcome",
    }
    assert len(_NoStartThread.started) == 1


def test_durable_agent_cancel_callback_is_nonterminal_until_fence_settlement(
    monkeypatch, tmp_path
):
    """Callback cancellation cannot outrun the exact fence's terminal commit."""

    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "callback-cancel-before-settlement")
    instance = _web_instance()
    instance.request_to_session[claim["request_id"]] = SESSION
    instance.request_owners[claim["request_id"]] = OWNER
    instance.sse_queues[claim["request_id"]] = instance._new_durable_sse_journal(
        store, claim
    )
    callback = WebChannel._make_sse_callback(instance, claim["request_id"])
    callback({"type": "agent_cancelled", "data": {"final_response": "partial"}})

    before_settlement = store.replay(claim["request_id"], OWNER)
    assert before_settlement["execution_state"] == "running"
    assert [payload["type"] for _event_id, payload in before_settlement["events"]] == [
        "phase"
    ]

    store.finish_execution(
        claim["request_id"],
        OWNER,
        SESSION,
        claim["lease_token"],
        RUNNER,
        outcome="cancelled",
        fence_token=claim["session_fence_token"],
    )
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)
    WebChannel.send(
        instance,
        Reply(ReplyType.TEXT, "partial"),
        _web_execution_context(claim),
    )
    after_settlement = store.replay(claim["request_id"], OWNER)
    assert after_settlement["execution_state"] == "cancelled"
    assert [payload["type"] for _event_id, payload in after_settlement["events"]] == [
        "phase", "cancelled"
    ]


def test_recovery_scrubs_stale_cancelled_marker_before_in_doubt_error(
    monkeypatch, tmp_path
):
    """A predecessor's unconfirmed cancelled event is not recovery evidence."""

    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "old-cancelled-before-settlement")
    # Simulate the predecessor callback protocol, which wrote a terminal
    # marker before it durably settled execution. The worker then disappears.
    _append_for_claim(
        store,
        claim,
        1,
        {"type": "cancelled", "content": "old false terminal"},
    )
    store.mark_interrupted_execution(
        claim["request_id"],
        OWNER,
        now=time.time() + store._lease_seconds() + 1.0,
    )
    instance = _web_instance()
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)

    assert WebChannel._recover_sse_request(instance, claim["request_id"], OWNER) is True
    journal = instance.sse_queues[claim["request_id"]]
    first = journal.read_after(0, timeout=0)
    second = journal.read_after(1, timeout=0)
    assert first is not None and first[1]["type"] == "phase"
    assert second is not None and second[1]["type"] == "error"
    assert store.replay(claim["request_id"], OWNER)["execution_state"] == "in_doubt"


def test_recovery_scrubs_previously_persisted_done_before_in_doubt_error(
    monkeypatch, tmp_path
):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    request_id = "old-terminal-before-fence"
    # Simulate a journal written by the predecessor implementation: it had a
    # transport done but never had an authenticated execution outcome.
    store.begin(request_id, OWNER, SESSION)
    store.append(request_id, 1, {"type": "done", "content": "must not replay"})
    instance = _web_instance()
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)

    assert WebChannel._recover_sse_request(instance, request_id, OWNER) is True
    journal = instance.sse_queues[request_id]
    first = journal.read_after(0, timeout=0)
    terminal = journal.read_after(1, timeout=0)

    assert first is not None
    assert first[0] == 1
    assert first[1]["type"] == "phase"
    assert terminal is not None
    assert terminal[0] == 2
    assert terminal[1]["type"] == "error"
    assert "unconfirmed" in terminal[1]["message"]
    assert store.replay(request_id, OWNER)["execution_state"] == "in_doubt"


def test_terminal_delivery_fences_an_unsettled_claim_instead_of_sending_done(
    monkeypatch, tmp_path
):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "terminal-delivery-unsettled")
    instance = _web_instance()
    instance.request_to_session[claim["request_id"]] = SESSION
    instance.request_owners[claim["request_id"]] = OWNER
    journal = web_channel._SSEEventJournal(
        lambda event_id, payload: _append_for_claim(store, claim, event_id, payload)
    )
    instance.sse_queues[claim["request_id"]] = journal
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)

    WebChannel.send(
        instance,
        Reply(ReplyType.TEXT, "this must not become done"),
        _web_execution_context(claim),
    )

    event = journal.read_after(0, timeout=0)
    assert event is not None
    assert event[1]["type"] == "error"
    assert (
        store.replay(claim["request_id"], OWNER)["execution_state"] == "in_doubt"
    )


def test_durable_cancelled_claim_hydrates_marker_and_never_appends_late_done(
    monkeypatch, tmp_path
):
    """A post-cancel send cannot turn a persisted cancellation into success."""

    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "cancelled-terminal-delivery")
    assert store.request_execution_cancellation(
        claim["request_id"], OWNER, detail="stop before completion"
    )["cancellation_state"] == "requested"
    store.finish_execution(
        claim["request_id"],
        OWNER,
        SESSION,
        claim["lease_token"],
        RUNNER,
        outcome="completed",
        fence_token=claim["session_fence_token"],
    )
    instance = _web_instance()
    instance.request_to_session[claim["request_id"]] = SESSION
    instance.request_owners[claim["request_id"]] = OWNER
    instance.sse_queues[claim["request_id"]] = instance._new_durable_sse_journal(
        store, claim
    )
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)

    WebChannel.send(
        instance,
        Reply(ReplyType.TEXT, "this must not be a completed answer"),
        _web_execution_context(claim),
    )

    replay = store.replay(claim["request_id"], OWNER)
    assert replay["execution_state"] == "cancelled"
    assert [payload["type"] for _event_id, payload in replay["events"]] == [
        "cancelled"
    ]
    journal = instance.sse_queues[claim["request_id"]]
    marker = journal.read_after(0, timeout=0)
    assert marker is not None
    assert marker[1]["type"] == "cancelled"
    assert journal.read_after(1, timeout=0) is None


def test_terminal_delivery_emits_done_only_after_completed_claim(
    monkeypatch, tmp_path
):
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "terminal-delivery-completed")
    store.finish_execution(
        claim["request_id"],
        OWNER,
        SESSION,
        claim["lease_token"],
        RUNNER,
        outcome="completed",
        fence_token=claim["session_fence_token"],
    )
    instance = _web_instance()
    instance.request_to_session[claim["request_id"]] = SESSION
    instance.request_owners[claim["request_id"]] = OWNER
    journal = web_channel._SSEEventJournal(
        lambda event_id, payload: _append_for_claim(store, claim, event_id, payload)
    )
    instance.sse_queues[claim["request_id"]] = journal
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)

    WebChannel.send(
        instance,
        Reply(ReplyType.TEXT, "durably completed"),
        _web_execution_context(claim),
    )

    event = journal.read_after(0, timeout=0)
    assert event is not None
    assert event[1]["type"] == "done"
    assert event[1]["content"] == "durably completed"


def test_agent_events_never_emit_terminal_done_before_durable_settlement():
    class _Journal:
        def __init__(self):
            self.items = []

        def put(self, item):
            self.items.append(item)

    request_id = "agent-event-terminal-order"
    journal = _Journal()
    channel = SimpleNamespace(
        sse_queues={request_id: journal},
        request_owners={},
    )
    callback = WebChannel._make_sse_callback(channel, request_id)

    callback({"type": "error", "data": {"error": "provider disconnected"}})
    callback({"type": "agent_end", "data": {"final_response": ""}})

    assert [item["type"] for item in journal.items] == ["phase"]


def test_web_and_desktop_clients_keep_one_secure_key_across_transport_retries():
    root = Path(__file__).resolve().parents[1]
    web_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(
        encoding="utf-8"
    )
    desktop_client = (
        root / "desktop" / "src" / "renderer" / "src" / "api" / "client.ts"
    ).read_text(encoding="utf-8")
    desktop_store = (
        root / "desktop" / "src" / "renderer" / "src" / "store" / "chatStore.ts"
    ).read_text(encoding="utf-8")

    assert "function createMessageIdempotencyKey()" in web_source
    assert "window.crypto.randomUUID" in web_source
    assert "idempotency_key: idempotencyKey" in web_source
    assert "idempotency_key: opts?.idempotencyKey" in desktop_client
    assert "globalThis.crypto.randomUUID()" in desktop_store
    assert "idempotencyKey," in desktop_store
    assert "const errorText = (" in web_source
    steer_source = web_source[
        web_source.index("function steerActiveTask()"):
        web_source.index("steerBtn.addEventListener", web_source.index("function steerActiveTask()"))
    ]
    assert "createMessageIdempotencyKey()" in steer_source
    assert "idempotency_key: idempotencyKey" in steer_source


def test_durable_queued_response_has_visible_and_cancellable_web_and_desktop_contract():
    """Keep the durable POST queue state visible until actual SSE activity starts.

    This is a source-contract regression guard, paired with the renderer build;
    browser/customer journey coverage remains a separate acceptance requirement.
    """
    root = Path(__file__).resolve().parents[1]
    web_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(
        encoding="utf-8"
    )
    desktop_client = (
        root / "desktop" / "src" / "renderer" / "src" / "api" / "client.ts"
    ).read_text(encoding="utf-8")
    desktop_store = (
        root / "desktop" / "src" / "renderer" / "src" / "store" / "chatStore.ts"
    ).read_text(encoding="utf-8")
    desktop_types = (
        root / "desktop" / "src" / "renderer" / "src" / "types.ts"
    ).read_text(encoding="utf-8")
    bubble = (
        root / "desktop" / "src" / "renderer" / "src" / "components" / "MessageBubble.tsx"
    ).read_text(encoding="utf-8")
    i18n_source = (root / "desktop" / "src" / "renderer" / "src" / "i18n.ts").read_text(
        encoding="utf-8"
    )

    # Web: the accepted durable queue response becomes visible before SSE starts,
    # and the pre-existing request identity/cancel mode remains active.
    assert web_source.count("setLoadingExecutionState(") == 4  # helper + 3 POST paths
    assert "data.execution_state || (data.queued ? 'queued' : '')" in web_source
    assert "loadingEl.dataset.executionState = 'queued';" in web_source
    assert "loading-dots" in web_source
    assert "loading-status" in web_source
    assert "execution_queued_position" in web_source
    assert web_source.count("setSendBtnCancelMode(data.request_id);") >= 3

    # Desktop: parse the backend contract, retain requestId/isStreaming for
    # cancellation, render the queue state, and remove it only after SSE data.
    assert "execution_state?: 'queued' | 'running' | 'completed'" in desktop_client
    assert "queue_position?: number" in desktop_client
    assert "isQueued?: boolean" in desktop_types
    assert "queuePosition?: number" in desktop_types
    assert "const isQueued = res.execution_state === 'queued' || res.queued === true" in desktop_store
    assert "patchSession(sid, { requestId: res.request_id })" in desktop_store
    assert "m.isQueued ? { ...m, isQueued: false, queuePosition: undefined } : m" in desktop_store
    assert "!message.isQueued" in bubble
    assert "t('msg_queued_position')" in bubble
    assert "msg_queued_position:" in i18n_source


def test_web_and_desktop_cancel_contract_distinguishes_requested_from_cancelled():
    """Keep the two-phase cancellation UX from regressing to fake success."""

    root = Path(__file__).resolve().parents[1]
    web_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(
        encoding="utf-8"
    )
    desktop_client = (
        root / "desktop" / "src" / "renderer" / "src" / "api" / "client.ts"
    ).read_text(encoding="utf-8")
    desktop_store = (
        root / "desktop" / "src" / "renderer" / "src" / "store" / "chatStore.ts"
    ).read_text(encoding="utf-8")
    web_server = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")

    assert '"cancellation_requested": cancellation_requested' in web_server
    assert '"cancellation_accepted": cancellation_accepted' in web_server
    assert "const cancellationAccepted = Number(data.cancelled || 0) + Number(data.cancellation_requested || 0);" in web_source
    assert "cancellation_requested?: number" in desktop_client
    assert "const cancellationAccepted = Number(result.cancelled || 0) + Number(result.cancellation_requested || 0)" in desktop_store
    assert "cancelled label is rendered exclusively by the bound SSE event" in web_source
    assert "`cancelled` and terminal `done` are observed" in desktop_store


def test_second_webchannel_observes_live_peer_sse_without_claiming_or_replaying_worker(
    monkeypatch, tmp_path
):
    """Two independent WebChannel instances share one live durable claim.

    The observer must use the durable event cursor rather than a local worker
    or a second claim. This exercises the real `stream_response` generator,
    including its empty-local-journal SQLite poll path. A separate spawned
    process test below establishes the process boundary independently.
    """
    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(store, "cross-instance-live-sse")
    writer = _web_instance()
    observer = _web_instance()
    request_id = claim["request_id"]
    writer.request_to_session[request_id] = SESSION
    writer.request_owners[request_id] = OWNER
    writer_journal = writer._new_durable_sse_journal(store, claim)
    writer.sse_queues[request_id] = writer_journal
    observer._recover_sse_request = lambda rid, owner: WebChannel._recover_sse_request(
        observer, rid, owner
    )
    observer_produced: list[Context] = []
    observer.produce = lambda context: observer_produced.append(context)
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)

    writer_journal.put({"type": "delta", "content": "written-by-peer"})
    stream = WebChannel.stream_response(observer, request_id, OWNER, after_event_id=0)
    first_chunk = next(stream)
    stream.close()

    first_lines = first_chunk.decode("utf-8").splitlines()
    assert int(next(line[4:] for line in first_lines if line.startswith("id: "))) == 1
    assert json.loads(next(line[6:] for line in first_lines if line.startswith("data: "))) == {
        "type": "delta",
        "content": "written-by-peer",
    }
    assert observer.request_owners[request_id] == OWNER
    assert observer.request_to_session[request_id] == SESSION
    assert observer_produced == []
    assert store.replay(request_id, OWNER)["execution_state"] == "running"
    assert store.claim_next_queued_execution("observer-must-not-claim") is None

    # The observer has no local writer. Its next generator must poll SQLite and
    # hydrate the second payload from the peer rather than return false success
    # or starting an Agent itself.
    writer_journal.put({"type": "delta", "content": "durable-tail-from-peer"})
    resumed = WebChannel.stream_response(observer, request_id, OWNER, after_event_id=1)
    second_chunk = next(resumed)
    resumed.close()
    second_lines = second_chunk.decode("utf-8").splitlines()
    assert int(next(line[4:] for line in second_lines if line.startswith("id: "))) == 2
    assert json.loads(next(line[6:] for line in second_lines if line.startswith("data: "))) == {
        "type": "delta",
        "content": "durable-tail-from-peer",
    }
    assert observer_produced == []
    assert store.replay(request_id, OWNER)["execution_state"] == "running"



def test_spawned_peer_writer_is_observed_without_second_worker_or_claim(monkeypatch, tmp_path):
    """A genuine Windows-spawned writer is observed by a separate WebChannel."""
    path = str(tmp_path / "web.sqlite3")
    context = multiprocessing.get_context("spawn")
    first_ready = context.Event()
    write_tail = context.Event()
    results = context.Queue()
    writer_process = context.Process(
        target=_write_durable_sse_from_child,
        args=(path, first_ready, write_tail, results),
    )
    writer_process.start()
    try:
        assert first_ready.wait(timeout=20)
        stage, detail = results.get(timeout=5)
        assert (stage, detail) == ("ready", "cross-process-live-sse")

        store = DurableSSEJournalStore(path)
        observer = _web_instance()
        observer._recover_sse_request = lambda rid, owner: WebChannel._recover_sse_request(
            observer, rid, owner
        )
        observer_produced: list[Context] = []
        observer.produce = lambda context: observer_produced.append(context)
        monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)

        first = WebChannel.stream_response(
            observer, "cross-process-live-sse", OWNER, after_event_id=0
        )
        first_chunk = next(first)
        first.close()
        first_lines = first_chunk.decode("utf-8").splitlines()
        assert int(next(line[4:] for line in first_lines if line.startswith("id: "))) == 1
        assert json.loads(next(line[6:] for line in first_lines if line.startswith("data: "))) == {
            "type": "delta",
            "content": "written-by-spawned-peer",
        }
        assert observer_produced == []
        assert store.replay("cross-process-live-sse", OWNER)["execution_state"] == "running"
        assert store.claim_next_queued_execution("observer-must-not-claim") is None

        write_tail.set()
        resumed = WebChannel.stream_response(
            observer, "cross-process-live-sse", OWNER, after_event_id=1
        )
        second_chunk = next(resumed)
        resumed.close()
        second_lines = second_chunk.decode("utf-8").splitlines()
        assert int(next(line[4:] for line in second_lines if line.startswith("id: "))) == 2
        assert json.loads(next(line[6:] for line in second_lines if line.startswith("data: "))) == {
            "type": "delta",
            "content": "durable-tail-from-spawned-peer",
        }
        assert observer_produced == []
        assert store.replay("cross-process-live-sse", OWNER)["execution_state"] == "running"
        assert results.get(timeout=10) == ("done", "cross-process-live-sse")
    finally:
        write_tail.set()
        writer_process.join(timeout=20)
        if writer_process.is_alive():
            writer_process.terminate()
            writer_process.join(timeout=10)
    assert writer_process.exitcode == 0



def test_session_mutation_atomically_closes_admission_cancels_queue_and_reopens_only_clear(tmp_path):
    """Delete/clear must not let another Web process enqueue during its gap."""

    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    running = _claim(
        store,
        "mutation-running",
        key="mutation-running-key-0001",
    )
    queued = _claim(
        store,
        "mutation-queued",
        key="mutation-queued-key-0002",
    )
    assert queued["claim_status"] == "queued"

    mutation = store.begin_session_mutation(
        OWNER,
        SESSION,
        mutation_kind="clear_context",
        detail="test destructive clear",
    )
    assert mutation["created"] is True
    assert mutation["cancelled"] == 1
    assert mutation["cancellation_requested"] == 1

    queued_replay = store.replay(queued["request_id"], OWNER)
    running_replay = store.replay(running["request_id"], OWNER)
    assert queued_replay is not None
    assert running_replay is not None
    assert queued_replay["execution_state"] == "cancelled"
    assert queued_replay["delivery_status"] == "mutation_pending"
    assert queued_replay["events"] == []
    # The cancellation marker remains in the durable audit log but is no longer
    # presentation-eligible once a clear/delete mutation has closed delivery.
    connection = sqlite3.connect(store.path)
    try:
        raw_cancel = connection.execute(
            "SELECT payload_json FROM web_sse_events WHERE request_id = ?",
            (queued["request_id"],),
        ).fetchone()
    finally:
        connection.close()
    assert raw_cancel is not None
    assert json.loads(raw_cancel[0])["type"] == "cancelled"
    assert running_replay["execution_state"] == "running"
    assert running_replay["delivery_status"] == "mutation_pending"
    assert running_replay["cancel_requested"] is True

    # Exact idempotency retries must not attach/replay or become a hidden
    # dispatcher while the destructive mutation holds admission closed.
    duplicate = _claim(
        store,
        queued["request_id"],
        key="mutation-queued-key-0002",
    )
    assert duplicate["claim_status"] == "mutation_pending"
    assert duplicate["mutation_pending"] is True
    assert duplicate["delivery_status"] == "mutation_pending"
    assert store.claim_next_queued_execution("mutation-observer") is None

    with pytest.raises(RuntimeError, match="destructive mutation"):
        _claim(
            store,
            "mutation-new-after-close",
            key="mutation-new-after-close-key-0003",
        )

    # Owner/session isolation remains scoped: the same session locator under a
    # different authenticated principal is not globally denied.
    other = _claim(
        store,
        "mutation-other-owner",
        owner="web:" + "b" * 32,
        key="mutation-other-owner-key-0004",
    )
    assert other["claim_status"] == "claimed"

    before_settlement = store.session_mutation_quiescence(
        OWNER, SESSION, mutation["mutation_token"]
    )
    assert before_settlement["quiescent"] is False
    assert before_settlement["pending_request_ids"] == [running["request_id"]]
    assert before_settlement["pending_execution_states"] == ["running"]

    # A late normal completion is coercively terminal-cancelled after the
    # durable intent, then the clear mutation can reopen admission.
    store.finish_execution(
        running["request_id"],
        OWNER,
        SESSION,
        running["lease_token"],
        RUNNER,
        outcome="completed",
        fence_token=running["session_fence_token"],
    )
    settled = store.session_mutation_quiescence(
        OWNER, SESSION, mutation["mutation_token"]
    )
    assert settled["quiescent"] is True
    assert store.advance_session_context_generation(
        OWNER, SESSION, mutation["mutation_token"]
    ) == 1
    store.release_session_mutation(
        OWNER,
        SESSION,
        mutation["mutation_token"],
        mutation_kind="clear_context",
    )
    reopened = _claim(
        store,
        "mutation-after-release",
        key="mutation-after-release-key-0005",
    )
    assert reopened["claim_status"] == "claimed"


def test_session_mutation_supersedes_completed_delivery_gap_and_rejects_late_done(tmp_path):
    """A clear/delete cannot be followed by old success transport delivery."""

    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(
        store,
        "mutation-completed-undelivered",
        key="mutation-completed-undelivered-key-0001",
    )
    store.finish_execution(
        claim["request_id"],
        OWNER,
        SESSION,
        claim["lease_token"],
        RUNNER,
        outcome="completed",
        fence_token=claim["session_fence_token"],
    )
    before = store.replay(claim["request_id"], OWNER)
    assert before is not None
    assert before["execution_state"] == "completed"
    assert before["state"] == "running"
    assert before["events"] == []

    mutation = store.begin_session_mutation(
        OWNER,
        SESSION,
        mutation_kind="clear_context",
        detail="clear during completed-to-SSE handoff",
    )
    assert mutation["superseded_terminal_deliveries"] == 1
    after = store.replay(claim["request_id"], OWNER)
    assert after is not None
    assert after["execution_state"] == "completed"
    assert after["state"] == "completed"
    assert after["delivery_status"] == "mutation_pending"
    assert after["events"] == []
    connection = sqlite3.connect(store.path)
    try:
        raw_error = connection.execute(
            "SELECT payload_json FROM web_sse_events WHERE request_id = ?",
            (claim["request_id"],),
        ).fetchone()
    finally:
        connection.close()
    assert raw_error is not None
    raw_payload = json.loads(raw_error[0])
    assert raw_payload["type"] == "error"
    assert "superseded by clear_context" in raw_payload["content"]

    with pytest.raises(RuntimeError, match="mutation_pending"):
        _append_for_claim(
            store,
            claim,
            1,
            {"type": "done", "content": "OLD_SUCCESS_AFTER_CLEAR"},
        )
    quiescence = store.session_mutation_quiescence(
        OWNER, SESSION, mutation["mutation_token"]
    )
    assert quiescence["quiescent"] is True


def test_clear_context_generation_revokes_old_replay_and_late_sse_append(tmp_path):
    """Old request evidence cannot cross a clear-context generation boundary."""

    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(
        store,
        "clear-generation-old-request",
        key="clear-generation-old-key-0001",
    )
    store.finish_execution(
        claim["request_id"],
        OWNER,
        SESSION,
        claim["lease_token"],
        RUNNER,
        outcome="completed",
        fence_token=claim["session_fence_token"],
    )
    _append_for_claim(
        store,
        claim,
        1,
        {"type": "done", "content": "old context success"},
    )
    before = store.replay(claim["request_id"], OWNER)
    assert before is not None
    assert before["delivery_status"] == "current"
    assert [payload["type"] for _event_id, payload in before["events"]] == ["done"]

    mutation = store.begin_session_mutation(
        OWNER,
        SESSION,
        mutation_kind="clear_context",
        detail="clear old completed response",
    )
    pending = store.replay(claim["request_id"], OWNER)
    assert pending is not None
    assert pending["delivery_status"] == "mutation_pending"
    assert pending["mutation_pending"] is True
    assert pending["events"] == []
    assert store.events_after(claim["request_id"], OWNER, 0) == []
    with pytest.raises(RuntimeError, match="mutation_pending"):
        _append_for_claim(
            store,
            claim,
            2,
            {"type": "voice_attach", "content": "must not cross mutation"},
        )

    generation = store.advance_session_context_generation(
        OWNER, SESSION, mutation["mutation_token"]
    )
    assert generation == 1
    store.release_session_mutation(
        OWNER,
        SESSION,
        mutation["mutation_token"],
        mutation_kind="clear_context",
    )

    stale = store.replay(claim["request_id"], OWNER)
    assert stale is not None
    assert stale["delivery_status"] == "stale_context"
    assert stale["stale_context"] is True
    assert stale["events"] == []
    assert store.events_after(claim["request_id"], OWNER, 0) == []
    with pytest.raises(RuntimeError, match="stale_context"):
        _append_for_claim(
            store,
            claim,
            2,
            {"type": "voice_attach", "content": "must not cross clear"},
        )

    retry = _claim(
        store,
        claim["request_id"],
        key="clear-generation-old-key-0001",
    )
    assert retry["claim_status"] == "stale_context"
    assert retry["stale_context"] is True
    assert retry["delivery_status"] == "stale_context"


def test_session_mutation_expired_holder_remains_in_doubt_and_blocks_quiescence(tmp_path):
    """Lease loss revokes write authority; it cannot prove a remote effect stopped."""

    store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    claim = _claim(
        store,
        "mutation-expired-holder",
        key="mutation-expired-holder-key-0001",
    )
    mutation = store.begin_session_mutation(
        OWNER,
        SESSION,
        mutation_kind="clear_context",
        detail="clear after a crashed worker",
    )
    status = store.session_mutation_quiescence(
        OWNER,
        SESSION,
        mutation["mutation_token"],
        now=claim["lease_expires_at"] + 0.001,
    )
    assert status["quiescent"] is False
    assert status["expired_execution_fences"] == 1
    assert status["pending_request_ids"] == [claim["request_id"]]
    assert status["pending_execution_states"] == ["in_doubt"]
    replay = store.replay(claim["request_id"], OWNER)
    assert replay is not None
    assert replay["execution_state"] == "in_doubt"
    assert replay["cancel_requested"] is True

    with pytest.raises(RuntimeError, match="cannot reopen"):
        store.release_session_mutation(
            OWNER,
            SESSION,
            mutation["mutation_token"],
            mutation_kind="clear_context",
        )
    with pytest.raises(RuntimeError, match="stale or missing"):
        store.finish_execution(
            claim["request_id"],
            OWNER,
            SESSION,
            claim["lease_token"],
            RUNNER,
            outcome="cancelled",
            fence_token=claim["session_fence_token"],
        )


def test_session_mutation_and_claim_race_has_no_post_closure_admission(tmp_path):
    """BEGIN IMMEDIATE serializes the only two safe race outcomes."""

    path = str(tmp_path / "web.sqlite3")
    barrier = threading.Barrier(2)

    def begin_mutation():
        store = DurableSSEJournalStore(path)
        barrier.wait(timeout=5)
        return (
            "mutation",
            store.begin_session_mutation(
                OWNER,
                SESSION,
                mutation_kind="clear_context",
                detail="race close",
            ),
        )

    def attempt_claim():
        store = DurableSSEJournalStore(path)
        barrier.wait(timeout=5)
        try:
            return (
                "claim",
                _claim(
                    store,
                    "mutation-race-claim",
                    key="mutation-race-claim-key-0001",
                ),
            )
        except Exception as exc:  # asserted below
            return ("claim_error", f"{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda fn: fn(), (begin_mutation, attempt_claim)))

    mutation = next(value for kind, value in outcomes if kind == "mutation")
    claim_outcome = next((item for item in outcomes if item[0] != "mutation"), None)
    assert claim_outcome is not None
    if claim_outcome[0] == "claim_error":
        assert "destructive mutation" in claim_outcome[1]
        assert DurableSSEJournalStore(path).replay("mutation-race-claim", OWNER) is None
    else:
        claim = claim_outcome[1]
        assert claim["claim_status"] == "claimed"
        replay = DurableSSEJournalStore(path).replay(claim["request_id"], OWNER)
        assert replay is not None
        assert replay["execution_state"] == "running"
        # If claim won the transaction, mutation ran afterwards and atomically
        # marked it for cancellation. It therefore cannot become an unnoticed
        # live writer while the destructive operation waits.
        assert replay["cancel_requested"] is True

    quiescence = DurableSSEJournalStore(path).session_mutation_quiescence(
        OWNER, SESSION, mutation["mutation_token"]
    )
    if quiescence["pending_request_ids"]:
        assert quiescence["pending_request_ids"] == ["mutation-race-claim"]
        assert quiescence["pending_execution_states"] == ["running"]
