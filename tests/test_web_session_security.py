import json
import sqlite3
import threading
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agent.memory.conversation_store import ConversationStore
from agent.memory.governance import IdentityContext


def _messages():
    return [
        {"role": "user", "content": [{"type": "text", "text": "owner proof"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "private reply"}]},
    ]


def test_conversation_store_cross_owner_operations_have_zero_side_effects(tmp_path):
    store = ConversationStore(tmp_path / "index.db")
    owner = "web:" + "a" * 32
    attacker = "web:" + "b" * 32
    session_id = "session-owner-isolation"

    store.claim_session(session_id, owner)
    store.append_messages(session_id, _messages(), channel_type="web", owner_id=owner)
    before = store.load_history_page(session_id, owner_id=owner)

    denied_calls = [
        lambda: store.load_messages(session_id),
        lambda: store.get_latest_pair_seqs(session_id),
        lambda: store.append_messages(
            session_id,
            [{"role": "user", "content": "ownerless overwrite"}],
            channel_type="web",
        ),
        lambda: store.clear_session(session_id),
        lambda: store.claim_session(session_id, attacker),
        lambda: store.append_messages(
            session_id,
            [{"role": "user", "content": "overwrite"}],
            channel_type="web",
            owner_id=attacker,
        ),
        lambda: store.load_history_page(session_id, owner_id=attacker),
        lambda: store.rename_session(session_id, "stolen", owner_id=attacker),
        lambda: store.clear_context(session_id, owner_id=attacker),
        lambda: store.delete_message_pair(session_id, 0, owner_id=attacker),
        lambda: store.attach_extras_to_last_assistant(
            session_id, {"audio": {"url": "/stolen"}}, owner_id=attacker
        ),
        lambda: store.clear_session(session_id, owner_id=attacker),
    ]
    for call in denied_calls:
        with pytest.raises(PermissionError):
            call()

    after = store.load_history_page(session_id, owner_id=owner)
    assert after == before
    assert store.owns_session(session_id, owner)
    assert not store.owns_session(session_id, attacker)
    assert store.list_sessions(channel_type="web", owner_id=owner)["total"] == 1
    assert store.list_sessions(channel_type="web", owner_id=attacker)["total"] == 0


def test_concurrent_session_claim_has_exactly_one_winner(tmp_path):
    store = ConversationStore(tmp_path / "index.db")
    session_id = "session-concurrent-claim"
    owners = [f"web:{index:032x}" for index in range(16)]
    barrier = threading.Barrier(len(owners))
    winners = []
    denied = []
    lock = threading.Lock()

    def claim(owner_id):
        barrier.wait()
        try:
            store.claim_session(session_id, owner_id)
        except PermissionError:
            with lock:
                denied.append(owner_id)
        else:
            with lock:
                winners.append(owner_id)

    threads = [threading.Thread(target=claim, args=(owner,)) for owner in owners]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert len(winners) == 1
    assert len(denied) == len(owners) - 1
    assert store.owns_session(session_id, winners[0])


def test_legacy_web_rows_migrate_to_non_claimable_legacy_owner(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                channel_type TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                context_start_seq INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                last_active INTEGER NOT NULL,
                msg_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                extras TEXT NOT NULL DEFAULT '',
                UNIQUE (session_id, seq)
            );
            INSERT INTO sessions
                (session_id, channel_type, created_at, last_active, msg_count)
            VALUES ('legacy-web-session', 'web', 1, 1, 0);
            """
        )
        conn.commit()
    finally:
        conn.close()

    store = ConversationStore(db_path)
    assert store.owns_session("legacy-web-session", "web:legacy")
    with pytest.raises(PermissionError):
        store.claim_session("legacy-web-session", "web:" + "c" * 32)


def test_v3_auth_tokens_use_fixed_expiry_policy_epoch_and_logout_revocation(monkeypatch, tmp_path):
    from channel.web import web_channel

    config = {"web_password": "correct horse battery staple", "web_session_expire_days": 30}
    monkeypatch.setattr(web_channel, "conf", lambda: config)
    monkeypatch.setattr(web_channel, "_PREVIEW_SECRET", b"subject-secret" * 3)
    with web_channel._revoked_auth_lock:
        web_channel._revoked_auth_nonces.clear()

    subject_id = "d" * 32
    subject_token = web_channel._create_auth_subject_token(subject_id)
    assert web_channel._verify_auth_subject_token(subject_token) == subject_id
    first = web_channel._create_auth_token(subject_id)
    config["web_password"] = "rotated password"
    assert web_channel._verify_auth_subject_token(subject_token) == subject_id
    assert not web_channel._verify_auth_token(first)
    config["web_password"] = "correct horse battery staple"
    second = web_channel._create_auth_token(subject_id)
    assert first != second
    # Reverting a password/policy never revives a token from an older epoch.
    assert web_channel._parse_auth_token(first) is None
    assert web_channel._parse_auth_token(second)["subject_id"] == subject_id

    owner_id = f"web:{subject_id}"
    target = tmp_path / "logout-revoked.txt"
    target.write_text("sensitive", encoding="utf-8")
    file_capability = web_channel._encode_file_capability(str(target), owner_id)
    preview_capability = web_channel._encode_dir_token(str(tmp_path), owner_id)
    stream_ticket = web_channel._issue_stream_ticket(owner_id, "logout-revocation")
    log_ticket = web_channel._issue_log_stream_ticket(owner_id)

    with patch.object(web_channel, "_request_auth_tokens", return_value=[second]):
        assert web_channel._get_auth_principal() == f"web:{subject_id}"
        web_channel._revoke_request_auth_token()
    assert not web_channel._verify_auth_token(second)
    with pytest.raises(ValueError, match="Revoked"):
        web_channel._decode_file_capability(file_capability.removeprefix("/file/"))
    with pytest.raises(ValueError, match="Revoked"):
        web_channel._decode_dir_token(preview_capability)
    assert web_channel._consume_stream_ticket(stream_ticket, "logout-revocation") is None
    assert web_channel._consume_log_stream_ticket(log_ticket) is None

    third = web_channel._create_auth_token(subject_id)
    tampered = third[:-1] + ("0" if third[-1] != "0" else "1")
    assert not web_channel._verify_auth_token(tampered)

    # Simulate a process restart: bearer dies, but the signed subject remains
    # usable only as input to a future password-authenticated login.
    monkeypatch.setattr(web_channel, "_AUTH_BOOT_NONCE", b"new-process-boot")
    assert not web_channel._verify_auth_token(third)
    assert web_channel._verify_auth_subject_token(subject_token) == subject_id



def test_auth_policy_ttl_change_cannot_resurrect_old_bearer_or_url_capability(
    monkeypatch, tmp_path
):
    from channel.web import web_channel

    config = {"web_password": "policy-password", "web_session_expire_days": 30}
    monkeypatch.setattr(web_channel, "conf", lambda: config)
    monkeypatch.setattr(web_channel, "_PREVIEW_SECRET", b"policy-secret" * 3)
    subject_id = "9" * 32
    owner_id = f"web:{subject_id}"
    token = web_channel._create_auth_token(subject_id)
    assert web_channel._verify_auth_token(token)
    target = tmp_path / "policy-revoked.txt"
    target.write_text("sensitive", encoding="utf-8")
    file_capability = web_channel._encode_file_capability(str(target), owner_id)
    preview_capability = web_channel._encode_dir_token(str(tmp_path), owner_id)
    stream_ticket = web_channel._issue_stream_ticket(owner_id, "policy-revocation")
    log_ticket = web_channel._issue_log_stream_ticket(owner_id)
    config["web_session_expire_days"] = 1
    assert not web_channel._verify_auth_token(token)
    with pytest.raises(ValueError, match="Revoked"):
        web_channel._decode_file_capability(file_capability.removeprefix("/file/"))
    with pytest.raises(ValueError, match="Revoked"):
        web_channel._decode_dir_token(preview_capability)
    assert web_channel._consume_stream_ticket(stream_ticket, "policy-revocation") is None
    assert web_channel._consume_log_stream_ticket(log_ticket) is None
    config["web_session_expire_days"] = 30
    assert not web_channel._verify_auth_token(token)


def test_conflicting_auth_credentials_fail_closed_and_logout_revokes_all(monkeypatch):
    from channel.web import web_channel

    config = {"web_password": "multi-token-password", "web_session_expire_days": 30}
    monkeypatch.setattr(web_channel, "conf", lambda: config)
    first = web_channel._create_auth_token("a" * 32)
    second = web_channel._create_auth_token("b" * 32)
    with patch.object(
        web_channel, "_request_auth_tokens", return_value=[first, second]
    ):
        assert web_channel._get_auth_principal() is None
        web_channel._revoke_request_auth_token()
    assert not web_channel._verify_auth_token(first)
    assert not web_channel._verify_auth_token(second)

def test_foreign_cancel_command_is_rejected_before_registry_side_effect(tmp_path):
    from channel.web import web_channel

    store = ConversationStore(tmp_path / "index.db")
    store.claim_session("victim-session", "web:" + "e" * 32)
    raw_class = web_channel.WebChannel.__closure__[0].cell_contents
    instance = object.__new__(raw_class)
    payload = {"session_id": "victim-session", "message": "/cancel", "lang": "en"}

    with patch("agent.memory.get_conversation_store", return_value=store), patch.object(
        web_channel.web, "data", return_value=json.dumps(payload).encode()
    ), patch("agent.protocol.get_cancel_registry") as registry_factory:
        response = json.loads(
            raw_class.post_message(instance, owner_id="web:" + "f" * 32)
        )

    assert response["status"] == "error"
    assert response["message"] == "session not found"
    registry_factory.assert_not_called()


def test_history_handler_does_not_disclose_foreign_session(tmp_path):
    from channel.web import web_channel

    store = ConversationStore(tmp_path / "index.db")
    owner = "web:" + "1" * 32
    attacker = "web:" + "2" * 32
    store.claim_session("history-private", owner)
    store.append_messages(
        "history-private", _messages(), channel_type="web", owner_id=owner
    )
    params = SimpleNamespace(session_id="history-private", page="1", page_size="20")

    with patch("agent.memory.get_conversation_store", return_value=store), patch.object(
        web_channel, "_require_auth", return_value=attacker
    ), patch.object(web_channel.web, "header"), patch.object(
        web_channel.web, "input", return_value=params
    ):
        denied = json.loads(web_channel.HistoryHandler().GET())

    assert denied == {"status": "error", "message": "session not found"}


def test_query_bearer_is_never_used_as_request_authentication():
    from channel.web import web_channel

    with patch.object(web_channel.web, "cookies", return_value={}, create=True), patch.object(
        web_channel, "_get_bearer_token", return_value="header-bearer"
    ), patch.object(
        web_channel.web,
        "input",
        return_value=SimpleNamespace(token="query-bearer"),
        create=True,
    ) as query_input:
        assert web_channel._request_auth_tokens() == ["header-bearer"]
    query_input.assert_not_called()


def test_stream_request_capability_is_bound_to_owner():
    from channel.web import web_channel

    raw_class = web_channel.WebChannel.__closure__[0].cell_contents
    instance = object.__new__(raw_class)
    instance.sse_queues = {"request-1": Queue()}
    instance.sse_last_active = {"request-1": 0.0}
    instance.request_to_session = {"request-1": "session-1"}
    instance.request_owners = {"request-1": "web:" + "3" * 32}
    instance.sse_queues["request-1"].put({"type": "done"})

    chunks = list(
        raw_class.stream_response(
            instance, "request-1", owner_id="web:" + "4" * 32
        )
    )
    assert len(chunks) == 1
    assert b"invalid request_id" in chunks[0]
    assert not instance.sse_queues["request-1"].empty()


def test_agent_cache_rejects_identity_downgrade_and_cross_principal_reuse():
    from bridge.agent_bridge import AgentBridge

    owner = IdentityContext(
        tenant_id="tenant-local",
        actor_user_id="web:" + "5" * 32,
        roles=frozenset(),
        trace_id="owner",
        auth_source="web-password",
    )
    attacker = IdentityContext(
        tenant_id="tenant-local",
        actor_user_id="web:" + "6" * 32,
        roles=frozenset(),
        trace_id="attacker",
        auth_source="web-password",
    )
    owner_id = "web:" + "5" * 32
    agent = SimpleNamespace(
        identity_context=owner, conversation_owner_id=owner_id
    )

    AgentBridge._assert_agent_identity(agent, owner, owner_id)
    with pytest.raises(PermissionError):
        AgentBridge._assert_agent_identity(agent, attacker, owner_id)
    with pytest.raises(PermissionError):
        AgentBridge._assert_agent_identity(
            agent, owner, "web:" + "9" * 32
        )
    with pytest.raises(PermissionError):
        AgentBridge._assert_agent_identity(agent, None, None)


def test_public_bind_without_password_falls_back_to_loopback(monkeypatch):
    from channel.web import web_channel

    monkeypatch.setattr(web_channel, "conf", lambda: {"web_password": ""})
    assert web_channel._resolve_web_bind_host("0.0.0.0") == ("127.0.0.1", False)
    assert web_channel._resolve_web_bind_host("::") == ("127.0.0.1", False)
    assert web_channel._resolve_web_bind_host("") == ("127.0.0.1", False)

    monkeypatch.setattr(web_channel, "conf", lambda: {"web_password": "secret"})
    assert web_channel._resolve_web_bind_host("") == ("127.0.0.1", False)
    assert web_channel._resolve_web_bind_host("0.0.0.0") == ("127.0.0.1", False)


def test_concurrent_legacy_schema_migration_all_initializers_recover(tmp_path):
    db_path = tmp_path / "concurrent-legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
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
            """
        )
        conn.commit()
    finally:
        conn.close()

    worker_count = 8
    barrier = threading.Barrier(worker_count)
    stores = []
    errors = []
    lock = threading.Lock()

    def initialize():
        barrier.wait()
        try:
            store = ConversationStore(db_path)
        except Exception as exc:
            with lock:
                errors.append(exc)
        else:
            with lock:
                stores.append(store)

    threads = [threading.Thread(target=initialize) for _ in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(stores) == worker_count
    columns = {
        row[1] for row in sqlite3.connect(db_path).execute(
            "PRAGMA table_info(sessions)"
        ).fetchall()
    }
    assert "owner_id" in columns


def test_concurrent_knowledge_runtime_startup_uses_one_complete_citation_secret(tmp_path):
    from agent.knowledge.runtime import GovernedKnowledgeRuntime

    worker_count = 8
    barrier = threading.Barrier(worker_count)
    errors = []
    lock = threading.Lock()

    def initialize():
        barrier.wait()
        try:
            runtime = GovernedKnowledgeRuntime(str(tmp_path), migrate_legacy=False)
            runtime.close()
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=initialize) for _ in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    secret_path = tmp_path / "knowledge" / ".system" / "citation-capability.key"
    assert len(secret_path.read_bytes()) == 32


def test_deleted_web_session_is_tombstoned_and_cannot_be_resurrected(tmp_path):
    store = ConversationStore(tmp_path / "index.db")
    owner = "web:" + "7" * 32
    session_id = "session-tombstone"
    store.claim_session(session_id, owner)
    store.append_messages(session_id, _messages(), channel_type="web", owner_id=owner)

    store.delete_session(session_id, owner_id=owner)
    # Retry after an unknown network outcome is a no-op success for the owner.
    store.delete_session(session_id, owner_id=owner)
    with pytest.raises(PermissionError):
        store.delete_session(session_id, owner_id="web:" + "0" * 32)

    assert not store.owns_session(session_id, owner)
    assert store.list_sessions(channel_type="web", owner_id=owner)["total"] == 0
    with pytest.raises(PermissionError):
        store.claim_session(session_id, owner)
    with pytest.raises(PermissionError):
        store.append_messages(
            session_id,
            [{"role": "assistant", "content": "late reply"}],
            channel_type="web",
            owner_id=owner,
        )

    conn = sqlite3.connect(tmp_path / "index.db")
    try:
        row = conn.execute(
            "SELECT state, msg_count FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        message_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert row == ("deleted", 0)
    assert message_count == 0


def test_delete_racing_with_late_writer_never_reactivates_session(tmp_path):
    store = ConversationStore(tmp_path / "index.db")
    owner = "web:" + "8" * 32
    session_id = "session-delete-race"
    store.claim_session(session_id, owner)
    start = threading.Barrier(2)
    writer_stopped = threading.Event()

    def late_writer():
        start.wait()
        for index in range(200):
            try:
                store.append_messages(
                    session_id,
                    [{"role": "assistant", "content": f"late-{index}"}],
                    channel_type="web",
                    owner_id=owner,
                )
            except PermissionError:
                writer_stopped.set()
                return

    thread = threading.Thread(target=late_writer)
    thread.start()
    start.wait()
    store.delete_session(session_id, owner_id=owner)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert writer_stopped.is_set()
    assert not store.owns_session(session_id, owner)
    conn = sqlite3.connect(tmp_path / "index.db")
    try:
        assert conn.execute(
            "SELECT state FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()[0] == "deleted"
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_upload_file_route_rejects_prefix_sibling_escape(tmp_path):
    from channel.web import web_channel

    upload_dir = tmp_path / "uploads"
    sibling = tmp_path / "uploads-stolen"
    upload_dir.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret", encoding="utf-8")

    escaped = (upload_dir / ".." / "uploads-stolen" / "secret.txt").resolve()
    assert not web_channel._is_within_directory(str(upload_dir), str(escaped))
    source = Path(web_channel.__file__).read_text(encoding="utf-8-sig")
    handler = source[source.index("class UploadsHandler:"):source.index("class FileServeHandler:")]
    assert "if not _is_within_directory(upload_dir, full_path)" in handler



def test_uploads_are_scoped_to_authenticated_owner(monkeypatch, tmp_path):
    from channel.web import web_channel

    owner = "web:" + "3" * 32
    attacker = "web:" + "4" * 32
    monkeypatch.setattr(
        web_channel,
        "conf",
        lambda: {"agent_workspace": str(tmp_path), "web_password": "password"},
    )
    monkeypatch.setattr(web_channel, "_PREVIEW_SECRET", b"u" * 32)
    owner_dir = Path(web_channel._owner_upload_dir(owner))
    attacker_dir = Path(web_channel._owner_upload_dir(attacker))
    assert owner_dir != attacker_dir
    (owner_dir / "secret.txt").write_bytes(b"owner-secret")

    with patch.object(web_channel, "_require_auth", return_value=attacker), patch.object(
        web_channel.web, "header"
    ):
        with pytest.raises(Exception):
            web_channel.UploadsHandler().GET("secret.txt")

    with patch.object(web_channel, "_require_auth", return_value=owner), patch.object(
        web_channel.web, "header"
    ):
        assert web_channel.UploadsHandler().GET("secret.txt") == b"owner-secret"


def test_preview_mount_rejects_foreign_owner_upload_even_with_signed_token(
    monkeypatch, tmp_path
):
    """A confused internal caller must not turn an owner A directory into B's preview."""

    from channel.web import web_channel

    owner = "web:" + "7" * 32
    attacker = "web:" + "8" * 32
    monkeypatch.setattr(
        web_channel,
        "conf",
        lambda: {"agent_workspace": str(tmp_path), "web_password": "password"},
    )
    monkeypatch.setattr(web_channel, "_PREVIEW_SECRET", b"preview-owner-scope" * 2)
    owner_dir = Path(web_channel._owner_upload_dir(owner))
    secret = owner_dir / "secret.txt"
    secret.write_bytes(b"owner-secret")

    # The token is syntactically authentic but was deliberately issued under a
    # different owner. PreviewHandler must still reject it at the upload-root
    # ownership boundary instead of trusting a directory capability alone.
    attacker_token = web_channel._encode_dir_token(str(owner_dir), attacker)
    with patch.object(web_channel.web, "header"):
        with pytest.raises(Exception):
            web_channel.PreviewHandler().GET(f"{attacker_token}/{secret.name}")

    owner_token = web_channel._encode_dir_token(str(owner_dir), owner)
    with patch.object(web_channel.web, "header"):
        assert web_channel.PreviewHandler().GET(
            f"{owner_token}/{secret.name}"
        ) == b"owner-secret"


def test_generic_file_and_workspace_resolve_cannot_bypass_upload_owner_scope(monkeypatch, tmp_path):
    from channel.web import web_channel

    owner = "web:" + "5" * 32
    attacker = "web:" + "6" * 32
    monkeypatch.setattr(
        web_channel,
        "conf",
        lambda: {"agent_workspace": str(tmp_path), "web_password": "password"},
    )
    monkeypatch.setattr(web_channel, "_PREVIEW_SECRET", b"v" * 32)
    attacker_dir = Path(web_channel._owner_upload_dir(attacker))
    secret = attacker_dir / "secret.txt"
    secret.write_bytes(b"do-not-leak")

    with patch.object(web_channel, "_require_auth", return_value=owner), patch.object(
        web_channel.web, "header"
    ), patch.object(web_channel.web, "input", return_value=SimpleNamespace(path=str(secret))):
        with pytest.raises(Exception):
            web_channel.FileServeHandler().GET()
        resolved = json.loads(web_channel.WorkspaceResolveHandler().GET())
        assert resolved["status"] == "error"
        assert resolved["message"] == "Path not allowed"


def test_file_serve_root_rejects_filesystem_root(monkeypatch, tmp_path):
    from channel.web import web_channel

    monkeypatch.setattr(
        web_channel,
        "conf",
        lambda: {"agent_workspace": str(tmp_path), "web_file_serve_root": str(Path(tmp_path).anchor)},
    )
    roots = web_channel._serve_allowed_roots()
    assert str(Path(tmp_path).anchor) not in roots
    assert str(tmp_path.resolve()) in roots

def test_preview_capability_uses_full_hmac_and_expires(monkeypatch, tmp_path):
    from channel.web import web_channel

    directory = tmp_path.resolve()
    monkeypatch.setattr(web_channel, "_PREVIEW_SECRET", b"p" * 32)
    monkeypatch.setattr(web_channel, "conf", lambda: {"web_preview_token_ttl_seconds": 60})
    monkeypatch.setattr(web_channel.time, "time", lambda: 1_000.0)
    owner_id = "web:" + "1" * 32
    token = web_channel._encode_dir_token(str(directory), owner_id)
    parts = token.split(".")
    assert parts[0] == "p3"
    assert len(parts) == 3
    assert len(parts[-1]) == 64
    assert web_channel._decode_dir_token(token) == str(directory)
    assert web_channel._decode_dir_capability(token) == (str(directory), owner_id)

    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    with pytest.raises(ValueError):
        web_channel._decode_dir_token(tampered)

    monkeypatch.setattr(web_channel.time, "time", lambda: 1_061.0)
    with pytest.raises(ValueError, match="Expired"):
        web_channel._decode_dir_token(token)


def test_login_failures_are_rate_limited_and_success_clears_window(monkeypatch):
    from channel.web import web_channel

    monkeypatch.setattr(
        web_channel,
        "conf",
        lambda: {"web_password": "correct", "web_session_expire_days": 30},
    )
    monkeypatch.setattr(web_channel, "_request_environment", lambda: {"REMOTE_ADDR": "127.0.0.1"})
    monkeypatch.setattr(web_channel.time, "time", lambda: 1000.0)
    monkeypatch.setattr(web_channel.web, "header", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(web_channel.web, "data", lambda: b'{"password":"wrong"}')
    with web_channel._login_attempts_lock:
        web_channel._login_attempts.clear()

    for _ in range(web_channel._LOGIN_MAX_FAILURES):
        assert json.loads(web_channel.AuthLoginHandler().POST())["status"] == "error"
    with patch.object(web_channel.web, "ctx", SimpleNamespace(status=""), create=True):
        limited = json.loads(web_channel.AuthLoginHandler().POST())
    assert limited["status"] == "error"

    monkeypatch.setattr(web_channel.time, "time", lambda: 1400.0)
    monkeypatch.setattr(web_channel.web, "data", lambda: b'{"password":"correct"}')
    monkeypatch.setattr(web_channel, "_create_auth_token", lambda _subject: "token")
    monkeypatch.setattr(web_channel, "_create_auth_subject_token", lambda _subject: "subject")
    monkeypatch.setattr(
        web_channel.web,
        "setcookie",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    success = json.loads(web_channel.AuthLoginHandler().POST())
    assert success["status"] == "success"
    with web_channel._login_attempts_lock:
        assert "127.0.0.1" not in web_channel._login_attempts


def test_agent_initialization_checks_durable_owner_before_cache_creation(tmp_path):
    from bridge.agent_bridge import AgentBridge

    store = ConversationStore(tmp_path / "index.db")
    owner = "web:" + "a" * 32
    store.claim_session("durable-owner", owner)
    assert store.active_session_owner("durable-owner") == owner

    with patch("agent.memory.get_conversation_store", return_value=store):
        AgentBridge._assert_persisted_session_binding("durable-owner", owner)
        with pytest.raises(PermissionError):
            AgentBridge._assert_persisted_session_binding("durable-owner", None)
        with pytest.raises(PermissionError):
            AgentBridge._assert_persisted_session_binding(
                "durable-owner", "web:" + "b" * 32
            )


def test_foreign_session_delete_cannot_cancel_victim_run(tmp_path):
    from agent.protocol.cancel import CancelTokenRegistry
    from channel.web import web_channel

    store = ConversationStore(tmp_path / "index.db")
    owner = "web:" + "a" * 32
    attacker = "web:" + "b" * 32
    session_id = "victim-delete-session"
    store.claim_session(session_id, owner)
    registry = CancelTokenRegistry()
    event = registry.register(
        "victim-request", session_id=session_id, owner_id=owner
    )

    with patch.object(web_channel, "_require_auth", return_value=attacker), patch(
        "agent.memory.get_conversation_store", return_value=store
    ), patch("agent.protocol.get_cancel_registry", return_value=registry), patch.object(
        web_channel.web, "header"
    ):
        response = json.loads(web_channel.SessionDetailHandler().DELETE(session_id))

    assert response == {"status": "error", "message": "session not found"}
    assert event.is_set() is False
    assert store.owns_session(session_id, owner)


def test_cancel_registry_owner_binding_rejects_cross_owner_cancel():
    from agent.protocol.cancel import CancelTokenRegistry

    registry = CancelTokenRegistry()
    owner = "web:" + "c" * 32
    attacker = "web:" + "d" * 32
    event = registry.register("owned-request", "owned-session", owner_id=owner)

    assert registry.cancel_request_owned("owned-request", attacker) is False
    assert registry.cancel_session_owned("owned-session", attacker) == 0
    assert event.is_set() is False
    assert registry.cancel_request_owned("owned-request", owner) is True
    assert event.is_set() is True

    with pytest.raises(PermissionError):
        registry.register("owned-request", "owned-session", owner_id=attacker)


def test_scheduler_output_cross_owner_persistence_failure_cannot_mutate_agent_cache(
    tmp_path
):
    from bridge.agent_bridge import AgentBridge

    store = ConversationStore(tmp_path / "index.db")
    owner = "web:" + "7" * 32
    attacker = "web:" + "8" * 32
    session_id = "scheduler-victim"
    store.claim_session(session_id, owner)
    store.append_messages(
        session_id,
        [{"role": "user", "content": "victim"}],
        channel_type="web",
        owner_id=owner,
    )

    agent = SimpleNamespace(
        messages=[{"role": "user", "content": "victim-cache"}],
        messages_lock=threading.RLock(),
        conversation_owner_id=owner,
    )
    bridge = object.__new__(AgentBridge)
    bridge.agents = {session_id: agent}
    before = list(agent.messages)

    with patch("agent.memory.get_conversation_store", return_value=store), patch(
        "config.conf",
        return_value={
            "conversation_persistence": True,
            "enable_thinking": False,
            "scheduler_inject_to_session": True,
            "scheduler_inject_max_per_session": 3,
        },
    ):
        with pytest.raises(RuntimeError, match="not durably persisted"):
            bridge.remember_scheduled_output(
                session_id,
                "ATTACKER-CONTROLLED-CONTEXT",
                channel_type="web",
                task_description="inject",
                owner_id=attacker,
            )

    assert agent.messages == before
    assert store.load_messages(session_id, owner_id=owner) == [
        {"role": "user", "content": "victim"}
    ]


def test_plaintext_public_bind_and_dns_rebinding_are_rejected(monkeypatch):
    from channel.web import web_channel

    config = {"web_password": "strong password", "web_session_expire_days": 30}
    monkeypatch.setattr(web_channel, "conf", lambda: config)
    assert web_channel._resolve_web_bind_host("0.0.0.0") == ("127.0.0.1", False)
    assert web_channel._resolve_web_bind_host("192.0.2.10") == ("127.0.0.1", False)

    monkeypatch.setattr(
        web_channel.web,
        "ctx",
        SimpleNamespace(env={"HTTP_HOST": "attacker.example:9876"}),
        raising=False,
    )
    with pytest.raises(Exception):
        web_channel._require_safe_request_host()

    monkeypatch.setattr(
        web_channel.web,
        "ctx",
        SimpleNamespace(
            env={
                "HTTP_HOST": "127.0.0.1:9876",
                "HTTP_ORIGIN": "https://attacker.example",
            }
        ),
        raising=False,
    )
    with pytest.raises(Exception):
        web_channel._require_safe_request_host()


def test_title_generation_rejects_foreign_session_before_model_side_effect(
    tmp_path
):
    from channel.web import web_channel

    store = ConversationStore(tmp_path / "index.db")
    owner = "web:" + "5" * 32
    attacker = "web:" + "6" * 32
    session_id = "title-victim"
    store.claim_session(session_id, owner)
    payload = json.dumps({
        "user_message": "private prompt",
        "assistant_reply": "private answer",
    }).encode()
    with patch.object(web_channel, "_require_auth", return_value=attacker), patch(
        "agent.memory.get_conversation_store", return_value=store
    ), patch.object(web_channel.web, "header"), patch.object(
        web_channel.web, "data", return_value=payload
    ), patch.object(web_channel, "_generate_session_title") as generate:
        response = json.loads(
            web_channel.SessionTitleHandler().POST(session_id)
        )
    assert response == {"status": "error", "message": "session not found"}
    generate.assert_not_called()


def test_message_delete_evicts_stale_agent_cache_before_success():
    from channel.web import web_channel

    owner = "web:" + "a" * 32
    store = Mock()
    store.delete_message_pair.return_value = 2
    bridge = Mock()
    bridge.has_cached_session.return_value = True
    bridge.sync_session_messages_from_store.return_value = -1
    payload = json.dumps({
        "session_id": "delete-cache-session",
        "user_seq": 1,
        "delete_user": True,
        "cascade": False,
    }).encode()
    bridge_factory = Mock()
    bridge_factory.get_agent_bridge.return_value = bridge

    with patch.object(web_channel, "_require_auth", return_value=owner), patch.object(
        web_channel.web, "header"
    ), patch.object(web_channel.web, "data", return_value=payload), patch(
        "agent.memory.get_conversation_store", return_value=store
    ), patch("bridge.bridge.Bridge", return_value=bridge_factory):
        response = json.loads(web_channel.MessageDeleteHandler().POST())

    assert response == {
        "status": "success",
        "deleted": 2,
        "cache_action": "evicted",
    }
    bridge.clear_session.assert_called_once_with("delete-cache-session")
