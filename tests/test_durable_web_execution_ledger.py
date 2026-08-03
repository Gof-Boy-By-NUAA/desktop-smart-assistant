"""Adversarial tests for durable authenticated Web Agent execution claims."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.protocol.cancel import get_cancel_registry
from bridge.agent_bridge import AgentBridge
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
) -> dict:
    return store.claim_execution(
        request_id,
        owner,
        session,
        key,
        digest,
        runner,
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
        results.put(
            "acquired" if claim["claim_status"] == "claimed" else "busy"
        )
    except Exception as exc:  # pragma: no cover - asserted by parent
        results.put(f"error:{type(exc).__name__}:{exc}")


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
    # A different idempotency key cannot reserve a second mutable turn.  It
    # receives no request lease and therefore cannot start a worker at all.
    assert second == {
        "claim_status": "session_busy",
        "owner_id": OWNER,
        "session_id": SESSION,
        "execution_state": "session_busy",
    }

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

    # Terminal settlement and fence release are one transaction.  A later
    # distinct request can now obtain the session.
    store.finish_execution(
        first["request_id"],
        OWNER,
        SESSION,
        first["lease_token"],
        RUNNER,
        outcome="completed",
    )
    third = _claim(
        store,
        "session-fence-third",
        key="web-session-fence-key-0003",
    )
    assert third["claim_status"] == "claimed"
    assert third["session_fence_token"]


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
        "acquired",
        "busy",
    ]


def test_crash_recovery_keeps_session_fenced_instead_of_reassigning_it(tmp_path):
    path = str(tmp_path / "web.sqlite3")
    first_store = DurableSSEJournalStore(path)
    first = _claim(
        first_store,
        "session-fence-crash-first",
        key="web-session-crash-key-0001",
    )
    assert first["claim_status"] == "claimed"

    # A fresh process must not infer that it is safe to steal an unfinished
    # session merely because the predecessor is no longer reachable.
    recovered_store = DurableSSEJournalStore(path)
    recovered_store.mark_interrupted_execution(first["request_id"], OWNER)
    assert (
        recovered_store.replay(first["request_id"], OWNER)["execution_state"]
        == "in_doubt"
    )
    second = _claim(
        recovered_store,
        "session-fence-crash-second",
        key="web-session-crash-key-0002",
    )
    assert second == {
        "claim_status": "session_busy",
        "owner_id": OWNER,
        "session_id": SESSION,
        "execution_state": "session_busy",
    }


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
        )
    assert store.replay(claim["request_id"], OWNER)["execution_state"] == "running"

    store.finish_execution(
        claim["request_id"],
        OWNER,
        SESSION,
        claim["lease_token"],
        RUNNER,
        outcome="completed",
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
        store.append(
            claim["request_id"], 1, {"type": "done", "content": "false success"}
        )

    store.finish_execution(
        claim["request_id"],
        OWNER,
        SESSION,
        claim["lease_token"],
        RUNNER,
        outcome="completed",
    )
    store.append(
        claim["request_id"], 1, {"type": "done", "content": "durably complete"}
    )
    replay = store.replay(claim["request_id"], OWNER)
    assert replay["execution_state"] == "completed"
    assert replay["events"] == [
        (1, {"type": "done", "content": "durably complete"})
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
    instance._drop_sse_request = lambda request_id: WebChannel._drop_sse_request(
        instance, request_id
    )
    return instance


class _NoStartThread:
    started: list[tuple] = []

    def __init__(self, *, target, args=(), **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.started.append((self.target, self.args))


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


def test_authenticated_post_rejects_distinct_key_while_session_turn_is_reserved(
    monkeypatch, tmp_path
):
    """The HTTP response must not claim a worker started when the session is busy."""

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
    monkeypatch.setattr(
        web_channel, "conf", lambda: {"single_chat_prefix": [""]}
    )
    monkeypatch.setattr(
        web_channel.web,
        "data",
        lambda: json.dumps(current_payload).encode(),
    )
    monkeypatch.setattr(web_channel.threading, "Thread", _NoStartThread)

    accepted = json.loads(WebChannel.post_message(instance, owner_id=OWNER))
    assert accepted["status"] == "success"
    assert len(_NoStartThread.started) == 1
    assert store.replay(accepted["request_id"], OWNER)["execution_state"] == "running"

    current_payload = {
        "session_id": SESSION,
        "message": "second external action",
        "stream": True,
        "idempotency_key": "web-session-reservation-key-0002",
    }
    rejected = json.loads(WebChannel.post_message(instance, owner_id=OWNER))

    assert rejected == {
        "status": "error",
        "stream": False,
        "execution_state": "session_busy",
        "message": (
            "Session is already processing another request; retry after it finishes"
        ),
    }
    assert len(_NoStartThread.started) == 1


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
    store.mark_interrupted_execution(first["request_id"], OWNER)
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
        lambda event_id, payload: store.append(claim["request_id"], event_id, payload)
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
    )
    instance = _web_instance()
    instance.request_to_session[claim["request_id"]] = SESSION
    instance.request_owners[claim["request_id"]] = OWNER
    journal = web_channel._SSEEventJournal(
        lambda event_id, payload: store.append(claim["request_id"], event_id, payload)
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
