"""Lossless, request-bound SSE reconnect behaviour.

These tests exercise the production journal rather than a mocked happy-path
EventSource: an old generator must not consume a reconnecting client's next
event or reclaim the replacement generator's request state.
"""

import json
import os
import threading
from types import SimpleNamespace

from channel.web import web_channel


WebChannel = dict(zip(
    web_channel.WebChannel.__code__.co_freevars,
    (cell.cell_contents for cell in web_channel.WebChannel.__closure__),
))["cls"]


def _channel(request_id, journal, *, with_generation_fence=False):
    channel = SimpleNamespace(
        sse_queues={request_id: journal},
        sse_last_active={},
        request_to_session={},
        request_owners={},
    )
    if with_generation_fence:
        channel._sse_stream_lock = threading.RLock()
        channel._sse_stream_generations = {}
    channel._drop_sse_request = lambda rid: WebChannel._drop_sse_request(channel, rid)
    return channel


def _event(chunk):
    text = chunk.decode("utf-8")
    event_id = None
    payload = None
    for line in text.splitlines():
        if line.startswith("id: "):
            event_id = int(line[4:])
        if line.startswith("data: "):
            payload = json.loads(line[6:])
    return event_id, payload


def test_reconnect_cursor_replays_only_events_not_acknowledged_by_client():
    request_id = "replay-cursor"
    journal = web_channel._SSEEventJournal()
    journal.put({"type": "delta", "content": "first"})
    journal.put({"type": "tool_end", "tool_call_id": "tool-1", "status": "success"})
    channel = _channel(request_id, journal)

    first = WebChannel.stream_response(channel, request_id, after_event_id=0)
    assert _event(next(first)) == (1, {"type": "delta", "content": "first"})
    first.close()  # network loss before the second event is acknowledged

    resumed = WebChannel.stream_response(channel, request_id, after_event_id=1)
    assert _event(next(resumed)) == (
        2,
        {"type": "tool_end", "tool_call_id": "tool-1", "status": "success"},
    )
    resumed.close()


def test_new_connection_supersedes_stale_generator_without_reclaiming_journal():
    request_id = "replay-handover"
    journal = web_channel._SSEEventJournal()
    journal.put({"type": "delta", "content": "first"})
    journal.put({"type": "delta", "content": "second"})
    channel = _channel(request_id, journal, with_generation_fence=True)

    stale = WebChannel.stream_response(channel, request_id, after_event_id=0)
    assert _event(next(stale))[0] == 1

    replacement = WebChannel.stream_response(channel, request_id, after_event_id=1)
    assert _event(next(replacement)) == (2, {"type": "delta", "content": "second"})

    # The stale WSGI generator notices it was fenced off and must not drop the
    # shared journal that now belongs to the replacement connection.
    try:
        next(stale)
        raise AssertionError("stale generator unexpectedly remained active")
    except StopIteration:
        pass
    assert request_id in channel.sse_queues
    replacement.close()


def test_ticket_cursor_is_request_and_owner_bound_and_consumed_once():
    owner = "web:" + "a" * 32
    request_id = "ticket-cursor"
    ticket = web_channel._issue_stream_ticket(owner, request_id, after_event_id=42)

    record = web_channel._consume_stream_ticket_record(ticket, request_id)
    assert record is not None
    assert record["owner_id"] == owner
    assert record["request_id"] == request_id
    assert record["after_event_id"] == 42
    assert web_channel._consume_stream_ticket_record(ticket, request_id) is None


def test_log_stream_ticket_is_owner_bound_short_lived_and_consumed_once(monkeypatch):
    owner = "web:" + "c" * 32
    ticket = web_channel._issue_log_stream_ticket(owner)
    assert web_channel._consume_log_stream_ticket(ticket) == owner
    assert web_channel._consume_log_stream_ticket(ticket) is None

    issued_at = web_channel.time.time()
    expiring = web_channel._issue_log_stream_ticket(owner)
    monkeypatch.setattr(web_channel.time, "time", lambda: issued_at + 61)
    assert web_channel._consume_log_stream_ticket(expiring) is None


def test_journal_capacity_exhaustion_is_explicit_not_a_silent_drop(monkeypatch):
    monkeypatch.setattr(web_channel, "_SSE_EVENT_JOURNAL_MAX_EVENTS", 1)
    monkeypatch.setattr(web_channel, "_SSE_EVENT_JOURNAL_MAX_BYTES", 1024 * 1024)
    journal = web_channel._SSEEventJournal()
    journal.put({"type": "delta", "content": "kept"})
    journal.put({"type": "delta", "content": "must-not-disappear"})

    assert journal.read_after(0, timeout=0) == (1, {"type": "delta", "content": "kept"})
    sequence, overflow = journal.read_after(1, timeout=0)
    assert sequence == 2
    assert overflow["type"] == "error"
    assert "unconfirmed" in overflow["message"]


def test_file_capability_is_short_lived_and_bound_to_exact_owner_and_path(monkeypatch, tmp_path):
    target = tmp_path / "artifact.txt"
    target.write_text("private artifact", encoding="utf-8")
    owner = "web:" + "b" * 32
    token_url = web_channel._encode_file_capability(str(target), owner)
    assert token_url.startswith("/file/f2.")
    capability = token_url.removeprefix("/file/")

    resolved_path, resolved_owner = web_channel._decode_file_capability(capability)
    assert resolved_path == os.path.realpath(target)
    assert resolved_owner == owner

    tampered = capability[:-1] + ("0" if capability[-1] != "0" else "1")
    try:
        web_channel._decode_file_capability(tampered)
        raise AssertionError("tampered file capability unexpectedly accepted")
    except ValueError:
        pass

    issued_at = web_channel.time.time()
    monkeypatch.setattr(web_channel.time, "time", lambda: issued_at + 601)
    try:
        web_channel._decode_file_capability(capability)
        raise AssertionError("expired file capability unexpectedly accepted")
    except ValueError:
        pass


def test_history_file_capabilities_are_fresh_owner_bound_and_non_mutating(
    monkeypatch, tmp_path
):
    target = tmp_path / "history-artifact.txt"
    target.write_text("history artifact", encoding="utf-8")
    owner = "web:" + "d" * 32
    stored_payload = {
        "type": "file_to_send",
        "path": str(target),
        "url": "/api/file?path=legacy&token=leaked-bearer",
        "file_name": target.name,
    }
    history = {
        "messages": [
            {"role": "user", "content": f"show this\n[Image: {target}]"},
            {
                "role": "assistant",
                "steps": [{"type": "tool", "result": json.dumps(stored_payload)}],
            },
        ]
    }
    monkeypatch.setattr(web_channel, "_is_path_allowed", lambda path: path == str(target))
    monkeypatch.setattr(
        web_channel, "_is_other_owner_upload_path", lambda path, principal: False
    )
    monkeypatch.setattr(web_channel, "_get_preview_secret", lambda: b"history-secret")

    decorated = web_channel._decorate_history_file_capabilities(history, owner)

    # The durable history stays raw; only the authenticated response obtains a
    # fresh ephemeral URL.
    assert "attachment_urls" not in history["messages"][0]
    assert json.loads(history["messages"][1]["steps"][0]["result"]) == stored_payload

    attachment_url = decorated["messages"][0]["attachment_urls"][str(target)]
    payload = json.loads(decorated["messages"][1]["steps"][0]["result"])
    assert attachment_url.startswith("/file/f2.")
    assert payload["url"].startswith("/file/f2.")
    assert "token=" not in payload["url"]
    resolved_path, resolved_owner = web_channel._decode_file_capability(
        attachment_url.removeprefix("/file/")
    )
    assert resolved_path == str(target)
    assert resolved_owner == owner


def test_history_file_capability_refuses_foreign_or_unavailable_path(monkeypatch, tmp_path):
    target = tmp_path / "foreign.txt"
    target.write_text("private", encoding="utf-8")
    history = {
        "messages": [
            {
                "role": "assistant",
                "steps": [{
                    "type": "tool",
                    "result": json.dumps({
                        "type": "file_to_send",
                        "path": str(target),
                        "url": "/api/file?path=legacy&token=leaked-bearer",
                    }),
                }],
            }
        ]
    }
    monkeypatch.setattr(web_channel, "_is_path_allowed", lambda path: True)
    monkeypatch.setattr(
        web_channel, "_is_other_owner_upload_path", lambda path, principal: True
    )

    decorated = web_channel._decorate_history_file_capabilities(
        history, "web:" + "e" * 32
    )
    payload = json.loads(decorated["messages"][0]["steps"][0]["result"])
    assert payload["url"] == ""
