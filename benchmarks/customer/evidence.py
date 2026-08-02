"""客户验收事件的追加哈希链。"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Tuple

from .json_utils import canonical_json_bytes


_GENESIS_HASH = "0" * 64


class EventChain:
    """只追加生成可独立重算的运行事件。"""

    def __init__(self) -> None:
        self._events: List[Dict[str, Any]] = []
        self._previous_hash = _GENESIS_HASH

    def append(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        event = {
            "sequence": len(self._events) + 1,
            "event_type": event_type,
            "payload": payload,
            "previous_hash": self._previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json_bytes(event)).hexdigest()
        completed = dict(event)
        completed["event_hash"] = event_hash
        self._events.append(completed)
        self._previous_hash = event_hash
        return completed

    @property
    def events(self) -> Tuple[Dict[str, Any], ...]:
        return tuple(self._events)

    @property
    def head_hash(self) -> str:
        return self._previous_hash


def verify_event_chain(events: Iterable[Dict[str, Any]]) -> Tuple[str, ...]:
    """独立重算事件序号、前驱和内容哈希。"""

    failures = []
    previous = _GENESIS_HASH
    for sequence, event in enumerate(events, start=1):
        if not isinstance(event, dict) or set(event) != {
            "sequence", "event_type", "payload", "previous_hash", "event_hash"
        }:
            failures.append("事件 %d 结构无效" % sequence)
            continue
        if event["sequence"] != sequence:
            failures.append("事件 %d 序号无效" % sequence)
        if event["previous_hash"] != previous:
            failures.append("事件 %d 前驱哈希无效" % sequence)
        payload = {
            "sequence": event["sequence"],
            "event_type": event["event_type"],
            "payload": event["payload"],
            "previous_hash": event["previous_hash"],
        }
        expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        if event["event_hash"] != expected:
            failures.append("事件 %d 内容哈希无效" % sequence)
        previous = event["event_hash"]
    return tuple(failures)
