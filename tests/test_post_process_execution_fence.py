from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest

from agent.protocol.agent import Agent
from agent.protocol.cancel import AgentCancelledError
from agent.tools.base_tool import BaseTool, ToolResult, ToolStage


class _PostProcessTool(BaseTool):
    stage = ToolStage.POST_PROCESS

    def __init__(self, name: str, calls: list[str]):
        self.name = name
        self.description = name
        self.params = {"type": "object", "properties": {}}
        self.calls = calls

    def execute(self, _params):
        self.calls.append(self.name)
        return ToolResult.success({"tool": self.name})


def _agent_with_tools(*tools: BaseTool) -> Agent:
    return Agent(
        "post-process guard test",
        tools=list(tools),
        enable_skills=False,
        output_mode="logger",
    )


def test_post_process_tools_use_guard_and_cancel_after_effect_is_not_recorded_success():
    calls: list[str] = []
    first = _PostProcessTool("first", calls)
    second = _PostProcessTool("second", calls)
    agent = _agent_with_tools(first, second)
    cancel_event = threading.Event()
    guard_enters: list[str] = []

    @contextmanager
    def guard():
        guard_enters.append("enter")
        yield
        # Simulate a durable guard observing cancellation at its exact after-tool
        # checkpoint. The first effect may already have happened, but it must not
        # be captured as a normal success or allow another automatic tool.
        cancel_event.set()
        raise AgentCancelledError("cancelled after post-process side effect")

    with pytest.raises(AgentCancelledError, match="post-process side effect"):
        agent._execute_post_process_tools(
            tool_execution_guard=guard,
            cancel_event=cancel_event,
        )

    assert calls == ["first"]
    assert guard_enters == ["enter"]
    assert agent.captured_actions == []
    assert first.cancel_event is None
    assert second.cancel_event is None


def test_post_process_tools_check_cancel_before_entering_tool_code():
    calls: list[str] = []
    tool = _PostProcessTool("only", calls)
    agent = _agent_with_tools(tool)
    cancel_event = threading.Event()
    cancel_event.set()
    guard_called = False

    @contextmanager
    def guard():
        nonlocal guard_called
        guard_called = True
        yield

    with pytest.raises(AgentCancelledError, match="before post-process"):
        agent._execute_post_process_tools(
            tool_execution_guard=guard,
            cancel_event=cancel_event,
        )

    assert calls == []
    assert guard_called is False
    assert tool.cancel_event is None


def test_post_process_tools_restore_request_event_and_capture_only_guarded_normal_result():
    calls: list[str] = []
    tool = _PostProcessTool("only", calls)
    agent = _agent_with_tools(tool)
    prior_event = threading.Event()
    tool.cancel_event = prior_event
    guard_count = 0

    @contextmanager
    def guard():
        nonlocal guard_count
        guard_count += 1
        yield

    agent._execute_post_process_tools(tool_execution_guard=guard, cancel_event=threading.Event())

    assert calls == ["only"]
    assert guard_count == 1
    assert tool.cancel_event is prior_event
    assert len(agent.captured_actions) == 1
    assert agent.captured_actions[0].tool_result.status == "success"
