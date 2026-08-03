"""Execute fail-closed attacks against the Web trust boundary."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, Iterator, List
from unittest.mock import patch

from agent.knowledge import GovernedKnowledgeRuntime, KnowledgeWriteCommand
from agent.knowledge.contracts import (
    KnowledgeAuthorizationError,
    KnowledgeCitationIntegrityError,
)
from agent.memory.conversation_store import ConversationStore
from agent.memory.governance import IdentityContext, MemoryScope, Sensitivity
from channel.web.sse_persistence import DurableSSEJournalStore


SCHEMA_VERSION = 1
REQUIRED_CHECKS = (
    "conversation_cross_owner_zero_side_effect",
    "ownerless_store_bypass_rejected",
    "delete_tombstone_blocks_late_writer",
    "concurrent_claim_exactly_one_winner",
    "concurrent_legacy_migration_recovers_all",
    "concurrent_knowledge_startup_recovers_all",
    "session_citation_server_capability_present",
    "session_citation_resolves_without_client_session_claim",
    "session_citation_tamper_rejected",
    "session_citation_cross_principal_rejected",
    "auth_logout_revokes_auth_and_url_capabilities",
    "auth_restart_invalidates_bearer",
    "password_rotation_preserves_non_auth_subject",
    "public_bind_without_password_fails_closed",
    "preview_capability_full_hmac_and_expiry",
    "upload_prefix_sibling_escape_rejected",
    "web_and_desktop_citation_ui_wired",
    "citation_handler_rejects_client_identity_claims",
    "session_delete_foreign_owner_zero_cancel_side_effect",
    "scheduler_cross_owner_context_injection_rejected",
    "dns_rebinding_host_origin_rejected",
    "session_citation_tombstone_revokes_capability",
    "generic_file_upload_owner_bypass_rejected",
    "sse_bearer_url_bypass_rejected",
    "login_bruteforce_rate_limited",
    "filesystem_root_serve_denied",
    "single_file_capability_tamper_and_expiry_rejected",
    "legacy_bearer_file_and_log_url_bypasses_rejected",
    "durable_web_execution_replay_and_terminal_fence",
    "web_steer_idempotency_and_type_fence",
)
SOURCE_PATHS = (
    "agent/knowledge/runtime.py",
    "agent/knowledge/service.py",
    "agent/knowledge/repository.py",
    "agent/memory/conversation_store.py",
    "agent/protocol/cancel.py",
    "agent/protocol/steer.py",
    "agent/tools/scheduler/integration.py",
    "agent/tools/scheduler/scheduler_service.py",
    "agent/tools/scheduler/scheduler_tool.py",
    "agent/tools/scheduler/task_store.py",
    "agent/retrieval/lexical.py",
    "bridge/agent_bridge.py",
    "bridge/agent_initializer.py",
    "channel/web/sse_persistence.py",
    "channel/web/web_channel.py",
    "channel/web/static/js/console.js",
    "desktop/src/renderer/src/api/client.ts",
    "desktop/src/renderer/src/App.tsx",
    "desktop/src/renderer/src/components/LoginGate.tsx",
    "desktop/src/renderer/src/i18n.ts",
    "desktop/src/renderer/src/pages/TasksPage.tsx",
    "desktop/src/renderer/src/store/chatStore.ts",
    "desktop/src/renderer/src/types.ts",
    "desktop/src/renderer/src/components/Markdown.tsx",
    "desktop/src/renderer/src/components/MessageBubble.tsx",
    "desktop/src/renderer/src/pages/DeliveryPage.tsx",
    "desktop/src/renderer/src/pages/LogsPage.tsx",
    "benchmarks/security/web_boundary.py",
    "benchmarks/security/verify.py",
)


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(root: Path | None = None) -> str:
    root = (root or _root()).resolve()
    digest = hashlib.sha256()
    for relative in sorted(SOURCE_PATHS):
        path = root / relative
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(_sha256(path).encode("ascii") + b"\n")
    return digest.hexdigest()


@contextmanager
def _temporary_workspace() -> Iterator[Path]:
    """Create and strictly clean an attack workspace, retrying transient Win32 locks.

    SQLite can release its final Windows file handle a few scheduler ticks after
    concurrent constructor threads have joined.  A single immediate rmtree made
    the formal security gate flaky under the full suite.  Cleanup still fails
    closed after bounded retries; it is never ignored.
    """

    path = Path(tempfile.mkdtemp(prefix="cow-web-boundary-"))
    try:
        yield path
    finally:
        last_error: PermissionError | None = None
        for attempt in range(8):
            gc.collect()
            try:
                shutil.rmtree(path)
            except PermissionError as exc:
                last_error = exc
                if attempt == 7:
                    raise
                time.sleep(min(0.5, 0.02 * (2**attempt)))
            else:
                last_error = None
                break
        if last_error is not None:
            raise last_error


def _identity(actor: str) -> IdentityContext:
    return IdentityContext(
        tenant_id="tenant-local",
        actor_user_id=actor,
        roles=frozenset(),
        trace_id="web-boundary-" + actor[-8:],
        auth_source="web-password",
    )


def _messages() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": [{"type": "text", "text": "private"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "proof"}]},
    ]


def _record(checks: List[Dict[str, Any]], name: str, attack: Callable[[], Any]) -> None:
    try:
        details = attack()
    except Exception as exc:
        checks.append({
            "name": name,
            "passed": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
    else:
        checks.append({"name": name, "passed": True, "details": details})


def run_checks(root: Path | None = None) -> List[Dict[str, Any]]:
    root = (root or _root()).resolve()
    checks: List[Dict[str, Any]] = []
    with _temporary_workspace() as tmp:
        store = ConversationStore(tmp / "conversation.db")
        owner = "web:" + "a" * 32
        attacker = "web:" + "b" * 32
        session_id = "security-session"
        store.claim_session(session_id, owner)
        store.append_messages(session_id, _messages(), channel_type="web", owner_id=owner)
        before = store.load_history_page(session_id, owner_id=owner)

        def cross_owner():
            denied = 0
            attacks = (
                lambda: store.load_messages(session_id, owner_id=attacker),
                lambda: store.rename_session(session_id, "stolen", owner_id=attacker),
                lambda: store.delete_message_pair(session_id, 0, owner_id=attacker),
                lambda: store.clear_session(session_id, owner_id=attacker),
            )
            for attack in attacks:
                try:
                    attack()
                except PermissionError:
                    denied += 1
            if denied != len(attacks):
                raise AssertionError("foreign operation returned success")
            if store.load_history_page(session_id, owner_id=owner) != before:
                raise AssertionError("foreign operation changed history")
            return {"denied_operations": denied}

        _record(checks, REQUIRED_CHECKS[0], cross_owner)

        def ownerless_bypass():
            denied = 0
            for attack in (
                lambda: store.load_messages(session_id),
                lambda: store.append_messages(
                    session_id,
                    [{"role": "assistant", "content": "late"}],
                    channel_type="web",
                ),
                lambda: store.clear_session(session_id),
            ):
                try:
                    attack()
                except PermissionError:
                    denied += 1
            if denied != 3:
                raise AssertionError("session-id-only path accessed an owned session")
            return {"denied_operations": denied}

        _record(checks, REQUIRED_CHECKS[1], ownerless_bypass)

        def tombstone():
            local = ConversationStore(tmp / "tombstone.db")
            sid = "delete-race"
            local.claim_session(sid, owner)
            local.append_messages(sid, _messages(), channel_type="web", owner_id=owner)
            local.delete_session(sid, owner)
            local.delete_session(sid, owner)
            try:
                local.append_messages(
                    sid, [{"role": "assistant", "content": "late"}],
                    channel_type="web", owner_id=owner,
                )
            except PermissionError:
                pass
            else:
                raise AssertionError("late writer resurrected tombstone")
            conn = sqlite3.connect(tmp / "tombstone.db")
            try:
                state = conn.execute(
                    "SELECT state FROM sessions WHERE session_id = ?", (sid,)
                ).fetchone()[0]
                count = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)
                ).fetchone()[0]
            finally:
                conn.close()
            if state != "deleted" or count != 0:
                raise AssertionError("tombstone invariant failed")
            return {"state": state, "message_count": count}

        _record(checks, REQUIRED_CHECKS[2], tombstone)

        def concurrent_claim():
            local = ConversationStore(tmp / "claim.db")
            owners = ["web:%032x" % index for index in range(12)]
            barrier = threading.Barrier(len(owners))
            winners: List[str] = []
            lock = threading.Lock()
            def worker(candidate: str) -> None:
                barrier.wait()
                try:
                    local.claim_session("same-session", candidate)
                except PermissionError:
                    return
                with lock:
                    winners.append(candidate)
            threads = [threading.Thread(target=worker, args=(candidate,)) for candidate in owners]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            if any(thread.is_alive() for thread in threads) or len(winners) != 1:
                raise AssertionError("claim did not produce one winner")
            return {"worker_count": len(owners), "winner_count": 1}

        _record(checks, REQUIRED_CHECKS[3], concurrent_claim)

        def concurrent_migration():
            db = tmp / "legacy.db"
            conn = sqlite3.connect(db)
            try:
                conn.executescript("""
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY, channel_type TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '', context_start_seq INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL, last_active INTEGER NOT NULL,
                    msg_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                    created_at INTEGER NOT NULL, extras TEXT NOT NULL DEFAULT '',
                    UNIQUE (session_id, seq)
                );
                """)
                conn.commit()
            finally:
                conn.close()
            barrier = threading.Barrier(8)
            errors: List[str] = []
            lock = threading.Lock()
            def worker() -> None:
                barrier.wait()
                try:
                    ConversationStore(db)
                except Exception as exc:
                    with lock:
                        errors.append(type(exc).__name__)
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)
            if errors or any(thread.is_alive() for thread in threads):
                raise AssertionError("concurrent migration failed: %r" % errors)
            return {"worker_count": 8, "errors": 0}

        _record(checks, REQUIRED_CHECKS[4], concurrent_migration)

        def concurrent_knowledge():
            workspace = tmp / "knowledge-startup"
            barrier = threading.Barrier(8)
            errors: List[str] = []
            lock = threading.Lock()
            def worker() -> None:
                barrier.wait()
                try:
                    runtime = GovernedKnowledgeRuntime(
                        str(workspace), migrate_legacy=False
                    )
                    runtime.close()
                except Exception as exc:
                    with lock:
                        errors.append(type(exc).__name__)
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)
            secret = workspace / "knowledge" / ".system" / "citation-capability.key"
            if errors or any(thread.is_alive() for thread in threads) or secret.stat().st_size != 32:
                raise AssertionError("concurrent knowledge startup failed: %r" % errors)
            return {"worker_count": 8, "secret_bytes": 32}

        _record(checks, REQUIRED_CHECKS[5], concurrent_knowledge)

        workspace = tmp / "citation"
        citation_owner = "web:" + "c" * 32
        identity = _identity(citation_owner)
        store.claim_session("citation-session", citation_owner)
        runtime = GovernedKnowledgeRuntime(str(workspace), migrate_legacy=False)
        try:
            runtime.write(
                identity,
                KnowledgeWriteCommand(
                    content="# Proof\nweb-boundary-citation-7741",
                    title="Proof",
                    source_ref="knowledge/session/security.md",
                    collection_id="session",
                    idempotency_key="web-boundary-citation",
                    projection_path="session/security.md",
                    scope=MemoryScope.SESSION,
                    session_id="citation-session",
                    sensitivity=Sensitivity.PRIVATE,
                ),
            )
            citation = runtime.search(
                identity, "web-boundary-citation-7741", session_id="citation-session"
            )[0].citation

            _record(
                checks,
                REQUIRED_CHECKS[6],
                lambda: (
                    {"binding_present": True}
                    if "&session_binding=" in citation.uri
                    else (_ for _ in ()).throw(AssertionError("missing session binding"))
                ),
            )
            _record(
                checks,
                REQUIRED_CHECKS[7],
                lambda: {"uri_match": runtime.resolve_verified_citation(identity, citation.uri).uri == citation.uri},
            )

            def tamper():
                match = re.search(
                    r"(&session_binding=[^.]+\.[0-9a-f]{1,16}\.)([0-9a-f]{64})",
                    citation.uri,
                )
                if match is None:
                    raise AssertionError("binding missing")
                signature = match.group(2)
                forged = citation.uri[:match.start(2)] + (
                    ("0" if signature[0] != "0" else "1") + signature[1:]
                ) + citation.uri[match.end(2):]
                try:
                    runtime.resolve_verified_citation(identity, forged)
                except KnowledgeCitationIntegrityError:
                    return {"tamper_rejected": True}
                raise AssertionError("forged capability resolved")

            _record(checks, REQUIRED_CHECKS[8], tamper)

            def replay():
                try:
                    runtime.resolve_verified_citation(_identity("web:" + "d" * 32), citation.uri)
                except KnowledgeAuthorizationError:
                    return {"cross_principal_rejected": True}
                raise AssertionError("cross-principal replay resolved")

            _record(checks, REQUIRED_CHECKS[9], replay)
        finally:
            runtime.close()

        from channel.web import web_channel
        original_conf = web_channel.conf
        original_preview = web_channel._PREVIEW_SECRET
        original_boot = web_channel._AUTH_BOOT_NONCE
        try:
            config = {
                "web_password": "first-password",
                "web_session_expire_days": 30,
                "agent_workspace": str(tmp / "web-workspace"),
            }
            web_channel.conf = lambda: config
            web_channel._PREVIEW_SECRET = b"s" * 32
            with web_channel._revoked_auth_lock:
                web_channel._revoked_auth_nonces.clear()
            with web_channel._stream_ticket_lock:
                web_channel._stream_tickets.clear()
            subject = "e" * 32
            owner = f"web:{subject}"
            subject_token = web_channel._create_auth_subject_token(subject)
            token = web_channel._create_auth_token(subject)
            revocable_file = tmp / "logout-revocable.txt"
            revocable_file.write_text("private", encoding="utf-8")
            file_capability = web_channel._encode_file_capability(
                str(revocable_file), owner
            )
            preview_capability = web_channel._encode_dir_token(str(tmp.resolve()), owner)
            stream_ticket = web_channel._issue_stream_ticket(owner, "logout-revocation")
            log_ticket = web_channel._issue_log_stream_ticket(owner)

            def logout_revoke():
                original_tokens = web_channel._request_auth_tokens
                try:
                    web_channel._request_auth_tokens = lambda: [token]
                    web_channel._revoke_request_auth_token()
                finally:
                    web_channel._request_auth_tokens = original_tokens
                if web_channel._verify_auth_token(token):
                    raise AssertionError("revoked token remained valid")
                for capability in (file_capability.removeprefix("/file/"), preview_capability):
                    try:
                        if capability == preview_capability:
                            web_channel._decode_dir_token(capability)
                        else:
                            web_channel._decode_file_capability(capability)
                    except ValueError:
                        continue
                    raise AssertionError("logout-revoked URL capability remained valid")
                if web_channel._consume_stream_ticket(stream_ticket, "logout-revocation") is not None:
                    raise AssertionError("logout-revoked SSE ticket remained usable")
                if web_channel._consume_log_stream_ticket(log_ticket) is not None:
                    raise AssertionError("logout-revoked log ticket remained usable")
                return {
                    "auth_nonce_revoked": True,
                    "file_and_preview_revoked": True,
                    "unconsumed_tickets_revoked": True,
                }

            _record(checks, REQUIRED_CHECKS[10], logout_revoke)

            fresh = web_channel._create_auth_token(subject)
            def restart_invalidates():
                web_channel._AUTH_BOOT_NONCE = b"simulated-new-process"
                if web_channel._verify_auth_token(fresh):
                    raise AssertionError("pre-restart bearer remained valid")
                return {"invalidated": True}
            _record(checks, REQUIRED_CHECKS[11], restart_invalidates)
            web_channel._AUTH_BOOT_NONCE = original_boot

            def password_rotation():
                config["web_password"] = "rotated-password"
                if web_channel._verify_auth_subject_token(subject_token) != subject:
                    raise AssertionError("subject was orphaned by password rotation")
                if web_channel._verify_auth_token(fresh):
                    raise AssertionError("old bearer survived password rotation")
                return {"subject_preserved": True, "old_bearer_invalid": True}
            _record(checks, REQUIRED_CHECKS[12], password_rotation)

            def public_bind():
                config["web_password"] = ""
                if web_channel._resolve_web_bind_host("0.0.0.0") != ("127.0.0.1", False):
                    raise AssertionError("anonymous public bind was accepted")
                config["web_password"] = "configured"
                if web_channel._resolve_web_bind_host("") != ("127.0.0.1", False):
                    raise AssertionError("password silently widened default bind")
                if web_channel._resolve_web_bind_host("0.0.0.0") != ("127.0.0.1", False):
                    raise AssertionError("plaintext public bind with password was accepted")
                return {
                    "anonymous_public_bind": "rejected",
                    "password_public_bind": "rejected",
                    "default": "loopback",
                }
            _record(checks, REQUIRED_CHECKS[13], public_bind)

            def preview():
                config["web_preview_token_ttl_seconds"] = 60
                preview_owner = "web:" + "a" * 32
                original_time = web_channel.time.time
                try:
                    web_channel.time.time = lambda: 1000.0
                    token_value = web_channel._encode_dir_token(
                        str(tmp.resolve()), preview_owner
                    )
                    if len(token_value.split(".")[-1]) != 64:
                        raise AssertionError("preview HMAC truncated")
                    if web_channel._decode_dir_token(token_value) != str(tmp.resolve()):
                        raise AssertionError("preview token did not round-trip")
                    if web_channel._decode_dir_capability(token_value) != (
                        str(tmp.resolve()),
                        preview_owner,
                    ):
                        raise AssertionError("preview token lost owner scope")
                    web_channel.time.time = lambda: 1061.0
                    try:
                        web_channel._decode_dir_token(token_value)
                    except ValueError:
                        return {
                            "hmac_hex_chars": 64,
                            "expiry_rejected": True,
                            "owner_bound": True,
                        }
                    raise AssertionError("expired preview token resolved")
                finally:
                    web_channel.time.time = original_time
            _record(checks, REQUIRED_CHECKS[14], preview)

            def upload_escape():
                upload = tmp / "uploads"
                sibling = tmp / "uploads-secret"
                upload.mkdir(exist_ok=True)
                sibling.mkdir(exist_ok=True)
                escaped = sibling / "secret.txt"
                if web_channel._is_within_directory(str(upload), str(escaped)):
                    raise AssertionError("prefix sibling accepted")
                first_owner = web_channel._owner_upload_dir("web:" + "3" * 32)
                second_owner = web_channel._owner_upload_dir("web:" + "4" * 32)
                if first_owner == second_owner:
                    raise AssertionError("different owners share an upload root")
                return {
                    "prefix_sibling_rejected": True,
                    "owner_roots_distinct": True,
                }
            _record(checks, REQUIRED_CHECKS[15], upload_escape)
        finally:
            web_channel.conf = original_conf
            web_channel._PREVIEW_SECRET = original_preview
            web_channel._AUTH_BOOT_NONCE = original_boot
            with web_channel._revoked_auth_lock:
                web_channel._revoked_auth_nonces.clear()

        def ui_wiring():
            web_js = (root / "channel/web/static/js/console.js").read_text(encoding="utf-8-sig")
            desktop = (root / "desktop/src/renderer/src/components/Markdown.tsx").read_text(encoding="utf-8")
            bubble = (root / "desktop/src/renderer/src/components/MessageBubble.tsx").read_text(encoding="utf-8")
            client = (root / "desktop/src/renderer/src/api/client.ts").read_text(encoding="utf-8")
            required = (
                "fetch('/api/knowledge/citation/resolve'" in web_js,
                "link_governed_citations" in web_js,
                "link_governed_citations" in desktop,
                "onCitationLink" in bubble and "resolveCitation" in bubble,
                "resolveKnowledgeCitation" in client,
            )
            if not all(required):
                raise AssertionError("citation UI wiring incomplete")
            return {"web": True, "desktop": True}
        _record(checks, REQUIRED_CHECKS[16], ui_wiring)

        def handler_contract():
            source = (root / "channel/web/web_channel.py").read_text(encoding="utf-8-sig")
            start = source.index("class KnowledgeCitationResolveHandler:")
            end = source.index("class KnowledgeGraphHandler:", start)
            handler = source[start:end]
            if 'set(body) != {"uri"}' not in handler:
                raise AssertionError("handler accepts fields other than uri")
            forbidden_client_claims = (
                'body.get("session_id")',
                'body.get("actor_user_id")',
                'body.get("roles")',
            )
            if any(claim in handler for claim in forbidden_client_claims):
                raise AssertionError("handler references client identity claims")
            if "_web_knowledge_service(owner_id)" not in handler:
                raise AssertionError("handler does not use server principal")
            return {"request_fields": ["uri"], "identity_source": "server"}
        _record(checks, REQUIRED_CHECKS[17], handler_contract)

        def foreign_delete_zero_cancel_side_effect():
            from agent.protocol.cancel import CancelTokenRegistry

            local_store = ConversationStore(tmp / "delete-owner.db")
            victim_owner = "web:" + "e" * 32
            foreign_owner = "web:" + "f" * 32
            victim_session = "formal-delete-victim"
            local_store.claim_session(victim_session, victim_owner)
            registry = CancelTokenRegistry()
            event = registry.register(
                "formal-delete-request",
                session_id=victim_session,
                owner_id=victim_owner,
            )
            with patch.object(
                web_channel, "_require_auth", return_value=foreign_owner
            ), patch(
                "agent.memory.get_conversation_store", return_value=local_store
            ), patch(
                "agent.protocol.get_cancel_registry", return_value=registry
            ), patch.object(web_channel.web, "header"):
                response = json.loads(
                    web_channel.SessionDetailHandler().DELETE(victim_session)
                )
            if response.get("status") != "error":
                raise AssertionError("foreign delete returned success")
            if event.is_set() or not local_store.owns_session(
                victim_session, victim_owner
            ):
                raise AssertionError("foreign delete produced a cancel/delete side effect")
            return {"cancel_event_set": False, "victim_still_owned": True}

        _record(checks, REQUIRED_CHECKS[18], foreign_delete_zero_cancel_side_effect)

        def scheduler_cross_owner_context():
            from agent.tools.scheduler.task_store import TaskStore
            from bridge.agent_bridge import AgentBridge

            victim_owner = "web:" + "1" * 32
            foreign_owner = "web:" + "2" * 32
            victim_session = "formal-scheduler-victim"
            local_store = ConversationStore(tmp / "scheduler-owner.db")
            local_store.claim_session(victim_session, victim_owner)
            local_store.append_messages(
                victim_session,
                [{"role": "user", "content": "victim"}],
                channel_type="web",
                owner_id=victim_owner,
            )
            tasks = TaskStore(str(tmp / "scheduler" / "tasks.json"))
            tasks.add_task({
                "id": "owned-task",
                "name": "owned",
                "enabled": True,
                "schedule": {"type": "interval", "seconds": 3600},
                "action": {
                    "type": "agent_task",
                    "task_description": "owner task",
                    "receiver": "owner",
                    "channel_type": "web",
                    "notify_session_id": victim_session,
                },
                "creator_owner_id": victim_owner,
            })
            if tasks.list_tasks(owner_id=foreign_owner):
                raise AssertionError("foreign owner listed scheduler task")
            cached = SimpleNamespace(
                messages=[{"role": "user", "content": "victim-cache"}],
                messages_lock=threading.RLock(),
                conversation_owner_id=victim_owner,
            )
            bridge = object.__new__(AgentBridge)
            bridge.agents = {victim_session: cached}
            before = list(cached.messages)
            with patch(
                "agent.memory.get_conversation_store", return_value=local_store
            ), patch(
                "config.conf",
                return_value={
                    "conversation_persistence": True,
                    "enable_thinking": False,
                    "scheduler_inject_to_session": True,
                    "scheduler_inject_max_per_session": 3,
                },
            ):
                try:
                    bridge.remember_scheduled_output(
                        victim_session,
                        "ATTACKER-CONTROLLED-CONTEXT",
                        channel_type="web",
                        task_description="inject",
                        owner_id=foreign_owner,
                    )
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("foreign scheduler output returned success")
            if cached.messages != before:
                raise AssertionError("foreign scheduler output changed agent cache")
            return {"foreign_tasks_visible": 0, "cache_mutated": False}

        _record(checks, REQUIRED_CHECKS[19], scheduler_cross_owner_context)

        def dns_rebinding_boundary():
            hostile_host = SimpleNamespace(env={
                "HTTP_HOST": "attacker.example:9876",
                "SERVER_NAME": "127.0.0.1",
            })
            with patch.object(web_channel.web, "ctx", hostile_host, create=True):
                try:
                    web_channel._require_safe_request_host()
                except Exception:
                    pass
                else:
                    raise AssertionError("hostile DNS-rebinding Host was accepted")

            hostile_origin = SimpleNamespace(env={
                "HTTP_HOST": "127.0.0.1:9876",
                "SERVER_NAME": "127.0.0.1",
                "HTTP_ORIGIN": "https://attacker.example",
            })
            with patch.object(web_channel.web, "ctx", hostile_origin, create=True):
                try:
                    web_channel._require_safe_request_host()
                except Exception:
                    pass
                else:
                    raise AssertionError("hostile Origin was accepted")

            safe = SimpleNamespace(env={
                "HTTP_HOST": "127.0.0.1:9876",
                "SERVER_NAME": "127.0.0.1",
                "HTTP_ORIGIN": "http://127.0.0.1:9876",
            })
            with patch.object(web_channel.web, "ctx", safe, create=True):
                web_channel._require_safe_request_host()
            return {"host_rejected": True, "origin_rejected": True}

        _record(checks, REQUIRED_CHECKS[20], dns_rebinding_boundary)

        def citation_tombstone_revocation():
            request = json.dumps({"uri": citation.uri}).encode("utf-8")
            with patch.object(
                web_channel, "_require_auth", return_value=citation_owner
            ), patch.object(web_channel.web, "header"), patch.object(
                web_channel.web, "data", return_value=request
            ), patch.object(
                web_channel, "_get_workspace_root", return_value=str(workspace)
            ), patch(
                "agent.memory.get_conversation_store", return_value=store
            ):
                active = json.loads(
                    web_channel.KnowledgeCitationResolveHandler().POST()
                )
            if active.get("status") != "success":
                raise AssertionError(
                    "active session citation did not resolve: %r" % active
                )
            store.delete_session("citation-session", citation_owner)
            with patch.object(
                web_channel, "_require_auth", return_value=citation_owner
            ), patch.object(web_channel.web, "header"), patch.object(
                web_channel.web, "data", return_value=request
            ), patch.object(
                web_channel, "_get_workspace_root", return_value=str(workspace)
            ), patch(
                "agent.memory.get_conversation_store", return_value=store
            ):
                deleted = json.loads(
                    web_channel.KnowledgeCitationResolveHandler().POST()
                )
            if deleted.get("code") != 410:
                raise AssertionError("deleted session citation still resolved")
            return {"active_resolved": True, "tombstone_code": 410}

        _record(checks, REQUIRED_CHECKS[21], citation_tombstone_revocation)

        def generic_file_upload_owner_bypass():
            victim_owner = "web:" + "7" * 32
            foreign_owner = "web:" + "8" * 32
            with patch.object(
                web_channel,
                "conf",
                return_value={
                    "agent_workspace": str(tmp / "web-workspace"),
                    "web_file_serve_root": str(tmp / "web-workspace"),
                },
            ), patch.object(web_channel, "_PREVIEW_SECRET", b"w" * 32):
                foreign_dir = Path(web_channel._owner_upload_dir(foreign_owner))
                secret = foreign_dir / "owner-secret.txt"
                secret.write_bytes(b"must-not-cross-owner")
                with patch.object(
                    web_channel, "_require_auth", return_value=victim_owner
                ), patch.object(web_channel.web, "header"), patch.object(
                    web_channel.web, "notfound", side_effect=web_channel.web.HTTPError("404 Not Found")
                ), patch.object(
                    web_channel.web,
                    "input",
                    return_value=SimpleNamespace(path=str(secret)),
                ):
                    try:
                        web_channel.FileServeHandler().GET()
                    except Exception:
                        pass
                    else:
                        raise AssertionError("generic file endpoint crossed upload owner boundary")
                    resolved = json.loads(web_channel.WorkspaceResolveHandler().GET())
            if resolved.get("status") != "error" or resolved.get("message") != "Path not allowed":
                raise AssertionError("workspace resolve exposed a foreign upload")
            return {"file_endpoint_rejected": True, "workspace_resolve_rejected": True}

        _record(checks, REQUIRED_CHECKS[22], generic_file_upload_owner_bypass)

        def sse_bearer_url_bypass():
            source = (root / "channel/web/web_channel.py").read_text(encoding="utf-8-sig")
            start = source.index("class StreamHandler:")
            end = source.index("class ChatHandler:", start)
            handler = source[start:end]
            if 'getattr(params, "token", "")' not in handler:
                raise AssertionError("SSE handler still accepts bearer query credentials")
            owner = "web:" + "9" * 32
            request_id = "request-bound-sse"
            ticket_value = web_channel._issue_stream_ticket(owner, request_id)
            if web_channel._consume_stream_ticket(ticket_value, "other-request") is not None:
                raise AssertionError("SSE ticket was not request-bound")
            if web_channel._consume_stream_ticket(ticket_value, request_id) != owner:
                raise AssertionError("valid SSE ticket did not resolve owner")
            if web_channel._consume_stream_ticket(ticket_value, request_id) is not None:
                raise AssertionError("SSE ticket was replayable after first consumption")
            return {"bearer_query_rejected": True, "request_bound": True, "one_shot": True}

        _record(checks, REQUIRED_CHECKS[23], sse_bearer_url_bypass)

        def login_bruteforce_rate_limited():
            key = "formal-login-attacker"
            with web_channel._login_attempts_lock:
                web_channel._login_attempts.clear()
            for _ in range(web_channel._LOGIN_MAX_FAILURES):
                if not web_channel._login_attempt_allowed(key):
                    raise AssertionError("login was limited before configured threshold")
                web_channel._record_login_failure(key)
            if web_channel._login_attempt_allowed(key):
                raise AssertionError("brute-force login remained unlimited")
            with web_channel._login_attempts_lock:
                web_channel._login_attempts.clear()
            return {"max_failures": web_channel._LOGIN_MAX_FAILURES, "limited": True}

        _record(checks, REQUIRED_CHECKS[24], login_bruteforce_rate_limited)

        def filesystem_root_serve_denied():
            with patch.object(
                web_channel,
                "conf",
                return_value={
                    "agent_workspace": str(tmp / "serve-workspace"),
                    "web_file_serve_root": str(Path(tmp).anchor),
                },
            ):
                roots = web_channel._serve_allowed_roots()
            if str(Path(tmp).anchor) in roots:
                raise AssertionError("filesystem root was accepted as a file-serving root")
            return {"filesystem_root_rejected": True}

        _record(checks, REQUIRED_CHECKS[25], filesystem_root_serve_denied)

        def single_file_capability():
            workspace = tmp / "capability-workspace"
            workspace.mkdir(exist_ok=True)
            target = workspace / "private-artifact.txt"
            target.write_text("private", encoding="utf-8")
            owner = "web:" + "c" * 32
            original_time = web_channel.time.time
            try:
                with patch.object(
                    web_channel,
                    "conf",
                    return_value={
                        "agent_workspace": str(workspace),
                        "web_file_serve_root": str(workspace),
                        "web_file_capability_ttl_seconds": 30,
                    },
                ), patch.object(web_channel, "_PREVIEW_SECRET", b"c" * 32):
                    web_channel.time.time = lambda: 1000.0
                    capability_url = web_channel._encode_file_capability(str(target), owner)
                    capability = capability_url.removeprefix("/file/")
                    path, token_owner = web_channel._decode_file_capability(capability)
                    if path != str(target.resolve()) or token_owner != owner:
                        raise AssertionError("file capability lost path or owner binding")
                    tampered = capability[:-1] + ("0" if capability[-1] != "0" else "1")
                    try:
                        web_channel._decode_file_capability(tampered)
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("tampered file capability resolved")
                    web_channel.time.time = lambda: 1031.0
                    try:
                        web_channel._decode_file_capability(capability)
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("expired file capability resolved")
            finally:
                web_channel.time.time = original_time
            client = (root / "desktop/src/renderer/src/api/client.ts").read_text(encoding="utf-8")
            if "previewUrl.startsWith('/file/')" not in client:
                raise AssertionError("desktop still appends bearer to file capabilities")
            return {"tamper_rejected": True, "expiry_rejected": True, "owner_bound": True}

        _record(checks, REQUIRED_CHECKS[26], single_file_capability)

        def legacy_bearer_file_and_log_url_bypasses():
            client = (root / "desktop/src/renderer/src/api/client.ts").read_text(
                encoding="utf-8"
            )
            console = (root / "channel/web/static/js/console.js").read_text(
                encoding="utf-8"
            )
            server = (root / "channel/web/web_channel.py").read_text(
                encoding="utf-8-sig"
            )
            if (
                "withToken(" in client
                or "getServeFileUrl" in client
                or "/api/file?path=" in console
                or "_get_query_token" in server
            ):
                raise AssertionError("legacy bearer-capable URL construction remains")

            workspace = tmp / "history-capability-workspace"
            workspace.mkdir(exist_ok=True)
            target = workspace / "history.txt"
            target.write_text("private history", encoding="utf-8")
            owner = "web:" + "e" * 32
            history = {
                "messages": [{
                    "role": "assistant",
                    "steps": [{
                        "type": "tool",
                        "result": json.dumps({
                            "type": "file_to_send",
                            "path": str(target),
                            "url": "/api/file?path=old&token=leaked",
                        }),
                    }],
                }]
            }
            with patch.object(
                web_channel,
                "conf",
                return_value={
                    "agent_workspace": str(workspace),
                    "web_file_serve_root": str(workspace),
                },
            ), patch.object(web_channel, "_PREVIEW_SECRET", b"e" * 32):
                decorated = web_channel._decorate_history_file_capabilities(
                    history, owner
                )
                payload = json.loads(
                    decorated["messages"][0]["steps"][0]["result"]
                )
                capability = str(payload.get("url") or "")
                if not capability.startswith("/file/") or "token=" in capability:
                    raise AssertionError("history preserved a bearer URL")
                path, capability_owner = web_channel._decode_file_capability(
                    capability.removeprefix("/file/")
                )
                if path != str(target.resolve()) or capability_owner != owner:
                    raise AssertionError("history capability lost owner/path binding")

            log_ticket = web_channel._issue_log_stream_ticket(owner)
            if web_channel._consume_log_stream_ticket(log_ticket) != owner:
                raise AssertionError("log ticket did not resolve owner")
            if web_channel._consume_log_stream_ticket(log_ticket) is not None:
                raise AssertionError("log ticket was replayable")
            return {
                "history_capability_bound": True,
                "query_bearer_disabled": True,
                "log_ticket_one_shot": True,
            }

        _record(checks, REQUIRED_CHECKS[27], legacy_bearer_file_and_log_url_bypasses)

        def durable_web_execution_replay_and_terminal_fence():
            """Attack the request ledger rather than trusting a transport ``done``.

            This deliberately combines request-retry, forged-writer, terminal
            delivery, old-journal, restart, and retention paths. A Web user
            must never see a success solely because a stale process committed
            an SSE ``done``; the authenticated execution outcome is controlling.
            """

            ledger = DurableSSEJournalStore(str(tmp / "web-execution-ledger.db"))
            ledger_owner = "web:" + "f" * 32
            ledger_attacker = "web:" + "g" * 32
            ledger_session = "web-execution-security-session"
            ledger_key = "web-execution-security-key-0001"
            ledger_runner = "web-execution-security-runner"
            digest = "a" * 64

            claim = ledger.claim_execution(
                "web-execution-first",
                ledger_owner,
                ledger_session,
                ledger_key,
                digest,
                ledger_runner,
            )
            duplicate = ledger.claim_execution(
                "web-execution-retry",
                ledger_owner,
                ledger_session,
                ledger_key,
                digest,
                ledger_runner,
            )
            if (
                claim["claim_status"] != "claimed"
                or duplicate["claim_status"] != "duplicate"
                or duplicate["request_id"] != claim["request_id"]
                or "lease_token" in duplicate
            ):
                raise AssertionError("same authenticated retry could create or seize a worker")

            try:
                ledger.claim_execution(
                    "web-execution-mutated-retry",
                    ledger_owner,
                    ledger_session,
                    ledger_key,
                    "b" * 64,
                    ledger_runner,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("mutated idempotency retry was accepted")

            try:
                ledger.finish_execution(
                    claim["request_id"],
                    ledger_owner,
                    ledger_session,
                    "forged-lease",
                    ledger_runner,
                    outcome="completed",
                )
            except RuntimeError:
                pass
            else:
                raise AssertionError("forged execution lease settled a request")

            try:
                ledger.append(
                    claim["request_id"],
                    1,
                    {"type": "done", "content": "false success"},
                )
            except RuntimeError:
                pass
            else:
                raise AssertionError("running authenticated request emitted done")

            ledger.append(
                claim["request_id"],
                1,
                {"type": "phase", "content": "still running"},
            )
            if (
                ledger.mark_interrupted_execution(
                    claim["request_id"], ledger_attacker
                )
                is not None
            ):
                raise AssertionError("foreign principal fenced another owner's request")
            if (
                ledger.replay(claim["request_id"], ledger_owner)["execution_state"]
                != "running"
            ):
                raise AssertionError("foreign fence changed request state")
            fenced = ledger.mark_interrupted_execution(claim["request_id"], ledger_owner)
            if fenced is None or fenced["execution_state"] != "in_doubt":
                raise AssertionError("interrupted worker was not fail-closed")
            try:
                ledger.append(
                    claim["request_id"],
                    2,
                    {"type": "done", "content": "false success after interruption"},
                )
            except RuntimeError:
                pass
            else:
                raise AssertionError("in-doubt request emitted done")
            if ledger.reap(now=time.time() + ledger._RETENTION_SECONDS + 1) != 0:
                raise AssertionError("in-doubt execution evidence was reaped")

            # Simulate an old server that had persisted transport completion
            # before the execution fence existed. Recovery must rewrite it to
            # a non-terminal phase and append an explicit uncertainty error.
            old_request_id = "web-execution-old-transport-done"
            ledger.begin(old_request_id, ledger_owner, ledger_session)
            ledger.append(
                old_request_id, 1, {"type": "done", "content": "stale success"}
            )
            raw_web_channel = dict(
                zip(
                    web_channel.WebChannel.__code__.co_freevars,
                    (
                        cell.cell_contents
                        for cell in web_channel.WebChannel.__closure__
                    ),
                )
            )["cls"]
            recovery = SimpleNamespace(
                request_to_session={},
                request_owners={},
                sse_queues={},
                sse_last_active={},
            )
            with patch.object(
                web_channel, "_get_durable_sse_store", lambda: ledger
            ):
                recovered = raw_web_channel._recover_sse_request(
                    recovery, old_request_id, ledger_owner
                )
            if not recovered:
                raise AssertionError("old durable request was not recoverable")
            journal = recovery.sse_queues[old_request_id]
            first = journal.read_after(0, timeout=0)
            terminal = journal.read_after(1, timeout=0)
            if (
                first is None
                or first[0] != 1
                or first[1].get("type") != "phase"
                or terminal is None
                or terminal[0] != 2
                or terminal[1].get("type") != "error"
                or "unconfirmed" not in str(terminal[1].get("message") or "")
            ):
                raise AssertionError("old terminal success replay was not scrubbed")
            old_replay = ledger.replay(old_request_id, ledger_owner)
            if old_replay is None or old_replay["execution_state"] != "in_doubt":
                raise AssertionError("old terminal event was trusted after recovery")
            return {
                "retry_single_worker": True,
                "mutated_retry_rejected": True,
                "forged_lease_rejected": True,
                "terminal_delivery_fenced": True,
                "foreign_fence_rejected": True,
                "old_done_scrubbed": True,
                "in_doubt_retained": True,
            }

        _record(
            checks,
            REQUIRED_CHECKS[28],
            durable_web_execution_replay_and_terminal_fence,
        )

        def web_steer_idempotency_and_type_fence():
            """Reject malformed Web control and deduplicate accepted retries."""

            from agent.protocol.steer import (
                SteerInbox,
                SteerResult,
                SteerStatus,
            )

            key = "steer-security-request-0001"
            inbox = SteerInbox(max_pending=1, max_idempotency_keys=2)
            accepted = inbox.submit("change target", idempotency_key=key)
            retry = inbox.submit("change target", idempotency_key=key)
            if (
                accepted != SteerResult(SteerStatus.ACCEPTED)
                or retry != SteerResult(SteerStatus.ACCEPTED, duplicate=True)
                or inbox.drain() != ["change target"]
                or not inbox.submit("change target", idempotency_key=key).duplicate
            ):
                raise AssertionError("accepted Web steer retry was injected twice")
            if inbox.submit(
                "new target", idempotency_key="steer-security-request-0002"
            ).status != SteerStatus.ACCEPTED:
                raise AssertionError("distinct steering instruction was rejected")
            if inbox.submit(
                "memory pressure", idempotency_key="steer-security-request-0003"
            ).status != SteerStatus.FULL:
                raise AssertionError("steer idempotency retention was unbounded")

            raw_web_channel = dict(
                zip(
                    web_channel.WebChannel.__code__.co_freevars,
                    (
                        cell.cell_contents
                        for cell in web_channel.WebChannel.__closure__
                    ),
                )
            )["cls"]
            calls: list[tuple[str, str, str]] = []
            fake_agent_bridge = SimpleNamespace(
                steer_session=lambda session, instruction, *, idempotency_key: (
                    calls.append((session, instruction, idempotency_key))
                    or SteerResult(SteerStatus.ACCEPTED)
                )
            )
            fake_bridge = SimpleNamespace(
                get_agent_bridge=lambda: fake_agent_bridge
            )
            control_owner = "web:" + "h" * 32

            with patch.object(
                web_channel.web,
                "data",
                return_value=json.dumps(
                    {
                        "session_id": "steer-security-session",
                        "message": "malformed type must not inject",
                        "steer": "false",
                    }
                ).encode(),
            ):
                malformed = json.loads(
                    raw_web_channel.post_message(
                        SimpleNamespace(), owner_id=control_owner
                    )
                )
            if malformed.get("status") != "error" or calls:
                raise AssertionError("non-boolean steer flag reached Agent control")

            with patch.object(
                web_channel.web,
                "data",
                return_value=json.dumps(
                    {
                        "session_id": "steer-security-session",
                        "message": "missing key must not inject",
                        "steer": True,
                        "stream": False,
                    }
                ).encode(),
            ):
                missing_key = json.loads(
                    raw_web_channel.post_message(
                        SimpleNamespace(), owner_id=control_owner
                    )
                )
            if missing_key.get("status") != "error" or calls:
                raise AssertionError("authenticated steer without key reached Agent control")

            with patch.object(
                web_channel, "_require_web_session", lambda *_args: None
            ), patch.object(
                web_channel.web,
                "data",
                return_value=json.dumps(
                    {
                        "session_id": "steer-security-session",
                        "message": "change target",
                        "steer": True,
                        "stream": False,
                        "idempotency_key": key,
                        "lang": "en",
                    }
                ).encode(),
            ), patch("bridge.bridge.Bridge", lambda: fake_bridge):
                delivered = json.loads(
                    raw_web_channel.post_message(
                        SimpleNamespace(), owner_id=control_owner
                    )
                )
            if (
                delivered.get("steered") is not True
                or calls != [("steer-security-session", "change target", key)]
            ):
                raise AssertionError("authenticated steer lost idempotency binding")
            return {
                "non_boolean_rejected": True,
                "missing_key_rejected": True,
                "retry_deduplicated": True,
                "retention_bounded": True,
                "server_binding_forwarded": True,
            }

        _record(
            checks,
            REQUIRED_CHECKS[29],
            web_steer_idempotency_and_type_fence,
        )

    return checks


def generate_report(root: Path | None = None) -> Dict[str, Any]:
    root = (root or _root()).resolve()
    checks = run_checks(root)
    names = [item["name"] for item in checks]
    passed = names == list(REQUIRED_CHECKS) and all(item["passed"] for item in checks)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "completed",
        "source_fingerprint_sha256": source_fingerprint(root),
        "required_checks": list(REQUIRED_CHECKS),
        "checks": checks,
        "passed": passed,
        "limitations": {
            "local_execution_only": True,
            "remote_ci_protected": False,
            "production_deployment_verified": False,
            "customer_execution_verified": False,
        },
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args(arguments)
    report = generate_report()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
