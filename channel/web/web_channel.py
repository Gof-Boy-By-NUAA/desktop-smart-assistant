import base64
import datetime
import hashlib
import hmac
import json
import logging
import math
import mimetypes
import os
import random
import re
import secrets
import shutil
import stat
import threading
import time
import uuid
from pathlib import Path
from queue import Queue, Empty
from typing import Any, List, Optional, Tuple
from urllib.parse import quote

import web

from bridge.context import *
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.chat_message import ChatMessage
from collections import OrderedDict, deque
from common import const
from common import i18n
from common.log import logger
from common.singleton import singleton
from config import conf, get_data_root, get_weixin_credentials_path

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".avi", ".mov", ".mkv"}

_TOOL_RESULT_SSE_MAX_JSON_BYTES = 256 * 1024
_TOOL_RESULT_SSE_MAX_DEPTH = 32
_TOOL_RESULT_SSE_MAX_PRESERVED_CITATIONS = 20
_SSE_EVENT_JOURNAL_MAX_EVENTS = 4096
_SSE_EVENT_JOURNAL_MAX_BYTES = 16 * 1024 * 1024
_DURABLE_SSE_STORE_LOCK = threading.Lock()
_DURABLE_SSE_STORE = None


def _get_durable_sse_store():
    """Return the data-root-bound append-only SSE journal store."""

    from channel.web.sse_persistence import DurableSSEJournalStore

    global _DURABLE_SSE_STORE
    path = os.path.realpath(os.path.join(get_data_root(), "web_sse_journal.sqlite3"))
    with _DURABLE_SSE_STORE_LOCK:
        if _DURABLE_SSE_STORE is None or _DURABLE_SSE_STORE.path != path:
            _DURABLE_SSE_STORE = DurableSSEJournalStore(path)
        return _DURABLE_SSE_STORE


class _SSEEventJournal:
    """Per-request bounded SSE event log with cursor-based replay.

    A ``queue.Queue`` permanently removes an item when a disconnected WSGI
    generator happens to read it. That makes reconnecting EventSource clients
    lose tool/output events. This journal keeps events until the request is
    explicitly reclaimed and gives every connection its own cursor instead.
    Overflow is explicit and terminal rather than silently dropping evidence.
    """

    def __init__(self, append_callback=None):
        self._events = deque()
        self._next_event_id = 1
        self._bytes = 0
        self._overflowed = False
        self._condition = threading.Condition()
        self._append_callback = append_callback

    @staticmethod
    def _encoded_size(item: dict) -> int:
        try:
            return len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except Exception:
            # The generator will emit a strict JSON error for malformed data;
            # reserve enough space here to fail closed before it can be hidden.
            return _SSE_EVENT_JOURNAL_MAX_BYTES + 1

    def _append_locked(self, event_id: int, item: dict, size: int) -> None:
        if self._append_callback is not None:
            # Durable append happens before the live SSE consumer can observe
            # this sequence. A crash therefore gives recovery the exact prefix
            # it may replay, never a made-up successful suffix.
            self._append_callback(event_id, item)
        self._events.append((event_id, item))
        self._next_event_id = event_id + 1
        self._bytes += size
        self._condition.notify_all()

    def _append_unconfirmed_locked(self) -> None:
        failure = {
            "type": "error",
            "message": "SSE durable journal write failed; task result is unconfirmed",
        }
        event_id = self._next_event_id
        size = self._encoded_size(failure)
        try:
            if self._append_callback is not None:
                self._append_callback(event_id, failure)
        except Exception:
            # The in-memory error is still deliberately terminal. The producer
            # must not turn a failed durable write into a normal SSE success.
            pass
        self._events.append((event_id, failure))
        self._next_event_id = event_id + 1
        self._bytes += size
        self._overflowed = True
        self._condition.notify_all()

    def restore(self, events) -> None:
        """Hydrate a durable prefix without re-appending it to storage."""

        with self._condition:
            previous = 0
            for event_id, item in events:
                if (
                    not isinstance(event_id, int)
                    or event_id <= previous
                    or not isinstance(item, dict)
                ):
                    raise ValueError("invalid durable SSE event sequence")
                self._events.append((event_id, item))
                self._bytes += self._encoded_size(item)
                previous = event_id
            self._next_event_id = previous + 1
            self._condition.notify_all()

    def put(self, item: dict):
        """Append an event. Kept queue-like because producers only call put()."""
        size = self._encoded_size(item)
        with self._condition:
            if self._overflowed:
                return
            if (
                len(self._events) >= _SSE_EVENT_JOURNAL_MAX_EVENTS
                or self._bytes + size > _SSE_EVENT_JOURNAL_MAX_BYTES
            ):
                overflow = {
                    "type": "error",
                    "message": "SSE event journal capacity exhausted; task result is unconfirmed",
                }
                try:
                    self._append_locked(
                        self._next_event_id, overflow, self._encoded_size(overflow)
                    )
                except Exception:
                    self._append_unconfirmed_locked()
                self._overflowed = True
                return
            try:
                self._append_locked(self._next_event_id, item, size)
            except Exception:
                self._append_unconfirmed_locked()

    def read_after(self, event_id: int, timeout: float):
        """Return ``(event_id, payload)`` newer than cursor, or ``None`` on timeout."""
        deadline = time.monotonic() + max(timeout, 0)
        with self._condition:
            while True:
                for sequence, payload in self._events:
                    if sequence > event_id:
                        return sequence, payload
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)


def _tool_result_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return f"{type(value).__module__}.{type(value).__qualname__}"


def _json_safe_tool_result(value: Any) -> Any:
    """把工具结果转换为严格 JSON 值，且不回退到 Python ``repr``。"""
    active_container_ids = set()

    def _normalize(item: Any, depth: int) -> Any:
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, str):
            try:
                item.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raw = item.encode("utf-8", errors="surrogatepass")
                return {
                    "_transport_type": "string",
                    "reason": "invalid_utf8_surrogate",
                    "character_count": len(item),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            return item
        if isinstance(item, float):
            if math.isfinite(item):
                return item
            return {
                "_transport_type": "number",
                "reason": "non_finite_value",
            }
        if isinstance(item, bytes):
            return {
                "_transport_type": "bytes",
                "byte_length": len(item),
                "sha256": hashlib.sha256(item).hexdigest(),
            }
        if depth >= _TOOL_RESULT_SSE_MAX_DEPTH:
            return {
                "_transport_type": _tool_result_type_name(item),
                "reason": "max_depth_exceeded",
            }

        is_container = isinstance(item, (dict, list, tuple, set, frozenset))
        container_id = id(item)
        if is_container:
            if container_id in active_container_ids:
                return {
                    "_transport_type": _tool_result_type_name(item),
                    "reason": "cyclic_reference",
                }
            active_container_ids.add(container_id)

        try:
            if isinstance(item, dict):
                if all(isinstance(key, str) for key in item):
                    return {
                        key: _normalize(child, depth + 1)
                        for key, child in item.items()
                    }
                return {
                    "_transport_type": "mapping",
                    "items": [
                        {
                            "key": _normalize(key, depth + 1),
                            "value": _normalize(child, depth + 1),
                        }
                        for key, child in item.items()
                    ],
                }
            if isinstance(item, (list, tuple)):
                return [_normalize(child, depth + 1) for child in item]
            if isinstance(item, (set, frozenset)):
                children = [_normalize(child, depth + 1) for child in item]
                children.sort(
                    key=lambda child: json.dumps(
                        child,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                )
                return {
                    "_transport_type": "set",
                    "items": children,
                }
        finally:
            if is_container:
                active_container_ids.remove(container_id)

        return {
            "_transport_type": _tool_result_type_name(item),
            "reason": "unsupported_json_type",
        }

    return _normalize(value, 0)


def _collect_v3_citation_transport_refs(value: Any) -> Tuple[List[dict], int]:
    """收集完整 v3 URI 与来源哈希；摘要不得切断任何一个身份字段。"""
    preserved = []
    total = 0

    def _walk(item: Any) -> None:
        nonlocal total
        if isinstance(item, dict):
            uri = item.get("uri")
            source_ref_hash = item.get("source_ref_hash")
            if (
                item.get("citation_version") == 3
                and isinstance(uri, str)
                and len(uri) <= 4096
                and uri.startswith("knowledge://")
                and isinstance(source_ref_hash, str)
                and re.fullmatch(r"[0-9a-f]{64}", source_ref_hash) is not None
                and uri.endswith(
                    f"&source_ref_hash={source_ref_hash}&citation_version=3"
                )
            ):
                total += 1
                if len(preserved) < _TOOL_RESULT_SSE_MAX_PRESERVED_CITATIONS:
                    preserved.append({
                        "citation_version": 3,
                        "source_ref_hash": source_ref_hash,
                        "uri": uri,
                    })
            for child in item.values():
                _walk(child)
        elif isinstance(item, list):
            for child in item:
                _walk(child)

    _walk(value)
    return preserved, total


def _summarize_oversized_tool_result(value: Any) -> dict:
    citations, citation_count = _collect_v3_citation_transport_refs(value)
    summary = {
        "_transport_summary": True,
        "original_type": _tool_result_type_name(value),
        "citations": citations,
        "citation_count": citation_count,
        "preserved_citation_count": len(citations),
    }
    if isinstance(value, dict):
        summary["top_level_item_count"] = len(value)
        result_count = value.get("result_count")
        if isinstance(result_count, int) and not isinstance(result_count, bool):
            summary["result_count"] = result_count
    elif isinstance(value, list):
        summary["item_count"] = len(value)
    elif isinstance(value, str):
        summary["character_count"] = len(value)
    return summary


def _prepare_sse_tool_result(value: Any) -> Tuple[Any, dict]:
    """生成有界、可审计的工具结果，保留 Citation v3 的原子身份字段。"""
    normalized = _json_safe_tool_result(value)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    metadata = {
        "encoding": "json",
        "truncated": False,
        "original_type": _tool_result_type_name(value),
        "original_json_bytes": len(encoded),
        "original_sha256": hashlib.sha256(encoded).hexdigest(),
    }
    if len(encoded) <= _TOOL_RESULT_SSE_MAX_JSON_BYTES:
        metadata["transmitted_json_bytes"] = len(encoded)
        return normalized, metadata

    summary = _summarize_oversized_tool_result(normalized)
    summary_encoded = json.dumps(
        summary,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    metadata.update({
        "truncated": True,
        "reason": "max_json_bytes_exceeded",
        "strategy": "structured_summary",
        "max_json_bytes": _TOOL_RESULT_SSE_MAX_JSON_BYTES,
        "transmitted_json_bytes": len(summary_encoded),
        "preserved_citation_count": summary["preserved_citation_count"],
        "citation_count": summary["citation_count"],
    })
    return summary, metadata

def _get_web_password() -> str:
    # Coerce to str so non-string values in config.json (e.g. numeric password) won't break comparisons
    pwd = conf().get("web_password", "")
    if pwd is None:
        return ""
    return str(pwd)


def _is_password_enabled():
    return bool(_get_web_password())


def _session_expire_seconds():
    return int(conf().get("web_session_expire_days", 30)) * 86400


def _resolve_web_bind_host(configured_host: str) -> Tuple[str, bool]:
    # The embedded server is plaintext HTTP and must never be the public TLS
    # boundary. A reverse proxy on the same host can reach this loopback socket.
    host = (configured_host or "127.0.0.1").strip().lower()
    loopback_aliases = {"127.0.0.1", "localhost", "::1", "[::1]"}
    if host not in loopback_aliases:
        logger.error(
            "[WebChannel] Refusing non-loopback plaintext bind; configure a "
            "TLS reverse proxy to the 127.0.0.1 listener"
        )
        return "127.0.0.1", False
    return ("::1" if host == "[::1]" else host), False


_AUTH_TOKEN_VERSION = "v3"
_AUTH_SUBJECT_VERSION = "s1"
_AUTH_CLOCK_SKEW_SECONDS = 60
_AUTH_BOOT_NONCE = uuid.uuid4().bytes
_AUTH_SUBJECT_RE = re.compile(r"^[0-9a-f]{32}$")
_revoked_auth_nonces = {}  # nonce -> token expiry epoch
_revoked_auth_lock = threading.Lock()
_stream_tickets = {}  # ticket -> {owner_id, request_id, expiry}; consumed on GET
_stream_ticket_lock = threading.Lock()
_STREAM_TICKET_TTL_SECONDS = 60
_log_stream_tickets = {}  # ticket -> {owner_id, expiry}; consumed on GET
_log_stream_ticket_lock = threading.Lock()
_LOG_STREAM_TICKET_TTL_SECONDS = 60
# URL-borne capabilities cannot carry an Authorization header.  Keep a
# per-owner, process-local epoch so logout invalidates the short-lived links,
# preview mounts and unconsumed EventSource tickets it issued.  The auth-policy
# epoch below also invalidates them across password/session policy changes and
# process restart.
_url_capability_epochs = {}  # canonical owner key -> monotonically increasing int
_url_capability_epoch_lock = threading.Lock()
_login_attempts = {}  # client key -> [window_start, failed_attempts]
_login_attempts_lock = threading.Lock()
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_FAILURES = 5
_auth_policy_lock = threading.Lock()
_auth_policy_fingerprint = None
_auth_policy_epoch = uuid.uuid4().bytes


def _subject_signature(payload: str) -> str:
    # Subject capabilities survive both restarts and password rotation. They are
    # never accepted as authentication: /auth/login still requires the current
    # password before it may reuse this server-signed device owner.
    return hmac.new(
        _get_preview_secret(),
        ("cow-web-subject:" + payload).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _current_auth_policy_epoch() -> bytes:
    """Rotate a process-local epoch whenever password/session policy changes.

    Returning a prior configuration never revives credentials issued under an
    older epoch. A process restart already invalidates all bearers via boot nonce.
    """

    global _auth_policy_fingerprint, _auth_policy_epoch
    fingerprint = (_get_web_password(), _session_expire_seconds())
    with _auth_policy_lock:
        if fingerprint != _auth_policy_fingerprint:
            _auth_policy_fingerprint = fingerprint
            _auth_policy_epoch = uuid.uuid4().bytes
        return _auth_policy_epoch


def _url_capability_owner_key(owner_id: Optional[str]) -> str:
    """Return a stable map key without changing the serialized owner claim."""

    return str(owner_id or "web:legacy")


def _current_url_capability_epoch(owner_id: Optional[str]) -> str:
    """Return the policy + per-owner revocation epoch for URL capabilities.

    The random policy epoch deliberately changes after restart as well as after
    password/session policy change.  That makes a durable file-signing secret
    insufficient to replay an old URL in a new process.
    """

    policy_epoch = _current_auth_policy_epoch().hex()
    owner_key = _url_capability_owner_key(owner_id)
    with _url_capability_epoch_lock:
        owner_epoch = _url_capability_epochs.get(owner_key, 0)
    return f"{policy_epoch}:{owner_epoch}"


def _revoke_owner_url_capabilities(owner_id: Optional[str]) -> None:
    """Invalidate one owner's URL capabilities and unconsumed stream tickets."""

    owner_key = _url_capability_owner_key(owner_id)
    with _url_capability_epoch_lock:
        _url_capability_epochs[owner_key] = _url_capability_epochs.get(owner_key, 0) + 1
    with _stream_ticket_lock:
        for ticket, record in list(_stream_tickets.items()):
            if record.get("owner_id") == owner_id:
                _stream_tickets.pop(ticket, None)
    with _log_stream_ticket_lock:
        for ticket, record in list(_log_stream_tickets.items()):
            if record.get("owner_id") == owner_id:
                _log_stream_tickets.pop(ticket, None)


def _auth_signature(payload: str) -> str:
    # Authentication credentials are deliberately process-bound. A logout
    # revocation cannot be forgotten by restart because every pre-restart token
    # becomes cryptographically invalid, while the non-auth subject can persist.
    key = hmac.new(
        _get_web_password().encode("utf-8"),
        b"cow-web-auth-v3:" + _AUTH_BOOT_NONCE + _current_auth_policy_epoch(),
        hashlib.sha256,
    ).digest()
    return hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()


def _create_auth_subject_token(subject_id: str) -> str:
    """Create a signed device subject capability used only during login."""
    if not _AUTH_SUBJECT_RE.fullmatch(subject_id or ""):
        raise ValueError("invalid auth subject")
    payload = f"{_AUTH_SUBJECT_VERSION}.{subject_id}"
    return f"{payload}.{_subject_signature(payload)}"


def _verify_auth_subject_token(token: str) -> Optional[str]:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    version, subject_id, signature = parts
    if version != _AUTH_SUBJECT_VERSION or not _AUTH_SUBJECT_RE.fullmatch(subject_id):
        return None
    payload = f"{version}.{subject_id}"
    if not hmac.compare_digest(signature, _subject_signature(payload)):
        return None
    return subject_id


def _create_auth_token(subject_id: Optional[str] = None) -> str:
    """Create a signed, replay-revocable token bound to a durable subject.

    Format: ``v3.<iat_hex>.<exp_hex>.<subject_hex>.<nonce_hex>.<hmac_hex>``.
    The random nonce prevents two logins in the same second from receiving the
    same credential; the subject remains stable through the separately signed
    device subject token so session ownership survives token renewal.
    """
    subject_id = subject_id or uuid.uuid4().hex
    if not _AUTH_SUBJECT_RE.fullmatch(subject_id):
        raise ValueError("invalid auth subject")
    issued_at = int(time.time())
    expires_at = issued_at + _session_expire_seconds()
    iat_hex = format(issued_at, "x")
    exp_hex = format(expires_at, "x")
    nonce = uuid.uuid4().hex
    payload = (
        f"{_AUTH_TOKEN_VERSION}.{iat_hex}.{exp_hex}.{subject_id}.{nonce}"
    )
    return f"{payload}.{_auth_signature(payload)}"


def _purge_revoked_auth_nonces(now: float) -> None:
    with _revoked_auth_lock:
        expired = [nonce for nonce, expiry in _revoked_auth_nonces.items() if expiry <= now]
        for nonce in expired:
            _revoked_auth_nonces.pop(nonce, None)


def _parse_auth_token(token: str, *, require_fresh: bool = True):
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 6:
        return None
    version, iat_hex, exp_hex, subject_id, nonce, signature = parts
    if version != _AUTH_TOKEN_VERSION:
        return None
    if not _AUTH_SUBJECT_RE.fullmatch(subject_id) or not _AUTH_SUBJECT_RE.fullmatch(nonce):
        return None
    try:
        timestamp = int(iat_hex, 16)
        expiry = int(exp_hex, 16)
    except ValueError:
        return None
    if expiry <= timestamp:
        return None
    payload = f"{version}.{iat_hex}.{exp_hex}.{subject_id}.{nonce}"
    if not hmac.compare_digest(signature, _auth_signature(payload)):
        return None

    now = time.time()
    if timestamp > now + _AUTH_CLOCK_SKEW_SECONDS:
        return None
    if require_fresh and (now >= expiry):
        return None
    _purge_revoked_auth_nonces(now)
    with _revoked_auth_lock:
        if nonce in _revoked_auth_nonces:
            return None
    return {
        "subject_id": subject_id,
        "nonce": nonce,
        "timestamp": timestamp,
        "expiry": expiry,
    }


def _verify_auth_token(token):
    """Return whether a v3 token is authentic, fresh, and not logged out."""
    return _parse_auth_token(token, require_fresh=True) is not None


def _request_environment() -> dict:
    try:
        env = web.ctx.env
    except Exception:
        return {}
    return env if isinstance(env, dict) else {}


def _get_bearer_token():
    """Extract the token from an `Authorization: Bearer <token>` header.

    The desktop client renders from a file:// origin, so cross-origin cookies
    to http://127.0.0.1 are unreliable (SameSite=Lax cookies aren't sent). It
    therefore authenticates via this header instead; browsers keep using the
    cookie set by /auth/login.
    """
    auth = _request_environment().get("HTTP_AUTHORIZATION", "") or ""
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


def _request_auth_tokens():
    tokens = []
    try:
        tokens.append(web.cookies().get("cow_auth_token", ""))
    except Exception:
        pass
    # Never authenticate ordinary requests from a URL query value: URLs are
    # copied into browser history, proxy logs and referrers.  SSE uses a
    # separate request-bound one-shot ticket, while desktop API calls use the
    # Authorization header and browser calls use the HttpOnly cookie.
    tokens.append(_get_bearer_token())
    return [token for token in tokens if token]


def _require_safe_request_host() -> None:
    """Reject DNS-rebinding and untrusted Host headers at the application edge."""

    env = _request_environment()
    if not env:
        # Direct unit/CLI invocation has no WSGI request. Real HTTP requests do.
        return
    raw_host = str(env.get("HTTP_HOST") or env.get("SERVER_NAME") or "")
    host = raw_host.strip().lower()
    if host.startswith("["):
        host_name = host.split("]", 1)[0] + "]"
    else:
        host_name = host.split(":", 1)[0]
    allowed = {"localhost", "127.0.0.1", "::1", "[::1]"}
    configured = conf().get("web_allowed_hosts", [])
    if isinstance(configured, str):
        configured = [item.strip() for item in configured.split(",")]
    if isinstance(configured, (list, tuple, set)):
        allowed.update(str(item).strip().lower() for item in configured if item)
    if host_name not in allowed:
        raise web.HTTPError(
            "421 Misdirected Request",
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps({"status": "error", "message": "Untrusted Host"}),
        )

    origin = str(env.get("HTTP_ORIGIN") or "").strip()
    if origin and origin != "null":
        try:
            from urllib.parse import urlsplit
            origin_host = (urlsplit(origin).hostname or "").lower()
        except Exception:
            origin_host = ""
        if origin_host not in {name.strip("[]") for name in allowed}:
            raise web.HTTPError(
                "403 Forbidden",
                {"Content-Type": "application/json; charset=utf-8"},
                json.dumps({"status": "error", "message": "Untrusted Origin"}),
            )


def _get_auth_principal() -> Optional[str]:
    """Return the server-verified Web session owner for this request."""
    if not _is_password_enabled():
        # No-password mode is intentionally one local legacy principal. Public
        # binding without a password is rejected during startup.
        return "web:legacy"
    parsed_tokens = [
        parsed
        for token in _request_auth_tokens()
        for parsed in [_parse_auth_token(token, require_fresh=True)]
        if parsed is not None
    ]
    subjects = {parsed["subject_id"] for parsed in parsed_tokens}
    # Conflicting cookie/header/query credentials are ambiguous and may cause an
    # operation or logout to execute under a different principal than intended.
    if len(subjects) != 1:
        return None
    return f"web:{next(iter(subjects))}"


def _check_auth():
    """Return True if request is authenticated or password not enabled."""
    return _get_auth_principal() is not None


def _require_auth() -> str:
    """Return the trusted principal or raise 401."""
    _require_safe_request_host()
    principal = _get_auth_principal()
    if principal is None:
        raise web.HTTPError(
            "401 Unauthorized",
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps({"status": "error", "message": "Unauthorized"}),
        )
    return principal


def _revoke_request_auth_token() -> None:
    """Revoke the concrete credential presented by this request until expiry."""
    parsed_tokens = []
    for token in _request_auth_tokens():
        parsed = _parse_auth_token(token, require_fresh=True)
        if parsed is not None:
            parsed_tokens.append(parsed)
    owner_ids = set()
    with _revoked_auth_lock:
        for parsed in parsed_tokens:
            _revoked_auth_nonces[parsed["nonce"]] = parsed["expiry"]
            owner_ids.add(f"web:{parsed['subject_id']}")
    for owner_id in owner_ids:
        _revoke_owner_url_capabilities(owner_id)


def _parse_sse_event_cursor(value: Any) -> int:
    """Parse an SSE cursor defensively; malformed values fail closed at zero."""
    try:
        cursor = int(value)
    except (TypeError, ValueError):
        return 0
    return cursor if 0 <= cursor <= 9_007_199_254_740_991 else 0


def _issue_stream_ticket(owner_id: str, request_id: str, after_event_id: int = 0) -> str:
    """Issue a short-lived request-bound capability for EventSource."""
    ticket = secrets.token_urlsafe(32)
    with _stream_ticket_lock:
        now = time.time()
        for stale, record in list(_stream_tickets.items()):
            if record["expiry"] <= now:
                _stream_tickets.pop(stale, None)
        _stream_tickets[ticket] = {
            "owner_id": owner_id,
            "request_id": request_id,
            "after_event_id": _parse_sse_event_cursor(after_event_id),
            "capability_epoch": _current_url_capability_epoch(owner_id),
            "expiry": now + _STREAM_TICKET_TTL_SECONDS,
        }
    return ticket


def _consume_stream_ticket_record(ticket: str, request_id: str) -> Optional[dict]:
    """Consume a request-bound one-shot SSE capability and return its claims."""
    if not ticket or not request_id:
        return None
    with _stream_ticket_lock:
        record = _stream_tickets.get(ticket)
        if not record or record["expiry"] <= time.time():
            _stream_tickets.pop(ticket, None)
            return None
        if record["request_id"] != request_id:
            return None
        _stream_tickets.pop(ticket, None)
    if not hmac.compare_digest(
        str(record.get("capability_epoch") or ""),
        _current_url_capability_epoch(record["owner_id"]),
    ):
        return None
    return record


def _consume_stream_ticket(ticket: str, request_id: str) -> Optional[str]:
    """Compatibility wrapper returning only the capability owner."""
    record = _consume_stream_ticket_record(ticket, request_id)
    return str(record["owner_id"]) if record is not None else None


def _issue_log_stream_ticket(owner_id: str) -> str:
    """Issue a short-lived one-shot capability for the diagnostics SSE stream."""

    ticket = secrets.token_urlsafe(32)
    with _log_stream_ticket_lock:
        now = time.time()
        for stale, record in list(_log_stream_tickets.items()):
            if record["expiry"] <= now:
                _log_stream_tickets.pop(stale, None)
        _log_stream_tickets[ticket] = {
            "owner_id": owner_id,
            "capability_epoch": _current_url_capability_epoch(owner_id),
            "expiry": now + _LOG_STREAM_TICKET_TTL_SECONDS,
        }
    return ticket


def _consume_log_stream_ticket(ticket: str) -> Optional[str]:
    """Consume a diagnostics stream capability exactly once."""

    if not ticket:
        return None
    with _log_stream_ticket_lock:
        record = _log_stream_tickets.get(ticket)
        if not record or record["expiry"] <= time.time():
            _log_stream_tickets.pop(ticket, None)
            return None
        _log_stream_tickets.pop(ticket, None)
    if not hmac.compare_digest(
        str(record.get("capability_epoch") or ""),
        _current_url_capability_epoch(record["owner_id"]),
    ):
        return None
    return str(record["owner_id"])


def _login_client_key() -> str:
    env = _request_environment()
    # The embedded server is loopback-only; REMOTE_ADDR is therefore the
    # trusted proxy/client socket identity rather than a user-supplied field.
    return str(env.get("REMOTE_ADDR") or "local")[:128]


def _login_attempt_allowed(key: str) -> bool:
    now = time.time()
    with _login_attempts_lock:
        record = _login_attempts.get(key)
        if not record or now - record[0] >= _LOGIN_WINDOW_SECONDS:
            _login_attempts[key] = [now, 0]
            return True
        return record[1] < _LOGIN_MAX_FAILURES


def _record_login_failure(key: str) -> None:
    now = time.time()
    with _login_attempts_lock:
        record = _login_attempts.get(key)
        if not record or now - record[0] >= _LOGIN_WINDOW_SECONDS:
            _login_attempts[key] = [now, 1]
        else:
            record[1] += 1


def _clear_login_failures(key: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(key, None)


# Localized text for /cancel system replies. Web is the only channel that
# honors a per-request `lang`; other channels reply in Chinese by default.
def _cancel_reply_text(cancelled: int, lang: str) -> str:
    en = lang.startswith("en")
    if cancelled > 0:
        return "🛑 Cancelled" if en else "🛑 已中止"
    return "Nothing to cancel." if en else "当前没有可中止的任务。"


def _steer_reply_text(status, lang: str) -> str:
    from agent.protocol import SteerStatus

    en = (lang or "").lower().startswith("en")
    messages = {
        SteerStatus.ACCEPTED: (
            "↪️ Active task redirected.", "↪️ 已引导当前任务。"
        ),
        SteerStatus.INACTIVE: (
            "No active task to steer.", "当前没有可引导的任务。"
        ),
        SteerStatus.CLOSING: (
            "The active task is already finishing.", "当前任务已结束，无法再引导。"
        ),
        SteerStatus.AMBIGUOUS: (
            "Multiple tasks are active in this session; the steering target is ambiguous.",
            "当前会话有多个任务在运行，无法确定引导目标。",
        ),
        SteerStatus.FULL: (
            "Too many steering updates are pending; try again after the agent processes them.",
            "引导指令过多，请等待当前任务处理后再试。",
        ),
        SteerStatus.INVALID: (
            "Usage: /steer <instruction>", "用法：/steer <引导指令>"
        ),
    }
    english, chinese = messages[status]
    return english if en else chinese


def _get_upload_dir() -> str:
    from common.utils import expand_path
    ws_root = expand_path(conf().get("agent_workspace", "~/cow"))
    tmp_dir = os.path.join(ws_root, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    return tmp_dir


def _owner_upload_dir(owner_id: Optional[str]) -> str:
    """Return an opaque, owner-scoped upload root."""

    path = os.path.join(_get_upload_dir(), _owner_upload_component(owner_id))
    os.makedirs(path, exist_ok=True)
    return path


def _owner_upload_component(owner_id: Optional[str]) -> str:
    """Derive the owner directory name without creating filesystem state."""
    owner = owner_id or "web:legacy"
    return hmac.new(
        _get_preview_secret(),
        ("cow-upload-owner:" + owner).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def _is_other_owner_upload_path(real_path: str, owner_id: Optional[str]) -> bool:
    """Reject cross-principal reads of the opaque per-owner upload roots.

    Uploads are under the workspace so the agent can process them, but that
    placement must not turn ``/api/file`` or workspace browsing into a second
    upload-serving API with weaker ownership checks.
    """
    upload_root = os.path.realpath(_get_upload_dir())
    real_path = os.path.realpath(real_path)
    try:
        relative = os.path.relpath(real_path, upload_root)
    except ValueError:
        return False
    if relative == os.curdir or relative.startswith(os.pardir + os.sep) or relative == os.pardir:
        return False
    first = relative.split(os.sep, 1)[0]
    if not re.fullmatch(r"[0-9a-f]{32}", first or ""):
        return False
    own_component = _owner_upload_component(owner_id)
    return first != own_component


def _get_workspace_root() -> str:
    """Resolve the agent workspace directory."""
    from common.utils import expand_path
    return expand_path(conf().get("agent_workspace", "~/cow"))


def _web_identity(owner_id: str, *, administrative: bool = False):
    """Build the only IdentityContext accepted from the Web trust boundary."""
    from agent.memory.config import MemoryConfig
    from agent.memory.governance import IdentityContext

    tenant_id = MemoryConfig(workspace_root=_get_workspace_root()).tenant_id
    roles = {"skill:read"}
    if administrative:
        # The knowledge management UI is an authenticated administrator surface,
        # but it does not receive knowledge:manage (which would bypass ownership).
        roles.add("knowledge:write_shared")
    actor_user_id = (
        "local-user"
        if owner_id == "web:legacy" and not _is_password_enabled()
        else owner_id
    )
    return IdentityContext(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        roles=frozenset(roles),
        trace_id=f"web-{uuid.uuid4().hex}",
        auth_source=("web-password" if _is_password_enabled() else "web-loopback"),
    )


def _web_knowledge_service(owner_id: str, *, administrative: bool = False):
    from agent.knowledge.service import KnowledgeService

    return KnowledgeService(
        _get_workspace_root(),
        identity=_web_identity(owner_id, administrative=administrative),
    )


def _claim_web_session(session_id: str, owner_id: str):
    """Atomically create or verify a Web session before side effects."""
    from agent.memory import get_conversation_store

    store = get_conversation_store()
    store.claim_session(session_id, owner_id, channel_type="web")
    return store


def _require_web_session(session_id: str, owner_id: str):
    """Return the store only when the trusted owner owns the session."""
    from agent.memory import get_conversation_store

    store = get_conversation_store()
    if not store.owns_session(session_id, owner_id):
        # Do not disclose whether the locator exists under another principal.
        raise PermissionError("session not found")
    return store


_PREVIEW_SECRET = None
_PREVIEW_SECRET_LOCK = threading.Lock()
_PREVIEW_SECRET_BYTES = 32
_PREVIEW_TOKEN_VERSION = "p3"
_FILE_CAPABILITY_VERSION = "f2"


def _read_preview_secret(path: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("preview secret is not a regular file")
        raw = os.read(fd, 65)
    finally:
        os.close(fd)
    try:
        encoded = raw.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", encoded):
            raise ValueError("bad length")
        return bytes.fromhex(encoded)
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError("preview secret is invalid")


def _read_preview_secret_when_ready(path: str, timeout_seconds: float = 2.0) -> bytes:
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while True:
        try:
            return _read_preview_secret(path)
        except (FileNotFoundError, RuntimeError) as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                raise last_error
            time.sleep(0.005)


def _get_preview_secret() -> bytes:
    """Load one durable 256-bit secret; partial/racy files fail closed."""
    global _PREVIEW_SECRET
    if _PREVIEW_SECRET is not None:
        return _PREVIEW_SECRET
    with _PREVIEW_SECRET_LOCK:
        if _PREVIEW_SECRET is not None:
            return _PREVIEW_SECRET
        path = os.path.join(get_data_root(), ".preview_secret")
        try:
            secret = _read_preview_secret(path)
        except FileNotFoundError:
            secret = os.urandom(_PREVIEW_SECRET_BYTES)
            flags = (
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            )
            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError:
                secret = _read_preview_secret_when_ready(path)
            else:
                try:
                    payload = secret.hex().encode("ascii")
                    view = memoryview(payload)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("short write while creating preview secret")
                        view = view[written:]
                    os.fsync(fd)
                except Exception:
                    try:
                        os.close(fd)
                    finally:
                        try:
                            os.unlink(path)
                        except OSError:
                            pass
                    raise
                else:
                    os.close(fd)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        except RuntimeError:
            # An invalid existing key is evidence of corruption or tampering;
            # silently replacing it would make old capabilities unpredictable.
            raise
        _PREVIEW_SECRET = secret
        return _PREVIEW_SECRET


def _preview_token_ttl_seconds() -> int:
    try:
        value = int(conf().get("web_preview_token_ttl_seconds", 3600))
    except (TypeError, ValueError):
        value = 3600
    return max(60, min(value, 86400))


def _file_capability_ttl_seconds() -> int:
    """Keep URL-borne single-file capabilities deliberately short-lived."""
    try:
        value = int(conf().get("web_file_capability_ttl_seconds", 300))
    except (TypeError, ValueError):
        value = 300
    return max(30, min(value, 600))


def _encode_file_capability(file_path: str, owner_id: Optional[str]) -> str:
    """Return a tamper-proof, expiry-bound capability for exactly one file.

    Unlike the legacy ``/api/file?path=...&token=<bearer>`` compatibility
    route, this URL contains no long-lived login bearer and cannot be changed
    into a different path or owner by editing its query string.
    """
    real_path = os.path.realpath(file_path)
    payload_data = {
        "path": real_path,
        "owner_id": str(owner_id or ""),
        "capability_epoch": _current_url_capability_epoch(owner_id),
        "expires_at": int(time.time()) + _file_capability_ttl_seconds(),
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    payload = f"{_FILE_CAPABILITY_VERSION}.{body}"
    signature = hmac.new(
        _get_preview_secret(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"/file/{payload}.{signature}"


def _decode_file_capability(token: str) -> Tuple[str, Optional[str]]:
    """Verify a single-file capability and return its canonical path/owner."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise ValueError("Malformed file capability")
    version, body, signature = parts
    if (
        version != _FILE_CAPABILITY_VERSION
        or not re.fullmatch(r"[A-Za-z0-9_-]+", body or "")
        or not re.fullmatch(r"[0-9a-f]{64}", signature or "")
    ):
        raise ValueError("Malformed file capability")
    payload = f"{version}.{body}"
    expected = hmac.new(
        _get_preview_secret(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid file capability")
    try:
        padded = body + "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        path = str(data["path"])
        owner_id = str(data.get("owner_id") or "")
        capability_epoch = str(data["capability_epoch"])
        expires_at = int(data["expires_at"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed file capability") from exc
    if expires_at < int(time.time()) or not os.path.isabs(path):
        raise ValueError("Expired file capability")
    real_path = os.path.realpath(path)
    if real_path != path:
        raise ValueError("Non-canonical file capability")
    if not hmac.compare_digest(
        capability_epoch, _current_url_capability_epoch(owner_id or None)
    ):
        raise ValueError("Revoked file capability")
    return real_path, owner_id or None


def _encode_dir_token(dir_path: str, owner_id: Optional[str] = None) -> str:
    """Encode an owner-bound directory into an expiring HMAC preview capability."""

    real = os.path.realpath(dir_path)
    payload_data = {
        "path": real,
        "owner_id": str(owner_id or ""),
        "capability_epoch": _current_url_capability_epoch(owner_id),
        "expires_at": int(time.time()) + _preview_token_ttl_seconds(),
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    payload = f"{_PREVIEW_TOKEN_VERSION}.{body}"
    signature = hmac.new(
        _get_preview_secret(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def _decode_dir_capability(token: str) -> Tuple[str, Optional[str]]:
    """Verify an owner-bound directory capability and return its claims."""

    parts = (token or "").split(".")
    if len(parts) != 3:
        raise ValueError("Malformed preview token")
    version, body, signature = parts
    if (
        version != _PREVIEW_TOKEN_VERSION
        or not re.fullmatch(r"[A-Za-z0-9_-]+", body or "")
        or not re.fullmatch(r"[0-9a-f]{64}", signature or "")
    ):
        raise ValueError("Malformed preview token")
    payload = f"{version}.{body}"
    expected = hmac.new(
        _get_preview_secret(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Bad preview token signature")
    try:
        padded = body + "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        path = str(data["path"])
        owner_id = str(data.get("owner_id") or "")
        capability_epoch = str(data["capability_epoch"])
        expires_at = int(data["expires_at"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed preview token") from exc
    if expires_at < int(time.time()):
        raise ValueError("Expired preview token")
    real = os.path.realpath(path)
    if real != path:
        raise ValueError("Non-canonical preview token")
    if not hmac.compare_digest(
        capability_epoch, _current_url_capability_epoch(owner_id or None)
    ):
        raise ValueError("Revoked preview token")
    return real, owner_id or None


def _decode_dir_token(token: str) -> str:
    """Compatibility wrapper returning only the authorized preview directory."""

    real, _owner_id = _decode_dir_capability(token)
    return real


def _serve_allowed_roots() -> list:
    """Roots that /api/file and /preview may read from (symlinks resolved)."""
    workspace = os.path.realpath(_get_workspace_root())
    configured = conf().get("web_file_serve_root")
    roots = [workspace]
    if configured:
        candidate = os.path.realpath(os.path.expanduser(str(configured)))
        # A filesystem root would turn an authenticated convenience endpoint
        # into an arbitrary-file disclosure primitive. Require an explicit
        # non-root directory; workspace remains the safe default.
        if os.path.dirname(candidate) != candidate:
            roots.insert(0, candidate)
        else:
            logger.warning("[WebChannel] Ignoring filesystem-root web_file_serve_root")
    return roots


def _is_path_allowed(real_path: str) -> bool:
    roots = _serve_allowed_roots()
    if os.sep in roots:
        return True
    for root in roots:
        try:
            if os.path.commonpath([real_path, root]) == root:
                return True
        except ValueError:
            continue
    return False


def _build_preview_url(abs_path: str, owner_id: Optional[str] = None) -> str:
    """
    Preview URL that mounts the file's *directory*, so relative assets
    referenced by an HTML page (./style.css, ./img/a.png) resolve correctly.
    """
    directory = os.path.dirname(abs_path)
    name = os.path.basename(abs_path)
    return f"/preview/{_encode_dir_token(directory, owner_id)}/{quote(name)}"


def _build_file_url(abs_path: str, owner_id: Optional[str]) -> str:
    """Capability URL for browser media/download contexts without headers."""
    return _encode_file_capability(abs_path, owner_id)


def _authorized_file_capability(
    file_path: Any, owner_id: Optional[str]
) -> Optional[str]:
    """Issue a file URL only after the same checks as authenticated file reads.

    This is deliberately used while *rendering a history response*, not while
    persisting it: a durable history record may contain an old absolute path,
    but must never preserve a bearer URL or a long-lived capability.
    """

    if not isinstance(file_path, str) or not file_path or not os.path.isabs(file_path):
        return None
    real_path = os.path.realpath(file_path)
    if (
        not _is_path_allowed(real_path)
        or _is_other_owner_upload_path(real_path, owner_id)
        or not os.path.isfile(real_path)
    ):
        return None
    return _build_file_url(real_path, owner_id)


def _decorate_history_file_capabilities(
    result: dict[str, Any], owner_id: Optional[str]
) -> dict[str, Any]:
    """Add fresh file capabilities to a history response without mutating DB data."""

    messages = result.get("messages")
    if not isinstance(messages, list):
        return result

    decorated_messages: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            decorated_messages.append(message)
            continue
        decorated_message = dict(message)

        # User attachment chips are reconstructed by the desktop renderer from
        # trailing [label: path] markers.  Return a separate map so the stored
        # prompt and its absolute path remain unchanged.
        attachment_urls: dict[str, str] = {}
        content = decorated_message.get("content")
        if isinstance(content, str):
            for line in content.splitlines():
                match = re.fullmatch(r"\[[^\]:]+\s*:\s*(.+)\]", line.strip())
                if match is None:
                    continue
                raw_path = match.group(1).strip()
                capability = _authorized_file_capability(raw_path, owner_id)
                if capability is not None:
                    attachment_urls[raw_path] = capability
        if attachment_urls:
            decorated_message["attachment_urls"] = attachment_urls

        raw_steps = decorated_message.get("steps")
        if isinstance(raw_steps, list):
            decorated_steps: list[Any] = []
            for raw_step in raw_steps:
                if not isinstance(raw_step, dict):
                    decorated_steps.append(raw_step)
                    continue
                step = dict(raw_step)
                raw_result = step.get("result")
                was_serialized = isinstance(raw_result, str)
                try:
                    payload = (
                        json.loads(raw_result)
                        if was_serialized
                        else raw_result
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = None
                if (
                    isinstance(payload, dict)
                    and payload.get("type") == "file_to_send"
                ):
                    payload = dict(payload)
                    existing_url = str(payload.get("url") or "")
                    is_remote = existing_url.lower().startswith(
                        ("http://", "https://")
                    )
                    capability = _authorized_file_capability(
                        payload.get("path"), owner_id
                    )
                    if capability is not None:
                        payload["url"] = capability
                    elif not is_remote:
                        # Do not replay an old /api/file URL (which could carry
                        # a leaked bearer) when its local file is no longer safe
                        # or available.
                        payload["url"] = ""
                    step["result"] = (
                        json.dumps(payload, ensure_ascii=False)
                        if was_serialized
                        else payload
                    )
                decorated_steps.append(step)
            decorated_message["steps"] = decorated_steps
        decorated_messages.append(decorated_message)

    decorated_result = dict(result)
    decorated_result["messages"] = decorated_messages
    return decorated_result


def _build_artifact_payload(data: dict, owner_id: Optional[str] = None) -> dict:
    """Turn an agent `artifact` event into an SSE payload for the web clients."""
    file_path = data.get("path", "")
    if not file_path:
        return None
    return {
        "type": "artifact",
        "abs_path": file_path,
        "rel_path": data.get("rel_path") or os.path.basename(file_path),
        "file_name": data.get("file_name") or os.path.basename(file_path),
        "kind": data.get("kind", "file"),
        "previewable": bool(data.get("previewable")),
        "size": data.get("size", 0),
        "raw_url": _build_file_url(file_path, owner_id),
        "preview_url": _build_preview_url(file_path, owner_id),
    }


def _sanitize_upload_relative_path(relative_path: str) -> str:
    """Normalize relative upload path and reject escapes / absolute paths."""
    relative_path = (relative_path or "").replace("\\", "/").strip("/")
    if not relative_path:
        raise ValueError("Empty relative path")
    parts = []
    for part in relative_path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("Invalid relative path")
        parts.append(part)
    if not parts:
        raise ValueError("Invalid relative path")
    norm_path = "/".join(parts)
    if os.path.isabs(norm_path):
        raise ValueError("Invalid relative path")
    return norm_path


def _sanitize_upload_id(upload_id: str) -> str:
    """Allow only simple batch ids for directory uploads."""
    sanitized = "".join(ch for ch in (upload_id or "") if ch.isalnum() or ch in ("-", "_"))
    if not sanitized:
        raise ValueError("Invalid upload id")
    return sanitized[:80]


def _is_within_directory(root_path: str, target_path: str) -> bool:
    root_path = os.path.realpath(root_path)
    target_path = os.path.realpath(target_path)
    try:
        return os.path.commonpath([root_path, target_path]) == root_path
    except ValueError:
        return False


def _resolve_upload_path(upload_root: str, relative_path: str) -> Tuple[str, str]:
    """Resolve a relative upload path under upload_root and reject escapes."""
    safe_rel_path = _sanitize_upload_relative_path(relative_path)
    upload_root_real = os.path.realpath(upload_root)
    save_path = os.path.realpath(os.path.join(upload_root_real, *safe_rel_path.split("/")))
    if not _is_within_directory(upload_root_real, save_path):
        raise ValueError("Invalid directory upload path")
    return safe_rel_path, save_path


def _read_uploaded_file_bytes(file_obj) -> bytes:
    """Return uploaded content as bytes across web.py upload object variants."""
    if isinstance(file_obj, bytes):
        return file_obj
    if isinstance(file_obj, str):
        return file_obj.encode("utf-8")

    content = None

    if hasattr(file_obj, "file") and hasattr(file_obj.file, "read"):
        content = file_obj.file.read()
    elif hasattr(file_obj, "read"):
        content = file_obj.read()
    elif hasattr(file_obj, "value"):
        content = file_obj.value

    if content is None:
        raise ValueError("Unable to read uploaded file content")
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    raise TypeError(f"Unsupported uploaded content type: {type(content).__name__}")


def _read_uploaded_file_bytes_limited(file_obj, max_bytes: int) -> bytes:
    """Read uploaded content and fail once it exceeds max_bytes."""
    if isinstance(file_obj, bytes):
        content = file_obj
    elif isinstance(file_obj, str):
        content = file_obj.encode("utf-8")
    elif hasattr(file_obj, "file") and hasattr(file_obj.file, "read"):
        content = file_obj.file.read(max_bytes + 1)
    elif hasattr(file_obj, "read"):
        content = file_obj.read(max_bytes + 1)
    elif hasattr(file_obj, "value"):
        content = file_obj.value
    else:
        raise ValueError("Unable to read uploaded file content")
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not isinstance(content, bytes):
        raise TypeError(f"Unsupported uploaded content type: {type(content).__name__}")
    if len(content) > max_bytes:
        raise ValueError("file too large")
    return content


def _raw_web_input():
    """Return unprocessed multipart form data when web.py exposes rawinput."""
    rawinput = getattr(getattr(web, "webapi", None), "rawinput", None)
    if not callable(rawinput):
        raise RuntimeError("web.py rawinput is not available")
    try:
        return rawinput(method="post")
    except TypeError:
        return rawinput()


def _ensure_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _generate_session_title(user_message: str, assistant_reply: str = "") -> str:
    """Delegate to the shared SessionService implementation."""
    from agent.chat.session_service import generate_session_title
    return generate_session_title(user_message, assistant_reply)


class WebMessage(ChatMessage):
    def __init__(
            self,
            msg_id,
            content,
            ctype=ContextType.TEXT,
            from_user_id="User",
            to_user_id="Chatgpt",
            other_user_id="Chatgpt",
    ):
        self.msg_id = msg_id
        self.ctype = ctype
        self.content = content
        self.from_user_id = from_user_id
        self.to_user_id = to_user_id
        self.other_user_id = other_user_id


@singleton
class WebChannel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = [ReplyType.VOICE]
    _instance = None

    # def __new__(cls):
    #     if cls._instance is None:
    #         cls._instance = super(WebChannel, cls).__new__(cls)
    #     return cls._instance

    def __init__(self):
        super().__init__()
        self.msg_id_counter = 0
        self.session_queues = {}  # session_id -> Queue (fallback polling)
        self.request_to_session = {}  # request_id -> session_id
        self.request_owners = {}  # request_id -> trusted Web owner principal
        self.sse_queues = {}  # request_id -> _SSEEventJournal (SSE streaming)
        self._sse_stream_lock = threading.RLock()
        self._sse_stream_generations = {}  # request_id -> active WSGI generator
        # request_id -> last-active timestamp. Refreshed while the SSE
        # generator is being consumed (client still connected). The janitor
        # only reclaims queues whose generator stopped refreshing this, so a
        # long-running but still-streaming reply is never wrongly killed.
        self.sse_last_active = {}
        self._http_server = None
        self._sse_janitor_started = False

    def _generate_msg_id(self):
        """生成唯一的消息ID"""
        self.msg_id_counter += 1
        return str(int(time.time())) + str(self.msg_id_counter)

    def _generate_request_id(self):
        """生成唯一的请求ID"""
        return str(uuid.uuid4())

    def _fetch_latest_pair_seqs(
        self, session_id: str, owner_id: Optional[str] = None
    ):
        """Query the conversation store for the latest user/bot message seqs.

        Returned as ``{"user_seq": int|None, "bot_seq": int|None}``; used to
        attach seq metadata onto the SSE ``done`` event so the frontend can
        wire edit / regenerate buttons for live-streamed bubbles without a
        page refresh.
        """
        try:
            from agent.memory import get_conversation_store
            return get_conversation_store().get_latest_pair_seqs(
                session_id, owner_id=owner_id
            )
        except Exception as e:
            logger.debug(f"[WebChannel] _fetch_latest_pair_seqs failed: {e}")
            return {"user_seq": None, "bot_seq": None}

    def send(self, reply: Reply, context: Context):
        try:
            if reply.type in self.NOT_SUPPORT_REPLYTYPE:
                logger.warning(f"Web channel doesn't support {reply.type} yet")
                return

            if reply.type == ReplyType.IMAGE_URL:
                time.sleep(0.5)

            request_id = context.get("request_id", None)
            if not request_id:
                logger.error("No request_id found in context, cannot send message")
                return

            session_id = self.request_to_session.get(request_id)
            if not session_id:
                logger.error(f"No session_id found for request {request_id}")
                return

            # SSE mode: push events to SSE queue
            if request_id in self.sse_queues:
                content = reply.content if reply.content is not None else ""

                # Intermediate status lines (e.g. /install-browser phases) must NOT use "done",
                # or the frontend closes EventSource and drops subsequent events.
                if getattr(reply, "sse_phase", False):
                    self.sse_queues[request_id].put({
                        "type": "phase",
                        "content": content,
                        "request_id": request_id,
                        "timestamp": time.time(),
                    })
                    logger.debug(f"SSE phase for request {request_id}")
                    return

                # Files are already pushed via on_event (file_to_send) during agent execution.
                # Skip duplicate file pushes here; just let the done event through.
                if reply.type in (ReplyType.IMAGE_URL, ReplyType.FILE) and content.startswith("file://"):
                    text_content = getattr(reply, 'text_content', '')
                    if text_content:
                        seqs = self._fetch_latest_pair_seqs(
                            session_id, self.request_owners.get(request_id)
                        )
                        self.sse_queues[request_id].put({
                            "type": "done",
                            "content": text_content,
                            "request_id": request_id,
                            "timestamp": time.time(),
                            "user_seq": seqs.get("user_seq"),
                            "bot_seq": seqs.get("bot_seq"),
                        })
                    logger.debug(f"SSE skipped duplicate file for request {request_id}")
                    return

                # Skip http-URL FILE/IMAGE_URL replies produced by chat_channel's media extraction:
                # the text reply (already sent as "done") contains the URL and the frontend will
                # render it via renderMarkdown/injectVideoPlayers, so no separate SSE event needed.
                if reply.type in (ReplyType.FILE, ReplyType.IMAGE_URL) and content.startswith(("http://", "https://")):
                    logger.debug(f"SSE skipped http media reply for request {request_id}")
                    return

                seqs = self._fetch_latest_pair_seqs(
                    session_id, self.request_owners.get(request_id)
                )
                self.sse_queues[request_id].put({
                    "type": "done",
                    "content": content,
                    "request_id": request_id,
                    "timestamp": time.time(),
                    "user_seq": seqs.get("user_seq"),
                    "bot_seq": seqs.get("bot_seq"),
                })
                logger.debug(f"SSE done sent for request {request_id}")
                # Auto-trigger TTS once the bot finishes its text reply. The
                # synthesis runs in the background so the chat stream is never
                # blocked; the resulting audio URL is pushed via a follow-up
                # `voice_attach` SSE event and persisted to messages.extras.
                if reply.type == ReplyType.TEXT and content.strip():
                    self._maybe_dispatch_auto_tts(request_id, session_id, content, context)
                return

            # Fallback: polling mode
            if session_id in self.session_queues:
                content = reply.content if reply.content is not None else ""
                # Skip file:// IMAGE_URL/FILE replies originating from an SSE-enabled
                # request: they were already pushed via the `file_to_send` event during
                # agent execution. By the time the chat_channel sends the IMAGE_URL reply,
                # the SSE stream has typically closed (after the text "done") and the
                # request_id is gone from sse_queues, so we'd otherwise duplicate the file
                # as a polling bubble. Scheduler/push tasks have no on_event and must
                # still go through polling normally.
                if (
                    reply.type in (ReplyType.IMAGE_URL, ReplyType.FILE)
                    and content.startswith("file://")
                    and context.get("on_event") is not None
                ):
                    logger.debug(f"Polling skipped duplicate file reply for session {session_id}")
                    return
                # SSE-enabled requests already stream the text reply to the
                # client. Do NOT also enqueue it for polling: if the user
                # switched away mid-run, the queued copy would resurface as a
                # duplicate bubble when they return and poll the session.
                if reply.type == ReplyType.TEXT and context.get("on_event") is not None:
                    logger.debug(f"Polling skipped SSE text reply for session {session_id}")
                    return
                response_data = {
                    "type": str(reply.type),
                    "content": content,
                    "timestamp": time.time(),
                    "request_id": request_id
                }
                self.session_queues[session_id].put(response_data)
                logger.debug(f"Response sent to poll queue for session {session_id}, request {request_id}")
            else:
                logger.warning(f"No response queue found for session {session_id}, response dropped")

        except Exception as e:
            logger.error(f"Error in send method: {e}")

    def _make_sse_callback(self, request_id: str):
        """Build an on_event callback that pushes agent stream events into the SSE queue."""

        # Cap reasoning bytes pushed to the frontend per request to avoid
        # browser stalls / crashes on very long chains-of-thought. Anything
        # beyond the cap is dropped from the stream (DB still persists a
        # truncated copy via _truncate_reasoning_for_storage).
        # Keep aligned with frontend REASONING_RENDER_CAP and backend
        # MAX_STORED_REASONING_CHARS.
        MAX_REASONING_STREAM_CHARS = 4 * 1024  # 4 KB
        # Use a single-element list as a mutable counter accessible from closure.
        reasoning_chars_sent = [0]
        reasoning_capped_notified = [False]
        # Captures the first error message emitted by agent_stream so the
        # subsequent agent_end handler can skip its "empty final_response"
        # fallback (which would otherwise overwrite the real error).
        streamed_error: List[str] = []

        def on_event(event: dict):
            if request_id not in self.sse_queues:
                return
            q = self.sse_queues[request_id]
            event_type = event.get("type")
            data = event.get("data", {})

            if event_type == "reasoning_update":
                delta = data.get("delta", "")
                if not delta:
                    return
                remaining = MAX_REASONING_STREAM_CHARS - reasoning_chars_sent[0]
                if remaining <= 0:
                    if not reasoning_capped_notified[0]:
                        reasoning_capped_notified[0] = True
                        q.put({
                            "type": "reasoning",
                            "content": "\n\n... [reasoning truncated for display] ...",
                        })
                    return
                if len(delta) > remaining:
                    delta = delta[:remaining]
                reasoning_chars_sent[0] += len(delta)
                q.put({"type": "reasoning", "content": delta})

            elif event_type == "message_update":
                delta = data.get("delta", "")
                if delta:
                    q.put({"type": "delta", "content": delta})

            elif event_type == "tool_execution_start":
                tool_name = data.get("tool_name", "tool")
                arguments = data.get("arguments", {})
                q.put({"type": "tool_start", "tool_call_id": data.get("tool_call_id"), "tool": tool_name, "arguments": arguments})

            elif event_type == "tool_execution_progress":
                q.put({
                    "type": "tool_progress",
                    "tool_call_id": data.get("tool_call_id"),
                    "tool": data.get("tool_name", "tool"),
                    "content": str(data.get("message", ""))[-4 * 1024:],
                })

            elif event_type == "tool_execution_end":
                tool_name = data.get("tool_name", "tool")
                status = data.get("status", "success")
                result = data.get("result", "")
                exec_time = data.get("execution_time", 0)
                result_payload, result_transport = _prepare_sse_tool_result(result)
                try:
                    valid_exec_time = (
                        isinstance(exec_time, (int, float))
                        and math.isfinite(exec_time)
                    )
                except (OverflowError, TypeError, ValueError):
                    valid_exec_time = False
                if not valid_exec_time:
                    exec_time = 0
                q.put({
                    "type": "tool_end",
                    "tool_call_id": data.get("tool_call_id"),
                    "tool": tool_name,
                    "status": status,
                    "result": result_payload,
                    "result_transport": result_transport,
                    "execution_time": round(exec_time, 2)
                })

            elif event_type == "message_end":
                tool_calls = data.get("tool_calls", [])
                if tool_calls:
                    q.put({"type": "message_end", "has_tool_calls": True})

            elif event_type == "error":
                # Agent raised an exception (LLM 401/timeout/etc). Surface the
                # real message instead of letting the empty-response fallback
                # below hide it as "(模型未返回任何内容)".
                err_msg = data.get("error") or "unknown error"
                logger.warning(
                    f"[WebChannel] agent_stream emitted error for "
                    f"request {request_id}: {err_msg}"
                )
                # Remember it so the agent_end handler below knows not to
                # rewrite the message into a generic empty-response notice.
                streamed_error.append(err_msg)
                q.put({
                    "type": "done",
                    "content": f"❌ {err_msg}",
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

            elif event_type == "agent_cancelled":
                # Push an explicit cancelled SSE event so the frontend
                # marks the bubble as stopped. A trailing "done" still
                # arrives with the partial answer.
                final_response = data.get("final_response", "")
                q.put({
                    "type": "cancelled",
                    "content": final_response,
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

            elif event_type == "agent_end":
                # Safety net: if the agent finishes with an empty final_response,
                # chat_channel skips _send_reply (because reply.content is empty),
                # which means no "done" event is ever emitted and the SSE stream
                # would hang until the 10-min idle timeout. Push a fallback "done"
                # here so the frontend always gets closure.
                final_response = data.get("final_response", "")
                if not final_response or not str(final_response).strip():
                    if streamed_error:
                        # Error was already surfaced via the `error` event
                        # handler above; nothing more to do here.
                        pass
                    else:
                        logger.warning(
                            f"[WebChannel] agent_end with empty final_response for "
                            f"request {request_id}, sending fallback done"
                        )
                        q.put({
                            "type": "done",
                            "content": i18n.t(
                                "(模型未返回任何内容，请重试或换一种方式描述你的需求)",
                                "(The model returned no content. Please retry or rephrase your request.)",
                            ),
                            "request_id": request_id,
                            "timestamp": time.time(),
                        })

            elif event_type == "file_to_send":
                file_path = data.get("path", "")
                file_name = data.get("file_name", os.path.basename(file_path))
                file_type = data.get("file_type", "file")
                # Remote URLs are passed through as-is; local files are served
                # via the backend /api/file endpoint.
                remote_url = data.get("url", "")
                is_remote = bool(remote_url) and remote_url.lower().startswith(("http://", "https://"))
                if is_remote:
                    web_url = remote_url
                else:
                    web_url = _build_file_url(
                        file_path, self.request_owners.get(request_id)
                    )
                is_image = file_type == "image"
                payload = {
                    "type": "image" if is_image else "file",
                    "content": web_url,
                    "file_name": file_name,
                    # Preserve the concrete media kind (image/video/audio/...)
                    # so richer clients can render an inline player.
                    "file_type": file_type,
                }
                # Expose the local absolute path so the desktop client can open
                # the file directly (Finder / default app) instead of the browser.
                if not is_remote and file_path:
                    payload["abs_path"] = file_path
                q.put(payload)

            elif event_type == "artifact":
                payload = _build_artifact_payload(
                    data, self.request_owners.get(request_id)
                )
                if payload:
                    q.put(payload)

        return on_event

    # ------------------------------------------------------------------
    # TTS auto-dispatch
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_voice_reply_mode() -> str:
        """
        Decide the TTS auto-reply policy.

        Source of truth is the cross-channel pair
        (`always_reply_voice`, `voice_reply_voice`) which chat_channel
        also consults. The web UI presents these as a single three-state
        picker (off / voice_if_voice / always) via a lossless mapping.
        """
        if conf().get("always_reply_voice", False):
            return "always"
        if conf().get("voice_reply_voice", False):
            return "voice_if_voice"
        return "off"

    # Mirror of ModelsHandler._TTS_PROVIDERS. zhipu is intentionally omitted
    # from the UI (GLM-TTS prelude beep); pinning it in config.json still works.
    _TTS_PROVIDERS_SUGGEST_ORDER = ["openai", "minimax", "dashscope", "linkai"]

    @classmethod
    def _tts_provider_ready(cls) -> bool:
        """True if user picked a provider OR any suggested vendor has an API key."""
        if (conf().get("text_to_voice") or "").strip():
            return True
        for pid in cls._TTS_PROVIDERS_SUGGEST_ORDER:
            meta = ConfigHandler.PROVIDER_MODELS.get(pid) or {}
            key_field = meta.get("api_key_field")
            if not key_field:
                continue
            val = (conf().get(key_field) or "").strip()
            if val and val not in ("YOUR API KEY", "YOUR_API_KEY"):
                return True
        return False

    def _maybe_dispatch_auto_tts(
        self,
        request_id: str,
        session_id: str,
        text: str,
        context: dict,
    ) -> None:
        try:
            mode = self._resolve_voice_reply_mode()
            if mode == "off":
                return
            if mode == "voice_if_voice" and not context.get("is_voice_input"):
                return
            if not self._tts_provider_ready():
                return
            threading.Thread(
                target=self._synthesize_tts_async,
                args=(
                    request_id, session_id, text,
                    context.get("session_owner_id") or None,
                ),
                daemon=True,
            ).start()
        except Exception as e:
            logger.debug(f"[WebChannel] auto-tts dispatch skipped: {e}")

    def _synthesize_tts_async(
        self,
        request_id: str,
        session_id: str,
        text: str,
        owner_id: Optional[str] = None,
    ) -> None:
        try:
            from bridge.bridge import Bridge
            reply = Bridge().fetch_text_to_voice(text)
            if reply is None or reply.type != ReplyType.VOICE or not reply.content:
                logger.warning(
                    f"[WebChannel] TTS produced no audio for request {request_id}: "
                    f"reply={reply}"
                )
                return
            url = self._publish_tts_audio(reply.content, owner_id=owner_id)
            if not url:
                logger.warning(f"[WebChannel] TTS publish failed for request {request_id}")
                return
            payload = {"audio": {"url": url, "kind": "tts"}}
            try:
                from agent.memory import get_conversation_store
                get_conversation_store().attach_extras_to_last_assistant(
                    session_id, payload, owner_id=owner_id
                )
            except Exception as e:
                logger.debug(f"[WebChannel] tts persist skipped: {e}")
            q = self.sse_queues.get(request_id)
            if q is None:
                logger.warning(
                    f"[WebChannel] TTS ready but SSE queue already closed "
                    f"for request {request_id} (url={url})"
                )
                return
            q.put({
                "type": "voice_attach",
                "url": url,
                "request_id": request_id,
                "timestamp": time.time(),
            })
            logger.info(f"[WebChannel] TTS voice_attach pushed for request {request_id}: {url}")
        except Exception as e:
            # TTS failures are intentionally silent (no user-facing error).
            logger.warning(f"[WebChannel] TTS synthesis failed: {e}")

    @staticmethod
    def _publish_tts_audio(
        src_path: str, owner_id: Optional[str] = None
    ) -> str:
        """Move a TTS file into uploads/ and return its public URL."""
        try:
            if not src_path or not os.path.isfile(src_path):
                logger.warning(f"[WebChannel] publish_tts_audio missing source: {src_path!r}")
                return ""
            ext = os.path.splitext(src_path)[1].lower() or ".mp3"
            upload_dir = _owner_upload_dir(owner_id)
            ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            dst_name = f"voice_reply_{ts}_{random.randint(0, 9999)}{ext}"
            dst_path = os.path.join(upload_dir, dst_name)
            shutil.move(src_path, dst_path)
            logger.debug(f"[WebChannel] publish_tts_audio moved {src_path} -> {dst_path}")
            return _build_file_url(dst_path, owner_id)
        except Exception as e:
            logger.warning(f"[WebChannel] publish_tts_audio failed: {e}")
            return ""

    @staticmethod
    def _cleanup_stale_voice_recordings(max_age_seconds: int = 3600) -> None:
        """Drop voice_input_* uploads older than max_age_seconds (run at startup)."""
        try:
            upload_dir = _get_upload_dir()
            if not os.path.isdir(upload_dir):
                return
            now = time.time()
            removed = 0
            for name in os.listdir(upload_dir):
                if not name.startswith("voice_input_"):
                    continue
                full = os.path.join(upload_dir, name)
                try:
                    if not os.path.isfile(full):
                        continue
                    if now - os.path.getmtime(full) > max_age_seconds:
                        os.remove(full)
                        removed += 1
                except OSError:
                    continue
            if removed:
                logger.info(f"[WebChannel] cleaned up {removed} stale voice recording(s) from {upload_dir}")
        except Exception as e:
            logger.warning(f"[WebChannel] voice cleanup failed: {e}")

    def upload_file(self, owner_id: Optional[str] = None):
        """Handle file or directory upload via multipart/form-data."""
        try:
            params = _raw_web_input()
            file_obj = params.get("file")
            file_objs = params.get("files")
            session_id = params.get("session_id", "")
            relative_path = params.get("relative_path", "")
            relative_paths = params.get("relative_paths")
            upload_id = params.get("upload_id", "")

            directory_files = _ensure_list(file_objs)

            # NOTE: cgi.FieldStorage raises TypeError on truthy checks for single-file
            # uploads (Python 3.9+). Always use `is not None` instead of `if file_obj`.
            if not directory_files and file_obj is not None and relative_path:
                directory_files = [file_obj]

            directory_rel_paths = _ensure_list(relative_paths)

            if not directory_rel_paths and relative_path:
                directory_rel_paths = [relative_path]

            is_directory_upload = bool(directory_files) or bool(directory_rel_paths) or bool(relative_path) or bool(upload_id)

            upload_dir = _owner_upload_dir(owner_id)
            if is_directory_upload:
                if not upload_id:
                    return json.dumps({"status": "error", "message": "Missing upload_id for directory upload"})
                if not directory_files:
                    return json.dumps({"status": "error", "message": "No files uploaded"})
                if len(directory_files) != len(directory_rel_paths):
                    return json.dumps({"status": "error", "message": "Directory upload payload mismatch"})

                safe_upload_id = _sanitize_upload_id(upload_id)
                upload_root = os.path.join(upload_dir, f"webdir_{safe_upload_id}")
                upload_root_real = os.path.realpath(upload_root)

                root_name = None
                saved_files = 0
                for file_obj, rel_path in zip(directory_files, directory_rel_paths):
                    if file_obj is None:
                        raise ValueError("Invalid uploaded file")
                    safe_rel_path, save_path = _resolve_upload_path(upload_root_real, rel_path)
                    current_root_name = safe_rel_path.split("/", 1)[0]
                    if root_name is None:
                        root_name = current_root_name
                    elif root_name != current_root_name:
                        raise ValueError("Directory upload must use a single root folder")
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    content_bytes = _read_uploaded_file_bytes(file_obj)
                    with open(save_path, "wb") as f:
                        f.write(content_bytes)
                    saved_files += 1

                if not root_name:
                    raise ValueError("Directory root path missing")

                root_path = os.path.realpath(os.path.join(upload_root_real, root_name))
                if not _is_within_directory(upload_root_real, root_path):
                    raise ValueError("Invalid directory upload path")

                logger.info(f"[WebChannel] Directory uploaded: {root_name} -> {root_path} ({saved_files} files)")
                return json.dumps({
                    "status": "success",
                    "file_path": root_path,
                    "file_name": root_name,
                    "file_type": "directory",
                    "file_count": saved_files,
                    "root_path": root_path,
                    "root_name": root_name,
                    "upload_type": "directory",
                }, ensure_ascii=False)

            if file_obj is None or not hasattr(file_obj, "filename") or not file_obj.filename:
                return json.dumps({"status": "error", "message": "No file uploaded"})

            original_name = file_obj.filename
            ext = os.path.splitext(original_name)[1].lower()
            safe_name = f"web_{uuid.uuid4().hex[:8]}{ext}"
            save_path = os.path.join(upload_dir, safe_name)
            public_path = safe_name
            display_name = original_name

            content_bytes = _read_uploaded_file_bytes(file_obj)
            with open(save_path, "wb") as f:
                f.write(content_bytes)

            if ext in IMAGE_EXTENSIONS:
                file_type = "image"
            elif ext in VIDEO_EXTENSIONS:
                file_type = "video"
            else:
                file_type = "file"

            preview_url = _build_file_url(save_path, owner_id)

            logger.info(f"[WebChannel] File uploaded: {original_name} -> {save_path} ({file_type})")

            return json.dumps({
                "status": "success",
                "file_path": save_path,
                "file_name": display_name,
                "file_type": file_type,
                "preview_url": preview_url,
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"[WebChannel] File upload error: {e}", exc_info=True)
            return json.dumps({"status": "error", "message": str(e)})

    def post_message(self, owner_id: Optional[str] = None):
        """
        Handle incoming messages from users via POST request.
        Returns a request_id for tracking this specific request.
        Supports optional attachments (file paths from /upload).
        """
        request_id = None
        try:
            data = web.data() or b"{}"
            if len(data) > 1024 * 1024:
                raise ValueError("message request too large")
            json_data = json.loads(data)
            if not isinstance(json_data, dict):
                raise ValueError("message request must be an object")
            session_id = json_data.get(
                "session_id", f"session_{uuid.uuid4()}"
            )
            if not isinstance(session_id, str):
                raise ValueError("invalid session_id")
            session_id = session_id.strip()
            prompt = json_data.get("message", "")
            if not isinstance(prompt, str):
                raise ValueError("message must be a string")
            use_sse = json_data.get("stream", True)
            if not isinstance(use_sse, bool):
                raise ValueError("stream must be a boolean")
            attachments = json_data.get("attachments", []) or []
            if not isinstance(attachments, list) or len(attachments) > 64:
                raise ValueError("invalid attachments")
            # Tag the message as originating from voice input so the post-reply
            # TTS hook can honour the `voice_if_voice` policy (mirrors the
            # desire_rtype concept used by other channels).
            is_voice_input = bool(json_data.get('is_voice', False))

            # Fast path for /cancel: bypass the session queue and SSE setup.
            # Web frontend (stream=true) only listens to SSE, so we return an
            # inline_reply payload to be rendered synchronously.
            stripped_prompt = (prompt or "").strip().lower()
            if stripped_prompt == "/cancel":
                if owner_id is not None:
                    _require_web_session(session_id, owner_id)
                from agent.protocol import get_cancel_registry
                cancelled = get_cancel_registry().cancel_session(session_id)
                lang = (json_data.get('lang') or 'zh').lower()
                msg_text = _cancel_reply_text(cancelled, lang)
                logger.info(
                    f"[WebChannel] /cancel fast-path: session={session_id}, cancelled={cancelled}, lang={lang}"
                )
                return json.dumps({
                    "status": "success",
                    "request_id": "",
                    "stream": False,
                    "inline_reply": msg_text,
                })

            # Explicit steering also bypasses the normal session queue. The
            # Web button sends ``steer: true`` with raw input; typed /steer
            # commands use the same endpoint and semantics as IM channels.
            steer_requested = bool(json_data.get("steer", False))
            is_steer_command = (
                re.match(r"^/steer(?:\s|$)", stripped_prompt) is not None
            )
            if steer_requested or is_steer_command:
                if owner_id is not None:
                    _require_web_session(session_id, owner_id)
                instruction = (
                    (prompt or "").strip()[len("/steer"):].strip()
                    if is_steer_command
                    else (prompt or "").strip()
                )
                from bridge.bridge import Bridge
                result = Bridge().get_agent_bridge().steer_session(
                    session_id, instruction
                )
                lang = (json_data.get("lang") or "zh").lower()
                msg_text = _steer_reply_text(result.status, lang)
                logger.info(
                    f"[WebChannel] steer fast-path: session={session_id}, "
                    f"status={result.status.value}, lang={lang}"
                )
                return json.dumps({
                    "status": "success",
                    "request_id": "",
                    "stream": False,
                    "steered": result.accepted,
                    "inline_reply": msg_text,
                }, ensure_ascii=False)

            if owner_id is not None:
                _claim_web_session(session_id, owner_id)

            # Append file references to the prompt (same format as QQ channel)
            if attachments:
                file_refs = []
                for att in attachments:
                    if not isinstance(att, dict):
                        raise ValueError("invalid attachment")
                    ftype = att.get("file_type", "file")
                    fpath = att.get("file_path", "")
                    if not fpath:
                        continue
                    if ftype == "workspace_ref":
                        # Already lives in the workspace (dragged from the file panel
                        # or picked with @); reference it in place so the agent opens
                        # the original instead of an uploaded copy. Naming the kind
                        # tells the agent whether to `read` it or `ls` into it.
                        is_dir = os.path.isdir(
                            os.path.join(_get_workspace_root(), fpath)
                        )
                        label = (
                            i18n.t('工作空间目录', 'Workspace directory') if is_dir
                            else i18n.t('工作空间文件', 'Workspace file')
                        )
                        file_refs.append(f"[{label}: {fpath}]")
                    elif ftype == "image":
                        file_refs.append(f"[{i18n.t('图片', 'Image')}: {fpath}]")
                    elif ftype == "video":
                        file_refs.append(f"[{i18n.t('视频', 'Video')}: {fpath}]")
                    elif ftype == "directory":
                        file_refs.append(f"[{i18n.t('目录', 'Directory')}: {fpath}]")
                    else:
                        file_refs.append(f"[{i18n.t('文件', 'File')}: {fpath}]")
                if file_refs:
                    prompt = prompt + "\n" + "\n".join(file_refs)
                    logger.info(f"[WebChannel] Attached {len(file_refs)} file(s) to message")

            request_id = self._generate_request_id()
            self.request_to_session[request_id] = session_id
            if owner_id is not None:
                self.request_owners[request_id] = owner_id

            if session_id not in self.session_queues:
                self.session_queues[session_id] = Queue()

            if use_sse:
                if not owner_id:
                    # All HTTP entry points provide an authenticated principal.
                    # Refuse a direct/legacy caller rather than start a stream
                    # whose durable owner cannot be verified on recovery.
                    raise PermissionError("SSE request owner is required")
                durable_store = _get_durable_sse_store()
                durable_store.begin(request_id, owner_id, session_id)
                self.sse_queues[request_id] = _SSEEventJournal(
                    lambda event_id, payload: durable_store.append(
                        request_id, event_id, payload
                    )
                )
                self.sse_last_active[request_id] = time.time()

            trigger_prefixs = conf().get("single_chat_prefix", [""])
            if check_prefix(prompt, trigger_prefixs) is None:
                if trigger_prefixs:
                    prompt = trigger_prefixs[0] + prompt
                    logger.debug(f"[WebChannel] Added prefix to message: {prompt}")

            msg = WebMessage(self._generate_msg_id(), prompt)
            msg.from_user_id = session_id

            context = self._compose_context(ContextType.TEXT, prompt, msg=msg, isgroup=False)

            if context is None:
                logger.warning(f"[WebChannel] Context is None for session {session_id}, message may be filtered")
                self._drop_sse_request(request_id)
                return json.dumps({"status": "error", "message": "Message was filtered"})

            context["session_id"] = session_id
            context["receiver"] = session_id
            if owner_id is not None:
                context["session_owner_id"] = owner_id
                context["trusted_identity"] = _web_identity(owner_id)
            context["request_id"] = request_id
            if is_voice_input:
                # Web channel runs its own TTS post-pipeline via
                # _maybe_dispatch_auto_tts; don't set desire_rtype here or
                # chat_channel would synthesize a duplicate VOICE reply.
                context["is_voice_input"] = True

            if use_sse:
                context["on_event"] = self._make_sse_callback(request_id)

            threading.Thread(target=self.produce, args=(context,)).start()

            return json.dumps({"status": "success", "request_id": request_id, "stream": use_sse})

        except Exception as e:
            if request_id:
                self._drop_sse_request(request_id)
            logger.error(f"Error processing message: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _drop_sse_request(self, request_id: str):
        """Reclaim all state tied to an SSE request to prevent fd/memory leaks.

        Removing the queue lets the WSGI generator and its socket be released,
        and dropping request_to_session avoids unbounded map growth.
        """
        self.sse_queues.pop(request_id, None)
        self.sse_last_active.pop(request_id, None)
        self.request_to_session.pop(request_id, None)
        getattr(self, "request_owners", {}).pop(request_id, None)
        lock = getattr(self, "_sse_stream_lock", None)
        generations = getattr(self, "_sse_stream_generations", None)
        if lock is not None and generations is not None:
            with lock:
                generations.pop(request_id, None)

    def _recover_sse_request(
        self, request_id: str, owner_id: Optional[str]
    ) -> bool:
        """Hydrate an owner-checked durable SSE prefix after a process loss.

        A restarted process cannot honestly resume the old agent worker. For a
        non-terminal durable run it therefore appends an in-memory terminal
        `unconfirmed` error after the durable prefix. This preserves every
        committed event while preventing a reconnect from displaying success.
        """

        if not request_id or not owner_id:
            return False
        try:
            replay = _get_durable_sse_store().replay(request_id, owner_id)
        except Exception as exc:
            logger.warning(
                f"[WebChannel] durable SSE recovery unavailable for {request_id}: {exc}"
            )
            return False
        if replay is None:
            return False
        journal = _SSEEventJournal()
        try:
            journal.restore(replay["events"])
        except ValueError as exc:
            logger.warning(
                f"[WebChannel] durable SSE recovery rejected for {request_id}: {exc}"
            )
            return False
        if replay["state"] != "completed":
            journal.put({
                "type": "error",
                "message": (
                    "SSE worker was interrupted; persisted events were replayed "
                    "but task result is unconfirmed"
                ),
                "request_id": request_id,
                "recovered": True,
            })
        self.request_to_session[request_id] = str(replay["session_id"])
        self.request_owners[request_id] = str(replay["owner_id"])
        self.sse_queues[request_id] = journal
        self.sse_last_active[request_id] = time.time()
        return True

    def _start_sse_janitor(self):
        """Start a background thread that reclaims orphaned SSE queues.

        When a client disconnects before the "done" event arrives (browser
        closed, session switched, network drop), the generator may keep the
        queue around to allow reconnection. Without a sweep these orphans
        accumulate, leaking file descriptors until cheroot raises
        "[Errno 24] Too many open files".

        Reclamation is based on idle time, not total age: an active stream
        refreshes ``sse_last_active`` every second while its generator is being
        consumed, so a long-running reply (even hours long) is never killed
        while the client stays connected. Only queues that stopped refreshing
        (client gone) past SSE_IDLE_TIMEOUT are reclaimed.
        """
        if self._sse_janitor_started:
            return
        self._sse_janitor_started = True

        SSE_IDLE_TIMEOUT = 1800  # 30 minutes with no client consumption
        SWEEP_INTERVAL = 60

        def _sweep():
            while True:
                time.sleep(SWEEP_INTERVAL)
                try:
                    now = time.time()
                    stale = [
                        rid for rid, ts in list(self.sse_last_active.items())
                        if now - ts > SSE_IDLE_TIMEOUT
                    ]
                    for rid in stale:
                        self._drop_sse_request(rid)
                    try:
                        _get_durable_sse_store().reap(now=now)
                    except Exception as durable_exc:
                        logger.warning(
                            f"[WebChannel] durable SSE journal reap failed: {durable_exc}"
                        )
                    if stale:
                        logger.info(
                            f"[WebChannel] SSE janitor reclaimed {len(stale)} "
                            f"idle stream(s)"
                        )
                except Exception as e:
                    logger.warning(f"[WebChannel] SSE janitor error: {e}")

        t = threading.Thread(target=_sweep, name="sse-janitor", daemon=True)
        t.start()

    def stream_response(
        self,
        request_id: str,
        owner_id: Optional[str] = None,
        after_event_id: int = 0,
    ):
        """
        SSE generator for a given request_id.
        Yields UTF-8 encoded bytes to avoid WSGI Latin-1 mangling.
        Supports client reconnection with a monotonic cursor. Production
        streams replay every event after ``after_event_id`` from the per-run
        journal; a reconnect cannot steal queued events from another WSGI
        generator. Plain ``Queue`` remains supported for focused unit tests.
        """
        if request_id not in self.sse_queues:
            recover = getattr(self, "_recover_sse_request", None)
            if not callable(recover) or not recover(request_id, owner_id):
                yield b"data: {\"type\": \"error\", \"message\": \"invalid request_id\"}\n\n"
                return
        if owner_id is not None and self.request_owners.get(request_id) != owner_id:
            yield b"data: {\"type\": \"error\", \"message\": \"invalid request_id\"}\n\n"
            return

        q = self.sse_queues[request_id]
        journal = isinstance(q, _SSEEventJournal)
        cursor = _parse_sse_event_cursor(after_event_id)
        stream_lock = getattr(self, "_sse_stream_lock", None)
        stream_generations = getattr(self, "_sse_stream_generations", None)
        connection_id = uuid.uuid4().hex
        if stream_lock is not None and stream_generations is not None:
            with stream_lock:
                # One active WSGI generator per request. A fresh ticketed
                # reconnect supersedes a stuck predecessor; the journal keeps
                # the cursor safe even in the handover window.
                stream_generations[request_id] = connection_id

        def owns_connection() -> bool:
            if stream_lock is None or stream_generations is None:
                return True
            with stream_lock:
                return stream_generations.get(request_id) == connection_id
        idle_timeout = 600  # 10 minutes without any real event
        deadline = time.time() + idle_timeout
        # After the main reply is done we keep the stream open for a short
        # tail so async post-processing (TTS auto-synthesis) can deliver a
        # `voice_attach` event before the client disconnects.
        POST_DONE_TAIL_SECONDS = 60
        # A cancel only takes effect at the agent's next checkpoint, so the run
        # keeps emitting events (tool results, the partial reply) for a while
        # after the user presses Stop. Stay open for them, just not for the
        # full idle timeout.
        CANCEL_GRACE_SECONDS = 60
        POST_CANCEL_TAIL_SECONDS = 3
        post_done = False
        post_deadline = 0.0
        cancelled = False

        try:
            while time.time() < deadline:
                if not owns_connection():
                    return
                # Mark the stream alive on every loop. While the client keeps
                # consuming, the generator runs and refreshes this, so the
                # janitor won't reclaim a long-running but active stream.
                self.sse_last_active[request_id] = time.time()
                try:
                    if journal:
                        entry = q.read_after(cursor, timeout=1)
                        if entry is None:
                            raise Empty
                        cursor, item = entry
                    else:
                        item = q.get(timeout=1)
                except Empty:
                    if post_done and time.time() >= post_deadline:
                        break
                    yield b": keepalive\n\n"
                    continue

                if not owns_connection():
                    return
                deadline = time.time() + (
                    CANCEL_GRACE_SECONDS if cancelled else idle_timeout
                )
                payload = json.dumps(item, ensure_ascii=False, allow_nan=False)
                if journal:
                    yield f"id: {cursor}\ndata: {payload}\n\n".encode("utf-8")
                else:
                    yield f"data: {payload}\n\n".encode("utf-8")

                itype = item.get("type")
                if itype == "done":
                    post_done = True
                    post_deadline = time.time() + (
                        POST_CANCEL_TAIL_SECONDS if cancelled
                        else POST_DONE_TAIL_SECONDS
                    )
                elif itype == "cancelled":
                    # Wait for the run to actually wind down and send its
                    # partial reply as "done"; closing on a blind timer here
                    # strands in-flight tool bubbles and makes the client
                    # reconnect onto a dropped queue.
                    cancelled = True
                    deadline = time.time() + CANCEL_GRACE_SECONDS
                elif itype == "voice_attach":
                    # WSGI buffers the previous chunk until the next yield;
                    # shrink the tail so the generator wakes up quickly to
                    # emit a couple of keepalive comments that push the
                    # voice_attach payload through to the browser.
                    post_done = True
                    post_deadline = time.time() + 2  # 2s post-attach tail
                elif itype == "error":
                    # An explicit transport error is terminal. Do not retain
                    # its journal until the generic idle timeout after the
                    # renderer has closed the EventSource.
                    post_done = True
                    post_deadline = time.time()
        except GeneratorExit:
            # Client disconnected (WSGI closed the generator). If the reply is
            # already complete there is nothing to resume, so reclaim now to
            # release the socket fd. Otherwise keep the queue briefly so a
            # reconnect with the same request_id can resume; the janitor will
            # reclaim it if no reconnect happens.
            if post_done and owns_connection():
                self._drop_sse_request(request_id)
            raise
        finally:
            # Drop the queue once the reply is actually complete or the idle
            # deadline has passed. Early client disconnects are handled by the
            # GeneratorExit branch above and the background janitor.
            if owns_connection() and (post_done or time.time() >= deadline):
                self._drop_sse_request(request_id)

    def cancel_request(self, owner_id: Optional[str] = None):
        """
        Cancel an in-flight agent run.

        Body: {"request_id": "...", "session_id": "..."}
        Either field is sufficient; request_id is preferred when known.
        Always returns success even when nothing was running, so the
        client's UX is idempotent.
        """
        try:
            from agent.protocol import get_cancel_registry

            data = web.data()
            try:
                json_data = json.loads(data) if data else {}
            except Exception:
                json_data = {}

            request_id = (json_data.get("request_id") or "").strip()
            session_id = (json_data.get("session_id") or "").strip()
            lang = (json_data.get("lang") or "zh").lower()

            if owner_id is not None:
                if request_id and self.request_owners.get(request_id) != owner_id:
                    raise PermissionError("request not found")
                if session_id:
                    _require_web_session(session_id, owner_id)

            registry = get_cancel_registry()
            cancelled = 0

            if request_id:
                if owner_id is not None:
                    if registry.cancel_request_owned(request_id, owner_id):
                        cancelled = 1
                elif registry.cancel_request(request_id):
                    cancelled = 1

            if cancelled == 0 and session_id:
                cancelled = (
                    registry.cancel_session_owned(session_id, owner_id)
                    if owner_id is not None
                    else registry.cancel_session(session_id)
                )

            if request_id and request_id in self.sse_queues:
                self.sse_queues[request_id].put({
                    "type": "cancelled",
                    "content": "🛑 Cancelled" if lang.startswith("en") else "🛑 已中止",
                    "request_id": request_id,
                    "timestamp": time.time(),
                })

            logger.info(
                f"[WebChannel] cancel request: request_id={request_id!r}, "
                f"session_id={session_id!r}, cancelled={cancelled}"
            )
            return json.dumps({
                "status": "success",
                "cancelled": cancelled,
            })

        except Exception as e:
            logger.error(f"[WebChannel] cancel_request error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def poll_response(self, owner_id: Optional[str] = None):
        """
        Poll for responses using the session_id.
        """
        try:
            data = web.data()
            json_data = json.loads(data)
            session_id = json_data.get('session_id')
            if owner_id is not None and session_id:
                _require_web_session(session_id, owner_id)

            if not session_id or session_id not in self.session_queues:
                return json.dumps({"status": "error", "message": "Invalid session ID"})

            # 尝试从队列获取响应，不等待
            try:
                # 使用peek而不是get，这样如果前端没有成功处理，下次还能获取到
                response = self.session_queues[session_id].get(block=False)

                # 返回响应，包含请求ID以区分不同请求
                return json.dumps({
                    "status": "success",
                    "has_content": True,
                    "content": response["content"],
                    "request_id": response["request_id"],
                    "timestamp": response["timestamp"]
                })

            except Empty:
                # 没有新响应
                return json.dumps({"status": "success", "has_content": False})

        except Exception as e:
            logger.error(f"Error polling response: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def chat_page(self):
        """Serve the chat HTML page."""
        file_path = os.path.join(os.path.dirname(__file__), 'chat.html')  # 使用绝对路径
        with open(file_path, 'r', encoding='utf-8') as f:
            html = f.read()
        # Inject the backend-resolved default language so the console can use
        # it on first load (when the user has no saved cow_lang preference).
        return html.replace("{{COW_DEFAULT_LANG}}", i18n.get_language())

    def startup(self):
        configured_host = conf().get("web_host", "")
        host, is_public_bind = _resolve_web_bind_host(configured_host)
        # The desktop app passes its chosen port via COW_WEB_PORT so its backend
        # never collides with a source-run web console (default 9899). This makes
        # the port a single source of truth owned by the Electron shell.
        port = int(os.environ.get("COW_WEB_PORT") or conf().get("web_port", 9899))

        self._cleanup_stale_voice_recordings()

        # Print available channel types (ordered by language: prioritize
        # locally-popular channels for the current UI language)
        logger.info(
            "[WebChannel] Available channels (edit `channel_type` in config.json to switch, separate multiple with commas):")
        zh_channels = [
            ("web", "Web"),
            ("terminal", "Terminal"),
            ("weixin", "WeChat"),
            ("feishu", "Feishu"),
            ("dingtalk", "DingTalk"),
            ("wecom_bot", "WeCom Bot"),
            ("wechatcom_app", "WeCom App"),
            ("wechat_kf", "WeChat Customer Service"),
            ("wechatmp", "WeChat Official Account"),
            ("wechatmp_service", "WeChat Official Account (Service)"),
            ("telegram", "Telegram"),
            ("slack", "Slack"),
            ("discord", "Discord"),
        ]
        en_channels = [
            ("web", "Web"),
            ("terminal", "Terminal"),
            ("telegram", "Telegram"),
            ("slack", "Slack"),
            ("discord", "Discord"),
            ("weixin", "WeChat"),
            ("feishu", "Feishu"),
            ("dingtalk", "DingTalk"),
            ("wecom_bot", "WeCom Bot"),
            ("wechatcom_app", "WeCom App"),
            ("wechat_kf", "WeChat Customer Service"),
            ("wechatmp", "WeChat Official Account"),
            ("wechatmp_service", "WeChat Official Account (Service)"),
        ]
        channels = en_channels if i18n.get_language() == "en" else zh_channels
        name_width = max(len(name) for name, _ in channels)
        for idx, (name, label) in enumerate(channels, 1):
            logger.info(f"[WebChannel]  {idx:>2}. {name:<{name_width}} - {label}")
        logger.info("[WebChannel] ✅ Web console is running")
        logger.info(f"[WebChannel] 🌐 Local access: http://localhost:{port}")
        logger.info(
            f"[WebChannel] ?? Listening on {host} only. For remote access, "
            "terminate TLS in a trusted reverse proxy on the same host and "
            "forward to this loopback listener."
        )

        # In desktop mode the Electron shell renders the UI, so don't pop a
        # browser window (also avoids issues when running detached/headless).
        if os.environ.get("COW_DESKTOP") != "1":
            try:
                import webbrowser
                webbrowser.open(f"http://localhost:{port}")
                logger.debug(f"[WebChannel] Opened browser at http://localhost:{port}")
            except Exception as e:
                logger.debug(f"[WebChannel] Could not open browser: {e}")

        # Ensure the static dir exists. In a packaged build it ships read-only
        # inside the bundle, so swallow errors instead of failing startup.
        static_dir = os.path.join(os.path.dirname(__file__), 'static')
        if not os.path.exists(static_dir):
            try:
                os.makedirs(static_dir)
                logger.debug(f"[WebChannel] Created static directory: {static_dir}")
            except OSError as e:
                logger.debug(f"[WebChannel] Skipped creating static dir (read-only bundle?): {e}")

        urls = (
            '/', 'RootHandler',
            '/api/health', 'HealthHandler',
            '/api/readiness', 'ReadinessHandler',
            '/api/release/evidence', 'ReleaseEvidenceHandler',
            '/stream/ticket', 'StreamTicketHandler',
            '/api/logs/ticket', 'LogsTicketHandler',
            '/auth/login', 'AuthLoginHandler',
            '/auth/check', 'AuthCheckHandler',
            '/auth/logout', 'AuthLogoutHandler',
            '/message', 'MessageHandler',
            '/upload', 'UploadHandler',
            '/uploads/(.*)', 'UploadsHandler',
            '/file/(.+)', 'FileCapabilityHandler',
            '/api/file', 'FileServeHandler',
            '/preview/(.+)', 'PreviewHandler',
            '/api/workspace/tree', 'WorkspaceTreeHandler',
            '/api/workspace/search', 'WorkspaceSearchHandler',
            '/api/workspace/resolve', 'WorkspaceResolveHandler',
            '/api/workspace/meta', 'WorkspaceMetaHandler',
            '/api/voice/asr', 'VoiceAsrHandler',
            '/api/voice/tts', 'VoiceTtsHandler',
            '/poll', 'PollHandler',
            '/stream', 'StreamHandler',
            '/cancel', 'CancelHandler',
            '/chat', 'ChatHandler',
            '/config', 'ConfigHandler',
            '/api/models', 'ModelsHandler',
            '/api/channels', 'ChannelsHandler',
            '/api/weixin/qrlogin', 'WeixinQrHandler',
            '/api/feishu/register', 'FeishuRegisterHandler',
            '/api/tools', 'ToolsHandler',
            '/api/skills', 'SkillsHandler',
            '/api/memory', 'MemoryHandler',
            '/api/memory/content', 'MemoryContentHandler',
            '/api/knowledge/list', 'KnowledgeListHandler',
            '/api/knowledge/read', 'KnowledgeReadHandler',
            '/api/knowledge/citation/resolve', 'KnowledgeCitationResolveHandler',
            '/api/knowledge/graph', 'KnowledgeGraphHandler',
            '/api/knowledge/action', 'KnowledgeActionHandler',
            '/api/knowledge/import', 'KnowledgeImportHandler',
            '/api/scheduler', 'SchedulerHandler',
            '/api/scheduler/run', 'SchedulerRunHandler',
            '/api/scheduler/toggle', 'SchedulerToggleHandler',
            '/api/scheduler/update', 'SchedulerUpdateHandler',
            '/api/scheduler/delete', 'SchedulerDeleteHandler',
            '/api/sessions', 'SessionsHandler',
            '/api/sessions/(.*)/generate_title', 'SessionTitleHandler',
            '/api/prompt/optimize', 'PromptOptimizeHandler',
            '/api/sessions/(.*)/clear_context', 'SessionClearContextHandler',
            '/api/sessions/(.*)', 'SessionDetailHandler',
            '/api/history', 'HistoryHandler',
            '/api/messages/delete', 'MessageDeleteHandler',
            '/api/logs', 'LogsHandler',
            '/api/version', 'VersionHandler',
            '/mcp/oauth/callback', 'McpOAuthCallbackHandler',
            '/assets/(.*)', 'AssetsHandler',
        )
        app = web.application(urls, globals(), autoreload=False)

        # 完全禁用web.py的HTTP日志输出
        web.httpserver.LogMiddleware.log = lambda self, status, environ: None

        # 配置web.py的日志级别为ERROR
        logging.getLogger("web").setLevel(logging.ERROR)
        logging.getLogger("web.httpserver").setLevel(logging.ERROR)

        # Build WSGI app with middleware (same as runsimple but without print)
        func = web.httpserver.StaticMiddleware(app.wsgifunc())
        func = web.httpserver.LogMiddleware(func)
        server = web.httpserver.WSGIServer((host, port), func)
        server.daemon_threads = True
        # Default request_queue_size(5) / timeout(10s) / numthreads(10) are
        # too small: when SSE streams occupy many threads, the backlog fills
        # and new connections get refused (ERR_CONNECTION_ABORTED).
        server.request_queue_size = 128
        server.timeout = 300
        server.requests.min = 20
        server.requests.max = 80
        self._http_server = server
        # Reclaim orphaned SSE queues so disconnected clients don't leak fds.
        self._start_sse_janitor()
        try:
            server.start()
        except (KeyboardInterrupt, SystemExit):
            server.stop()
        except OSError as e:
            if e.errno in (48, 98):  # macOS/Linux EADDRINUSE
                logger.error(
                    f"[WebChannel] 端口 {port} 已被占用，可执行 `cow restart` 清理残留进程，"
                    f"或在 config.json 中修改 web_port"
                )
            raise

    def stop(self):
        if self._http_server:
            try:
                self._http_server.stop()
                logger.info("[WebChannel] HTTP server stopped")
            except Exception as e:
                logger.warning(f"[WebChannel] Error stopping HTTP server: {e}")
            self._http_server = None


class RootHandler:
    def GET(self):
        raise web.seeother('/chat')


class HealthHandler:
    # Unauthenticated liveness probe. The desktop shell polls this to know the
    # backend is up; it must never require auth (a set web_password would
    # otherwise make startup hang). Returns no sensitive data.
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        return json.dumps({"status": "ok"})


class ReadinessHandler:
    """Dependency-aware readiness probe; liveness remains /api/health."""

    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        checks = {
            "conversation_store": False,
            "workspace_writable": False,
            "disk_space": False,
            "queues_bounded": False,
        }
        try:
            from agent.memory import get_conversation_store
            checks["conversation_store"] = bool(
                get_conversation_store().healthcheck()
            )
        except Exception:
            checks["conversation_store"] = False
        try:
            workspace = _get_workspace_root()
            os.makedirs(workspace, exist_ok=True)
            checks["workspace_writable"] = os.access(workspace, os.W_OK)
            minimum_free = max(
                int(conf().get("web_readiness_min_free_bytes", 100 * 1024 * 1024)),
                1,
            )
            checks["disk_space"] = shutil.disk_usage(workspace).free >= minimum_free
        except Exception:
            checks["workspace_writable"] = False
            checks["disk_space"] = False
        try:
            channel = WebChannel()
            max_queues = max(int(conf().get("web_readiness_max_queues", 10000)), 1)
            checks["queues_bounded"] = (
                len(channel.sse_queues) <= max_queues
                and len(channel.session_queues) <= max_queues
            )
        except Exception:
            checks["queues_bounded"] = False
        ready = all(checks.values())
        if not ready:
            try:
                web.ctx.status = "503 Service Unavailable"
            except Exception:
                pass
        return json.dumps({
            "status": "ready" if ready else "not_ready",
            "checks": checks,
        }, ensure_ascii=False)


class ReleaseEvidenceHandler:
    """Read-only delivery evidence view; it can never create a PASS claim."""

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        root = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', '..'))
        path = os.path.join(root, 'benchmarks', 'results', 'release-evidence-manifest.json')
        if not os.path.isfile(path):
            return json.dumps({
                "status": "not_available",
                "passed": False,
                "hard_denials": {"FDE_CASE_EVIDENCE": "ABSENT"},
                "message": "release evidence manifest is absent",
            }, ensure_ascii=False)
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                manifest = json.load(handle)
            from benchmarks.evidence.release_manifest import verify_manifest
            verification = verify_manifest(manifest, Path(root))
            if verification.get('integrity_passed') is not True:
                return json.dumps({
                    "status": "invalid_evidence",
                    "passed": False,
                    "hard_denials": {"FDE_CASE_EVIDENCE": "ABSENT"},
                    "message": "release evidence integrity verification failed",
                    "verification": {
                        "passed": False,
                        "checks": verification.get('checks', []),
                    },
                }, ensure_ascii=False)
            reports = {
                name: {
                    key: item.get(key)
                    for key in ('status', 'passed', 'fresh', 'limitations')
                    if key in item
                }
                for name, item in manifest.get('reports', {}).items()
                if isinstance(item, dict)
            }
            return json.dumps({
                "status": "completed",
                "passed": bool(manifest.get('passed') is True and verification.get('passed') is True),
                "hard_denials": manifest.get('hard_denials', {}),
                "required_conditions": manifest.get('required_conditions', {}),
                "reports": reports,
                "verification": {
                    "passed": bool(verification.get('passed') is True),
                    "integrity_passed": True,
                    "checks": verification.get('checks', []),
                },
            }, ensure_ascii=False)
        except Exception as exc:
            logger.exception(f"[ReleaseEvidenceHandler] evidence read failed: {exc}")
            return json.dumps({
                "status": "invalid_evidence",
                "passed": False,
                "hard_denials": {"FDE_CASE_EVIDENCE": "ABSENT"},
                "message": "release evidence could not be verified",
            }, ensure_ascii=False)


class McpOAuthCallbackHandler:
    """OAuth redirect target for MCP servers requiring authorization.

    The browser lands here after the user authorizes a remote MCP server.
    We exchange the authorization code for tokens and bring the server
    online. Unauthenticated by design: the OAuth `state` param is the
    single-use secret that binds this request to a pending authorization.
    """

    def GET(self):
        web.header('Content-Type', 'text/html; charset=utf-8')
        params = web.input(code="", state="", error="", error_description="")

        def _page(title: str, message: str) -> str:
            return (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>{title}</title></head>"
                "<body style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
                "max-width:520px;margin:64px auto;padding:0 20px;text-align:center;color:#1f2328'>"
                f"<h2>{title}</h2><p style='color:#57606a'>{message}</p></body></html>"
            )

        if params.error:
            logger.warning(f"[MCP-OAuth] callback error: {params.error} {params.error_description}")
            return _page("授权失败", f"{params.error}: {params.error_description or ''}")

        if not params.code or not params.state:
            return _page("参数缺失", "回调缺少 code 或 state 参数。")

        try:
            from agent.tools.mcp.mcp_oauth import pop_pending
            from agent.tools.mcp.mcp_client import notify_server_authorized
        except Exception as e:
            logger.warning(f"[MCP-OAuth] callback import failed: {e}")
            return _page("内部错误", "OAuth 模块不可用。")

        handler = pop_pending(params.state)
        if handler is None:
            return _page("会话已过期", "授权请求不存在或已过期，请重新触发授权。")

        try:
            ok = handler.finish_authorization(params.code)
        except Exception as e:
            logger.warning(f"[MCP-OAuth] token exchange crashed: {e}")
            ok = False

        if not ok:
            return _page("授权失败", "换取令牌失败，请重试。")

        notify_server_authorized(handler.server_name)
        logger.info(f"[MCP-OAuth] Server '{handler.server_name}' authorized via web callback")
        return _page(
            "授权成功",
            f"MCP 服务 “{handler.server_name}” 已授权，可以返回聊天继续使用了。",
        )


class AuthCheckHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        if not _is_password_enabled():
            return json.dumps({"status": "success", "auth_required": False})
        if _check_auth():
            return json.dumps({"status": "success", "auth_required": True, "authenticated": True})
        return json.dumps({"status": "success", "auth_required": True, "authenticated": False})


class AuthLoginHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        if not _is_password_enabled():
            return json.dumps({"status": "success"})
        client_key = _login_client_key()
        if not _login_attempt_allowed(client_key):
            try:
                web.ctx.status = "429 Too Many Requests"
            except Exception:
                pass
            return json.dumps({"status": "error", "message": "Too many login attempts"})
        try:
            raw = web.data() or b"{}"
            if len(raw) > 4096:
                raise ValueError("request too large")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("object required")
        except Exception:
            return json.dumps({"status": "error", "message": "Invalid request"})
        password = str(data.get("password", "") or "")
        expected = _get_web_password()
        if not hmac.compare_digest(password, expected):
            _record_login_failure(client_key)
            logger.warning("[WebChannel] Invalid login attempt")
            return json.dumps({"status": "error", "message": "Wrong password"})

        _clear_login_failures(client_key)

        # Reuse only a server-signed device subject. Raw client identity fields
        # are never accepted, so guessing another owner cannot claim sessions.
        subject_token = str(data.get("subject_token", "") or "")
        if not subject_token:
            try:
                subject_token = web.cookies().get("cow_auth_subject", "") or ""
            except Exception:
                subject_token = ""
        subject_id = _verify_auth_subject_token(subject_token) or uuid.uuid4().hex
        subject_token = _create_auth_subject_token(subject_id)
        token = _create_auth_token(subject_id)
        web.setcookie(
            "cow_auth_token", token, expires=_session_expire_seconds(),
            path="/", httponly=True, samesite="Lax",
        )
        web.setcookie(
            "cow_auth_subject", subject_token, expires=10 * 365 * 86400,
            path="/", httponly=True, samesite="Lax",
        )
        # Desktop file:// clients persist both signed capabilities and send the
        # auth token via Authorization. The subject token is useful only after
        # the password is presented again; it is not accepted as authentication.
        return json.dumps({
            "status": "success",
            "token": token,
            "subject_token": subject_token,
        })


class AuthLogoutHandler:
    def POST(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        if _is_password_enabled():
            _require_auth()
            _revoke_request_auth_token()
        web.setcookie("cow_auth_token", "", expires=-1, path="/")
        # Keep the signed subject cookie so a later password-authenticated login
        # on the same device can recover its own history without changing owner.
        return json.dumps({"status": "success"})


class MessageHandler:
    def POST(self):
        owner_id = _require_auth()
        return WebChannel().post_message(owner_id=owner_id)


class UploadHandler:
    def POST(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        return WebChannel().upload_file(owner_id=owner_id)


class VoiceAsrHandler:
    """Receive a mic recording, persist it under uploads/ and run ASR.
    Returns {status, text, audio_url} so the UI can render a playback bubble."""
    def POST(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')

        saved_path = None
        try:
            params = _raw_web_input()
            file_obj = params.get("file")
            if file_obj is None:
                return json.dumps({"status": "error", "message": "no audio file"})

            filename = getattr(file_obj, "filename", "") or "recording.webm"
            ext = os.path.splitext(filename)[1].lower() or ".webm"
            if ext not in (".webm", ".ogg", ".opus", ".mp4", ".m4a", ".mp3", ".wav"):
                ext = ".webm"

            upload_dir = _owner_upload_dir(owner_id)
            owner_component = os.path.basename(upload_dir)
            ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            saved_name = f"voice_input_{ts}_{random.randint(0, 9999)}{ext}"
            saved_path = os.path.join(upload_dir, saved_name)
            with open(saved_path, "wb") as f:
                f.write(file_obj.file.read() if hasattr(file_obj, "file") else file_obj.value)

            audio_url = _build_file_url(saved_path, owner_id)

            from bridge.bridge import Bridge
            reply = Bridge().fetch_voice_to_text(saved_path)
            if reply is None:
                return json.dumps({
                    "status": "error",
                    "message": "ASR returned no reply",
                    "audio_url": audio_url,
                })

            from bridge.reply import ReplyType
            if reply.type == ReplyType.TEXT:
                return json.dumps({
                    "status": "success",
                    "text": reply.content or "",
                    "audio_url": audio_url,
                })
            return json.dumps({
                "status": "error",
                "message": reply.content or "ASR failed",
                "audio_url": audio_url,
            })
        except Exception as e:
            logger.exception(f"[VoiceAsrHandler] failed: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class VoiceTtsHandler:
    """On-demand TTS for the in-chat "read aloud" button. Returns the
    audio URL and (when session_id is given) persists it onto the message."""
    def POST(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            data = json.loads(web.data() or b"{}")
            text = (data.get("text") or "").strip()
            session_id = (data.get("session_id") or "").strip()
            if not text:
                return json.dumps({"status": "error", "message": "empty text"})
            if session_id:
                _require_web_session(session_id, owner_id)
            # `@singleton` makes WebChannel a factory function — go via instance.
            channel = WebChannel()
            if not channel._tts_provider_ready():
                return json.dumps({"status": "error", "message": "tts not configured"})

            from bridge.bridge import Bridge
            reply = Bridge().fetch_text_to_voice(text)
            if reply is None or reply.type != ReplyType.VOICE or not reply.content:
                msg = getattr(reply, "content", "") or "tts failed"
                return json.dumps({"status": "error", "message": str(msg)})

            url = channel._publish_tts_audio(
                reply.content, owner_id=owner_id
            )
            if not url:
                return json.dumps({"status": "error", "message": "publish failed"})

            if session_id:
                try:
                    from agent.memory import get_conversation_store
                    get_conversation_store().attach_extras_to_last_assistant(
                        session_id,
                        {"audio": {"url": url, "kind": "tts"}},
                        owner_id=owner_id,
                    )
                except Exception as e:
                    logger.debug(f"[VoiceTtsHandler] persist skipped: {e}")

            return json.dumps({"status": "success", "audio_url": url})
        except Exception as e:
            logger.exception(f"[VoiceTtsHandler] failed: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class UploadsHandler:
    def GET(self, file_name):
        owner_id = _require_auth()
        try:
            upload_dir = _owner_upload_dir(owner_id)
            full_path = os.path.realpath(os.path.join(upload_dir, file_name))
            if not _is_within_directory(upload_dir, full_path):
                raise web.notfound()
            if not os.path.isfile(full_path):
                raise web.notfound()
            content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
            web.header('Content-Type', content_type)
            web.header('Cache-Control', 'private, no-store')
            with open(full_path, 'rb') as f:
                return f.read()
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Error serving upload: {e}")
            raise web.notfound()


class FileCapabilityHandler:
    """Serve a single path authorized only by a short-lived HMAC capability."""

    def GET(self, capability):
        try:
            _require_safe_request_host()
            file_path, owner_id = _decode_file_capability(capability)
            if not _is_path_allowed(file_path):
                raise web.notfound()
            if owner_id is not None and _is_other_owner_upload_path(file_path, owner_id):
                raise web.notfound()
            if not os.path.isfile(file_path):
                raise web.notfound()
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            file_name = os.path.basename(file_path)
            web.header('Content-Type', content_type)
            web.header('Content-Disposition', f"inline; filename*=UTF-8''{quote(file_name)}")
            web.header('Cache-Control', 'private, no-store')
            web.header('Referrer-Policy', 'no-referrer')
            web.header('X-Content-Type-Options', 'nosniff')
            with open(file_path, 'rb') as f:
                return f.read()
        except web.HTTPError:
            raise
        except Exception as e:
            logger.warning(f"[WebChannel] Refused file capability: {e}")
            raise web.notfound()


class FileServeHandler:
    def GET(self):
        owner_id = _require_auth()
        try:
            params = web.input(path="")
            file_path = params.path
            if not file_path or not os.path.isabs(file_path):
                raise web.notfound()
            # Resolve symlinks and confine access to the allowed root dirs,
            # so this endpoint can't be abused to read arbitrary files (e.g. /etc/passwd, ~/.ssh).
            # Defaults to the user home dir plus the agent workspace; set web_file_serve_root="/"
            # to allow the whole filesystem.
            file_path = os.path.realpath(file_path)
            if not _is_path_allowed(file_path):
                raise web.notfound()
            if _is_other_owner_upload_path(file_path, owner_id):
                raise web.notfound()
            if not os.path.isfile(file_path):
                raise web.notfound()
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            file_name = os.path.basename(file_path)
            from urllib.parse import quote
            web.header('Content-Type', content_type)
            web.header('Content-Disposition', f"inline; filename*=UTF-8''{quote(file_name)}")
            web.header('Cache-Control', 'private, no-store')
            with open(file_path, 'rb') as f:
                return f.read()
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Error serving file: {e}")
            raise web.notfound()


class PreviewHandler:
    """
    Directory-mounted file server for the preview panel: /preview/<token>/<relpath>

    Unlike /api/file (single file, query param) this mounts the file's directory,
    so relative assets inside a generated HTML page resolve normally. The token is
    HMAC-signed, which is what authorizes the request - the sandboxed iframe can't
    send the auth cookie.
    """

    def GET(self, path_info):
        try:
            token, _, rel_path = (path_info or "").partition("/")
            if not token or not rel_path:
                raise web.notfound()

            from urllib.parse import unquote
            rel_path = unquote(rel_path)

            try:
                base_dir, capability_owner = _decode_dir_capability(token)
            except ValueError:
                raise web.notfound()

            full_path = os.path.realpath(os.path.join(base_dir, rel_path))
            base_real = os.path.realpath(base_dir)
            # Confine to the mounted directory, then to the globally allowed roots.
            if os.path.commonpath([full_path, base_real]) != base_real:
                raise web.notfound()
            if (
                not _is_path_allowed(full_path)
                or _is_other_owner_upload_path(full_path, capability_owner)
                or not os.path.isfile(full_path)
            ):
                raise web.notfound()

            content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
            web.header('Content-Type', content_type)
            web.header('Cache-Control', 'no-cache')
            web.header('X-Content-Type-Options', 'nosniff')
            if content_type.startswith("text/html"):
                # Agent-generated pages are untrusted. The CSP sandbox forces an
                # opaque origin even when the page is opened as a top-level tab,
                # so it can't read the console's localStorage auth token; the
                # panel's iframe already applies the same flags.
                #
                # No frame-ancestors here: the desktop renderer is loaded from
                # file:// (or the Vite dev server), so 'self' would block its
                # preview iframe outright. The sandbox is what carries the
                # security guarantee; framing alone reveals nothing extra.
                web.header(
                    'Content-Security-Policy',
                    "sandbox allow-scripts allow-popups allow-forms allow-modals",
                )
            with open(full_path, 'rb') as f:
                return f.read()
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Error serving preview: {e}")
            raise web.notfound()


class PollHandler:
    def POST(self):
        owner_id = _require_auth()
        return WebChannel().poll_response(owner_id=owner_id)


class CancelHandler:
    def POST(self):
        owner_id = _require_auth()
        return WebChannel().cancel_request(owner_id=owner_id)


class StreamTicketHandler:
    """Exchange header/cookie auth for a short-lived request-bound SSE ticket."""

    def POST(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            raw = web.data() or b"{}"
            if len(raw) > 4096:
                raise ValueError("request too large")
            data = json.loads(raw)
            request_id = str(data.get("request_id", "") or "")
            after_event_id = _parse_sse_event_cursor(data.get("after_event_id", 0))
        except Exception:
            return json.dumps({"status": "error", "message": "Invalid request"})
        channel = WebChannel()
        if not request_id:
            raise web.notfound()
        if channel.request_owners.get(request_id) != owner_id and not channel._recover_sse_request(
            request_id, owner_id
        ):
            raise web.notfound()
        return json.dumps({
            "status": "success",
            "ticket": _issue_stream_ticket(owner_id, request_id, after_event_id),
            "expires_in": _STREAM_TICKET_TTL_SECONDS,
        })


class LogsTicketHandler:
    """Exchange normal authentication for a one-shot diagnostics SSE ticket."""

    def POST(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        return json.dumps({
            "status": "success",
            "ticket": _issue_log_stream_ticket(owner_id),
            "expires_in": _LOG_STREAM_TICKET_TTL_SECONDS,
        })


class StreamHandler:
    def GET(self):
        params = web.input(request_id='')
        request_id = params.request_id
        if not request_id:
            raise web.badrequest()

        ticket = getattr(params, "ticket", "") or ""
        after_event_id = 0
        if ticket:
            _require_safe_request_host()
            ticket_record = _consume_stream_ticket_record(ticket, request_id)
            if ticket_record is None:
                raise web.HTTPError("401 Unauthorized")
            owner_id = str(ticket_record["owner_id"])
            after_event_id = _parse_sse_event_cursor(ticket_record.get("after_event_id", 0))
        else:
            if getattr(params, "token", ""):
                raise web.HTTPError("401 Unauthorized")
            owner_id = _require_auth()
            after_event_id = _parse_sse_event_cursor(getattr(params, "after_event_id", 0))

        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        web.header('X-Accel-Buffering', 'no')
        web.header('Access-Control-Allow-Origin', '*')

        return WebChannel().stream_response(
            request_id, owner_id=owner_id, after_event_id=after_event_id
        )


class ChatHandler:
    def GET(self):
        web.header('Cache-Control', 'no-cache, no-store, must-revalidate')
        web.header('Pragma', 'no-cache')
        file_path = os.path.join(os.path.dirname(__file__), 'chat.html')
        with open(file_path, 'r', encoding='utf-8') as f:
            html = f.read()
        cache_bust = str(int(time.time()))
        html = html.replace('assets/js/console.js', f'assets/js/console.js?v={cache_bust}')
        html = html.replace('assets/css/console.css', f'assets/css/console.css?v={cache_bust}')
        # Inject the backend-resolved default language for first-load fallback.
        html = html.replace("{{COW_DEFAULT_LANG}}", i18n.get_language())
        return html


class ConfigHandler:

    _RECOMMENDED_MODELS = [
        const.DEEPSEEK_V4_FLASH, const.DEEPSEEK_V4_PRO,
        const.MINIMAX_M3, const.MINIMAX_M2_7_HIGHSPEED, const.MINIMAX_M2_7,
        # claude-opus-5 is the Claude default; claude-sonnet-5 / claude-fable-5 follow right after it.
        const.CLAUDE_OPUS_5, const.CLAUDE_SONNET_5, const.CLAUDE_FABLE_5, const.CLAUDE_4_8_OPUS, const.CLAUDE_4_7_OPUS, const.CLAUDE_4_6_SONNET, const.CLAUDE_4_6_OPUS,
        const.GEMINI_35_FLASH, const.GEMINI_31_FLASH_LITE_PRE, const.GEMINI_31_PRO_PRE, const.GEMINI_3_FLASH_PRE,
        const.GPT_56_LUNA, const.GPT_56_TERRA, const.GPT_56_SOL, const.GPT_55, const.GPT_54, const.GPT_54_MINI, const.GPT_54_NANO, const.GPT_5, const.GPT_41, const.GPT_4o,
        const.GLM_5_2, const.GLM_5_1, const.GLM_5_TURBO, const.GLM_5, const.GLM_4_7,
        const.QWEN37_PLUS, const.QWEN37_MAX, const.QWEN36_PLUS,
        const.DOUBAO_SEED_2_1_PRO, const.DOUBAO_SEED_2_1_TURBO, const.DOUBAO_SEED_2_CODE,
        const.KIMI_K3, const.KIMI_K2_7_CODE, const.KIMI_K2_7_CODE_HIGHSPEED, const.KIMI_K2_6, const.KIMI_K2_5, const.KIMI_K2,
        const.ERNIE_5_1, const.ERNIE_5, const.ERNIE_X1_1, const.ERNIE_45_TURBO_128K, const.ERNIE_45_TURBO_32K,
        const.MIMO_V2_5_PRO, const.MIMO_V2_5,
    ]

    # Generic placeholder hints surfaced in the web console. We deliberately
    # show the version-path tail (e.g. "/v1") so users are reminded to type
    # the full base URL. The form is intentionally vague (`...../v1`) so it
    # never looks like a real default a user might paste verbatim — and we
    # never auto-rewrite anything on the server side.
    _PLACEHOLDER_V1 = "https://...../v1"
    _PLACEHOLDER_QIANFAN = "https://...../v2"
    _PLACEHOLDER_ZHIPU = "https://...../api/paas/v4"
    _PLACEHOLDER_DOUBAO = "https://...../api/v3"
    _PLACEHOLDER_GEMINI = "https://....."

    PROVIDER_MODELS = OrderedDict([
        ("deepseek", {
            "label": "DeepSeek",
            "api_key_field": "deepseek_api_key",
            "api_base_key": "deepseek_api_base",
            "api_base_default": "https://api.deepseek.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.DEEPSEEK_V4_FLASH, const.DEEPSEEK_V4_PRO, const.DEEPSEEK_CHAT, const.DEEPSEEK_REASONER],
        }),
        ("minimax", {
            "label": "MiniMax",
            "api_key_field": "minimax_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": [const.MINIMAX_M3, const.MINIMAX_M2_7, const.MINIMAX_M2_7_HIGHSPEED],
        }),
        ("claudeAPI", {
            "label": "Claude",
            "api_key_field": "claude_api_key",
            "api_base_key": "claude_api_base",
            "api_base_default": "https://api.anthropic.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.CLAUDE_OPUS_5, const.CLAUDE_SONNET_5, const.CLAUDE_FABLE_5, const.CLAUDE_4_8_OPUS, const.CLAUDE_4_7_OPUS, const.CLAUDE_4_6_SONNET, const.CLAUDE_4_6_OPUS],
        }),
        ("gemini", {
            "label": "Gemini",
            "api_key_field": "gemini_api_key",
            "api_base_key": "gemini_api_base",
            "api_base_default": "https://generativelanguage.googleapis.com",
            "api_base_placeholder": _PLACEHOLDER_GEMINI,
            "models": [const.GEMINI_35_FLASH, const.GEMINI_31_FLASH_LITE_PRE, const.GEMINI_31_PRO_PRE, const.GEMINI_3_FLASH_PRE],
        }),
        ("openai", {
            "label": "OpenAI",
            "api_key_field": "open_ai_api_key",
            "api_base_key": "open_ai_api_base",
            "api_base_default": "https://api.openai.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.GPT_56_LUNA, const.GPT_56_TERRA, const.GPT_56_SOL, const.GPT_55, const.GPT_54, const.GPT_54_MINI, const.GPT_54_NANO, const.GPT_5, const.GPT_41, const.GPT_4o],
        }),
        ("zhipu", {
            "label": {"zh": "智谱AI", "en": "GLM"},
            "api_key_field": "zhipu_ai_api_key",
            "api_base_key": "zhipu_ai_api_base",
            "api_base_default": "https://open.bigmodel.cn/api/paas/v4",
            "api_base_placeholder": _PLACEHOLDER_ZHIPU,
            "models": [const.GLM_5_2, const.GLM_5_1, const.GLM_5_TURBO, const.GLM_5, const.GLM_4_7],
        }),
        ("dashscope", {
            "label": {"zh": "通义千问", "en": "Qwen"},
            "api_key_field": "dashscope_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": [const.QWEN37_PLUS, const.QWEN37_MAX, const.QWEN36_PLUS],
        }),
        ("doubao", {
            "label": {"zh": "豆包", "en": "Doubao"},
            "api_key_field": "ark_api_key",
            "api_base_key": "ark_base_url",
            "api_base_default": "https://ark.cn-beijing.volces.com/api/v3",
            "api_base_placeholder": _PLACEHOLDER_DOUBAO,
            "models": [const.DOUBAO_SEED_2_1_PRO, const.DOUBAO_SEED_2_1_TURBO, const.DOUBAO_SEED_2_PRO, const.DOUBAO_SEED_2_CODE],
        }),
        ("moonshot", {
            "label": "Kimi",
            "api_key_field": "moonshot_api_key",
            "api_base_key": "moonshot_base_url",
            "api_base_default": "https://api.moonshot.cn/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.KIMI_K3, const.KIMI_K2_7_CODE, const.KIMI_K2_7_CODE_HIGHSPEED, const.KIMI_K2_6, const.KIMI_K2_5, const.KIMI_K2],
        }),
        ("qianfan", {
            "label": {"zh": "百度千帆", "en": "ERNIE"},
            "api_key_field": "qianfan_api_key",
            "api_base_key": "qianfan_api_base",
            "api_base_default": "https://qianfan.baidubce.com/v2",
            "api_base_placeholder": _PLACEHOLDER_QIANFAN,
            "models": [const.ERNIE_5_1, const.ERNIE_5, const.ERNIE_X1_1, const.ERNIE_45_TURBO_128K, const.ERNIE_45_TURBO_32K],
        }),
        ("mimo", {
            "label": {"zh": "小米 MiMo", "en": "MiMo"},
            "api_key_field": "mimo_api_key",
            "api_base_key": "mimo_api_base",
            "api_base_default": "https://api.xiaomimimo.com/v1",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [const.MIMO_V2_5_PRO, const.MIMO_V2_5],
        }),
        ("linkai", {
            "label": "LinkAI",
            "api_key_field": "linkai_api_key",
            "api_base_key": None,
            "api_base_default": None,
            "api_base_placeholder": "",
            "models": _RECOMMENDED_MODELS,
        }),
        ("custom", {
            "label": {"zh": "自定义", "en": "Custom"},
            "api_key_field": "custom_api_key",
            "api_base_key": "custom_api_base",
            "api_base_default": "",
            "api_base_placeholder": _PLACEHOLDER_V1,
            "models": [],
        }),
    ])

    EDITABLE_KEYS = {
        "cow_lang",
        "model", "bot_type", "use_linkai",
        "open_ai_api_base", "deepseek_api_base", "qianfan_api_base", "claude_api_base", "gemini_api_base",
        "zhipu_ai_api_base", "moonshot_base_url", "ark_base_url", "custom_api_base", "mimo_api_base",
        "open_ai_api_key", "deepseek_api_key", "qianfan_api_key", "claude_api_key", "gemini_api_key",
        "zhipu_ai_api_key", "dashscope_api_key", "moonshot_api_key",
        "ark_api_key", "minimax_api_key", "linkai_api_key", "custom_api_key", "mimo_api_key",
        "custom_providers",
        "agent_max_context_tokens", "agent_max_context_turns", "agent_max_steps",
        "enable_thinking", "self_evolution_enabled", "web_password",
    }

    @staticmethod
    def _mask_key(value: str) -> str:
        """Mask the middle part of an API key for display."""
        if not value or len(value) <= 8:
            return value
        return value[:4] + "*" * (len(value) - 8) + value[-4:]

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            local_config = conf()
            use_agent = local_config.get("agent", True)
            title = "SmartAssistant" if use_agent else "AI Assistant"

            api_bases = {}
            api_keys_masked = {}
            for pid, pinfo in self.PROVIDER_MODELS.items():
                base_key = pinfo.get("api_base_key")
                if base_key:
                    api_bases[base_key] = local_config.get(base_key, pinfo["api_base_default"])
                key_field = pinfo.get("api_key_field")
                if key_field and key_field not in api_keys_masked:
                    raw = local_config.get(key_field, "")
                    api_keys_masked[key_field] = self._mask_key(raw) if raw else ""

            providers = {}
            for pid, p in self.PROVIDER_MODELS.items():
                providers[pid] = {
                    "label": p["label"],
                    "models": p["models"],
                    "api_base_key": p["api_base_key"],
                    "api_base_default": p["api_base_default"],
                    "api_base_placeholder": p.get("api_base_placeholder", ""),
                    "api_key_field": p.get("api_key_field"),
                }

            # Expose user-defined custom providers as "custom:<id>" entries so
            # the legacy config page can display and select them. Credentials
            # are managed on the Models page, hence the null key/base fields.
            # Mirrors the Models page: when expanded entries exist, the bare
            # legacy "custom" entry is hidden — unless the flat single-provider
            # custom config is still active or filled in.
            try:
                from models.custom_provider import get_custom_providers
                custom_list = get_custom_providers()
                legacy_custom_in_use = ModelsHandler._legacy_custom_in_use(local_config)
                if custom_list and not legacy_custom_in_use:
                    providers.pop("custom", None)
                for cp in custom_list:
                    cid = f"custom:{cp.get('id')}"
                    cname = cp.get("name") or cp.get("id")
                    providers[cid] = {
                        "label": {"zh": cname, "en": cname},
                        "models": [cp["model"]] if cp.get("model") else [],
                        "api_base_key": None,
                        "api_base_default": None,
                        "api_base_placeholder": "",
                        "api_key_field": None,
                    }
            except Exception as cp_err:
                logger.warning(f"[ConfigHandler] failed to expand custom providers: {cp_err}")

            raw_pwd = str(local_config.get("web_password", "") or "")
            masked_pwd = ("*" * len(raw_pwd)) if raw_pwd else ""

            result = {
                "status": "success",
                "use_agent": use_agent,
                "title": title,
                "model": local_config.get("model", ""),
                "bot_type": "openai" if local_config.get("bot_type") == "chatGPT" else local_config.get("bot_type", ""),
                "use_linkai": bool(local_config.get("use_linkai", False)),
                "channel_type": local_config.get("channel_type", ""),
                "agent_max_context_tokens": local_config.get("agent_max_context_tokens", 50000),
                "agent_max_context_turns": local_config.get("agent_max_context_turns", 20),
                "agent_max_steps": local_config.get("agent_max_steps", 20),
                "enable_thinking": bool(local_config.get("enable_thinking", False)),
                "self_evolution_enabled": bool(local_config.get("self_evolution_enabled", False)),
                "api_bases": api_bases,
                "api_keys": api_keys_masked,
                "providers": providers,
                "web_password_masked": masked_pwd,
            }
            # The desktop app runs on the local trusted machine, so it can edit
            # the real password in place (cursor at the end, delete to clear).
            # Browser access only ever sees the masked value.
            if os.environ.get("COW_DESKTOP") == "1":
                result["web_password"] = raw_pwd
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error getting config: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            data = json.loads(web.data())
            updates = data.get("updates", {})
            if not updates:
                return json.dumps({"status": "error", "message": "no updates provided"})

            local_config = conf()
            applied = {}
            for key, value in updates.items():
                if key not in self.EDITABLE_KEYS:
                    continue
                if key in ("agent_max_context_tokens", "agent_max_context_turns", "agent_max_steps"):
                    value = int(value)
                if key in ("use_linkai", "enable_thinking", "self_evolution_enabled"):
                    value = bool(value)
                local_config[key] = value
                applied[key] = value

            if not applied:
                return json.dumps({"status": "error", "message": "no valid keys to update"})

            config_path = os.path.join(get_data_root(), "config.json")
            old_password = ""  # Store old password before update
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    file_cfg = json.load(f)
                    # Capture old password before updating
                    if "web_password" in applied:
                        old_password = file_cfg.get("web_password", "")
            else:
                file_cfg = {}
            file_cfg.update(applied)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(file_cfg, f, indent=4, ensure_ascii=False)

            logger.info(f"[WebChannel] Config updated: {list(applied.keys())}")

            # Apply a language change immediately so backend logs, agent
            # replies and CLI output switch without a restart.
            if "cow_lang" in applied:
                try:
                    i18n.resolve_language(applied["cow_lang"])
                    logger.info(f"[WebChannel] Language switched to: {i18n.get_language()}")
                except Exception as lang_err:
                    logger.warning(f"[WebChannel] Failed to apply language: {lang_err}")

            password_warning = None
            if "web_password" in applied and not applied["web_password"] and old_password:
                password_warning = "password_cleared"
                logger.warning(
                    "[WebChannel] Web password cleared. The listener remains "
                    "loopback-only; remote access must stay behind a TLS proxy."
                )

            # Reset Bridge so that bot routing reflects the new config.
            # Without this, Bridge keeps its cached bot instance (e.g. LinkAIBot)
            # even after the user switches bot_type / use_linkai / model in UI.
            bridge_routing_keys = {"bot_type", "use_linkai", "model"}
            if any(k in applied for k in bridge_routing_keys):
                try:
                    from bridge.bridge import Bridge
                    Bridge().reset_bot()
                    logger.info("[WebChannel] Bridge bot routing reset due to config change")
                except Exception as reset_err:
                    logger.warning(f"[WebChannel] Failed to reset bridge: {reset_err}")

            return json.dumps({"status": "success", "applied": applied, "warning": password_warning}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error updating config: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class ModelsHandler:
    """API for the unified Models console.

    Layered model:
      Layer 1 (providers): vendor credentials shared across capabilities.
                            Stored as flat *_api_key / *_api_base fields in
                            config.json — the same fields ConfigHandler
                            already manages.
      Layer 2 (capabilities): which provider/model is used by chat / vision /
                            asr / tts / embedding / image / search.

    GET  /api/models           -> overview (providers + capabilities)
    POST /api/models/provider  -> upsert a vendor credential
    DELETE /api/models/provider -> clear a vendor credential
    POST /api/models/capability -> set provider/model for a capability
    """

    # Capability -> provider ids drawn from ConfigHandler.PROVIDER_MODELS.
    _ASR_PROVIDERS = ["openai", "dashscope", "zhipu", "linkai"]
    # Web-console white-list. Other vendors stay usable via direct config.
    _TTS_PROVIDERS = ["openai", "minimax", "dashscope", "mimo", "linkai"]

    # TTS engine catalog (speech models, not voice timbres). Entries are
    # either a bare code or {value, hint?} when a friendly label helps.
    _TTS_PROVIDER_MODELS = {
        "openai":    ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"],
        "minimax": [
            {"value": "speech-2.8-hd",    "hint": "情绪渲染融合语气词,自然听感"},
            {"value": "speech-2.8-turbo", "hint": "极致生成速度,更自然逼真"},
            {"value": "speech-2.6-hd",    "hint": "超低延时,归一化升级"},
            {"value": "speech-2.6-turbo", "hint": "更快更便宜,适合语音聊天/数字人"},
        ],
        "dashscope": [
            {"value": "qwen3-tts-flash", "hint": "覆盖普通话、方言与主流外语"},
        ],
        # 小米 MiMo TTS 系列，通过 chat completions 接口合成
        "mimo": [
            {"value": "mimo-v2.5-tts", "hint": "预置音色 · 支持唱歌模式"},
        ],
        # Aggregating gateway: a single endpoint multiplexes several
        # underlying TTS engines, selected via the `model` field.
        # Each engine exposes its own voice catalog (see _TTS_PROVIDER_VOICES).
        "linkai": [
            {"value": "tts-1",  "hint": "OpenAI · 多语种通用"},
            {"value": "doubao", "hint": "字节豆包 · 中文音色丰富"},
            {"value": "baidu",  "hint": "百度 · 中文主播音色"},
        ],
    }

    # ASR engine catalog per provider. The first entry of each list is the
    # runtime default (mirrors DEFAULT_ASR_MODEL in voice/*). Users can still
    # pick "custom" in the UI to send any other model id.
    _ASR_PROVIDER_MODELS = {
        "openai": [
            {"value": "gpt-4o-mini-transcribe", "hint": "默认 · 速度快"},
            {"value": "gpt-4o-transcribe",      "hint": "更高准确率"},
            {"value": "whisper-1",              "hint": "经典 Whisper"},
        ],
        "dashscope": [
            {"value": "qwen3-asr-flash", "hint": "覆盖普通话、方言与主流外语"},
        ],
        "zhipu": [
            {"value": "glm-asr-2512", "hint": "智谱语音识别"},
        ],
        # LinkAI gateway pins whisper-1 for ASR and ignores any other id,
        # so expose only that to avoid misleading the user.
        "linkai": [
            {"value": "whisper-1", "hint": "网关固定使用"},
        ],
    }

    # Per-provider voice timbres. Entries can be a bare code string
    # (label = code) or {value, hint?} when a friendly secondary label
    # helps recognition. We keep `value` as the raw API code so power
    # users can cross-reference config.json.
    _TTS_PROVIDER_VOICES = {
        "openai":    [
            "alloy", "echo", "fable", "onyx", "nova", "shimmer",
            "ash", "ballad", "coral", "sage", "verse",
        ],
        "minimax": [
            # Mandarin Chinese (full catalog)
            {"value": "male-qn-qingse",                           "hint": "中文 · 青涩青年（男）"},
            {"value": "male-qn-jingying",                         "hint": "中文 · 精英青年（男）"},
            {"value": "male-qn-badao",                            "hint": "中文 · 霸道青年（男）"},
            {"value": "male-qn-daxuesheng",                       "hint": "中文 · 青年大学生（男）"},
            {"value": "female-shaonv",                            "hint": "中文 · 少女（女）"},
            {"value": "female-yujie",                             "hint": "中文 · 御姐（女）"},
            {"value": "female-chengshu",                          "hint": "中文 · 成熟女性（女）"},
            {"value": "female-tianmei",                           "hint": "中文 · 甜美女性（女）"},
            {"value": "male-qn-qingse-jingpin",                   "hint": "中文 · 青涩青年-beta（男）"},
            {"value": "male-qn-jingying-jingpin",                 "hint": "中文 · 精英青年-beta（男）"},
            {"value": "male-qn-badao-jingpin",                    "hint": "中文 · 霸道青年-beta（男）"},
            {"value": "male-qn-daxuesheng-jingpin",               "hint": "中文 · 青年大学生-beta（男）"},
            {"value": "female-shaonv-jingpin",                    "hint": "中文 · 少女-beta（女）"},
            {"value": "female-yujie-jingpin",                     "hint": "中文 · 御姐-beta（女）"},
            {"value": "female-chengshu-jingpin",                  "hint": "中文 · 成熟女性-beta（女）"},
            {"value": "female-tianmei-jingpin",                   "hint": "中文 · 甜美女性-beta（女）"},
            {"value": "clever_boy",                               "hint": "中文 · 聪明男童"},
            {"value": "cute_boy",                                 "hint": "中文 · 可爱男童"},
            {"value": "lovely_girl",                              "hint": "中文 · 萌萌女童"},
            {"value": "cartoon_pig",                              "hint": "中文 · 卡通猪小琪"},
            {"value": "bingjiao_didi",                            "hint": "中文 · 病娇弟弟"},
            {"value": "junlang_nanyou",                           "hint": "中文 · 俊朗男友"},
            {"value": "chunzhen_xuedi",                           "hint": "中文 · 纯真学弟"},
            {"value": "lengdan_xiongzhang",                       "hint": "中文 · 冷淡学长"},
            {"value": "badao_shaoye",                             "hint": "中文 · 霸道少爷"},
            {"value": "tianxin_xiaoling",                         "hint": "中文 · 甜心小玲"},
            {"value": "qiaopi_mengmei",                           "hint": "中文 · 俏皮萌妹"},
            {"value": "wumei_yujie",                              "hint": "中文 · 妩媚御姐"},
            {"value": "diadia_xuemei",                            "hint": "中文 · 嗲嗲学妹"},
            {"value": "danya_xuejie",                             "hint": "中文 · 淡雅学姐"},
            {"value": "Chinese (Mandarin)_Reliable_Executive",    "hint": "中文 · 沉稳高管"},
            {"value": "Chinese (Mandarin)_News_Anchor",           "hint": "中文 · 新闻女声"},
            {"value": "Chinese (Mandarin)_Mature_Woman",          "hint": "中文 · 傲娇御姐"},
            {"value": "Chinese (Mandarin)_Unrestrained_Young_Man","hint": "中文 · 不羁青年"},
            {"value": "Arrogant_Miss",                            "hint": "中文 · 嚣张小姐"},
            {"value": "Robot_Armor",                              "hint": "中文 · 机械战甲"},
            {"value": "Chinese (Mandarin)_Kind-hearted_Antie",    "hint": "中文 · 热心大婶"},
            {"value": "Chinese (Mandarin)_HK_Flight_Attendant",   "hint": "中文 · 港普空姐"},
            {"value": "Chinese (Mandarin)_Humorous_Elder",        "hint": "中文 · 搞笑大爷"},
            {"value": "Chinese (Mandarin)_Gentleman",             "hint": "中文 · 温润男声"},
            {"value": "Chinese (Mandarin)_Warm_Bestie",           "hint": "中文 · 温暖闺蜜"},
            {"value": "Chinese (Mandarin)_Male_Announcer",        "hint": "中文 · 播报男声"},
            {"value": "Chinese (Mandarin)_Sweet_Lady",            "hint": "中文 · 甜美女声"},
            {"value": "Chinese (Mandarin)_Southern_Young_Man",    "hint": "中文 · 南方小哥"},
            {"value": "Chinese (Mandarin)_Wise_Women",            "hint": "中文 · 阅历姐姐"},
            {"value": "Chinese (Mandarin)_Gentle_Youth",          "hint": "中文 · 温润青年"},
            {"value": "Chinese (Mandarin)_Warm_Girl",             "hint": "中文 · 温暖少女"},
            {"value": "Chinese (Mandarin)_Kind-hearted_Elder",    "hint": "中文 · 花甲奶奶"},
            {"value": "Chinese (Mandarin)_Cute_Spirit",           "hint": "中文 · 憨憨萌兽"},
            {"value": "Chinese (Mandarin)_Radio_Host",            "hint": "中文 · 电台男主播"},
            {"value": "Chinese (Mandarin)_Lyrical_Voice",         "hint": "中文 · 抒情男声"},
            {"value": "Chinese (Mandarin)_Straightforward_Boy",   "hint": "中文 · 率真弟弟"},
            {"value": "Chinese (Mandarin)_Sincere_Adult",         "hint": "中文 · 真诚青年"},
            {"value": "Chinese (Mandarin)_Gentle_Senior",         "hint": "中文 · 温柔学姐"},
            {"value": "Chinese (Mandarin)_Stubborn_Friend",       "hint": "中文 · 嘴硬竹马"},
            {"value": "Chinese (Mandarin)_Crisp_Girl",            "hint": "中文 · 清脆少女"},
            {"value": "Chinese (Mandarin)_Pure-hearted_Boy",      "hint": "中文 · 清澈邻家弟弟"},
            {"value": "Chinese (Mandarin)_Soft_Girl",             "hint": "中文 · 柔和少女"},
            # Cantonese (full catalog)
            {"value": "Cantonese_ProfessionalHost（F)",            "hint": "粤语 · 专业女主持"},
            {"value": "Cantonese_GentleLady",                     "hint": "粤语 · 温柔女声"},
            {"value": "Cantonese_ProfessionalHost（M)",            "hint": "粤语 · 专业男主持"},
            {"value": "Cantonese_PlayfulMan",                     "hint": "粤语 · 活泼男声"},
            {"value": "Cantonese_CuteGirl",                       "hint": "粤语 · 可爱女孩"},
            {"value": "Cantonese_KindWoman",                      "hint": "粤语 · 善良女声"},
            # English (curated: 1F + 1M)
            {"value": "English_Graceful_Lady",                    "hint": "英文 · Graceful Lady（女）"},
            {"value": "English_Trustworthy_Man",                  "hint": "英文 · Trustworthy Man（男）"},
            # Japanese (curated: 1F + 1M)
            {"value": "Japanese_KindLady",                        "hint": "日文 · Kind Lady（女）"},
            {"value": "Japanese_LoyalKnight",                     "hint": "日文 · Loyal Knight（男）"},
            # Korean (curated: 1F + 1M)
            {"value": "Korean_SweetGirl",                         "hint": "韩文 · Sweet Girl（女）"},
            {"value": "Korean_CheerfulBoyfriend",                 "hint": "韩文 · Cheerful Boyfriend（男）"},
        ],
        "dashscope": [
            {"value": "Cherry",   "hint": "芊悦 · 阳光女声"},
            {"value": "Serena",   "hint": "苏瑶 · 温柔女声"},
            {"value": "Chelsie",  "hint": "千雪 · 二次元少女"},
            {"value": "Ethan",    "hint": "晨煦 · 阳光男声"},
            {"value": "Moon",     "hint": "月白 · 率性男声"},
            {"value": "Kai",      "hint": "凯 · 治愈男声"},
            {"value": "Nofish",   "hint": "不吃鱼 · 设计师男声"},
            {"value": "Bella",    "hint": "萌宝 · 小萝莉"},
            {"value": "Bunny",    "hint": "萌小姬 · 萌系少女"},
            {"value": "Stella",   "hint": "少女阿月 · 元气少女"},
            {"value": "Neil",     "hint": "阿闻 · 新闻主播"},
            {"value": "Seren",    "hint": "小婉 · 助眠女声"},
            {"value": "Jada",     "hint": "上海话 · 阿珍"},
            {"value": "Dylan",    "hint": "北京话 · 晓东"},
            {"value": "Sunny",    "hint": "四川话 · 晴儿"},
            {"value": "Eric",     "hint": "四川话 · 程川"},
            {"value": "Rocky",    "hint": "粤语 · 阿强"},
            {"value": "Kiki",     "hint": "粤语 · 阿清"},
            {"value": "Peter",    "hint": "天津话 · 李彼得"},
            {"value": "Marcus",   "hint": "陕西话 · 秦川"},
            {"value": "Roy",      "hint": "闽南语 · 阿杰"},
        ],
        # 小米 MiMo 预置音色列表（mimo-v2.5-tts），文档：
        # https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis-v2.5
        "mimo": [
            {"value": "冰糖",   "hint": "中文 · 女声 · 冰糖"},
            {"value": "茉莉",   "hint": "中文 · 女声 · 茉莉"},
            {"value": "苏打",   "hint": "中文 · 男声 · 苏打"},
            {"value": "白桦",   "hint": "中文 · 男声 · 白桦"},
            {"value": "Mia",   "hint": "英文 · 女声 · Mia"},
            {"value": "Chloe", "hint": "英文 · 女声 · Chloe"},
            {"value": "Milo",  "hint": "英文 · 男声 · Milo"},
            {"value": "Dean",  "hint": "英文 · 男声 · Dean"},
        ],
        # Aggregating gateway: voices are scoped per engine model. The
        # frontend picks the correct list based on the selected model so
        # users don't see incompatible timbres for the active engine.
        "linkai": {
            "tts-1": [
                "alloy", "echo", "fable", "onyx", "nova", "shimmer",
            ],
            "doubao": [
                {"value": "zh_female_wanwanxiaohe_moon_bigtts",       "hint": "湾湾小何"},
                {"value": "BV007_streaming",                          "hint": "亲切女声"},
                {"value": "BV001_streaming",                          "hint": "通用女声"},
                {"value": "BV002_streaming",                          "hint": "通用男声"},
                {"value": "BV051_streaming",                          "hint": "奶气萌娃"},
                {"value": "zh_female_linjianvhai_moon_bigtts",        "hint": "邻家女孩"},
                {"value": "BV700_streaming",                          "hint": "灿灿"},
                {"value": "BV019_streaming",                          "hint": "重庆小伙"},
                {"value": "BV524_streaming",                          "hint": "日语男声"},
                {"value": "BV021_streaming",                          "hint": "东北老铁"},
                {"value": "BV701_streaming",                          "hint": "擎苍"},
                {"value": "BV113_streaming",                          "hint": "甜宠少御"},
                {"value": "BV056_streaming",                          "hint": "阳光男声"},
                {"value": "BV213_streaming",                          "hint": "广西表哥"},
                {"value": "BV119_streaming",                          "hint": "通用赘婿"},
                {"value": "BV705_streaming",                          "hint": "炀炀"},
                {"value": "BV033_streaming",                          "hint": "温柔小哥"},
                {"value": "BV102_streaming",                          "hint": "儒雅青年"},
                {"value": "BV522_streaming",                          "hint": "气质女生"},
                {"value": "BV034_streaming",                          "hint": "知性姐姐 · 双语"},
                {"value": "BV005_streaming",                          "hint": "活泼女声"},
                {"value": "zh_female_wanqudashu_moon_bigtts",         "hint": "湾区大叔"},
                {"value": "zh_female_daimengchuanmei_moon_bigtts",    "hint": "呆萌川妹"},
                {"value": "zh_male_guozhoudege_moon_bigtts",          "hint": "广州德哥"},
                {"value": "zh_male_beijingxiaoye_moon_bigtts",        "hint": "北京小爷"},
                {"value": "zh_male_shaonianzixin_moon_bigtts",        "hint": "少年梓辛 / Brayan"},
                {"value": "zh_female_meilinvyou_moon_bigtts",         "hint": "魅力女友"},
                {"value": "zh_male_shenyeboke_moon_bigtts",           "hint": "深夜播客"},
                {"value": "zh_female_sajiaonvyou_moon_bigtts",        "hint": "柔美女友"},
                {"value": "zh_female_yuanqinvyou_moon_bigtts",        "hint": "撒娇学妹"},
                {"value": "zh_male_haoyuxiaoge_moon_bigtts",          "hint": "浩宇小哥"},
                {"value": "zh_male_guangxiyuanzhou_moon_bigtts",      "hint": "广西远舟"},
                {"value": "zh_female_meituojieer_moon_bigtts",        "hint": "妹坨洁儿"},
                {"value": "zh_male_yuzhouzixuan_moon_bigtts",         "hint": "豫州子轩"},
                {"value": "BV115_streaming",                          "hint": "古风少御"},
                {"value": "zh_female_gaolengyujie_moon_bigtts",       "hint": "高冷御姐"},
                {"value": "zh_male_yuanboxiaoshu_moon_bigtts",        "hint": "渊博小叔"},
                {"value": "zh_male_yangguangqingnian_moon_bigtts",    "hint": "阳光青年"},
                {"value": "zh_male_aojiaobazong_moon_bigtts",         "hint": "傲娇霸总"},
                {"value": "zh_male_jingqiangkanye_moon_bigtts",       "hint": "京腔侃爷 / Harmony"},
                {"value": "zh_female_shuangkuaisisi_moon_bigtts",     "hint": "爽快思思 / Skye"},
                {"value": "zh_male_wennuanahu_moon_bigtts",           "hint": "温暖阿虎 / Alvin"},
                {"value": "multi_female_shuangkuaisisi_moon_bigtts",  "hint": "はるこ / Esmeralda"},
                {"value": "multi_male_jingqiangkanye_moon_bigtts",    "hint": "かずね / Javier or Álvaro"},
                {"value": "multi_female_gaolengyujie_moon_bigtts",    "hint": "あけみ"},
                {"value": "multi_male_wanqudashu_moon_bigtts",        "hint": "ひろし / Roberto"},
                {"value": "ICL_zh_female_bingruoshaonv_tob",          "hint": "病弱少女"},
                {"value": "ICL_zh_female_huoponvhai_tob",             "hint": "活泼女孩"},
                {"value": "ICL_zh_female_heainainai_tob",             "hint": "和蔼奶奶"},
                {"value": "ICL_zh_female_linjuayi_tob",               "hint": "邻居阿姨"},
                {"value": "zh_female_wenrouxiaoya_moon_bigtts",       "hint": "温柔小雅"},
                {"value": "zh_female_tianmeixiaoyuan_moon_bigtts",    "hint": "甜美小源"},
                {"value": "zh_female_qingchezizi_moon_bigtts",        "hint": "清澈梓梓"},
                {"value": "zh_male_dongfanghaoran_moon_bigtts",       "hint": "东方浩然"},
                {"value": "zh_male_jieshuoxiaoming_moon_bigtts",      "hint": "解说小明"},
                {"value": "zh_female_kailangjiejie_moon_bigtts",      "hint": "开朗姐姐"},
                {"value": "zh_male_linjiananhai_moon_bigtts",         "hint": "邻家男孩"},
                {"value": "zh_female_tianmeiyueyue_moon_bigtts",      "hint": "甜美悦悦"},
                {"value": "zh_female_xinlingjitang_moon_bigtts",      "hint": "心灵鸡汤"},
            ],
            "baidu": [
                {"value": "baidu_0",    "hint": "度小美 · 标准女主播"},
                {"value": "baidu_1",    "hint": "度小宇 · 亲切男声"},
                {"value": "baidu_3",    "hint": "度逍遥 · 情感男声"},
                {"value": "baidu_4",    "hint": "度丫丫 · 童声"},
                {"value": "baidu_5",    "hint": "度小娇 · 成熟女主播"},
                {"value": "baidu_5003", "hint": "度逍遥 · 情感男声"},
                {"value": "baidu_5118", "hint": "度小鹿 · 甜美女声"},
                {"value": "baidu_103",  "hint": "度米朵 · 可爱童声"},
                {"value": "baidu_106",  "hint": "度博文 · 专业男主播"},
                {"value": "baidu_110",  "hint": "度小童 · 童声主播"},
                {"value": "baidu_111",  "hint": "度小萌 · 软萌妹子"},
                {"value": "baidu_4003", "hint": "度逍遥 · 情感男声"},
                {"value": "baidu_4100", "hint": "度小雯 · 活力女主播"},
                {"value": "baidu_4103", "hint": "度米朵 · 可爱女声"},
                {"value": "baidu_4105", "hint": "度灵儿 · 清澈女声"},
                {"value": "baidu_4106", "hint": "度博文 · 专业男主播"},
                {"value": "baidu_4115", "hint": "度小贤 · 电台男主播"},
                {"value": "baidu_4117", "hint": "度小乔 · 活泼女声"},
                {"value": "baidu_4119", "hint": "度小鹿 · 甜美女声"},
                {"value": "baidu_4129", "hint": "度小彦 · 知识男主播"},
                {"value": "baidu_4140", "hint": "度小新 · 专业女主播"},
                {"value": "baidu_4143", "hint": "度清风 · 配音男声"},
                {"value": "baidu_4144", "hint": "度姗姗 · 娱乐女声"},
                {"value": "baidu_4149", "hint": "度星河 · 广告男声"},
                {"value": "baidu_4206", "hint": "度博文 · 综艺男声"},
                {"value": "baidu_4226", "hint": "南方 · 电台女主播"},
                {"value": "baidu_4254", "hint": "度小清 · 广告女声"},
                {"value": "baidu_4278", "hint": "度小贝 · 知识女主播"},
            ],
        },
    }
    _EMBEDDING_PROVIDERS = ["openai", "dashscope", "doubao", "zhipu", "linkai", "custom"]

    # Embedding model catalog per provider. Mirrors the default_model in
    # agent/memory/embedding/provider.py::EMBEDDING_VENDORS.
    # Custom providers have no preset list — model names vary per vendor,
    # so the user always types the model id manually.
    _EMBEDDING_PROVIDER_MODELS = {
        "openai":    ["text-embedding-3-small", "text-embedding-3-large"],
        "dashscope": ["text-embedding-v4"],
        "doubao":    ["doubao-embedding-vision-251215"],
        "zhipu":     ["embedding-3"],
        "linkai":    ["text-embedding-3-small"],
        "custom":    [],
    }

    # Capability-scoped model catalogs. The chat dropdown can reuse the
    # provider's generic model list, but vision and image generation are
    # served by a narrower subset that the runtime actually dispatches to —
    # see agent/tools/vision/vision.py and skills/image-generation/SKILL.md.
    # Anything not listed here intentionally hides the model dropdown so
    # users cannot pin a chat-only model and silently get a 4xx at runtime.
    _VISION_PROVIDER_MODELS = {
        # OpenAI ordering puts the GPT-5.6 family first, then GPT-5.5/5.4,
        # GPT-5 and the GPT-4.1/4o backstops.
        "openai":    [
            const.GPT_56_LUNA,
            const.GPT_56_TERRA,
            const.GPT_56_SOL,
            const.GPT_55,
            const.GPT_54,
            const.GPT_54_MINI,
            const.GPT_54_NANO,
            const.GPT_5,
            const.GPT_41,
            const.GPT_41_MINI,
            const.GPT_4o,
        ],
        "doubao":    [const.DOUBAO_SEED_2_1_PRO, const.DOUBAO_SEED_2_1_TURBO, const.DOUBAO_SEED_2_PRO],
        "moonshot":  [const.KIMI_K2_6],
        "dashscope": [const.QWEN37_PLUS, const.QWEN36_PLUS],
        # claude-sonnet-5 stays first here (unlike the chat lists): the first
        # entry is the auto-picked vision model, and image understanding does
        # not justify the Opus price.
        "claudeAPI": [const.CLAUDE_SONNET_5, const.CLAUDE_OPUS_5, const.CLAUDE_FABLE_5, const.CLAUDE_4_8_OPUS, const.CLAUDE_4_7_OPUS, const.CLAUDE_4_6_SONNET, const.CLAUDE_4_6_OPUS],
        "gemini":    [const.GEMINI_35_FLASH, const.GEMINI_31_FLASH_LITE_PRE, const.GEMINI_31_PRO_PRE, const.GEMINI_3_FLASH_PRE],
        "qianfan":   [const.ERNIE_45_TURBO_VL],
        # Zhipu's bot hard-codes the call to glm-5v-turbo regardless of what
        # name is passed in (see models/zhipuai/zhipuai_bot.py::call_vision),
        # so listing the chat models here would silently route to the same
        # endpoint. Surface only the model the runtime can truly dispatch to.
        "zhipu":     [const.GLM_5V_TURBO],
        # MiniMax's vision endpoint is similarly hard-coded to MiniMax-Text-01
        # (see models/minimax/minimax_bot.py::call_vision); the M2.x chat
        # family is text-only.
        "minimax":   [const.MINIMAX_TEXT_01],
        # MiMo 原生全模态模型：v2.5-pro / v2.5 支持图像/音频/视频输入
        "mimo":      [const.MIMO_V2_5_PRO, const.MIMO_V2_5],
        # LinkAI proxies the underlying vendor; surface a curated set of
        # multimodal models. Order: gpt-4.1-mini → gpt-5.4-mini as the
        # cross-vendor baselines, then each vendor's recommended default.
        "linkai":    [
            const.GPT_41_MINI,
            const.GPT_54_MINI,
            const.QWEN37_PLUS,
            const.DOUBAO_SEED_2_1_PRO,
            const.KIMI_K2_6,
            const.CLAUDE_SONNET_5,
            const.CLAUDE_FABLE_5,
            const.GEMINI_31_FLASH_LITE_PRE,
        ],
        # Custom OpenAI-compatible providers have no preset list — model
        # names vary per vendor, so the user types the model id manually.
        "custom": [],
    }

    # Image-generation catalog. Source of truth: skills/image-generation/SKILL.md.
    # Listed verbatim (not via const.*) because these are skill-side names
    # the script forwards directly to the vendor's image endpoint.
    #
    # Two shapes are accepted per model entry:
    #   - bare string                           → the model id, no hint
    #   - {"value": ..., "hint": "..."}         → model id + dim secondary
    #                                             label rendered on the right
    #                                             of the dropdown row. Useful
    #                                             for surfacing brand names
    #                                             (e.g. "Nano Banana 2" next
    #                                             to gemini-3.1-flash-image-preview).
    # The skill itself maps either form to the real vendor endpoint, so the
    # hint is purely cosmetic.
    _IMAGE_PROVIDER_MODELS = {
        "openai":    ["gpt-image-2", "gpt-image-1"],
        "gemini": [
            {"value": "gemini-3.1-flash-image-preview", "hint": "Nano Banana 2"},
            {"value": "gemini-3-pro-image-preview",     "hint": "Nano Banana Pro"},
            {"value": "gemini-2.5-flash-image",         "hint": "Nano Banana"},
        ],
        "doubao":    ["seedream-5.0-lite", "seedream-4.5"],
        "dashscope": ["qwen-image-2.0-pro", "qwen-image-2.0"],
        "minimax":   ["image-01"],
        "linkai": [
            "gpt-image-2",
            {"value": "gemini-3.1-flash-image-preview", "hint": "Nano Banana 2"},
            {"value": "gemini-3-pro-image-preview",     "hint": "Nano Banana Pro"},
            "seedream-5.0-lite",
        ],
    }

    @staticmethod
    def _config_path() -> str:
        return os.path.join(get_data_root(), "config.json")

    @classmethod
    def _read_file_config(cls) -> dict:
        path = cls._config_path()
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @classmethod
    def _write_file_config(cls, data: dict) -> None:
        with open(cls._config_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    @staticmethod
    def _is_real_key(value: str) -> bool:
        return bool(value) and value not in ("", "YOUR API KEY", "YOUR_API_KEY")

    @classmethod
    def _custom_provider_cards(cls, local_config: dict) -> List[dict]:
        """Expand ``custom_providers`` into one card per provider.

        Each user-defined OpenAI-compatible provider becomes its own card with
        id ``custom:<id>`` so the frontend can render, edit, delete and
        activate them independently. The card carries ``is_custom=True`` and
        ``active`` flags that the UI uses to render the extra controls.

        Returns an empty list when no multi-providers are configured, in which
        case the caller keeps the single legacy ``custom`` card untouched —
        guaranteeing backward compatibility with the flat
        ``custom_api_key`` / ``custom_api_base`` config.
        """
        try:
            from models.custom_provider import get_custom_providers, parse_custom_bot_type
            providers = get_custom_providers()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[ModelsHandler] failed to load custom_providers: {e}")
            providers = []
        if not providers:
            return []

        # Determine the currently active provider id from bot_type.
        bot_type = local_config.get("bot_type") or ""
        _, active_id = parse_custom_bot_type(bot_type)

        meta = ConfigHandler.PROVIDER_MODELS.get("custom") or {}
        cards = []
        for p in providers:
            pid = p.get("id") or ""
            name = p.get("name") or pid
            raw_key = p.get("api_key") or ""
            raw_base = p.get("api_base") or ""
            configured = cls._is_real_key(raw_key)
            cards.append({
                "id": f"custom:{pid}",
                "label": {"zh": name, "en": name},
                "configured": configured,
                "is_custom": True,
                "custom_id": pid,
                "custom_name": name,
                "active": (pid == active_id),
                "model": p.get("model") or "",
                # Custom cards are edited via the dedicated set_custom_provider
                # action, not the field-based set_provider flow, so the field
                # names are intentionally null.
                "api_key_field": None,
                "api_base_field": None,
                "api_key_masked": ConfigHandler._mask_key(raw_key) if configured else "",
                "api_base": raw_base,
                "api_base_default": "",
                "api_base_placeholder": meta.get("api_base_placeholder") or "",
                "models": [p.get("model")] if p.get("model") else [],
            })
        return cards

    @classmethod
    def _legacy_custom_in_use(cls, local_config: dict) -> bool:
        """True when the flat single-provider custom config is still relevant:
        either it is the active bot_type, or its key/base fields are filled.
        In that case the legacy "custom" card must stay visible even when
        multi ``custom_providers`` entries exist."""
        if (local_config.get("bot_type") or "") == "custom":
            return True
        return (cls._is_real_key(local_config.get("custom_api_key") or "")
                or bool(local_config.get("custom_api_base")))

    @classmethod
    def _provider_overview(cls) -> List[dict]:
        """All known providers (configured first, unconfigured after).
        Re-uses ConfigHandler.PROVIDER_MODELS for the canonical list.

        When the user has defined multiple custom (OpenAI-compatible)
        providers via ``custom_providers``, the single built-in ``custom``
        card is replaced by one card per provider (see
        ``_custom_provider_cards``). Otherwise the legacy single ``custom``
        card is shown unchanged.
        """
        local_config = conf()
        custom_cards = cls._custom_provider_cards(local_config)
        # Keep the legacy single "custom" card visible alongside the expanded
        # ones when the flat custom_api_key/base config is active or filled,
        # so existing single-provider setups never disappear from the UI.
        keep_legacy_custom = cls._legacy_custom_in_use(local_config)
        items = []
        for pid, p in ConfigHandler.PROVIDER_MODELS.items():
            if pid == "custom" and custom_cards:
                # Multi-provider mode: emit the expanded cards, plus the
                # legacy card when it is still in use.
                items.extend(custom_cards)
                if not keep_legacy_custom:
                    continue
            key_field = p.get("api_key_field")
            base_field = p.get("api_base_key")
            raw_key = local_config.get(key_field, "") if key_field else ""
            raw_base = local_config.get(base_field, "") if base_field else ""
            configured = cls._is_real_key(raw_key)
            items.append({
                "id": pid,
                "label": p["label"],
                "configured": configured,
                "is_custom": (pid == "custom"),
                "api_key_field": key_field,
                "api_base_field": base_field,
                "api_key_masked": ConfigHandler._mask_key(raw_key) if configured else "",
                "api_base": raw_base or (p.get("api_base_default") or ""),
                "api_base_default": p.get("api_base_default") or "",
                "api_base_placeholder": p.get("api_base_placeholder") or "",
                "models": list(p.get("models") or []),
            })

        def _sort_key(it):
            pid = it["id"]
            # Custom expanded cards share the sort weight of the base "custom"
            # entry so they cluster where the single custom card used to be.
            base_id = "custom" if it.get("is_custom") else pid
            try:
                order = list(ConfigHandler.PROVIDER_MODELS.keys()).index(base_id)
            except ValueError:
                order = len(ConfigHandler.PROVIDER_MODELS)
            return (0 if it["configured"] else 1, order)

        items.sort(key=_sort_key)
        return items

    @classmethod
    def _chat_capability(cls, local_config: dict) -> dict:
        """Main chat model — drives the agent. bot_type maps to a provider id."""
        bot_type = local_config.get("bot_type") or ""
        provider_id = "openai" if bot_type == "chatGPT" else bot_type
        is_custom_id = provider_id.startswith("custom:")
        if (provider_id not in ConfigHandler.PROVIDER_MODELS and not is_custom_id
                and local_config.get("use_linkai")):
            provider_id = "linkai"
        # In multi-provider mode, replace the single "custom" entry with the
        # expanded "custom:<id>" ids so the chat dropdown matches the cards.
        # The legacy "custom" entry stays when its flat config is still used.
        provider_ids = []
        custom_cards = cls._custom_provider_cards(local_config)
        keep_legacy_custom = cls._legacy_custom_in_use(local_config)
        for pid in ConfigHandler.PROVIDER_MODELS.keys():
            if pid == "custom" and custom_cards:
                provider_ids.extend(c["id"] for c in custom_cards)
                if keep_legacy_custom:
                    provider_ids.append(pid)
            else:
                provider_ids.append(pid)
        return {
            "editable": True,
            "current_provider": provider_id,
            "current_model": local_config.get("model", ""),
            "providers": provider_ids,
            "use_linkai": bool(local_config.get("use_linkai", False)),
        }

    # Auto-fallback order for vision when no explicit model is pinned.
    # Mirrors agent/tools/vision/vision.py::_resolve_providers — DeepSeek and
    # other text-only chat bots are intentionally absent, since they cannot
    # actually serve a vision request. Each entry is
    #   (provider_id, api_key_field, default_vision_model)
    # and lookups are case-insensitive on the api_key_field. LinkAI and
    # OpenAI are handled separately below so use_linkai can promote LinkAI
    # to the front of the chain.
    _VISION_AUTO_ORDER = [
        ("moonshot",  "moonshot_api_key",  const.KIMI_K2_6),
        ("doubao",    "ark_api_key",       const.DOUBAO_SEED_2_PRO),
        ("dashscope", "dashscope_api_key", const.QWEN37_PLUS),
        ("claudeAPI", "claude_api_key",    const.CLAUDE_SONNET_5),
        ("gemini",    "gemini_api_key",    const.GEMINI_35_FLASH),
        ("qianfan",   "qianfan_api_key",   const.ERNIE_45_TURBO_VL),
        ("zhipu",     "zhipu_ai_api_key",  const.GLM_5V_TURBO),
        ("minimax",   "minimax_api_key",   const.MINIMAX_TEXT_01),
        ("mimo",      "mimo_api_key",      const.MIMO_V2_5_PRO),
    ]

    @classmethod
    def _predict_vision_auto(cls, local_config: dict) -> dict:
        """Predict which provider vision.py will actually dispatch to when
        no tools.vision.model is set. Mirrors the fallback order in
        agent/tools/vision/vision.py::_resolve_providers so the UI hint
        matches reality."""
        chat = cls._chat_capability(local_config)
        main_provider = chat["current_provider"]
        main_model = chat["current_model"]
        use_linkai_flag = bool(local_config.get("use_linkai", False))
        linkai_configured = cls._is_real_key(local_config.get("linkai_api_key", ""))

        def _try(pid: str, model_default: str):
            # Look up the api_key for this provider via the canonical
            # provider table so we don't hardcode field names here.
            meta = ConfigHandler.PROVIDER_MODELS.get(pid) or {}
            key_field = meta.get("api_key_field")
            if not key_field:
                return None
            if not cls._is_real_key(local_config.get(key_field, "")):
                return None
            # Pick a model that the vision runtime can actually dispatch to
            # for this provider. Using `main_model` here is unsafe — for
            # vendors like Zhipu/MiniMax the bot hard-codes the vision model
            # name regardless of the chat-model name, so surfacing the chat
            # model name in the hint is misleading. Trust the curated
            # _VISION_PROVIDER_MODELS list: prefer the main model only if
            # it appears there; otherwise show the vendor's first vision-
            # capable model.
            allowed = cls._VISION_PROVIDER_MODELS.get(pid, [])
            if pid == main_provider and main_model and main_model in allowed:
                return {"provider": pid, "model": main_model}
            fallback = allowed[0] if allowed else model_default
            return {"provider": pid, "model": fallback}

        # 1. use_linkai → suppress the hint entirely. LinkAI is a proxy and
        #    we don't observe which underlying model it picks; surfacing
        #    "LinkAI" with no model would not tell the user anything useful.
        if use_linkai_flag and linkai_configured:
            return {"provider": "", "model": ""}

        # 2. Main bot — only when it natively supports vision. We approximate
        #    "natively supports" by membership in _VISION_PROVIDER_MODELS,
        #    which is the same set vision.py's _DISCOVERABLE_MODELS covers
        #    (minus the chat-only DeepSeek family).
        if main_provider in cls._VISION_PROVIDER_MODELS:
            hit = _try(main_provider, main_model)
            if hit:
                return hit

        # 3. Other discoverable providers in declared order
        for pid, _key, default_model in cls._VISION_AUTO_ORDER:
            hit = _try(pid, default_model)
            if hit:
                return hit

        # 4. OpenAI raw HTTP
        if cls._is_real_key(local_config.get("open_ai_api_key", "")):
            return {"provider": "openai", "model": const.GPT_55}

        # 5. LinkAI as last resort (only reached when use_linkai is off)
        if linkai_configured:
            return {"provider": "linkai", "model": const.GPT_41_MINI}

        return {"provider": "", "model": ""}

    @classmethod
    def _vision_capability(cls, local_config: dict) -> dict:
        """Vision model. tools.vision.model is the explicit override; otherwise
        the runtime fallback chain in agent/tools/vision/vision.py decides."""
        tools_conf = local_config.get("tools") or local_config.get("tool") or {}
        if not isinstance(tools_conf, dict):
            tools_conf = {}
        vision_conf = tools_conf.get("vision") or {}
        if not isinstance(vision_conf, dict):
            vision_conf = {}
        user_specified = (vision_conf.get("model") or "").strip()
        explicit_provider = (vision_conf.get("provider") or "").strip()

        # Build provider list: built-in providers + expanded custom:<id> entries.
        # Same pattern as _embedding_capability — each user-created custom
        # provider gets its own dropdown entry showing the user-chosen name.
        providers = []
        custom_cards = cls._custom_provider_cards(local_config)
        for pid in cls._VISION_PROVIDER_MODELS:
            if pid == "custom":
                if custom_cards:
                    providers.extend(c["id"] for c in custom_cards)
            else:
                providers.append(pid)

        # Provider resolution priority:
        #   1. Explicit `tools.vision.provider` (persisted via UI; supports
        #      custom model names that prefix-inference can't recognize).
        #   2. Scan per-provider model lists by model name.
        # Empty provider keeps the dropdown on "auto" when we can't tell.
        inferred_provider = ""
        if explicit_provider and explicit_provider in providers:
            inferred_provider = explicit_provider
        elif user_specified:
            for pid, models in cls._VISION_PROVIDER_MODELS.items():
                if user_specified in models:
                    # For "custom" key, map to the first custom card
                    inferred_provider = custom_cards[0]["id"] if pid == "custom" and custom_cards else pid
                    break

        # In auto mode the hint should reflect what vision.py will actually
        # dispatch to — surface that prediction via fallback_* so the UI
        # shows e.g. "openai / gpt-4.1-mini" instead of the chat-model name.
        predicted = cls._predict_vision_auto(local_config)

        return {
            "editable": True,
            "strategy": "specified" if user_specified else "auto",
            "user_specified_model": user_specified,
            "current_provider": inferred_provider,
            "current_model": user_specified,
            "fallback_provider": predicted["provider"],
            "fallback_model": predicted["model"],
            "providers": providers,
            "provider_models": cls._VISION_PROVIDER_MODELS,
        }

    @classmethod
    def _asr_capability(cls, local_config: dict) -> dict:
        # "Pick or empty" — when voice_to_text is unset we don't show a
        # current selection. `suggested_provider` previews which vendor
        # the bridge auto-picker would land on (purely a UX hint, NOT
        # persisted). Once the user saves a vendor, we lock onto it.
        explicit = (local_config.get("voice_to_text") or "").strip().lower()
        suggested = ""
        if not explicit:
            for pid in cls._ASR_PROVIDERS:
                meta = ConfigHandler.PROVIDER_MODELS.get(pid) or {}
                key_field = meta.get("api_key_field")
                if key_field and cls._is_real_key(local_config.get(key_field, "")):
                    suggested = pid
                    break
        return {
            "editable": True,
            "current_provider": explicit,
            "suggested_provider": suggested,
            "current_model": (local_config.get("voice_to_text_model") or "") if explicit else "",
            "providers": cls._ASR_PROVIDERS,
            "provider_models": cls._ASR_PROVIDER_MODELS,
        }

    @classmethod
    def _tts_capability(cls, local_config: dict) -> dict:
        explicit = (local_config.get("text_to_voice") or "").strip().lower()
        # Providers outside the white-list don't drive the picker, but their
        # underlying runtime config is preserved so bridge still routes them.
        ui_provider = explicit if explicit in cls._TTS_PROVIDERS else ""
        suggested = ""
        if not ui_provider:
            for pid in cls._TTS_PROVIDERS:
                meta = ConfigHandler.PROVIDER_MODELS.get(pid) or {}
                key_field = meta.get("api_key_field")
                if key_field and cls._is_real_key(local_config.get(key_field, "")):
                    suggested = pid
                    break
        return {
            "editable": True,
            "current_provider": ui_provider,
            "suggested_provider": suggested,
            "current_model": (local_config.get("text_to_voice_model") or "") if ui_provider else "",
            "current_voice": (local_config.get("tts_voice_id") or "") if ui_provider else "",
            "providers": cls._TTS_PROVIDERS,
            "provider_models": cls._TTS_PROVIDER_MODELS,
            "provider_voices": cls._TTS_PROVIDER_VOICES,
            "reply_mode": cls._tts_reply_mode(local_config),
        }

    @staticmethod
    def _tts_reply_mode(local_config: dict) -> str:
        if local_config.get("always_reply_voice", False):
            return "always"
        if local_config.get("voice_reply_voice", False):
            return "voice_if_voice"
        return "off"

    @classmethod
    def _embedding_capability(cls, local_config: dict) -> dict:
        # Embedding is "pick or empty" — runtime's legacy openai/linkai
        # fallback is a safety net, not a UX-visible auto mode.
        # `suggested_provider` is a UI-only hint (NOT persisted) that
        # preselects the dropdown to whichever configured vendor we'd
        # recommend, so users don't have to expand the menu to find it.
        explicit = (local_config.get("embedding_provider") or "").strip().lower()
        suggested = ""
        if not explicit:
            for pid in cls._EMBEDDING_PROVIDERS:
                if pid == "custom":
                    continue
                meta = ConfigHandler.PROVIDER_MODELS.get(pid) or {}
                key_field = meta.get("api_key_field")
                if key_field and cls._is_real_key(local_config.get(key_field, "")):
                    suggested = pid
                    break
            if not suggested:
                custom_cards = cls._custom_provider_cards(local_config)
                if custom_cards:
                    suggested = custom_cards[0]["id"]

        # Build provider list: built-in providers + expanded custom:<id> entries
        # Same pattern as _chat_capability — each user-created custom provider
        # gets its own dropdown entry showing the user-chosen name.
        providers = []
        custom_cards = cls._custom_provider_cards(local_config)
        for pid in cls._EMBEDDING_PROVIDERS:
            if pid == "custom":
                if custom_cards:
                    providers.extend(c["id"] for c in custom_cards)
                # No custom providers configured — skip the bare "custom" entry
                # since the runtime cannot resolve its credentials.
            else:
                providers.append(pid)

        return {
            "editable": True,
            "current_provider": explicit,
            "suggested_provider": suggested,
            "current_model": local_config.get("embedding_model", "") or "",
            "current_dim": int(local_config.get("embedding_dimensions") or 0) or None,
            "providers": providers,
            "provider_models": cls._EMBEDDING_PROVIDER_MODELS,
        }

    # Auto-fallback order for image generation. Mirrors the global priority
    # used inside skills/image-generation/scripts/generate.py
    # (`_DEFAULT_PROVIDER_ORDER`): OpenAI → Gemini → Seedream(Ark/doubao) →
    # Qwen(dashscope) → MiniMax → LinkAI. Each entry maps the
    # provider-card id to the script's per-provider DEFAULT_MODEL so the
    # hint matches what the runtime would actually request.
    _IMAGE_AUTO_ORDER = [
        ("openai",    "gpt-image-2"),
        ("gemini",    "gemini-3.1-flash-image-preview"),  # nano-banana-2
        ("doubao",    "seedream-5.0-lite"),
        ("dashscope", "qwen-image-2.0"),
        ("minimax",   "image-01"),
        ("linkai",    "gpt-image-2"),
    ]

    @classmethod
    def _predict_image_auto(cls, local_config: dict) -> dict:
        """Predict which provider/model the image-generation skill will hit
        when no SKILL_IMAGE_GENERATION_MODEL override is set. Mirrors
        skills/image-generation/scripts/generate.py::_build_providers so
        the UI hint matches reality. Chat-only providers (DeepSeek etc.)
        are absent by design — image generation never falls back to a chat
        bot regardless of the main model.

        When use_linkai is enabled the hint is suppressed entirely — LinkAI
        proxies to whichever backend it deems appropriate and surfacing
        "LinkAI" alone tells the user nothing actionable."""
        use_linkai_flag = bool(local_config.get("use_linkai", False))
        linkai_configured = cls._is_real_key(local_config.get("linkai_api_key", ""))
        if use_linkai_flag and linkai_configured:
            return {"provider": "", "model": ""}

        for pid, default_model in cls._IMAGE_AUTO_ORDER:
            meta = ConfigHandler.PROVIDER_MODELS.get(pid) or {}
            key_field = meta.get("api_key_field")
            if not key_field:
                continue
            if cls._is_real_key(local_config.get(key_field, "")):
                return {"provider": pid, "model": default_model}
        return {"provider": "", "model": ""}

    @classmethod
    def _image_capability(cls, local_config: dict) -> dict:
        """Image generation. Source of truth: config["skills"]["image-generation"]["model"]
        (mirrors the per-skill config schema documented in skills/image-generation).
        The runtime resolver in skills/image-generation/scripts/generate.py
        reads this via the SKILL_IMAGE_GENERATION_MODEL env var that the
        agent_initializer syncs at startup; provider is inferred from the
        model name prefix, mirroring vision.py's design.

        ``skill`` (singular) is still tolerated as a legacy fallback —
        config.load_config() folds it into ``skills`` at startup.
        """
        skills_node = local_config.get("skills") or local_config.get("skill") or {}
        if not isinstance(skills_node, dict):
            skills_node = {}
        img_node = skills_node.get("image-generation") or {}
        if not isinstance(img_node, dict):
            img_node = {}
        explicit_model = (img_node.get("model") or "").strip()
        explicit_provider = (img_node.get("provider") or "").strip()

        # Provider resolution priority:
        #   1. Explicit `skills.image-generation.provider` (persisted via UI;
        #      supports custom model names that prefix-inference can't catch).
        #   2. Scan per-provider model catalog by model name.
        # Empty provider keeps the dropdown on "auto" when we can't tell.
        inferred_provider = ""
        if explicit_provider and explicit_provider in cls._IMAGE_PROVIDER_MODELS:
            inferred_provider = explicit_provider
        elif explicit_model:
            for pid, models in cls._IMAGE_PROVIDER_MODELS.items():
                for entry in models:
                    val = entry if isinstance(entry, str) else (entry.get("value") or "")
                    if val == explicit_model:
                        inferred_provider = pid
                        break
                if inferred_provider:
                    break

        # In auto mode the hint should reflect what generate.py will actually
        # dispatch to — surface that prediction via fallback_* so the UI
        # never claims a chat-only bot (e.g. minimax/MiniMax-M2.7) "would
        # generate the image", which is impossible.
        predicted = cls._predict_image_auto(local_config)

        return {
            "editable": True,
            "strategy": "specified" if explicit_model else "auto",
            "current_provider": inferred_provider,
            "current_model": explicit_model,
            "fallback_provider": predicted["provider"],
            "fallback_model": predicted["model"],
            "providers": list(cls._IMAGE_PROVIDER_MODELS.keys()),
            "provider_models": cls._IMAGE_PROVIDER_MODELS,
            # The dispatcher that honors a pinned provider isn't wired up
            # yet; advertise this so the UI can show a "saved but not active"
            # banner until the runtime catches up.
            "runtime_active": False,
            "note": "router_pending",
        }

    # Canonical search provider order. Mirrors PROVIDER_ORDER in
    # agent/tools/web_search/web_search.py — keep them in sync.
    _SEARCH_PROVIDERS = ("bocha", "qianfan", "zhipu", "linkai")

    _SEARCH_PROVIDER_LABELS = {
        "bocha":   {"zh": "博查", "en": "Bocha"},
        "zhipu":   {"zh": "智谱", "en": "GLM"},
        "qianfan": {"zh": "百度千帆", "en": "ERNIE"},
        "linkai":  {"zh": "LinkAI", "en": "LinkAI"},
    }

    @classmethod
    def _search_provider_key(cls, provider: str, local_config: dict) -> str:
        """Resolve the (raw) key for a given search provider."""
        if provider == "bocha":
            tools_cfg = local_config.get("tools") or {}
            block = tools_cfg.get("web_search") or {} if isinstance(tools_cfg, dict) else {}
            return (block.get("bocha_api_key") if isinstance(block, dict) else "") or os.environ.get("BOCHA_API_KEY", "")
        if provider == "zhipu":
            return local_config.get("zhipu_ai_api_key") or os.environ.get("ZHIPUAI_API_KEY", "")
        if provider == "qianfan":
            return local_config.get("qianfan_api_key") or os.environ.get("QIANFAN_API_KEY", "")
        if provider == "linkai":
            return local_config.get("linkai_api_key") or os.environ.get("LINKAI_API_KEY", "")
        return ""

    @classmethod
    def _search_capability(cls, local_config: dict) -> dict:
        """Search is editable: pick auto (default) or pin a specific backend.
        Providers reuse model-vendor keys (zhipu/qianfan/linkai) so they show
        up as configured once the user adds those vendors; bocha keeps its
        own key under tools.web_search."""
        tools_cfg = local_config.get("tools") or {}
        ws_cfg = tools_cfg.get("web_search") or {} if isinstance(tools_cfg, dict) else {}
        if not isinstance(ws_cfg, dict):
            ws_cfg = {}

        providers = []
        configured_ids = []
        for pid in cls._SEARCH_PROVIDERS:
            ok = cls._is_real_key(cls._search_provider_key(pid, local_config))
            raw_key = cls._search_provider_key(pid, local_config) if ok else ""
            providers.append({
                "id": pid,
                "label": cls._SEARCH_PROVIDER_LABELS.get(pid, pid),
                "configured": ok,
                # bocha owns its key under tools.web_search; the other three
                # piggy-back on a model-vendor credential. Frontend uses
                # this hint to decide which credential editor to surface.
                "needs_dedicated_key": pid == "bocha",
                "api_key_masked": ConfigHandler._mask_key(raw_key) if raw_key else "",
            })
            if ok:
                configured_ids.append(pid)

        strategy = (ws_cfg.get("strategy") or "auto").strip().lower()
        if strategy not in ("auto", "fixed"):
            strategy = "auto"
        fixed_provider = (ws_cfg.get("provider") or "").strip().lower()
        if fixed_provider and fixed_provider not in configured_ids:
            fixed_provider = ""

        # current_provider drives the chip in the header — show the actually
        # active backend (pinned or first auto-picked).
        if strategy == "fixed" and fixed_provider:
            current = fixed_provider
        else:
            current = configured_ids[0] if configured_ids else ""

        return {
            "editable": True,
            "strategy": strategy,
            "providers": providers,
            "configured_providers": configured_ids,
            "current_provider": current,
            "fixed_provider": fixed_provider,
            "available": bool(current),
        }

    @classmethod
    def _capabilities(cls, local_config: dict) -> dict:
        return {
            "chat":      cls._chat_capability(local_config),
            "vision":    cls._vision_capability(local_config),
            "asr":       cls._asr_capability(local_config),
            "tts":       cls._tts_capability(local_config),
            "embedding": cls._embedding_capability(local_config),
            "image":     cls._image_capability(local_config),
            "search":    cls._search_capability(local_config),
        }

    def GET(self):
        _require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            local_config = conf()
            return json.dumps({
                "status": "success",
                "providers": self._provider_overview(),
                "capabilities": self._capabilities(local_config),
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[ModelsHandler] GET failed: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            data = json.loads(web.data() or b"{}")
            action = data.get("action") or ""
            if action == "set_provider":
                return self._handle_set_provider(data)
            if action == "delete_provider":
                return self._handle_delete_provider(data)
            if action == "set_custom_provider":
                return self._handle_set_custom_provider(data)
            if action == "delete_custom_provider":
                return self._handle_delete_custom_provider(data)
            if action == "set_active_custom_provider":
                return self._handle_set_active_custom_provider(data)
            if action == "set_capability":
                return self._handle_set_capability(data)
            if action == "set_voice_reply_mode":
                return self._handle_set_voice_reply_mode(data)
            if action == "set_search_credential":
                return self._handle_set_search_credential(data)
            return json.dumps({"status": "error", "message": f"unknown action: {action!r}"})
        except Exception as e:
            logger.error(f"[ModelsHandler] POST failed: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _handle_set_provider(self, data: dict) -> str:
        provider_id = (data.get("provider_id") or "").strip()
        meta = ConfigHandler.PROVIDER_MODELS.get(provider_id)
        if not meta:
            return json.dumps({"status": "error", "message": f"unknown provider: {provider_id}"})

        # api_key absent / empty / null => leave the existing key untouched
        # (used by the "edit only base url" flow). To clear the key, callers
        # must use action=delete_provider explicitly.
        api_key_raw = data.get("api_key")
        api_key = api_key_raw.strip() if isinstance(api_key_raw, str) else ""

        # api_base presence is significant: an explicit "" means "reset to
        # default", whereas a missing key means "no change".
        api_base_present = "api_base" in data
        api_base = (data.get("api_base") or "").strip() if api_base_present else None

        applied = {}
        local_config = conf()
        file_cfg = self._read_file_config()

        key_field = meta.get("api_key_field")
        if key_field and api_key:
            local_config[key_field] = api_key
            file_cfg[key_field] = api_key
            applied[key_field] = True
        base_field = meta.get("api_base_key")
        if base_field and api_base_present:
            local_config[base_field] = api_base
            file_cfg[base_field] = api_base
            applied[base_field] = True

        if not applied:
            # Nothing actually changed (e.g. user opened the modal and hit
            # save without editing). Treat as a successful no-op so the
            # frontend can show "Saved" instead of surfacing an error.
            return json.dumps({"status": "success", "provider": provider_id, "noop": True})

        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] provider {provider_id} updated: {sorted(applied.keys())}")

        # Vendor credentials affect bot routing for any capability that uses
        # them; safest to reset Bridge so the next request rebuilds bots.
        self._reset_bridge()
        return json.dumps({"status": "success", "provider": provider_id})

    def _handle_delete_provider(self, data: dict) -> str:
        provider_id = (data.get("provider_id") or "").strip()
        meta = ConfigHandler.PROVIDER_MODELS.get(provider_id)
        if not meta:
            return json.dumps({"status": "error", "message": f"unknown provider: {provider_id}"})

        local_config = conf()
        file_cfg = self._read_file_config()

        cleared = []
        for field_name in (meta.get("api_key_field"), meta.get("api_base_key")):
            if not field_name:
                continue
            # Always write the key — even if it was absent before — so the
            # in-memory conf() reflects the cleared state without needing a
            # restart. (`in local_config` was too strict: provider keys that
            # were ever set then deleted manually wouldn't get reset.)
            local_config[field_name] = ""
            file_cfg[field_name] = ""
            cleared.append(field_name)

        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] provider {provider_id} cleared: {cleared}")
        self._reset_bridge()
        return json.dumps({"status": "success", "provider": provider_id, "cleared": cleared})

    # ------------------------------------------------------------------
    # Multiple custom (OpenAI-compatible) providers
    # ------------------------------------------------------------------
    # These actions manage the ``custom_providers`` list.  Activation is done
    # by setting ``bot_type`` to ``"custom:<id>"``.  There is no separate
    # ``custom_active_provider`` field — a single source of truth.

    @staticmethod
    def _normalize_custom_providers(raw) -> List[dict]:
        """Return a clean list of provider dicts (drops malformed entries)."""
        if not isinstance(raw, list):
            return []
        out = []
        for p in raw:
            if isinstance(p, dict) and (p.get("id") or "").strip():
                out.append(p)
        return out

    def _persist_custom_providers(self, providers: List[dict], bot_type=None) -> None:
        """Write the providers list to both in-memory conf and the on-disk
        config, then reset the bridge so bots rebuild.

        If ``bot_type`` is given, also update ``bot_type``.  When activating a
        provider (bot_type is ``custom:<id>``), also write the provider's
        ``model`` into the global ``model`` field so that all paths (chat,
        agent, vision) automatically use the correct model."""
        from models.custom_provider import parse_custom_bot_type

        local_config = conf()
        file_cfg = self._read_file_config()
        local_config["custom_providers"] = providers
        file_cfg["custom_providers"] = providers
        if bot_type is not None:
            local_config["bot_type"] = bot_type
            file_cfg["bot_type"] = bot_type
            # Sync the provider's model into the global model field.
            _, pid = parse_custom_bot_type(bot_type)
            if pid:
                provider = next((p for p in providers if p.get("id") == pid), None)
                if provider and provider.get("model"):
                    local_config["model"] = provider["model"]
                    file_cfg["model"] = provider["model"]
        self._write_file_config(file_cfg)
        self._reset_bridge()

    def _handle_set_custom_provider(self, data: dict) -> str:
        """Add a new custom provider or update an existing one.

        Payload::

            {
              "action": "set_custom_provider",
              "id": "3f2a9c1b",             # required for edit; omit for create
              "name": "my-provider",         # required, display label
              "api_base": "https://...",     # required when creating
              "api_key": "sk-...",           # optional on edit (keep existing)
              "model": "model-name",         # optional default model
              "make_active": true            # optional, also activate it
            }
        """
        from models.custom_provider import generate_provider_id, parse_custom_bot_type

        name = (data.get("name") or "").strip()
        if not name:
            return json.dumps({"status": "error", "message": "name is required"})

        provider_id = (data.get("id") or "").strip()
        api_base = (data.get("api_base") or "").strip()
        # api_key omitted/empty on edit => keep the existing one.
        api_key_raw = data.get("api_key")
        api_key = api_key_raw.strip() if isinstance(api_key_raw, str) else ""
        model = (data.get("model") or "").strip()
        make_active = bool(data.get("make_active"))

        local_config = conf()
        providers = self._normalize_custom_providers(local_config.get("custom_providers"))

        existing = next((p for p in providers if p.get("id") == provider_id), None) if provider_id else None
        if existing is None:
            # Creating a new provider — api_base is mandatory.
            if not api_base:
                return json.dumps({"status": "error", "message": "api_base is required"})
            provider_id = generate_provider_id()
            entry = {"id": provider_id, "name": name, "api_key": api_key, "api_base": api_base}
            if model:
                entry["model"] = model
            providers.append(entry)
            created = True
        else:
            existing["name"] = name
            if api_base:
                existing["api_base"] = api_base
            if api_key:
                existing["api_key"] = api_key
            # Only touch model when explicitly provided in the payload; an
            # explicit empty string clears it, a missing key keeps it (the
            # UI modal no longer sends model, so manual config survives edits).
            if "model" in data:
                if model:
                    existing["model"] = model
                else:
                    existing.pop("model", None)
            created = False

        # Decide bot_type — only switch when explicitly requested.
        new_bot_type = None
        if make_active:
            new_bot_type = f"custom:{provider_id}"

        self._persist_custom_providers(providers, new_bot_type)
        logger.info(
            f"[ModelsHandler] custom provider {name!r} (id={provider_id}) "
            f"{'created' if created else 'updated'}"
        )
        return json.dumps({
            "status": "success",
            "id": provider_id,
            "name": name,
            "created": created,
        })

    def _handle_delete_custom_provider(self, data: dict) -> str:
        """Remove a custom provider by id."""
        from models.custom_provider import parse_custom_bot_type

        provider_id = (data.get("id") or "").strip()
        if not provider_id:
            return json.dumps({"status": "error", "message": "id is required"})

        local_config = conf()
        providers = self._normalize_custom_providers(local_config.get("custom_providers"))
        remaining = [p for p in providers if p.get("id") != provider_id]
        if len(remaining) == len(providers):
            return json.dumps({"status": "error", "message": f"unknown custom provider id: {provider_id}"})

        # If the deleted provider was active, fall back to the first remaining.
        _, current_active_id = parse_custom_bot_type(local_config.get("bot_type") or "")
        new_bot_type = None
        if current_active_id == provider_id:
            if remaining:
                new_bot_type = f"custom:{remaining[0]['id']}"
            else:
                new_bot_type = "custom"  # revert to legacy

        self._persist_custom_providers(remaining, new_bot_type)
        logger.info(f"[ModelsHandler] custom provider id={provider_id} deleted")
        return json.dumps({"status": "success", "id": provider_id})

    def _handle_set_active_custom_provider(self, data: dict) -> str:
        """Activate a custom provider by setting bot_type to 'custom:<id>'."""
        provider_id = (data.get("id") or "").strip()
        if not provider_id:
            return json.dumps({"status": "error", "message": "id is required"})

        local_config = conf()
        providers = self._normalize_custom_providers(local_config.get("custom_providers"))
        if not any(p.get("id") == provider_id for p in providers):
            return json.dumps({"status": "error", "message": f"unknown custom provider id: {provider_id}"})

        new_bot_type = f"custom:{provider_id}"
        self._persist_custom_providers(providers, new_bot_type)
        logger.info(f"[ModelsHandler] active custom provider set to id={provider_id}")
        return json.dumps({"status": "success", "active_id": provider_id})

    def _handle_set_capability(self, data: dict) -> str:
        capability = (data.get("capability") or "").strip()
        provider_id = (data.get("provider_id") or "").strip()
        model = (data.get("model") or "").strip()

        if capability == "chat":
            return self._set_chat(provider_id, model)
        if capability == "vision":
            return self._set_vision(provider_id, model)
        if capability == "asr":
            return self._set_asr(provider_id, model)
        if capability == "tts":
            return self._set_tts(provider_id, model, (data.get("voice") or "").strip())
        if capability == "embedding":
            return self._set_embedding(provider_id, model)
        if capability == "image":
            return self._set_image(provider_id, model)
        if capability == "search":
            return self._set_search(
                (data.get("strategy") or "").strip().lower(),
                (data.get("provider") or "").strip().lower(),
            )
        return json.dumps({"status": "error", "message": f"capability not editable: {capability}"})

    def _set_image(self, provider_id: str, model: str) -> str:
        # Source of truth: skills.image-generation.{provider, model}. The
        # provider field is persisted so users picking a custom model under
        # a specific vendor still get routed there — runtime falls back to
        # model-name prefix inference only when provider is empty.
        local_config = conf()
        file_cfg = self._read_file_config()

        self._set_nested_namespace_value(local_config, "skills", "image-generation", "model", model or "")
        self._set_nested_namespace_value(file_cfg, "skills", "image-generation", "model", model or "")
        self._set_nested_namespace_value(local_config, "skills", "image-generation", "provider", provider_id or "")
        self._set_nested_namespace_value(file_cfg, "skills", "image-generation", "provider", provider_id or "")
        self._drop_legacy_namespace(local_config, "skill", "skills", child="image-generation")
        self._drop_legacy_namespace(file_cfg, "skill", "skills", child="image-generation")

        self._write_file_config(file_cfg)

        # The skill subprocess reads SKILL_IMAGE_GENERATION_{MODEL,PROVIDER}
        # from env at startup; mirror the change so live edits apply without
        # restart.
        model_env = "SKILL_IMAGE_GENERATION_MODEL"
        provider_env = "SKILL_IMAGE_GENERATION_PROVIDER"
        if model:
            os.environ[model_env] = model
        else:
            os.environ.pop(model_env, None)
        if provider_id:
            os.environ[provider_env] = provider_id
        else:
            os.environ.pop(provider_env, None)

        logger.info(f"[ModelsHandler] image updated: provider={provider_id!r} model={model!r}")
        return json.dumps({
            "status": "success",
            "provider": provider_id,
            "model": model,
            "router_pending": True,
        })

    def _set_chat(self, provider_id: str, model: str) -> str:
        # Accept expanded custom provider ids ("custom:<id>") as well as the
        # built-in vendors, so the chat capability card and the custom
        # providers section behave consistently.
        custom_provider = None
        if provider_id.startswith("custom:"):
            from models.custom_provider import parse_custom_bot_type
            _, custom_id = parse_custom_bot_type(provider_id)
            providers = self._normalize_custom_providers(conf().get("custom_providers"))
            custom_provider = next((p for p in providers if p.get("id") == custom_id), None)
            if custom_provider is None:
                return json.dumps({"status": "error", "message": f"unknown custom provider id: {custom_id}"})
        elif provider_id and provider_id not in ConfigHandler.PROVIDER_MODELS:
            return json.dumps({"status": "error", "message": f"unknown provider: {provider_id}"})

        applied = {}
        local_config = conf()
        file_cfg = self._read_file_config()

        # Fall back to the custom provider's default model when none is given.
        if not model and custom_provider:
            model = custom_provider.get("model") or ""

        if provider_id:
            bot_type_value = "chatGPT" if provider_id == "openai" else provider_id
            local_config["bot_type"] = bot_type_value
            file_cfg["bot_type"] = bot_type_value
            applied["bot_type"] = bot_type_value
            use_linkai = (provider_id == "linkai")
            local_config["use_linkai"] = use_linkai
            file_cfg["use_linkai"] = use_linkai
            applied["use_linkai"] = use_linkai
        if model:
            local_config["model"] = model
            file_cfg["model"] = model
            applied["model"] = model

        if not applied:
            return json.dumps({"status": "success", "applied": {}, "noop": True})

        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] chat updated: {applied}")
        self._reset_bridge()
        return json.dumps({"status": "success", "applied": applied})

    def _set_vision(self, provider_id: str, model: str) -> str:
        # Source of truth: tools.vision.{provider, model}. The provider field
        # is persisted so users picking a custom model under a specific vendor
        # still get routed there — runtime falls back to model-name prefix
        # inference only when provider is empty.
        # Validate provider_id — mirrors _set_chat / _set_embedding pattern.
        if provider_id.startswith("custom:"):
            from models.custom_provider import parse_custom_bot_type
            _, custom_id = parse_custom_bot_type(provider_id)
            providers = self._normalize_custom_providers(conf().get("custom_providers"))
            custom_provider = next((p for p in providers if p.get("id") == custom_id), None)
            if custom_provider is None:
                return json.dumps({"status": "error", "message": f"unknown custom provider id: {custom_id}"})
            if not model:
                model = custom_provider.get("model") or ""
        elif provider_id and provider_id not in {k for k in ModelsHandler._VISION_PROVIDER_MODELS if k != "custom"}:
            return json.dumps({"status": "error", "message": f"unknown provider: {provider_id}"})

        if provider_id and not model:
            return json.dumps({
                "status": "error",
                "message": "vision model is required when a provider is selected",
            })

        local_config = conf()
        file_cfg = self._read_file_config()
        self._set_nested_namespace_value(file_cfg, "tools", "vision", "model", model)
        self._set_nested_namespace_value(local_config, "tools", "vision", "model", model)
        self._set_nested_namespace_value(file_cfg, "tools", "vision", "provider", provider_id or "")
        self._set_nested_namespace_value(local_config, "tools", "vision", "provider", provider_id or "")
        self._drop_legacy_namespace(file_cfg, "tool", "tools", child="vision")
        self._drop_legacy_namespace(local_config, "tool", "tools", child="vision")

        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] vision updated: provider={provider_id!r} model={model!r}")
        return json.dumps({"status": "success", "provider": provider_id, "model": model})

    @staticmethod
    def _set_nested_namespace_value(cfg, top: str, name: str, key: str, value):
        """Set ``cfg[top][name][key] = value``, creating missing dicts."""
        bucket = cfg.get(top)
        if not isinstance(bucket, dict):
            bucket = {}
        node = bucket.get(name)
        if not isinstance(node, dict):
            node = {}
        node[key] = value
        bucket[name] = node
        cfg[top] = bucket

    @staticmethod
    def _drop_legacy_namespace(cfg, legacy: str, canonical: str, child: str) -> None:
        """Strip the deprecated singular key so config.json stays single-source."""
        legacy_section = cfg.get(legacy)
        if not isinstance(legacy_section, dict):
            return
        legacy_section.pop(child, None)
        if legacy_section:
            cfg[legacy] = legacy_section
        else:
            cfg.pop(legacy, None)

    def _handle_set_voice_reply_mode(self, data: dict) -> str:
        # UI picker (off / voice_if_voice / always) maps to the legacy
        # always_reply_voice + voice_reply_voice pair that chat_channel.py
        # reads, so all channels (web/feishu/wecom/...) share the routing.
        mode = (data.get("mode") or "").strip().lower()
        if mode not in ("off", "voice_if_voice", "always"):
            return json.dumps({"status": "error", "message": f"invalid mode: {mode!r}"})
        always = (mode == "always")
        if_voice = (mode == "voice_if_voice")
        local_config = conf()
        file_cfg = self._read_file_config()
        local_config["always_reply_voice"] = always
        local_config["voice_reply_voice"] = if_voice
        file_cfg["always_reply_voice"] = always
        file_cfg["voice_reply_voice"] = if_voice
        self._write_file_config(file_cfg)
        logger.info(
            f"[ModelsHandler] voice reply mode set: {mode!r} "
            f"(always_reply_voice={always}, voice_reply_voice={if_voice})"
        )
        return json.dumps({"status": "success", "mode": mode})

    def _set_simple(self, key: str, value: str) -> str:
        local_config = conf()
        file_cfg = self._read_file_config()
        local_config[key] = value
        file_cfg[key] = value
        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] {key} set: {value!r}")
        # Hot-swap the cached voice bot so the change takes effect immediately.
        if key in ("voice_to_text", "text_to_voice"):
            self._refresh_voice_routing()
        return json.dumps({"status": "success", key: value})

    def _set_asr(self, provider_id: str, model: str) -> str:
        local_config = conf()
        file_cfg = self._read_file_config()
        local_config["voice_to_text"] = provider_id
        file_cfg["voice_to_text"] = provider_id
        # Only overwrite the model when one is supplied. An empty model means
        # "keep whatever is configured" so switching provider from the console
        # never wipes a user's hand-set voice_to_text_model (runtime falls back
        # to the engine default via `or DEFAULT_ASR_MODEL` regardless).
        if model:
            local_config["voice_to_text_model"] = model
            file_cfg["voice_to_text_model"] = model
        self._write_file_config(file_cfg)
        logger.info(
            f"[ModelsHandler] asr updated: provider={provider_id!r} "
            f"model={model!r}"
        )
        self._refresh_voice_routing()
        return json.dumps({
            "status": "success",
            "provider": provider_id,
            "model": local_config.get("voice_to_text_model", ""),
        })

    def _set_tts(self, provider_id: str, model: str, voice: str = "") -> str:
        local_config = conf()
        file_cfg = self._read_file_config()
        local_config["text_to_voice"] = provider_id
        file_cfg["text_to_voice"] = provider_id
        local_config["text_to_voice_model"] = model
        file_cfg["text_to_voice_model"] = model
        local_config["tts_voice_id"] = voice
        file_cfg["tts_voice_id"] = voice
        self._write_file_config(file_cfg)
        logger.info(
            f"[ModelsHandler] tts updated: provider={provider_id!r} "
            f"model={model!r} voice={voice!r}"
        )
        self._refresh_voice_routing()
        return json.dumps({
            "status": "success",
            "provider": provider_id, "model": model, "voice": voice,
        })

    @staticmethod
    def _refresh_voice_routing() -> None:
        try:
            from bridge.bridge import Bridge
            Bridge().refresh_voice()
        except Exception as e:
            logger.warning(f"[ModelsHandler] Bridge voice refresh failed: {e}")

    def _set_embedding(self, provider_id: str, model: str) -> str:
        # Validate provider_id — mirrors _set_chat's validation pattern.
        if provider_id.startswith("custom:"):
            from models.custom_provider import parse_custom_bot_type
            _, custom_id = parse_custom_bot_type(provider_id)
            providers = self._normalize_custom_providers(conf().get("custom_providers"))
            custom_provider = next((p for p in providers if p.get("id") == custom_id), None)
            if custom_provider is None:
                return json.dumps({"status": "error", "message": f"unknown custom provider id: {custom_id}"})
            # Fall back to the custom provider's default model when none is given.
            if not model:
                model = custom_provider.get("model") or ""
        elif provider_id and provider_id not in {p for p in ModelsHandler._EMBEDDING_PROVIDERS if p != "custom"}:
            return json.dumps({"status": "error", "message": f"unknown provider: {provider_id}"})

        # A provider without a model leaves the runtime in a broken half-state,
        # so reject that explicitly instead of silently writing it through.
        if provider_id and not model:
            return json.dumps({
                "status": "error",
                "message": "embedding model is required when a provider is selected",
            })
        local_config = conf()
        file_cfg = self._read_file_config()
        local_config["embedding_provider"] = provider_id
        file_cfg["embedding_provider"] = provider_id
        local_config["embedding_model"] = model
        file_cfg["embedding_model"] = model
        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] embedding updated: provider={provider_id!r} model={model!r}")
        # The next /memory rebuild-index command hot-swaps the provider onto
        # the running MemoryManager (see plugins/cow_cli). The dim may have
        # changed, so the frontend prompts the user to rebuild.
        return json.dumps({"status": "success", "provider": provider_id, "model": model})

    def _set_search(self, strategy: str, provider: str) -> str:
        """Persist search routing under tools.web_search.{strategy,provider}.

        strategy 'auto'  -> provider field is cleared (auto picks at call time)
        strategy 'fixed' -> provider must be in the canonical list; runtime
                            silently falls back to auto if its key is missing.
        """
        if strategy not in ("auto", "fixed"):
            return json.dumps({"status": "error", "message": f"invalid strategy: {strategy!r}"})
        if strategy == "fixed":
            if provider not in self._SEARCH_PROVIDERS:
                return json.dumps({"status": "error", "message": f"unknown provider: {provider!r}"})
        else:
            provider = ""

        local_config = conf()
        file_cfg = self._read_file_config()
        self._set_nested_namespace_value(local_config, "tools", "web_search", "strategy", strategy)
        self._set_nested_namespace_value(file_cfg,     "tools", "web_search", "strategy", strategy)
        self._set_nested_namespace_value(local_config, "tools", "web_search", "provider", provider)
        self._set_nested_namespace_value(file_cfg,     "tools", "web_search", "provider", provider)
        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] search updated: strategy={strategy!r} provider={provider!r}")
        return json.dumps({"status": "success", "strategy": strategy, "provider": provider})

    def _handle_set_search_credential(self, data: dict) -> str:
        """Persist the bocha API key under tools.web_search.bocha_api_key.

        The other three providers (zhipu/qianfan/linkai) reuse model-vendor
        credentials, so they go through set_provider with the standard
        model-vendor flow.
        """
        api_key = (data.get("api_key") or "").strip() if isinstance(data.get("api_key"), str) else ""
        local_config = conf()
        file_cfg = self._read_file_config()
        self._set_nested_namespace_value(local_config, "tools", "web_search", "bocha_api_key", api_key)
        self._set_nested_namespace_value(file_cfg,     "tools", "web_search", "bocha_api_key", api_key)
        self._write_file_config(file_cfg)
        logger.info(f"[ModelsHandler] search credential set: bocha_api_key={'***' if api_key else ''}")
        return json.dumps({"status": "success", "provider": "bocha"})

    @staticmethod
    def _reset_bridge() -> None:
        try:
            from bridge.bridge import Bridge
            Bridge().reset_bot()
            logger.info("[ModelsHandler] Bridge bot routing reset")
        except Exception as e:
            logger.warning(f"[ModelsHandler] Bridge reset failed: {e}")


class ChannelsHandler:
    """API for managing external channel configurations (feishu, dingtalk, etc)."""

    CHANNEL_DEFS = OrderedDict([
        ("weixin", {
            "label": {"zh": "微信", "en": "WeChat"},
            "icon": "fa-comment",
            "color": "emerald",
            "fields": [],
        }),
        ("feishu", {
            "label": {"zh": "飞书", "en": "Feishu"},
            "icon": "fa-paper-plane",
            "color": "blue",
            "fields": [
                {"key": "feishu_app_id", "label": "App ID", "type": "text"},
                {"key": "feishu_app_secret", "label": "App Secret", "type": "secret"},
            ],
        }),
        ("dingtalk", {
            "label": {"zh": "钉钉", "en": "DingTalk"},
            "icon": "fa-comments",
            "color": "blue",
            "fields": [
                {"key": "dingtalk_client_id", "label": "Client ID", "type": "text"},
                {"key": "dingtalk_client_secret", "label": "Client Secret", "type": "secret"},
            ],
        }),
        ("wecom_bot", {
            "label": {"zh": "企微智能机器人", "en": "WeCom Bot"},
            "icon": "fa-robot",
            "color": "emerald",
            "fields": [
                {"key": "wecom_bot_id", "label": "Bot ID", "type": "text"},
                {"key": "wecom_bot_secret", "label": "Secret", "type": "secret"},
            ],
        }),
        ("qq", {
            "label": {"zh": "QQ 机器人", "en": "QQ Bot"},
            "icon": "fa-comment",
            "color": "blue",
            "fields": [
                {"key": "qq_app_id", "label": "App ID", "type": "text"},
                {"key": "qq_app_secret", "label": "App Secret", "type": "secret"},
            ],
        }),
        ("wechatcom_app", {
            "label": {"zh": "企微自建应用", "en": "WeCom App"},
            "icon": "fa-building",
            "color": "emerald",
            "fields": [
                {"key": "wechatcom_corp_id", "label": "Corp ID", "type": "text"},
                {"key": "wechatcomapp_agent_id", "label": "Agent ID", "type": "text"},
                {"key": "wechatcomapp_secret", "label": "Secret", "type": "secret"},
                {"key": "wechatcomapp_token", "label": "Token", "type": "secret"},
                {"key": "wechatcomapp_aes_key", "label": "AES Key", "type": "secret"},
                {"key": "wechatcomapp_port", "label": "Port", "type": "number", "default": 9898},
            ],
        }),
        ("wechat_kf", {
            "label": {"zh": "微信客服", "en": "WeChat Customer Service"},
            "icon": "fa-headset",
            "color": "emerald",
            "fields": [
                {"key": "wechat_kf_corp_id", "label": "Corp ID", "type": "text"},
                {"key": "wechat_kf_secret", "label": "Secret", "type": "secret"},
                {"key": "wechat_kf_token", "label": "Token", "type": "secret"},
                {"key": "wechat_kf_aes_key", "label": "AES Key", "type": "secret"},
                {"key": "wechat_kf_port", "label": "Port", "type": "number", "default": 9888},
            ],
        }),
        ("wechatmp", {
            "label": {"zh": "公众号", "en": "WeChat MP"},
            "icon": "fa-comment-dots",
            "color": "emerald",
            "fields": [
                {"key": "wechatmp_app_id", "label": "App ID", "type": "text"},
                {"key": "wechatmp_app_secret", "label": "App Secret", "type": "secret"},
                {"key": "wechatmp_token", "label": "Token", "type": "secret"},
                {"key": "wechatmp_aes_key", "label": "AES Key", "type": "secret"},
                {"key": "wechatmp_port", "label": "Port", "type": "number", "default": 8080},
            ],
        }),
        ("telegram", {
            "label": {"zh": "Telegram", "en": "Telegram"},
            "icon": "fa-paper-plane",
            "color": "sky",
            "fields": [
                {"key": "telegram_token", "label": "Bot Token", "type": "secret"},
            ],
        }),
        ("slack", {
            "label": {"zh": "Slack", "en": "Slack"},
            "icon": "fa-hashtag",
            "color": "purple",
            "fields": [
                {"key": "slack_bot_token", "label": "Bot Token (xoxb-)", "type": "secret"},
                {"key": "slack_app_token", "label": "App Token (xapp-)", "type": "secret"},
            ],
        }),
        ("discord", {
            "label": {"zh": "Discord", "en": "Discord"},
            "icon": "fa-discord",
            "color": "indigo",
            "fields": [
                {"key": "discord_token", "label": "Bot Token", "type": "secret"},
            ],
        }),
    ])

    @staticmethod
    def _get_weixin_login_status() -> str:
        try:
            import sys
            app_module = sys.modules.get('__main__') or sys.modules.get('app')
            mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
            if mgr:
                ch = mgr.get_channel("weixin")
                if ch and hasattr(ch, 'login_status'):
                    return ch.login_status
        except Exception:
            pass
        return "unknown"

    @staticmethod
    def _mask_secret(value: str) -> str:
        if not value or len(value) <= 8:
            return value
        return value[:4] + "*" * (len(value) - 8) + value[-4:]

    @staticmethod
    def _parse_channel_list(raw) -> list:
        if isinstance(raw, list):
            return [ch.strip() for ch in raw if ch.strip()]
        if isinstance(raw, str):
            return [ch.strip() for ch in raw.split(",") if ch.strip()]
        return []

    @classmethod
    def _active_channel_set(cls) -> set:
        return set(cls._parse_channel_list(conf().get("channel_type", "")))

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from common import i18n
            local_config = conf()
            active_channels = self._active_channel_set()
            channels = []
            is_hant = i18n.get_language() == i18n.ZH_HANT
            for ch_name, ch_def in self.CHANNEL_DEFS.items():
                fields_out = []
                for f in ch_def["fields"]:
                    raw_val = local_config.get(f["key"], f.get("default", ""))
                    if f["type"] == "secret" and raw_val:
                        display_val = self._mask_secret(str(raw_val))
                    else:
                        display_val = raw_val
                    
                    label_val = f["label"]
                    if is_hant and isinstance(label_val, str):
                        label_val = i18n.to_traditional(label_val)
                    elif is_hant and isinstance(label_val, dict):
                        label_val = label_val.copy()
                        label_val["zh-Hant"] = i18n.to_traditional(label_val.get("zh", ""))

                    fields_out.append({
                        "key": f["key"],
                        "label": label_val,
                        "type": f["type"],
                        "value": display_val,
                        "default": f.get("default", ""),
                    })
                
                label_val = ch_def["label"]
                if is_hant and isinstance(label_val, str):
                    label_val = i18n.to_traditional(label_val)
                elif is_hant and isinstance(label_val, dict):
                    label_val = label_val.copy()
                    label_val["zh-Hant"] = i18n.to_traditional(label_val.get("zh", ""))

                ch_info = {
                    "name": ch_name,
                    "label": label_val,
                    "icon": ch_def["icon"],
                    "color": ch_def["color"],
                    "active": ch_name in active_channels,
                    "fields": fields_out,
                }
                if ch_name == "weixin" and ch_name in active_channels:
                    ch_info["login_status"] = self._get_weixin_login_status()
                channels.append(ch_info)
            return json.dumps({"status": "success", "channels": channels}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Channels API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            action = body.get("action")
            channel_name = body.get("channel")

            if not action or not channel_name:
                return json.dumps({"status": "error", "message": "action and channel required"})

            if channel_name not in self.CHANNEL_DEFS:
                return json.dumps({"status": "error", "message": f"unknown channel: {channel_name}"})

            if action == "save":
                return self._handle_save(channel_name, body.get("config", {}))
            elif action == "connect":
                return self._handle_connect(channel_name, body.get("config", {}))
            elif action == "disconnect":
                return self._handle_disconnect(channel_name)
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
        except Exception as e:
            logger.error(f"[WebChannel] Channels POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _handle_save(self, channel_name: str, updates: dict):
        ch_def = self.CHANNEL_DEFS[channel_name]
        valid_keys = {f["key"] for f in ch_def["fields"]}
        secret_keys = {f["key"] for f in ch_def["fields"] if f["type"] == "secret"}

        local_config = conf()
        applied = {}
        for key, value in updates.items():
            if key not in valid_keys:
                continue
            if key in secret_keys:
                if not value or (len(value) > 8 and "*" * 4 in value):
                    continue
            field_def = next((f for f in ch_def["fields"] if f["key"] == key), None)
            if field_def:
                if field_def["type"] == "number":
                    value = int(value)
                elif field_def["type"] == "bool":
                    value = bool(value)
            local_config[key] = value
            applied[key] = value

        if not applied:
            return json.dumps({"status": "error", "message": "no valid fields to update"})

        config_path = os.path.join(get_data_root(), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg.update(applied)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        logger.info(f"[WebChannel] Channel '{channel_name}' config updated: {list(applied.keys())}")

        should_restart = False
        active_channels = self._active_channel_set()
        if channel_name in active_channels:
            should_restart = True
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
                if mgr:
                    threading.Thread(
                        target=mgr.restart,
                        args=(channel_name,),
                        daemon=True,
                    ).start()
                    logger.info(f"[WebChannel] Channel '{channel_name}' restart triggered")
            except Exception as e:
                logger.warning(f"[WebChannel] Failed to restart channel '{channel_name}': {e}")

        return json.dumps({
            "status": "success",
            "applied": list(applied.keys()),
            "restarted": should_restart,
        }, ensure_ascii=False)

    def _handle_connect(self, channel_name: str, updates: dict):
        """Save config fields, add channel to channel_type, and start it."""
        ch_def = self.CHANNEL_DEFS[channel_name]
        valid_keys = {f["key"] for f in ch_def["fields"]}
        secret_keys = {f["key"] for f in ch_def["fields"] if f["type"] == "secret"}

        # Feishu connected via web console must use websocket (long connection) mode
        if channel_name == "feishu":
            updates.setdefault("feishu_event_mode", "websocket")
            valid_keys.add("feishu_event_mode")

        local_config = conf()
        applied = {}
        for key, value in updates.items():
            if key not in valid_keys:
                continue
            if key in secret_keys:
                if not value or (len(value) > 8 and "*" * 4 in value):
                    continue
            field_def = next((f for f in ch_def["fields"] if f["key"] == key), None)
            if field_def:
                if field_def["type"] == "number":
                    value = int(value)
                elif field_def["type"] == "bool":
                    value = bool(value)
            local_config[key] = value
            applied[key] = value

        existing = self._parse_channel_list(conf().get("channel_type", ""))
        if channel_name not in existing:
            existing.append(channel_name)
        new_channel_type = ",".join(existing)
        local_config["channel_type"] = new_channel_type

        config_path = os.path.join(get_data_root(), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg.update(applied)
        file_cfg["channel_type"] = new_channel_type
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        logger.info(f"[WebChannel] Channel '{channel_name}' connecting, channel_type={new_channel_type}")

        # Feishu pulls its SDK bundle on first use; tell the UI so it can warn
        # about the one-time wait rather than reporting an instant success.
        downloading = False
        if channel_name == "feishu":
            try:
                from channel.feishu import lark_install
                downloading = lark_install.needs_download()
            except Exception as e:
                logger.warning(f"[WebChannel] Could not check Feishu SDK state: {e}")

        def _do_start():
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                clear_fn = getattr(app_module, '_clear_singleton_cache', None) if app_module else None
                mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
                if mgr is None:
                    logger.warning(f"[WebChannel] ChannelManager not available, cannot start '{channel_name}'")
                    return
                # Stop existing instance first if still running (e.g. re-connect without disconnect)
                existing_ch = mgr.get_channel(channel_name)
                if existing_ch is not None:
                    logger.info(f"[WebChannel] Stopping existing '{channel_name}' before reconnect...")
                    mgr.stop(channel_name)
                # Always wait for the remote service to release the old connection before
                # establishing a new one (DingTalk drops callbacks on duplicate connections)
                logger.info(f"[WebChannel] Waiting for '{channel_name}' old connection to close...")
                time.sleep(5)
                if clear_fn:
                    clear_fn(channel_name)
                logger.info(f"[WebChannel] Starting channel '{channel_name}'...")
                mgr.start([channel_name], first_start=False)
                logger.info(f"[WebChannel] Channel '{channel_name}' start completed")
            except Exception as e:
                logger.error(f"[WebChannel] Failed to start channel '{channel_name}': {e}",
                             exc_info=True)

        threading.Thread(target=_do_start, daemon=True).start()

        return json.dumps({
            "status": "success",
            "channel_type": new_channel_type,
            "downloading": downloading,
        }, ensure_ascii=False)

    def _handle_disconnect(self, channel_name: str):
        existing = self._parse_channel_list(conf().get("channel_type", ""))
        existing = [ch for ch in existing if ch != channel_name]
        new_channel_type = ",".join(existing)

        local_config = conf()
        local_config["channel_type"] = new_channel_type

        config_path = os.path.join(get_data_root(), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        else:
            file_cfg = {}
        file_cfg["channel_type"] = new_channel_type
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(file_cfg, f, indent=4, ensure_ascii=False)

        def _do_stop():
            try:
                import sys
                app_module = sys.modules.get('__main__') or sys.modules.get('app')
                mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
                clear_fn = getattr(app_module, '_clear_singleton_cache', None) if app_module else None
                if mgr:
                    mgr.stop(channel_name)
                else:
                    logger.warning(f"[WebChannel] ChannelManager not found, cannot stop '{channel_name}'")
                if clear_fn:
                    clear_fn(channel_name)
                logger.info(f"[WebChannel] Channel '{channel_name}' disconnected, "
                            f"channel_type={new_channel_type}")
            except Exception as e:
                logger.warning(f"[WebChannel] Failed to stop channel '{channel_name}': {e}",
                               exc_info=True)

        threading.Thread(target=_do_stop, daemon=True).start()

        return json.dumps({
            "status": "success",
            "channel_type": new_channel_type,
        }, ensure_ascii=False)


class WeixinQrHandler:
    """Handle WeChat QR code login from the web console.

    GET  /api/weixin/qrlogin          → fetch a new QR code
    POST /api/weixin/qrlogin          → poll QR status or start channel after login
    """

    _qr_state = {}

    @staticmethod
    def _qr_to_data_uri(data: str) -> str:
        """Generate a QR code as a PNG data URI."""
        try:
            import qrcode as qr_lib
            import io
            import base64
            qr = qr_lib.QRCode(error_correction=qr_lib.constants.ERROR_CORRECT_L, box_size=6, border=2)
            qr.add_data(data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/png;base64,{b64}"
        except ImportError:
            return ""

    @staticmethod
    def _get_running_channel():
        try:
            import sys
            app_module = sys.modules.get('__main__') or sys.modules.get('app')
            mgr = getattr(app_module, '_channel_mgr', None) if app_module else None
            if mgr:
                return mgr.get_channel("weixin")
        except Exception:
            pass
        return None

    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            running_ch = self._get_running_channel()
            if running_ch and hasattr(running_ch, '_current_qr_url') and running_ch._current_qr_url:
                qr_image = self._qr_to_data_uri(running_ch._current_qr_url)
                return json.dumps({
                    "status": "success",
                    "qrcode_url": running_ch._current_qr_url,
                    "qr_image": qr_image,
                    "source": "channel",
                })

            from channel.weixin.weixin_api import WeixinApi, DEFAULT_BASE_URL
            base_url = conf().get("weixin_base_url", DEFAULT_BASE_URL)
            api = WeixinApi(base_url=base_url)
            qr_resp = api.fetch_qr_code()
            qrcode = qr_resp.get("qrcode", "")
            qrcode_url = qr_resp.get("qrcode_img_content", "")
            if not qrcode:
                return json.dumps({"status": "error", "message": "No QR code returned"})
            qr_image = self._qr_to_data_uri(qrcode_url)
            WeixinQrHandler._qr_state = {
                "qrcode": qrcode,
                "qrcode_url": qrcode_url,
                "base_url": base_url,
            }
            return json.dumps({"status": "success", "qrcode_url": qrcode_url, "qr_image": qr_image})
        except Exception as e:
            logger.error(f"[WebChannel] WeixinQr GET error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            action = body.get("action", "poll")

            if action == "poll":
                return self._poll_status()
            elif action == "refresh":
                return self.GET()
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
        except Exception as e:
            logger.error(f"[WebChannel] WeixinQr POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _poll_status(self):
        state = WeixinQrHandler._qr_state
        qrcode = state.get("qrcode", "")
        base_url = state.get("base_url", "")
        if not qrcode:
            return json.dumps({"status": "error", "message": "No active QR session"})

        from channel.weixin.weixin_api import WeixinApi, DEFAULT_BASE_URL
        api = WeixinApi(base_url=base_url or DEFAULT_BASE_URL)
        try:
            status_resp = api.poll_qr_status(qrcode, timeout=10)
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

        qr_status = status_resp.get("status", "wait")

        if qr_status == "confirmed":
            bot_token = status_resp.get("bot_token", "")
            bot_id = status_resp.get("ilink_bot_id", "")
            result_base_url = status_resp.get("baseurl", base_url)
            user_id = status_resp.get("ilink_user_id", "")

            if not bot_token or not bot_id:
                return json.dumps({"status": "error", "message": "Login confirmed but missing token"})

            cred_path = get_weixin_credentials_path()
            from channel.weixin.weixin_channel import _save_credentials
            _save_credentials(cred_path, {
                "token": bot_token,
                "base_url": result_base_url,
                "bot_id": bot_id,
                "user_id": user_id,
            })
            conf()["weixin_token"] = bot_token
            conf()["weixin_base_url"] = result_base_url

            WeixinQrHandler._qr_state = {}
            logger.info(f"[WebChannel] WeChat QR login confirmed: bot_id={bot_id}")

            return json.dumps({
                "status": "success",
                "qr_status": "confirmed",
                "bot_id": bot_id,
            })

        if qr_status == "expired":
            new_resp = api.fetch_qr_code()
            new_qrcode = new_resp.get("qrcode", "")
            new_qrcode_url = new_resp.get("qrcode_img_content", "")
            new_qr_image = self._qr_to_data_uri(new_qrcode_url)
            WeixinQrHandler._qr_state["qrcode"] = new_qrcode
            WeixinQrHandler._qr_state["qrcode_url"] = new_qrcode_url
            return json.dumps({
                "status": "success",
                "qr_status": "expired",
                "qrcode_url": new_qrcode_url,
                "qr_image": new_qr_image,
            })

        return json.dumps({"status": "success", "qr_status": qr_status})


class FeishuRegisterHandler:
    """飞书智能体应用一键创建（OAuth 设备授权流，基于 lark.register_app SDK）。

    GET  /api/feishu/register   → 启动注册：调用 SDK 生成二维码 URL，立即返回；
                                   后台线程继续轮询飞书侧直到用户扫码授权。
    POST /api/feishu/register   → 轮询当前会话状态（downloading / pending / done /
                                   error / expired）。桌面版首次启用时要先下载飞书
                                   SDK 包，此时二维码尚不存在，改由轮询补发。
                                   注册成功后不直接写 config，由前端再调
                                   /api/channels {action:'connect'} 走标准启用流程。
    """

    # 进程内单例状态（{url, expire_in, status, app_id, app_secret, error, thread}）。
    # 简单的本地自部署场景下不需要 session 隔离。
    _state = {}
    _lock = threading.Lock()

    @staticmethod
    def _qr_to_data_uri(data: str) -> str:
        """复用 WeixinQrHandler 的二维码渲染。"""
        return WeixinQrHandler._qr_to_data_uri(data)

    @classmethod
    def _reset_state(cls):
        with cls._lock:
            cls._state = {}

    @classmethod
    def _start_register_thread(cls):
        """启动一次新的注册会话。如已有进行中的会话，先取消（通过 cancel_event）。"""
        # 先取消可能存在的上一次会话，避免两个 SDK 线程并发 poll 同一个端点
        with cls._lock:
            old_cancel = cls._state.get("cancel_event") if cls._state else None
            if old_cancel is not None:
                old_cancel.set()
            cancel_event = threading.Event()
            cls._state = {"status": "starting", "cancel_event": cancel_event}

        def _worker():
            try:
                # Desktop builds don't bundle lark_oapi; fetch it on demand the
                # first time the user enables Feishu (requires network). Flag it
                # so the modal explains the wait instead of just spinning.
                from channel.feishu import lark_install
                if lark_install.needs_download():
                    with cls._lock:
                        cls._state["status"] = "downloading"
                lark_install.ensure(allow_install=True)
                import lark_oapi as lark
            except ImportError as e:
                with cls._lock:
                    cls._state["status"] = "error"
                    cls._state["error"] = (
                        "飞书 SDK 不可用，请联网后重试，"
                        "或手动执行 pip install -U 'lark-oapi>=1.5.5'（%s）" % e
                    )
                return

            def _on_qr(info):
                # SDK 拿到二维码 URL 后立即回调；写入 state 让前端 GET 立刻能拿到
                with cls._lock:
                    cls._state["url"] = info.get("url", "")
                    cls._state["expire_in"] = info.get("expire_in", 600)
                    cls._state["qr_image"] = cls._qr_to_data_uri(info.get("url", ""))
                    cls._state["status"] = "pending"
                logger.info(f"[FeishuRegister] QR ready, expire_in={info.get('expire_in')}s")

            def _on_status(info):
                # 过滤掉 polling 心跳（每 5 秒一次，纯噪音）；
                # 保留 slow_down / domain_switched 等真正的状态切换事件
                status = info.get("status")
                if status == "polling":
                    return
                logger.info(f"[FeishuRegister] SDK status: {info}")

            try:
                result = lark.register_app(
                    on_qr_code=_on_qr,
                    on_status_change=_on_status,
                    source="smart_assistant",
                    cancel_event=cancel_event,
                )
                with cls._lock:
                    cls._state["status"] = "done"
                    cls._state["app_id"] = result.get("client_id", "")
                    cls._state["app_secret"] = result.get("client_secret", "")
                logger.info(f"[FeishuRegister] App created: app_id={result.get('client_id')}")
            except Exception as e:
                err_msg = str(e)
                err_cls = e.__class__.__name__
                # 飞书 SDK 抛出的 AppExpiredError / AppAccessDeniedError / RegisterAppError
                if "Expired" in err_cls:
                    status = "expired"
                elif "Denied" in err_cls:
                    status = "denied"
                elif "abort" in err_msg.lower() or "cancel" in err_msg.lower():
                    # 被新一轮注册抢占，保持安静
                    return
                else:
                    status = "error"
                with cls._lock:
                    # 仅当当前 state 仍属于本次 worker 时才写入，避免覆盖更新的会话
                    if cls._state.get("cancel_event") is cancel_event:
                        cls._state["status"] = status
                        cls._state["error"] = err_msg
                logger.warning(f"[FeishuRegister] Register failed ({err_cls}): {err_msg}")

        threading.Thread(target=_worker, daemon=True, name="feishu-register").start()

    def GET(self):
        """启动一次新的注册会话。如果已有 pending/done 会话则覆盖。"""
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            self._start_register_thread()
            # 等待 SDK 拿到二维码 URL（最多 10s）。SDK 内部会马上回调 _on_qr。
            import time as _t
            for _ in range(100):
                with self._lock:
                    if self._state.get("url") or self._state.get("status") in (
                        "downloading", "error", "expired", "denied"
                    ):
                        break
                _t.sleep(0.1)
            with self._lock:
                if self._state.get("status") in ("error", "expired", "denied"):
                    return json.dumps({
                        "status": "error",
                        "message": self._state.get("error", "register failed"),
                    })
                if self._state.get("status") == "downloading":
                    # The SDK bundle is still coming down; the QR only exists
                    # once it lands, so hand the frontend over to polling.
                    return json.dumps({
                        "status": "success",
                        "register_status": "downloading",
                    })
                if not self._state.get("url"):
                    return json.dumps({
                        "status": "error",
                        "message": "等待飞书二维码超时，请重试",
                    })
                return json.dumps({
                    "status": "success",
                    "qrcode_url": self._state["url"],
                    "qr_image": self._state.get("qr_image", ""),
                    "expire_in": self._state.get("expire_in", 600),
                })
        except Exception as e:
            logger.error(f"[WebChannel] FeishuRegister GET error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        """轮询注册结果。"""
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            action = body.get("action", "poll")
            if action != "poll":
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})

            with self._lock:
                status = self._state.get("status", "idle")
                if status == "done":
                    payload = {
                        "status": "success",
                        "register_status": "done",
                        "app_id": self._state.get("app_id", ""),
                        "app_secret": self._state.get("app_secret", ""),
                    }
                    # 一次性返回凭据后清掉，避免敏感信息长期驻留内存
                    self._state = {}
                    return json.dumps(payload)
                if status in ("error", "expired", "denied"):
                    return json.dumps({
                        "status": "success",
                        "register_status": status,
                        "message": self._state.get("error", ""),
                    })
                if status == "downloading":
                    return json.dumps({
                        "status": "success",
                        "register_status": "downloading",
                    })
                # pending / starting：还在等用户扫码。二维码可能是在 GET 返回
                # "downloading" 之后才生成的，带上让前端补渲染。
                payload = {"status": "success", "register_status": "pending"}
                if self._state.get("url"):
                    payload["qrcode_url"] = self._state["url"]
                    payload["qr_image"] = self._state.get("qr_image", "")
                return json.dumps(payload)
        except Exception as e:
            logger.error(f"[WebChannel] FeishuRegister POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class ToolsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.tools.tool_manager import ToolManager
            from common import i18n
            tm = ToolManager()
            if not tm.tool_classes:
                tm.load_tools()
            tools = []
            lang = i18n.get_language()
            for name, cls in tm.tool_classes.items():
                try:
                    instance = cls()
                    desc = instance.description
                    if lang == i18n.ZH_HANT and desc:
                        desc = i18n.to_traditional(desc)
                    elif lang == "en" and name == "scheduler":
                        desc = (
                            "Create, query and manage scheduled tasks (reminders, periodic tasks, etc.).\n\n"
                            "⚠️ IMPORTANT: Only use this tool when delayed or periodic execution is needed."
                        )
                    tools.append({
                        "name": name,
                        "description": desc,
                    })
                except Exception:
                    tools.append({"name": name, "description": ""})
            return json.dumps({"status": "success", "tools": tools}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Tools API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SkillsHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.skills.service import SkillService
            from agent.skills.manager import SkillManager
            from common import i18n
            workspace_root = _get_workspace_root()
            manager = SkillManager(custom_dir=os.path.join(workspace_root, "skills"))
            service = SkillService(manager)
            skills = service.query()
            if i18n.get_language() == i18n.ZH_HANT:
                for skill in skills:
                    if isinstance(skill, dict):
                        for k, v in list(skill.items()):
                            if k in ("name", "description", "display_name") and isinstance(v, str):
                                skill[k] = i18n.to_traditional(v)
            return json.dumps({"status": "success", "skills": skills}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Skills API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.skills.service import SkillService
            from agent.skills.manager import SkillManager
            body = json.loads(web.data())
            action = body.get("action")
            name = body.get("name")
            if not action or not name:
                return json.dumps({"status": "error", "message": "action and name are required"})
            workspace_root = _get_workspace_root()
            manager = SkillManager(custom_dir=os.path.join(workspace_root, "skills"))
            service = SkillService(manager)
            if action == "open":
                service.open({"name": name})
            elif action == "close":
                service.close({"name": name})
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
            return json.dumps({"status": "success"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Skills POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class MemoryHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.memory.service import MemoryService
            params = web.input(page='1', page_size='20', category='memory')
            workspace_root = _get_workspace_root()
            service = MemoryService(workspace_root)
            result = service.list_files(
                page=int(params.page), page_size=int(params.page_size),
                category=params.category,
            )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Memory API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class MemoryContentHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.memory.service import MemoryService
            params = web.input(filename='', category='memory')
            if not params.filename:
                return json.dumps({"status": "error", "message": "filename required"})
            workspace_root = _get_workspace_root()
            service = MemoryService(workspace_root)
            result = service.get_content(params.filename, category=params.category)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except ValueError:
            return json.dumps({"status": "error", "message": "invalid filename"})
        except FileNotFoundError:
            return json.dumps({"status": "error", "message": "file not found"})
        except Exception as e:
            logger.error(f"[WebChannel] Memory content API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerHandler:
    def GET(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.tools.scheduler.task_store import TaskStore
            workspace_root = _get_workspace_root()
            store_path = os.path.join(workspace_root, "scheduler", "tasks.json")
            store = TaskStore(store_path)
            tasks = store.list_tasks(owner_id=owner_id)
            return json.dumps({"status": "success", "tasks": tasks}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerRunHandler:
    def POST(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            task_id = body.get("task_id")
            if not task_id:
                return json.dumps({"status": "error", "message": "task_id required"})
            idempotency_key = body.get("idempotency_key")
            if not isinstance(idempotency_key, str):
                return json.dumps({
                    "status": "error",
                    "message": "idempotency_key required for manual task execution",
                })

            from agent.tools.scheduler.integration import get_scheduler_service
            service = get_scheduler_service()
            if service is None:
                return json.dumps({
                    "status": "error",
                    "message": "Scheduler service is not running",
                })

            result = service.run_task_now(
                task_id,
                owner_id=owner_id,
                idempotency_key=idempotency_key,
            )
            return json.dumps({
                "status": "success",
                "message": f"Task '{task_id}' queued for immediate execution",
                "execution": result,
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler manual run error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerToggleHandler:
    def POST(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            task_id = body.get("task_id")
            enabled = body.get("enabled", True)
            if not task_id:
                return json.dumps({"status": "error", "message": "task_id required"})
            from agent.tools.scheduler.task_store import TaskStore
            workspace_root = _get_workspace_root()
            store_path = os.path.join(workspace_root, "scheduler", "tasks.json")
            store = TaskStore(store_path)
            store.enable_task(task_id, enabled, owner_id=owner_id)
            task = store.get_task(task_id, owner_id=owner_id)
            return json.dumps({"status": "success", "task": task}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler toggle error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerUpdateHandler:
    def POST(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            task_id = body.get("task_id")
            if not task_id:
                return json.dumps({"status": "error", "message": "task_id required"})
            
            from agent.tools.scheduler.task_store import TaskStore
            from agent.tools.scheduler.scheduler_service import SchedulerService
            from datetime import datetime
            workspace_root = _get_workspace_root()
            store_path = os.path.join(workspace_root, "scheduler", "tasks.json")
            store = TaskStore(store_path)
            
            # Get original task (single query to avoid repeated I/O)
            original_task = store.get_task(task_id, owner_id=owner_id)
            if not original_task:
                return json.dumps({"status": "error", "message": f"Task '{task_id}' not found"})
            
            # Build updates dict
            updates = {}
            if "name" in body:
                updates["name"] = body["name"]
            if "enabled" in body:
                updates["enabled"] = body["enabled"]
            
            # Update schedule
            if "schedule" in body:
                updates["schedule"] = body["schedule"]
                # If schedule config changed, recalculate next_run_at
                # Build merged temp task data for calculation (without modifying the original object)
                merged = dict(original_task)
                merged.update(updates)
                if "action" in body:
                    merged["action"] = body["action"]
                temp_service = SchedulerService(store, lambda t: None)
                next_run = temp_service._calculate_next_run(merged, datetime.now())
                if next_run:
                    updates["next_run_at"] = next_run.isoformat()
                else:
                    # Cannot calculate next run time, schedule config may be invalid
                    return json.dumps({
                        "status": "error", 
                        "message": "Cannot calculate next run time. Please check the schedule config (e.g., cron expression format, or whether the one-time task time has already passed)."
                    }, ensure_ascii=False)
            
            # Update action
            if "action" in body:
                # Get the task's original channel_type
                original_action = original_task.get("action", {})
                if not isinstance(original_action, dict):
                    original_action = {}
                action_patch = body["action"]
                if not isinstance(action_patch, dict):
                    return json.dumps({
                        "status": "error",
                        "message": "Action must be an object."
                    }, ensure_ascii=False)
                editable_action_fields = {
                    "type", "content", "task_description"
                }
                protected_fields = set(action_patch) - editable_action_fields
                if protected_fields:
                    return json.dumps({
                        "status": "error",
                        "message": "Protected action fields cannot be modified: "
                            + ", ".join(sorted(protected_fields)),
                    }, ensure_ascii=False)

                # The Web editor only exposes a subset of action fields. Merge
                # that patch into the stored action so scheduler metadata such
                # as notify_session_id, silent, and channel-specific delivery
                # fields survive unrelated edits.
                action = dict(original_action)
                action.update(action_patch)
                action_type = action.get("type")
                if action_type == "send_message":
                    action.pop("task_description", None)
                    action.pop("silent", None)
                elif action_type == "agent_task":
                    action.pop("content", None)

                old_channel = original_action.get("channel_type", "web")
                channel_type = action.get("channel_type") or old_channel
                action["channel_type"] = channel_type
                
                # If channel type changed or no receiver, reject the update.
                # Note: the web UI disables the channel selector, so this branch
                # is only reachable via direct API calls. Changing a task's channel
                # after creation is not supported because the receiver identity is
                # channel-bound and cannot be trivially re-populated (e.g. weixin
                # requires a valid context_token tied to the original user-session).
                if old_channel and old_channel != channel_type:
                    return json.dumps({
                        "status": "error",
                        "message": f"Cannot change channel type from '{old_channel}' to '{channel_type}'. Please create a new task on the target channel instead."
                    }, ensure_ascii=False)
                if not action.get("receiver"):
                    return json.dumps({
                        "status": "error",
                        "message": "Receiver is required. Please create a new task through the chat interface."
                    }, ensure_ascii=False)
                updates["action"] = action
                
                # If schedule was not updated but action was, ensure next_run_at exists
                if "schedule" not in body and "next_run_at" not in original_task:
                    merged = dict(original_task)
                    merged.update(updates)
                    temp_service = SchedulerService(store, lambda t: None)
                    next_run = temp_service._calculate_next_run(merged, datetime.now())
                    if next_run:
                        updates["next_run_at"] = next_run.isoformat()
            
            store.update_task(task_id, updates, owner_id=owner_id)
            task = store.get_task(task_id, owner_id=owner_id)
            return json.dumps({"status": "success", "task": task}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler update error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SchedulerDeleteHandler:
    def POST(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data())
            task_id = body.get("task_id")
            if not task_id:
                return json.dumps({"status": "error", "message": "task_id required"})
            
            from agent.tools.scheduler.task_store import TaskStore
            workspace_root = _get_workspace_root()
            store_path = os.path.join(workspace_root, "scheduler", "tasks.json")
            store = TaskStore(store_path)
            store.delete_task(task_id, owner_id=owner_id)
            return json.dumps({"status": "success"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Scheduler delete error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionsHandler:
    def GET(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(page='1', page_size='50')
            from agent.memory import get_conversation_store
            store = get_conversation_store()
            result = store.list_sessions(
                channel_type="web",
                page=int(params.page),
                page_size=int(params.page_size),
                owner_id=owner_id,
            )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Sessions API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionDetailHandler:
    def DELETE(self, session_id: str):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        logger.info(f"[WebChannel] DELETE session request: {session_id}")
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            # Authorize before every side effect. A destructive mutation must
            # acquire AgentBridge's session fence after cancelling all queued
            # work; otherwise a late assistant/tool write can resurrect data.
            from agent.memory import get_conversation_store
            store = get_conversation_store()
            _require_web_session(session_id, owner_id)
            try:
                from bridge.bridge import Bridge
                ab = Bridge().get_agent_bridge()
                if not ab.cancel_and_wait_for_session(session_id, owner_id):
                    return json.dumps({
                        "status": "error",
                        "message": "session cancellation is still pending; delete was not applied",
                    })
            except Exception as fence_error:
                logger.warning(
                    f"[WebChannel] Session delete mutation fence failed: {fence_error}"
                )
                return json.dumps({
                    "status": "error",
                    "message": "session cancellation could not be confirmed; delete was not applied",
                })
            # Commit a durable tombstone. Late persistence is rejected instead
            # of recreating the deleted row.
            store.delete_session(session_id, owner_id=owner_id)

            ab.clear_session(session_id)
            logger.info(f"[WebChannel] Removed agent instance for session {session_id}")

            channel = WebChannel()
            channel.session_queues.pop(session_id, None)
            for request_id, request_session in list(channel.request_to_session.items()):
                if (
                    request_session == session_id
                    and channel.request_owners.get(request_id) == owner_id
                    and request_id in channel.sse_queues
                ):
                    channel.sse_queues[request_id].put({
                        "type": "cancelled",
                        "content": "Session deleted",
                        "request_id": request_id,
                        "timestamp": time.time(),
                    })

            logger.info(f"[WebChannel] Session deleted: {session_id}")
            return json.dumps({"status": "success"})
        except Exception as e:
            logger.error(f"[WebChannel] Session delete error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def PUT(self, session_id: str):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})
            body = json.loads(web.data())
            title = body.get("title", "").strip()
            if not title:
                return json.dumps({"status": "error", "message": "title required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            found = store.rename_session(session_id, title, owner_id=owner_id)
            if not found:
                return json.dumps({"status": "error", "message": "session not found"})
            return json.dumps({"status": "success"})
        except Exception as e:
            logger.error(f"[WebChannel] Session rename error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionTitleHandler:
    def POST(self, session_id: str):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            body = json.loads(web.data())
            user_message = body.get("user_message", "")
            assistant_reply = body.get("assistant_reply", "")
            if not user_message:
                return json.dumps({"status": "error", "message": "user_message required"})

            # Check ownership/existence before spending model resources and
            # never convert a zero-row update into a success response.
            _require_web_session(session_id, owner_id)
            title = _generate_session_title(user_message, assistant_reply)

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            updated = store.rename_session(
                session_id, title, owner_id=owner_id
            )
            if not updated:
                return json.dumps({
                    "status": "error", "message": "session not found"
                })
            logger.info(
                f"[WebChannel] Session title set: sid={session_id}, "
                f"title='{title}', db_updated={updated}"
            )

            return json.dumps({"status": "success", "title": title}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Title generation error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class PromptOptimizeHandler:
    """Optimize a colloquial user prompt into a structured AI-ready instruction."""

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b"{}")
            user_input = (body.get("input") or "").strip()
            if not user_input:
                return json.dumps({"status": "error", "message": "input required"})

            context_messages = body.get("context_messages", None)

            from agent.chat.session_service import optimize_prompt
            optimized = optimize_prompt(user_input, context_messages)

            return json.dumps(
                {"status": "success", "optimized": optimized},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"[WebChannel] Prompt optimization error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SessionClearContextHandler:
    def POST(self, session_id: str):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            try:
                from bridge.bridge import Bridge
                bridge = Bridge()
                ab = bridge.get_agent_bridge()
                if not ab.cancel_and_wait_for_session(session_id, owner_id):
                    return json.dumps({
                        "status": "error",
                        "message": "session cancellation is still pending; context was not cleared",
                    })
            except Exception as fence_error:
                logger.warning(
                    f"[WebChannel] Clear context mutation fence failed: {fence_error}"
                )
                return json.dumps({
                    "status": "error",
                    "message": "session cancellation could not be confirmed; context was not cleared",
                })

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            new_seq = store.clear_context(session_id, owner_id=owner_id)
            ab.clear_session(session_id)
            logger.info(f"[WebChannel] Cleared agent instance for session {session_id}")

            return json.dumps({"status": "success", "context_start_seq": new_seq})
        except Exception as e:
            logger.error(f"[WebChannel] Clear context error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class HistoryHandler:
    def GET(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            params = web.input(session_id='', page='1', page_size='20')
            session_id = params.session_id.strip()
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            from agent.memory import get_conversation_store
            store = get_conversation_store()
            result = store.load_history_page(
                session_id=session_id,
                page=int(params.page),
                page_size=int(params.page_size),
                owner_id=owner_id,
            )
            result = _decorate_history_file_capabilities(result, owner_id)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] History API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class MessageDeleteHandler:
    def POST(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Access-Control-Allow-Origin', '*')
        try:
            data = json.loads(web.data())
            session_id = data.get('session_id', '').strip()
            user_seq = data.get('user_seq')
            delete_user = data.get('delete_user', True)
            cascade = data.get('cascade', False)
            
            if not session_id or user_seq is None:
                return json.dumps({"status": "error", "message": "session_id and user_seq required"})
            
            # 1. Delete from database
            from agent.memory import get_conversation_store
            store = get_conversation_store()
            deleted = store.delete_message_pair(
                session_id,
                int(user_seq),
                delete_user=delete_user,
                cascade=cascade,
                owner_id=owner_id,
            )

            # 2. Sync agent's in-memory context so its next turn sees the
            # same history as the DB. Handled by the agent_bridge helper.
            cache_action = "not_loaded"
            agent_bridge = None
            try:
                from bridge.bridge import Bridge
                agent_bridge = Bridge().get_agent_bridge()
                had_cached_agent = agent_bridge.has_cached_session(session_id)
                synced = agent_bridge.sync_session_messages_from_store(
                    session_id, owner_id=owner_id
                )
                if had_cached_agent and synced < 0:
                    # Eviction is a safe convergence path: the next request must
                    # rebuild from the already-committed owner-checked store.
                    agent_bridge.clear_session(session_id)
                    cache_action = "evicted"
                elif synced >= 0:
                    cache_action = "synchronized"
            except Exception as sync_err:
                logger.warning(f"[WebChannel] Failed to sync agent memory: {sync_err}")
                if agent_bridge is None:
                    return json.dumps({
                        "status": "error",
                        "message": "message deleted but cache convergence failed",
                    })
                try:
                    agent_bridge.clear_session(session_id)
                    cache_action = "evicted"
                except Exception:
                    return json.dumps({
                        "status": "error",
                        "message": "message deleted but cache convergence failed",
                    })

            return json.dumps({
                "status": "success",
                "deleted": deleted,
                "cache_action": cache_action,
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Message delete error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class LogsHandler:
    def GET(self):
        params = web.input(ticket='', token='')
        ticket = str(getattr(params, 'ticket', '') or '')
        if ticket:
            _require_safe_request_host()
            if _consume_log_stream_ticket(ticket) is None:
                raise web.HTTPError("401 Unauthorized")
        else:
            if getattr(params, 'token', ''):
                raise web.HTTPError("401 Unauthorized")
            _require_auth()
        web.header('Content-Type', 'text/event-stream; charset=utf-8')
        web.header('Cache-Control', 'no-cache')
        web.header('X-Accel-Buffering', 'no')

        log_path = os.path.join(get_data_root(), "run.log")

        def generate():
            if not os.path.isfile(log_path):
                yield b"data: {\"type\": \"error\", \"message\": \"run.log not found\"}\n\n"
                return

            # Read last 200 lines for initial display
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                tail_lines = lines[-200:]
                chunk = ''.join(tail_lines)
                payload = json.dumps({"type": "init", "content": chunk}, ensure_ascii=False)
                yield f"data: {payload}\n\n".encode('utf-8')
            except Exception as e:
                yield f"data: {{\"type\": \"error\", \"message\": \"{e}\"}}\n\n".encode('utf-8')
                return

            # Tail new lines
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(0, 2)  # seek to end
                    deadline = time.time() + 600  # 10 min max
                    while time.time() < deadline:
                        line = f.readline()
                        if line:
                            payload = json.dumps({"type": "line", "content": line}, ensure_ascii=False)
                            yield f"data: {payload}\n\n".encode('utf-8')
                        else:
                            yield b": keepalive\n\n"
                            time.sleep(1)
            except GeneratorExit:
                return
            except Exception:
                return

        return generate()


class AssetsHandler:
    def GET(self, file_path):  # 修改默认参数
        try:
            # 如果请求是/static/，需要处理
            if file_path == '':
                # 返回目录列表...
                pass

            # 获取当前文件的绝对路径
            current_dir = os.path.dirname(os.path.abspath(__file__))
            static_dir = os.path.join(current_dir, 'static')

            full_path = os.path.normpath(os.path.join(static_dir, file_path))

            # 安全检查：确保请求的文件在static目录内
            if not os.path.abspath(full_path).startswith(os.path.abspath(static_dir)):
                logger.error(f"Security check failed for path: {full_path}")
                raise web.notfound()

            if not os.path.exists(full_path) or not os.path.isfile(full_path):
                # Browsers routinely probe optional asset variants (e.g. a
                # .ttf fallback declared alongside .woff2 in @font-face);
                # logging these as errors floods the console with harmless
                # noise. Keep it at debug level — real misconfigurations
                # will still surface via the network panel.
                logger.debug(f"Static file not found: {full_path}")
                raise web.notfound()

            # 设置正确的Content-Type
            content_type = mimetypes.guess_type(full_path)[0]
            if content_type:
                web.header('Content-Type', content_type)
            else:
                # 默认为二进制流
                web.header('Content-Type', 'application/octet-stream')

            # 读取并返回文件内容
            with open(full_path, 'rb') as f:
                return f.read()

        except web.HTTPError:
            # The 404 path above already logged at debug; re-raise as-is so
            # web.py returns the original status to the client.
            raise
        except Exception as e:
            logger.error(f"Error serving static file: {e}", exc_info=True)
            raise web.notfound()


def _workspace_service():
    from agent.workspace.service import WorkspaceService
    return WorkspaceService(_get_workspace_root())


def _decorate_entry(svc, entry: dict, owner_id: Optional[str] = None) -> Optional[dict]:
    """Attach the URLs the frontend needs to preview or download an entry."""
    if entry.get("is_dir"):
        return entry
    abs_path = entry.get("abs_path") or os.path.join(svc.root, entry["path"])
    if owner_id is not None and _is_other_owner_upload_path(abs_path, owner_id):
        return None
    entry["abs_path"] = abs_path
    entry["raw_url"] = _build_file_url(abs_path, owner_id)
    entry["preview_url"] = _build_preview_url(abs_path, owner_id)
    return entry


class WorkspaceTreeHandler:
    def GET(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(path='', show_hidden='')
            svc = _workspace_service()
            result = svc.list_dir(params.path, show_hidden=params.show_hidden == '1')
            result["entries"] = [
                decorated
                for e in result["entries"]
                if (decorated := _decorate_entry(svc, e, owner_id)) is not None
            ]
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Workspace tree error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class WorkspaceSearchHandler:
    def GET(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            params = web.input(q='', limit='30')
            try:
                limit = max(1, min(100, int(params.limit)))
            except (TypeError, ValueError):
                limit = 30
            svc = _workspace_service()
            result = svc.search(params.q, limit=limit)
            result["results"] = [
                decorated
                for e in result["results"]
                if (decorated := _decorate_entry(svc, e, owner_id)) is not None
            ]
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Workspace search error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class WorkspaceResolveHandler:
    """
    Metadata + preview/raw URLs for one entry, given a relative or absolute path.

    Directories resolve as well (the client then browses instead of previewing),
    just without the file URLs.
    """

    def GET(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.protocol.artifact import classify_kind, is_previewable
            params = web.input(path='')
            raw_path = (params.path or '').strip()
            if not raw_path:
                return json.dumps({"status": "error", "message": "path is required"})

            svc = _workspace_service()
            if os.path.isabs(os.path.expanduser(raw_path)):
                abs_path = os.path.realpath(os.path.expanduser(raw_path))
                if not _is_path_allowed(abs_path):
                    return json.dumps({"status": "error", "message": "Path not allowed"})
                if _is_other_owner_upload_path(abs_path, owner_id):
                    return json.dumps({"status": "error", "message": "Path not allowed"})
                is_dir = os.path.isdir(abs_path)
                if not is_dir and not os.path.isfile(abs_path):
                    return json.dumps({"status": "error", "message": "File not found"})
                kind = "directory" if is_dir else classify_kind(abs_path)
                entry = {
                    "name": os.path.basename(abs_path),
                    "path": svc.to_rel(abs_path),
                    "abs_path": abs_path,
                    "is_dir": is_dir,
                    "kind": kind,
                    "previewable": (not is_dir) and is_previewable(kind),
                    "size": 0 if is_dir else os.path.getsize(abs_path),
                    "mtime": os.path.getmtime(abs_path),
                }
            else:
                entry = svc.stat_file(raw_path)

            # A directory has nothing to serve; the client browses into it.
            if not entry["is_dir"]:
                entry["raw_url"] = _build_file_url(entry["abs_path"], owner_id)
                entry["preview_url"] = _build_preview_url(entry["abs_path"], owner_id)
            return json.dumps({"status": "success", "file": entry}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Workspace resolve error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class WorkspaceMetaHandler:
    def GET(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            return json.dumps({"status": "success", **_workspace_service().meta()}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Workspace meta error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class KnowledgeListHandler:
    def GET(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        svc = None
        try:
            svc = _web_knowledge_service(owner_id)
            result = svc.list_tree()
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge list error: {e}")
            return json.dumps({"status": "error", "message": str(e)})
        finally:
            if svc is not None:
                svc.close()


class KnowledgeReadHandler:
    def GET(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        svc = None
        try:
            params = web.input(path='')
            svc = _web_knowledge_service(owner_id)
            result = svc.read_file(params.path)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge read error: {e}")
            return json.dumps({"status": "error", "message": str(e)})
        finally:
            if svc is not None:
                svc.close()


class KnowledgeCitationResolveHandler:
    """Resolve a governed citation without accepting client-supplied identity claims."""

    def POST(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        service = None
        try:
            content_length = int(_request_environment().get("CONTENT_LENGTH") or 0)
            if content_length > 8192:
                return json.dumps({
                    "status": "error",
                    "code": 413,
                    "error_code": "citation_request_too_large",
                    "message": "citation request too large",
                }, ensure_ascii=False)
            raw_body = web.data() or b"{}"
            if len(raw_body) > 8192:
                return json.dumps({
                    "status": "error",
                    "code": 413,
                    "error_code": "citation_request_too_large",
                    "message": "citation request too large",
                }, ensure_ascii=False)
            try:
                body = json.loads(raw_body)
            except (TypeError, ValueError, UnicodeDecodeError):
                return json.dumps({
                    "status": "error",
                    "code": 400,
                    "error_code": "invalid_citation_request",
                    "message": "request body must be valid JSON",
                }, ensure_ascii=False)
            if not isinstance(body, dict) or set(body) != {"uri"}:
                return json.dumps({
                    "status": "error",
                    "code": 400,
                    "error_code": "invalid_citation_request",
                    "message": "request body must contain only uri",
                }, ensure_ascii=False)
            uri = body.get("uri")
            if not isinstance(uri, str) or not uri.startswith("knowledge://"):
                return json.dumps({
                    "status": "error",
                    "code": 400,
                    "error_code": "invalid_citation_request",
                    "message": "valid knowledge:// uri required",
                }, ensure_ascii=False)

            service = _web_knowledge_service(owner_id)
            citation = service.resolve_citation(uri)
            if citation.get("scope") == "session":
                citation_session = str(citation.get("session_id") or "")
                try:
                    _require_web_session(citation_session, owner_id)
                except PermissionError as exc:
                    from agent.knowledge.contracts import KnowledgeCitationIntegrityError
                    raise KnowledgeCitationIntegrityError(
                        "citation session is no longer active"
                    ) from exc
            return json.dumps({
                "status": "success",
                "code": 200,
                "citation": citation,
            }, ensure_ascii=False)
        except Exception as e:
            logger.debug(
                f"[WebChannel] Citation resolution rejected: "
                f"{type(e).__name__}: {e}"
            )
            from agent.knowledge.contracts import (
                KnowledgeAuthorizationError,
                KnowledgeCitationIntegrityError,
                KnowledgeCitationVersionError,
            )
            if isinstance(e, KnowledgeAuthorizationError):
                code, error_code = 403, "citation_forbidden"
            elif isinstance(e, KnowledgeCitationVersionError):
                code, error_code = 409, "unsupported_citation_version"
            elif isinstance(e, KnowledgeCitationIntegrityError):
                code, error_code = 410, "citation_expired_or_invalid"
            else:
                logger.error(f"[WebChannel] Citation resolve error: {e}")
                code, error_code = 500, "citation_resolution_failed"
            return json.dumps({
                "status": "error",
                "code": code,
                "error_code": error_code,
                "message": "citation cannot be resolved; search again",
            }, ensure_ascii=False)
        finally:
            if service is not None:
                service.close()


class KnowledgeGraphHandler:
    def GET(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        svc = None
        try:
            svc = _web_knowledge_service(owner_id)
            return json.dumps(svc.build_graph(), ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge graph error: {e}")
            return json.dumps({"nodes": [], "links": []})
        finally:
            if svc is not None:
                svc.close()


class KnowledgeActionHandler:
    def POST(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        svc = None
        try:
            body = json.loads(web.data() or b"{}")
            if not isinstance(body, dict):
                raise ValueError("request body must be an object")
            action = body.get("action", "")
            payload = body.get("payload") or {}
            svc = _web_knowledge_service(owner_id, administrative=True)
            result = svc.dispatch(action, payload)
            return json.dumps({
                "status": "success" if result["code"] < 300 else "error",
                **result,
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge action error: {e}")
            return json.dumps({"status": "error", "code": 500, "message": str(e), "payload": None})
        finally:
            if svc is not None:
                svc.close()


class KnowledgeImportHandler:
    def POST(self):
        owner_id = _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        svc = None
        try:
            from agent.knowledge.service import KnowledgeService
            content_length = int(_request_environment().get("CONTENT_LENGTH") or 0)
            if content_length > KnowledgeService.MAX_IMPORT_TOTAL_SIZE:
                return json.dumps({
                    "status": "error",
                    "code": 413,
                    "message": "import batch too large",
                    "payload": None,
                })
            params = _raw_web_input()
            target_category = params.get("target_category", "")
            conflict_strategy = params.get("conflict_strategy", "skip")
            uploaded = _ensure_list(params.get("files"))
            single = params.get("file")
            if single is not None:
                uploaded.append(single)
            if not uploaded:
                return json.dumps({"status": "error", "code": 400, "message": "No files uploaded", "payload": None})
            if len(uploaded) > KnowledgeService.MAX_IMPORT_FILES:
                return json.dumps({
                    "status": "error",
                    "code": 400,
                    "message": f"too many files: max {KnowledgeService.MAX_IMPORT_FILES}",
                    "payload": None,
                })

            files = []
            total_size = 0
            for file_obj in uploaded:
                if file_obj is None:
                    continue
                filename = getattr(file_obj, "filename", "") or getattr(file_obj, "name", "")
                content = _read_uploaded_file_bytes_limited(file_obj, KnowledgeService.MAX_IMPORT_FILE_SIZE)
                total_size += len(content)
                if total_size > KnowledgeService.MAX_IMPORT_TOTAL_SIZE:
                    return json.dumps({
                        "status": "error",
                        "code": 413,
                        "message": "import batch too large",
                        "payload": None,
                    })
                files.append({
                    "filename": filename,
                    "content": content,
                })

            svc = _web_knowledge_service(owner_id, administrative=True)
            result = svc.dispatch("import_documents", {
                "target_category": target_category,
                "conflict_strategy": conflict_strategy,
                "files": files,
            })
            return json.dumps({
                "status": "success" if result["code"] < 300 else "error",
                **result,
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[WebChannel] Knowledge import error: {e}", exc_info=True)
            return json.dumps({"status": "error", "code": 500, "message": str(e), "payload": None})
        finally:
            if svc is not None:
                svc.close()


class VersionHandler:
    def GET(self):
        web.header('Content-Type', 'application/json; charset=utf-8')
        from cli import __version__
        return json.dumps({"version": __version__})


