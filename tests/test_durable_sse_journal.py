"""Crash/replay invariants for the durable Web SSE journal."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from channel.web import web_channel
from channel.web.sse_persistence import DurableSSEJournalStore


WebChannel = dict(zip(
    web_channel.WebChannel.__code__.co_freevars,
    (cell.cell_contents for cell in web_channel.WebChannel.__closure__),
))["cls"]


def _event(chunk: bytes) -> tuple[int, dict]:
    event_id = None
    payload = None
    for line in chunk.decode("utf-8").splitlines():
        if line.startswith("id: "):
            event_id = int(line[4:])
        elif line.startswith("data: "):
            payload = json.loads(line[6:])
    assert event_id is not None
    assert isinstance(payload, dict)
    return event_id, payload


def test_durable_store_requires_contiguous_owner_scoped_strict_events(tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "sse.sqlite3"))
    request_id = "durable-request"
    owner = "web:" + "a" * 32
    store.begin(request_id, owner, "session-a")
    store.append(request_id, 1, {"type": "delta", "content": "one"})
    # A retry is idempotent only when the exact canonical JSON is identical.
    store.append(request_id, 1, {"content": "one", "type": "delta"})
    store.append(request_id, 2, {"type": "done", "content": "two"})

    replay = store.replay(request_id, owner)
    assert replay is not None
    assert replay["state"] == "completed"
    assert replay["events"] == [
        (1, {"type": "delta", "content": "one"}),
        (2, {"type": "done", "content": "two"}),
    ]
    assert store.replay(request_id, "web:" + "b" * 32) is None

    with pytest.raises(ValueError, match="non-contiguous"):
        store.append(request_id, 4, {"type": "delta"})
    with pytest.raises(ValueError, match="conflicting"):
        store.append(request_id, 2, {"type": "done", "content": "changed"})


def test_durable_append_failure_emits_unconfirmed_not_original_event(tmp_path):
    store = DurableSSEJournalStore(str(tmp_path / "sse.sqlite3"))
    request_id = "durable-failure"
    owner = "web:" + "c" * 32
    store.begin(request_id, owner, "session-c")

    def unavailable(_event_id, _payload):
        raise OSError("simulated fsync failure")

    journal = web_channel._SSEEventJournal(unavailable)
    journal.put({"type": "delta", "content": "must-not-claim-delivery"})
    event_id, payload = journal.read_after(0, timeout=0)
    assert event_id == 1
    assert payload["type"] == "error"
    assert "unconfirmed" in payload["message"]


def test_recovery_replays_durable_prefix_then_explicitly_marks_running_worker_unconfirmed(
    monkeypatch, tmp_path
):
    store = DurableSSEJournalStore(str(tmp_path / "sse.sqlite3"))
    request_id = "durable-recovery"
    owner = "web:" + "d" * 32
    store.begin(request_id, owner, "session-d")
    store.append(request_id, 1, {"type": "delta", "content": "persisted prefix"})

    monkeypatch.setattr(web_channel, "_get_durable_sse_store", lambda: store)
    channel = SimpleNamespace(
        request_to_session={},
        request_owners={},
        sse_queues={},
        sse_last_active={},
    )
    channel._recover_sse_request = lambda rid, principal: WebChannel._recover_sse_request(
        channel, rid, principal
    )
    channel._drop_sse_request = lambda rid: WebChannel._drop_sse_request(channel, rid)

    stream = WebChannel.stream_response(channel, request_id, owner, after_event_id=0)
    assert _event(next(stream)) == (
        1,
        {"type": "delta", "content": "persisted prefix"},
    )
    recovered_id, recovered = _event(next(stream))
    assert recovered_id == 2
    assert recovered["type"] == "error"
    assert recovered["recovered"] is True
    assert "unconfirmed" in recovered["message"]
    stream.close()

    assert not WebChannel._recover_sse_request(
        channel, request_id, "web:" + "e" * 32
    )
