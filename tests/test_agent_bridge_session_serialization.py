from __future__ import annotations

import threading
import time

from bridge.agent_bridge import AgentBridge
from bridge.context import Context
from agent.protocol.cancel import get_cancel_registry


class _BlockingAgent:
    def __init__(self):
        self.tools = []
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self._last_run_new_messages = []

    def run_stream(self, **_kwargs):
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=5)
        return "done"


def _bridge_for_test(agent: _BlockingAgent) -> AgentBridge:
    bridge = object.__new__(AgentBridge)
    bridge._session_run_locks = {}
    bridge._session_run_locks_guard = threading.Lock()
    bridge.get_agent = lambda **_kwargs: agent
    bridge._pre_persist_user_message = lambda *_args, **_kwargs: False
    bridge._schedule_mcp_hot_reload = lambda *_args, **_kwargs: None
    return bridge


def test_same_session_turns_are_serialized_and_lock_entries_are_reclaimed():
    agent = _BlockingAgent()
    bridge = _bridge_for_test(agent)
    context = Context(kwargs={"session_id": "shared-session"})
    replies = []

    first = threading.Thread(
        target=lambda: replies.append(bridge.agent_reply("one", context=context))
    )
    second = threading.Thread(
        target=lambda: replies.append(bridge.agent_reply("two", context=context))
    )
    first.start()
    assert agent.started.wait(timeout=2)
    second.start()
    deadline = time.time() + 2
    registry = get_cancel_registry()
    while time.time() < deadline:
        with registry._lock:
            queued_count = len(registry._by_session.get("shared-session", ()))
        if queued_count == 2:
            break
        time.sleep(0.01)
    assert agent.calls == 1
    assert queued_count == 2

    agent.release.set()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert agent.calls == 2
    assert len(replies) == 2
    assert bridge._session_run_locks == {}
    assert registry.has_active("shared-session") is False


def test_destructive_session_mutation_waits_for_cancellation_fence():
    agent = _BlockingAgent()
    bridge = _bridge_for_test(agent)
    context = Context(kwargs={"session_id": "mutating-session"})
    worker = threading.Thread(
        target=lambda: bridge.agent_reply("one", context=context)
    )
    worker.start()
    assert agent.started.wait(timeout=2)

    assert bridge.cancel_and_wait_for_session("mutating-session", timeout=0.01) is False
    agent.release.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert bridge.cancel_and_wait_for_session("mutating-session", timeout=1) is True
