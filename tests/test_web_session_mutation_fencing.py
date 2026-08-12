from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from channel.web.sse_persistence import DurableSSEJournalStore


OWNER = "web:" + "a" * 32
SESSION = "session-under-durable-mutation"
RUNNER = "web-mutation-test-runner"


def _bridge_with_local_fence(value: bool):
    agent_bridge = Mock()
    agent_bridge.cancel_and_wait_for_session.return_value = value
    return SimpleNamespace(get_agent_bridge=lambda: agent_bridge), agent_bridge


def _claim_running(store: DurableSSEJournalStore, request_id: str = "mutation-running") -> dict:
    return store.claim_execution(
        request_id,
        OWNER,
        SESSION,
        "mutation-handler-key-0001",
        "a" * 64,
        RUNNER,
        {"prompt": "durable mutation test", "is_voice_input": False},
    )


def _claim_new(store: DurableSSEJournalStore, request_id: str, key: str) -> dict:
    return store.claim_execution(
        request_id,
        OWNER,
        SESSION,
        key,
        "b" * 64,
        RUNNER,
        {"prompt": "post mutation request", "is_voice_input": False},
    )


def _configure_handler(monkeypatch, durable_store, conversation_store, bridge):
    from channel.web import web_channel

    monkeypatch.setattr(web_channel, "_require_auth", lambda: OWNER)
    monkeypatch.setattr(web_channel.web, "header", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(web_channel, "_require_web_session", lambda *_args: conversation_store)
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: durable_store)
    # Unit tests must not spend five seconds waiting for a deliberately live
    # cross-process claim. The production default remains fail-closed at 5 s.
    monkeypatch.setattr(web_channel, "_SESSION_MUTATION_WAIT_SECONDS", 0.0)
    return web_channel


def test_clear_context_does_not_trust_local_fence_when_durable_run_is_pending(
    monkeypatch, tmp_path
):
    from channel.web import web_channel

    durable_store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    running = _claim_running(durable_store)
    bridge, agent_bridge = _bridge_with_local_fence(True)
    conversation_store = Mock()
    _configure_handler(monkeypatch, durable_store, conversation_store, bridge)

    with patch("bridge.bridge.Bridge", return_value=bridge):
        result = json.loads(
            web_channel.SessionClearContextHandler().POST(SESSION)
        )

    assert result["status"] == "error"
    assert "not cleared" in result["message"]
    # True only proves this process had no lock holder. The durable running
    # claim remains active in another process and therefore blocks the clear.
    agent_bridge.cancel_and_wait_for_session.assert_called_once_with(
        SESSION, OWNER, timeout=0.0
    )
    conversation_store.clear_context.assert_not_called()
    replay = durable_store.replay(running["request_id"], OWNER)
    assert replay is not None
    assert replay["execution_state"] == "running"
    assert replay["cancel_requested"] is True
    with pytest.raises(RuntimeError, match="destructive mutation"):
        _claim_new(durable_store, "clear-must-not-admit", "clear-must-not-admit-key-0002")


def test_delete_does_not_trust_local_fence_when_durable_run_is_pending(
    monkeypatch, tmp_path
):
    from channel.web import web_channel

    durable_store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    running = _claim_running(durable_store, "delete-running")
    bridge, agent_bridge = _bridge_with_local_fence(True)
    conversation_store = Mock()
    _configure_handler(monkeypatch, durable_store, conversation_store, bridge)

    with patch("bridge.bridge.Bridge", return_value=bridge):
        result = json.loads(web_channel.SessionDetailHandler().DELETE(SESSION))

    assert result["status"] == "error"
    assert "not applied" in result["message"]
    agent_bridge.cancel_and_wait_for_session.assert_called_once_with(
        SESSION, OWNER, timeout=0.0
    )
    conversation_store.delete_session.assert_not_called()
    replay = durable_store.replay(running["request_id"], OWNER)
    assert replay is not None
    assert replay["execution_state"] == "running"
    assert replay["cancel_requested"] is True
    with pytest.raises(RuntimeError, match="destructive mutation"):
        _claim_new(durable_store, "delete-must-not-admit", "delete-must-not-admit-key-0002")


def test_clear_context_keeps_gate_closed_when_local_fence_is_not_quiescent(
    monkeypatch, tmp_path
):
    from channel.web import web_channel

    durable_store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    bridge, agent_bridge = _bridge_with_local_fence(False)
    conversation_store = Mock()
    _configure_handler(monkeypatch, durable_store, conversation_store, bridge)

    with patch("bridge.bridge.Bridge", return_value=bridge):
        result = json.loads(
            web_channel.SessionClearContextHandler().POST(SESSION)
        )

    assert result["status"] == "error"
    assert "not cleared" in result["message"]
    conversation_store.clear_context.assert_not_called()
    agent_bridge.cancel_and_wait_for_session.assert_called_once_with(
        SESSION, OWNER, timeout=0.0
    )
    with pytest.raises(RuntimeError, match="destructive mutation"):
        _claim_new(durable_store, "local-fence-must-close", "local-fence-must-close-key-0003")


def test_clear_context_reopens_admission_only_after_cache_and_store_converge(
    monkeypatch, tmp_path
):
    from channel.web import web_channel

    durable_store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    bridge, agent_bridge = _bridge_with_local_fence(True)
    conversation_store = Mock()
    conversation_store.clear_context.return_value = 17
    _configure_handler(monkeypatch, durable_store, conversation_store, bridge)

    with patch("bridge.bridge.Bridge", return_value=bridge):
        result = json.loads(
            web_channel.SessionClearContextHandler().POST(SESSION)
        )

    assert result == {"status": "success", "context_start_seq": 17}
    conversation_store.clear_context.assert_called_once_with(SESSION, owner_id=OWNER)
    agent_bridge.clear_session.assert_called_once_with(SESSION)
    # Clear advances the durable context generation before it reopens admission.
    next_claim = _claim_new(
        durable_store, "clear-generation-check", "clear-generation-check-key-0007"
    )
    assert next_claim["session_context_generation"] == 1
    durable_store.finish_execution(
        next_claim["request_id"],
        OWNER,
        SESSION,
        next_claim["lease_token"],
        RUNNER,
        outcome="cancelled",
        fence_token=next_claim["session_fence_token"],
    )
    admitted = _claim_new(
        durable_store, "clear-reopened", "clear-reopened-admission-key-0004"
    )
    assert admitted["claim_status"] == "claimed"
    assert admitted["session_context_generation"] == 1


def test_delete_keeps_durable_tombstone_after_store_and_cache_converge(
    monkeypatch, tmp_path
):
    from channel.web import web_channel

    durable_store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    bridge, agent_bridge = _bridge_with_local_fence(True)
    conversation_store = Mock()
    _configure_handler(monkeypatch, durable_store, conversation_store, bridge)

    with patch("bridge.bridge.Bridge", return_value=bridge):
        result = json.loads(web_channel.SessionDetailHandler().DELETE(SESSION))

    assert result == {"status": "success"}
    agent_bridge.clear_session.assert_called_once_with(SESSION)
    conversation_store.delete_session.assert_called_once_with(SESSION, owner_id=OWNER)
    with pytest.raises(RuntimeError, match="destructive mutation"):
        _claim_new(durable_store, "delete-tombstoned", "delete-tombstoned-admission-key-0005")
    mutation = durable_store.begin_session_mutation(
        OWNER,
        SESSION,
        mutation_kind="delete_session",
        detail="retry deleted session",
    )
    with pytest.raises(ValueError, match="only clear_context"):
        durable_store.release_session_mutation(
            OWNER,
            SESSION,
            mutation["mutation_token"],
            mutation_kind="delete_session",
        )


def test_delete_retry_after_response_loss_requires_owner_bound_completion_receipt(
    monkeypatch, tmp_path
):
    """Only a completed owner-bound delete receipt makes retry idempotently successful."""

    from channel.web import web_channel

    durable_store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    bridge, agent_bridge = _bridge_with_local_fence(True)
    conversation_store = Mock()
    _configure_handler(monkeypatch, durable_store, conversation_store, bridge)

    with patch("bridge.bridge.Bridge", return_value=bridge):
        first = json.loads(web_channel.SessionDetailHandler().DELETE(SESSION))

    assert first == {"status": "success"}
    receipt = durable_store.get_delete_session_mutation(OWNER, SESSION)
    assert receipt is not None
    assert receipt["completed_at"] is not None
    assert durable_store.get_delete_session_mutation("web:" + "b" * 32, SESSION) is None

    # Simulate a lost HTTP response followed by client retry: the conversation
    # store now rejects active-session lookup because it is tombstoned, but the
    # same authenticated owner gets the durable completion receipt.
    monkeypatch.setattr(
        web_channel,
        "_require_web_session",
        lambda *_args: (_ for _ in ()).throw(PermissionError("session not found")),
    )
    with patch("bridge.bridge.Bridge", return_value=bridge):
        retry = json.loads(web_channel.SessionDetailHandler().DELETE(SESSION))

    assert retry == {"status": "success", "already_deleted": True}
    conversation_store.delete_session.assert_called_once_with(SESSION, owner_id=OWNER)
    agent_bridge.clear_session.assert_called_once_with(SESSION)


def test_incomplete_delete_mutation_never_returns_already_deleted(monkeypatch, tmp_path):
    """A mutation row without the post-delete receipt remains fail-closed."""

    from channel.web import web_channel

    durable_store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    mutation = durable_store.begin_session_mutation(
        OWNER,
        SESSION,
        mutation_kind="delete_session",
        detail="simulate crash before conversation tombstone",
    )
    bridge, agent_bridge = _bridge_with_local_fence(True)
    conversation_store = Mock()
    conversation_store.delete_session.side_effect = RuntimeError("conversation store unavailable")
    _configure_handler(monkeypatch, durable_store, conversation_store, bridge)
    monkeypatch.setattr(
        web_channel,
        "_require_web_session",
        lambda *_args: (_ for _ in ()).throw(PermissionError("session not found")),
    )

    with patch("bridge.bridge.Bridge", return_value=bridge), patch(
        "agent.memory.get_conversation_store", return_value=conversation_store
    ):
        response = json.loads(web_channel.SessionDetailHandler().DELETE(SESSION))

    assert response == {
        "status": "error",
        "message": "session deletion could not be confirmed; admission remains closed",
    }
    receipt = durable_store.get_delete_session_mutation(OWNER, SESSION)
    assert receipt is not None
    assert receipt["mutation_token"] == mutation["mutation_token"]
    assert receipt["completed_at"] is None
    assert response.get("already_deleted") is None
    agent_bridge.clear_session.assert_called_once_with(SESSION)


def test_clear_authorization_failure_does_not_create_a_durable_closure(monkeypatch, tmp_path):
    from channel.web import web_channel

    durable_store = DurableSSEJournalStore(str(tmp_path / "web.sqlite3"))
    bridge, _agent_bridge = _bridge_with_local_fence(True)
    monkeypatch.setattr(web_channel, "_require_auth", lambda: OWNER)
    monkeypatch.setattr(web_channel.web, "header", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        web_channel,
        "_require_web_session",
        lambda *_args: (_ for _ in ()).throw(PermissionError("session not found")),
    )
    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: durable_store)

    with patch("bridge.bridge.Bridge", return_value=bridge):
        result = json.loads(web_channel.SessionClearContextHandler().POST(SESSION))

    assert result == {"status": "error", "message": "session not found"}
    admitted = _claim_new(
        durable_store, "auth-does-not-close", "auth-does-not-close-admission-key-0006"
    )
    assert admitted["claim_status"] == "claimed"


@pytest.mark.parametrize("operation", ["clear", "delete"])
def test_expired_remote_holder_blocks_destructive_mutation_without_false_quiescence(
    monkeypatch, tmp_path, operation
):
    from channel.web import web_channel

    durable_store = DurableSSEJournalStore(str(tmp_path / f"{operation}.sqlite3"))
    running = _claim_running(durable_store, f"{operation}-expired-holder")
    # Simulate an independently running worker whose heartbeat is lost. The
    # actual process/tool could still be executing, so expiry may remove its
    # journal fence but must never authorize clear/delete.
    connection = sqlite3.connect(durable_store.path)
    try:
        connection.execute(
            "UPDATE web_session_execution_fences SET lease_expires_at = 0 "
            "WHERE request_id = ?",
            (running["request_id"],),
        )
        connection.commit()
    finally:
        connection.close()

    bridge, agent_bridge = _bridge_with_local_fence(True)
    conversation_store = Mock()
    _configure_handler(monkeypatch, durable_store, conversation_store, bridge)
    with patch("bridge.bridge.Bridge", return_value=bridge):
        if operation == "clear":
            result = json.loads(
                web_channel.SessionClearContextHandler().POST(SESSION)
            )
        else:
            result = json.loads(web_channel.SessionDetailHandler().DELETE(SESSION))

    assert result["status"] == "error"
    assert "outcome is unconfirmed" in result["message"]
    agent_bridge.cancel_and_wait_for_session.assert_called_once_with(
        SESSION, OWNER, timeout=0.0
    )
    conversation_store.clear_context.assert_not_called()
    conversation_store.delete_session.assert_not_called()
    replay = durable_store.replay(running["request_id"], OWNER)
    assert replay is not None
    assert replay["execution_state"] == "in_doubt"
