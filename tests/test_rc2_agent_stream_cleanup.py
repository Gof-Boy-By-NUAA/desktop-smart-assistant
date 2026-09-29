"""R-002: real loopback HTTP and locked zai SDK stream cleanup, no vendor call."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from zai import ZhipuAiClient
from zai.core import APIReachLimitError

from agent.protocol.agent_stream import AgentStreamExecutor
from agent.protocol.cancel import AgentCancelledError
from agent.protocol.models import LLMRequest
from bridge.agent_bridge import AgentLLMModel
from models.zhipuai.zhipuai_bot import ZHIPUAIBot


@pytest.fixture
def real_stream(monkeypatch):
    release_tail = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for content in ("first", "second"):
                    payload = {"id": "rc2-stream", "object": "chat.completion.chunk", "created": 1,
                               "model": "glm-4", "choices": [{"index": 0, "delta": {"content": content},
                                                                "finish_reason": None}]}
                    self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                    self.wfile.flush()
                    if content == "first":
                        assert release_tail.wait(5)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except ConnectionError:
                # Expected when the owned client closes a cancelled response.
                return

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    client = ZhipuAiClient(api_key="loopback-rc2-placeholder", base_url=f"http://127.0.0.1:{server.server_port}/v4/",
                           max_retries=0, timeout=5)
    bot = object.__new__(ZHIPUAIBot)
    bot.args = {"model": "glm-4"}
    bot.client = client
    responses = []
    create = client.chat.completions.create

    def record_real_response(**kwargs):
        result = create(**kwargs)
        responses.append(result.response)
        return result

    # Recording spy delegates to the real SDK and real socket unchanged.
    monkeypatch.setattr(client.chat.completions, "create", record_real_response)
    import config
    import agent.tools
    monkeypatch.setattr(config, "conf", lambda: {"model": "glm-4", "bot_type": "zhipu"})
    # MCP discovery is not part of this stream/resource test.
    monkeypatch.setattr(agent.tools, "ToolManager", lambda: SimpleNamespace(sync_mcp_into_agent=lambda _agent: None))
    from bridge import agent_bridge
    monkeypatch.setattr(agent_bridge, "conf", config.conf)
    model = object.__new__(AgentLLMModel)
    model._bot = bot
    model._bot_model = "glm-4"
    model._bot_type = "zhipu"
    try:
        yield model, responses, release_tail
    finally:
        release_tail.set()
        client.close()
        server.shutdown()
        server.server_close()
        server_thread.join(5)


def test_rc2_closing_model_generator_closes_underlying_real_sdk_response(real_stream):
    model, responses, release_tail = real_stream
    stream = model.call_stream(LLMRequest(messages=[{"role": "user", "content": "hello"}], stream=True))
    try:
        assert next(stream)["choices"][0]["delta"]["content"] == "first"
        assert responses and not responses[0].is_closed
        stream.close()
        assert responses[0].is_closed
    finally:
        release_tail.set()
        stream.close()


def test_rc2_cancel_is_checked_on_next_yield_and_closes_sdk_response(real_stream):
    model, responses, release_tail = real_stream
    cancel = threading.Event()

    def on_event(event):
        if event.get("type") == "message_update":
            cancel.set()
            release_tail.set()

    executor = AgentStreamExecutor(None, model, "", [], messages=[{"role": "user", "content": "hello"}],
                                   cancel_event=cancel, on_event=on_event)
    with pytest.raises(AgentCancelledError):
        executor._call_llm_stream(retry_on_empty=False, max_retries=0)
    assert responses[0].is_closed


def test_rc2_cancel_interrupts_real_sdk_before_first_response_event(monkeypatch):
    """A real loopback SDK request must unwind while waiting for response headers."""
    request_seen = threading.Event()
    release_server = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request_seen.set()
            release_server.wait(10)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    client = ZhipuAiClient(
        api_key="loopback-rc2-placeholder",
        base_url=f"http://127.0.0.1:{server.server_port}/v4/",
        max_retries=0,
        timeout=10,
    )
    bot = object.__new__(ZHIPUAIBot)
    bot.args = {"model": "glm-4"}
    bot.client = client

    import agent.tools
    import config
    from bridge import agent_bridge

    monkeypatch.setattr(config, "conf", lambda: {"model": "glm-4", "bot_type": "zhipu"})
    monkeypatch.setattr(agent_bridge, "conf", config.conf)
    monkeypatch.setattr(
        agent.tools,
        "ToolManager",
        lambda: SimpleNamespace(sync_mcp_into_agent=lambda _agent: None),
    )
    model = object.__new__(AgentLLMModel)
    model._bot = bot
    model._bot_model = "glm-4"
    model._bot_type = "zhipu"
    cancel = threading.Event()
    executor = AgentStreamExecutor(
        None,
        model,
        "",
        [],
        messages=[{"role": "user", "content": "hello"}],
        cancel_event=cancel,
    )
    errors = []

    def run():
        try:
            executor._call_llm_stream(retry_on_empty=False, max_retries=0)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert request_seen.wait(5)
        cancel.set()
        worker.join(2)
        assert not worker.is_alive(), "cancel must close the blocking SDK transport"
        assert len(errors) == 1 and isinstance(errors[0], AgentCancelledError)
    finally:
        cancel.set()
        release_server.set()
        worker.join(10)
        client.close()
        server.shutdown()
        server.server_close()
        server_thread.join(5)


def test_rc2_cancel_closes_the_active_sdk_response_before_the_shared_client():
    """Cancel an established stream even when closing its shared client cannot unblock it."""

    cancel = threading.Event()
    iteration_started = threading.Event()
    response_closed = threading.Event()

    class BlockingResponse:
        def close(self):
            response_closed.set()

    class BlockingStream:
        response = BlockingResponse()

        def __iter__(self):
            iteration_started.set()
            if not response_closed.wait(5):
                raise TimeoutError("active response was not closed")
            raise OSError("response closed")

    class Completions:
        def create(self, **_kwargs):
            return BlockingStream()

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=Completions())
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    client = Client()
    bot = object.__new__(ZHIPUAIBot)
    bot.client = client
    worker = threading.Thread(
        target=lambda: list(
            bot._handle_stream_response(
                {"model": "glm-4", "messages": [], "stream": True},
                cancel_event=cancel,
            )
        )
    )
    worker.start()
    try:
        assert iteration_started.wait(2)
        cancel.set()
        worker.join(1)
        assert not worker.is_alive(), "cancel must close the active SDK response"
        assert response_closed.is_set()
        assert client.close_calls == 0
    finally:
        cancel.set()
        response_closed.set()
        worker.join(5)


def test_rc2_zhipu_stream_has_one_cancellation_aware_retry_owner(monkeypatch):
    """Only streams disable SDK retries; sync features keep the SDK default."""

    captured = {}

    def build_client(**kwargs):
        captured.update(kwargs)
        return object()

    import models.zhipuai.zhipuai_bot as zhipu_module

    monkeypatch.setattr(zhipu_module, "ZhipuAiClient", build_client)
    monkeypatch.setattr(
        zhipu_module,
        "conf",
        lambda: {
            "zhipu_ai_api_key": "rc2-placeholder",
            "zhipu_ai_api_base": "https://example.invalid/v4",
        },
    )

    assert ZHIPUAIBot._build_client(max_retries=0) is not None
    assert captured["max_retries"] == 0

    captured.clear()
    assert ZHIPUAIBot._build_client() is not None
    assert "max_retries" not in captured


def test_rc2_desktop_scrubs_internal_cancel_marker_on_terminal_and_history():
    """The private Agent marker must never become the user-visible cancel result."""

    source = (
        Path(__file__).resolve().parents[1]
        / "desktop"
        / "src"
        / "renderer"
        / "src"
        / "store"
        / "chatStore.ts"
    ).read_text(encoding="utf-8")
    cancelled_case = source.split("case 'cancelled':", 1)[1].split("case 'done':", 1)[0]
    history_mapper = source.split("function historyToMessage", 1)[1].split(
        "export const useChatStore", 1
    )[0]

    assert "content: stripCancelMarker(m.content)" in cancelled_case
    assert "content: stripCancelMarker(finalContent)" in history_mapper
    assert "isCancelled: finalContent !== stripCancelMarker(finalContent)" in history_mapper


def test_zhipu_status_error_metadata_is_preserved_without_agent_retry(monkeypatch):
    """Injected SDK status failure proves local propagation; no vendor request is made."""
    calls = []
    captured = []
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://example.invalid/chat/completions"),
        json={"error": {"code": "test-limit-code", "message": "test provider limit"}},
    )

    class Completions:
        def create(self, **_kwargs):
            calls.append(True)
            raise APIReachLimitError("wrapped provider failure", response=response)

    bot = object.__new__(ZHIPUAIBot)
    bot.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    def call_stream(_request):
        for chunk in bot._handle_stream_response({"model": "glm-4", "messages": [], "stream": True}):
            captured.append(chunk)
            yield chunk

    import agent.tools

    monkeypatch.setattr(
        agent.tools,
        "ToolManager",
        lambda: SimpleNamespace(sync_mcp_into_agent=lambda _agent: None),
    )
    executor = AgentStreamExecutor(
        None,
        SimpleNamespace(call_stream=call_stream),
        "",
        [],
        messages=[{"role": "user", "content": "hello"}],
    )
    with pytest.raises(RuntimeError, match="test provider limit"):
        executor._call_llm_stream(retry_on_empty=False)

    assert calls == [True]
    assert captured == [{
        "error": True,
        "message": "test provider limit",
        "status_code": 429,
        "error_code": "test-limit-code",
        "error_type": "APIReachLimitError",
        "retryable": False,
    }]


def test_rc2_cancel_interrupts_retry_wait_and_does_not_call_provider_again(monkeypatch):
    wait_entered = threading.Event()
    calls = []
    closed = []
    errors = []

    class ObservedEvent(threading.Event):
        def wait(self, timeout=None):
            assert closed == [True], "response must be released before retry waiting"
            wait_entered.set()
            return super().wait(timeout)

    class FailedStream:
        def __iter__(self):
            yield {"choices": [{"delta": {"content": "first"}}]}
            raise RuntimeError("connection timeout")

        def close(self):
            closed.append(True)

    def call_stream(_request):
        calls.append(True)
        return FailedStream()

    cancel = ObservedEvent()
    executor = AgentStreamExecutor(None, SimpleNamespace(call_stream=call_stream), "", [],
                                   messages=[{"role": "user", "content": "hello"}], cancel_event=cancel)

    def run():
        try:
            executor._call_llm_stream(retry_on_empty=False)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert wait_entered.wait(5)
        cancel.set()
        worker.join(5)
    finally:
        cancel.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], AgentCancelledError)
    assert calls == [True]
    assert closed == [True]
    assert executor.messages[-1] == {"role": "assistant", "content": [{"type": "text", "text": "first"}]}


@pytest.mark.parametrize("ending", ["eof", "read_error"])
def test_rc2_cancel_preserves_partial_text_and_balanced_end_at_stream_boundary(ending):
    """Injected EOF/read errors exercise the executor's cancellation wind-down."""
    cancel = threading.Event()
    closed = threading.Event()
    events = []

    def call_stream(_request):
        try:
            yield {"choices": [{"delta": {"content": "first", "tool_calls": [{
                "index": 0, "id": "partial-tool", "function": {"name": "unused", "arguments": '{"incomplete":'}
            }]}}]}
            if ending == "read_error":
                raise RuntimeError("connection read failed")
        finally:
            closed.set()

    def on_event(event):
        events.append(event)
        if event.get("type") == "message_update":
            cancel.set()

    executor = AgentStreamExecutor(None, SimpleNamespace(call_stream=call_stream), "", [],
                                   messages=[{"role": "user", "content": "hello"}],
                                   cancel_event=cancel, on_event=on_event)
    with pytest.raises(AgentCancelledError):
        executor._call_llm_stream(retry_on_empty=False, max_retries=0)
    assert closed.is_set()
    assert executor.messages[-1] == {"role": "assistant", "content": [{"type": "text", "text": "first"}]}
    assert sum(event["type"] == "message_start" for event in events) == 1
    endings = [event for event in events if event["type"] == "message_end"]
    assert len(endings) == 1
    assert endings[0]["data"]["cancelled"] is True
    assert endings[0]["data"]["tool_calls"] == []


def test_retry_handoff_cancel_preserves_partial_text_and_closes_message(monkeypatch):
    """故障注入交接窗口；观察真实回调直到异常或正常结束，覆盖外层取消传播。"""
    import os
    from pathlib import Path
    from queue import Queue

    import agent.tools
    import config
    from agent.protocol.agent import Agent

    visible_text = "attempt-one-visible"
    delivery = Queue()
    provider_calls = []
    retry_entries = []
    retry_delays = []
    stream_closed = threading.Event()
    history_at_end = []
    actual_errors = []

    class HandoffEvent(threading.Event):
        def wait(self, timeout=None):
            # 延迟时长只记录；取消在其后的递归入口精确激活。
            retry_delays.append(timeout)
            return self.is_set()

    cancel = HandoffEvent()

    def call_stream(_request):
        provider_calls.append(True)

        def provider_stream():
            try:
                yield {"choices": [{"delta": {
                    "content": visible_text,
                    "tool_calls": [{
                        "index": 0, "id": "handoff-partial-tool",
                        "function": {"name": "unused", "arguments": '{"incomplete":'},
                    }],
                }}]}
                raise RuntimeError("connection timeout")
            finally:
                stream_closed.set()

        return provider_stream()

    def on_event(event):
        if event["type"] == "message_end":
            history_at_end.append(json.loads(json.dumps(executor.messages)))
        delivery.put(("event", event))

    # 外部模型与 MCP/config 是非目标依赖；不替代执行器的取消、历史或事件逻辑。
    monkeypatch.setattr(config, "conf", lambda: {"enable_thinking": False})
    monkeypatch.setattr(agent.tools, "ToolManager", lambda: SimpleNamespace(
        sync_mcp_into_agent=lambda _agent: None,
    ))
    model = SimpleNamespace(model="fault-injected", call_stream=call_stream)
    owner = Agent("", model=model, tools=[], max_steps=1, enable_skills=False)
    executor = AgentStreamExecutor(
        owner, model, "", [],
        cancel_event=cancel, on_event=on_event,
    )
    original_call = executor._call_llm_stream

    def enter_attempt(*args, **kwargs):
        retry_count = kwargs.get("retry_count", 0)
        retry_entries.append(retry_count)
        if retry_count == 1:
            # Attempt 1 的最后一次取消检查已返回，Attempt 2 的真实入口尚未执行。
            cancel.set()
        return original_call(*args, **kwargs)

    monkeypatch.setattr(executor, "_call_llm_stream", enter_attempt)

    def run():
        try:
            result = executor.run_stream("hello")
        except BaseException as exc:
            actual_errors.append(exc)
            delivery.put(("exception", exc))
        else:
            delivery.put(("return", result))

    def observe_stream():
        # 回调接口的观察迭代器：只转发执行器实际发出的事件和实际抛出的异常。
        while True:
            kind, value = delivery.get(timeout=5)
            if kind == "exception":
                raise value
            if kind == "return":
                return
            yield value

    events = []
    propagated = False
    exhausted_normally = False
    terminal_exception = None
    worker = threading.Thread(target=run)
    worker.start()
    observed = observe_stream()
    try:
        while True:
            try:
                events.append(next(observed))
            except AgentCancelledError as exc:
                propagated = True
                terminal_exception = exc
                break
            except StopIteration:
                exhausted_normally = True
                break
    finally:
        cancel.set()
        worker.join(5)
        observed.close()

    assert not worker.is_alive(), "owned executor thread must terminate"
    starts = [event for event in events if event["type"] == "message_start"]
    endings = [event for event in events if event["type"] == "message_end"]
    assistant_payload = [message["content"] for message in executor.messages
                         if message.get("role") == "assistant"]
    expected_payload = [{"type": "text", "text": visible_text}]
    observation = {
        "provider_call_count": len(provider_calls),
        "retry_entries": retry_entries,
        "retry_delays": retry_delays,
        "message_start_count": len(starts),
        "message_end_count": len(endings),
        "persisted_payload": assistant_payload,
        "history_at_message_end": history_at_end,
        "AgentCancelledError_propagated": propagated,
        "normal_StopIteration_observed": exhausted_normally,
        "stream_closed": stream_closed.is_set(),
        "observed_lifecycle": [event["type"] for event in events] + [
            "AgentCancelledError" if propagated else "StopIteration"
        ],
        "verification_boundary": "actual executor run_stream; conversation DB not exercised",
    }
    Path(os.environ["COW_DATA_DIR"], "retry-handoff-observation.json").write_text(
        json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps(observation, ensure_ascii=False))

    assert len(provider_calls) == 1
    assert retry_entries == [0, 1]
    assert retry_delays == [2]
    assert len(starts) == 1
    assert assistant_payload.count(expected_payload) == 1, "Attempt 1 visible text must persist exactly once"
    assert len(endings) == 1, "Attempt 1 must own exactly one closing message_end"
    assert endings[0]["data"] == {"content": visible_text, "tool_calls": [], "cancelled": True}
    assert history_at_end == [[
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": expected_payload},
    ]], "partial history must exist before message_end, without an empty retry replacement"
    assert all(block["type"] == "text" for payload in assistant_payload for block in payload)
    assert not any(event["type"] in ("tool_start", "tool_end") for event in events)
    assert propagated, "run_stream must propagate AgentCancelledError to its caller"
    assert not exhausted_normally, "normal StopIteration must not replace cancellation"
    assert terminal_exception is actual_errors[0]
    assert events.index(endings[0]) > next(i for i, event in enumerate(events)
                                         if event["type"] == "message_update")
    assert observation["observed_lifecycle"][-1] == "AgentCancelledError"
    assert stream_closed.is_set()
