from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch


def _bridge_with_fence(value: bool):
    agent_bridge = Mock()
    agent_bridge.cancel_and_wait_for_session.return_value = value
    return SimpleNamespace(get_agent_bridge=lambda: agent_bridge), agent_bridge


def test_clear_context_does_not_mutate_when_run_cancellation_is_pending(monkeypatch):
    from channel.web import web_channel

    bridge, agent_bridge = _bridge_with_fence(False)
    store = Mock()
    monkeypatch.setattr(web_channel, "_require_auth", lambda: "web:" + "a" * 32)
    monkeypatch.setattr(web_channel.web, "header", lambda *_args, **_kwargs: None)
    with patch("bridge.bridge.Bridge", return_value=bridge), patch(
        "agent.memory.get_conversation_store", return_value=store
    ):
        result = json.loads(web_channel.SessionClearContextHandler().POST("session"))

    assert result["status"] == "error"
    assert "not cleared" in result["message"]
    agent_bridge.cancel_and_wait_for_session.assert_called_once_with(
        "session", "web:" + "a" * 32
    )
    store.clear_context.assert_not_called()


def test_delete_does_not_tombstone_when_run_cancellation_is_pending(monkeypatch):
    from channel.web import web_channel

    owner = "web:" + "a" * 32
    bridge, agent_bridge = _bridge_with_fence(False)
    store = Mock()
    monkeypatch.setattr(web_channel, "_require_auth", lambda: owner)
    monkeypatch.setattr(web_channel, "_require_web_session", lambda *_args: None)
    monkeypatch.setattr(web_channel.web, "header", lambda *_args, **_kwargs: None)
    with patch("bridge.bridge.Bridge", return_value=bridge), patch(
        "agent.memory.get_conversation_store", return_value=store
    ):
        result = json.loads(web_channel.SessionDetailHandler().DELETE("session"))

    assert result["status"] == "error"
    assert "not applied" in result["message"]
    agent_bridge.cancel_and_wait_for_session.assert_called_once_with("session", owner)
    store.delete_session.assert_not_called()
