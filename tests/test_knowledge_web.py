import json
from pathlib import Path
from unittest.mock import patch


def test_knowledge_action_handler_delegates_to_dispatch(tmp_path):
    from channel.web.web_channel import KnowledgeActionHandler

    request = {"action": "create_category", "payload": {"path": "research"}}
    dispatched = {"action": "create_category", "code": 200, "message": "success",
                  "payload": {"path": "research", "created": True}}

    with patch("channel.web.web_channel._require_auth", return_value="web:test-owner"), \
         patch("channel.web.web_channel.web.header"), \
         patch("channel.web.web_channel.web.data", return_value=json.dumps(request).encode()), \
         patch("channel.web.web_channel._get_workspace_root", return_value=str(tmp_path)), \
         patch("agent.knowledge.service.KnowledgeService.dispatch", return_value=dispatched) as dispatch:
        response = json.loads(KnowledgeActionHandler().POST())

    dispatch.assert_called_once_with("create_category", {"path": "research"})
    assert response["status"] == "success"
    assert response["payload"]["created"] is True


def test_knowledge_action_handler_preserves_dispatch_error(tmp_path):
    from channel.web.web_channel import KnowledgeActionHandler

    dispatched = {"action": "delete_documents", "code": 403,
                  "message": "protected knowledge file: index.md", "payload": None}
    request = {"action": "delete_documents", "payload": {"paths": ["index.md"]}}

    with patch("channel.web.web_channel._require_auth", return_value="web:test-owner"), \
         patch("channel.web.web_channel.web.header"), \
         patch("channel.web.web_channel.web.data", return_value=json.dumps(request).encode()), \
         patch("channel.web.web_channel._get_workspace_root", return_value=str(tmp_path)), \
         patch("agent.knowledge.service.KnowledgeService.dispatch", return_value=dispatched):
        response = json.loads(KnowledgeActionHandler().POST())

    assert response["status"] == "error"
    assert response["code"] == 403
    assert response["message"] == "protected knowledge file: index.md"


def test_knowledge_frontend_management_contract():
    root = Path(__file__).parents[1]
    html = (root / "channel/web/chat.html").read_text(encoding="utf-8")
    js = (root / "channel/web/static/js/console.js").read_text(encoding="utf-8")

    assert 'id="knowledge-dialog-overlay"' in html
    assert 'id="knowledge-dialog-textarea"' in html
    assert 'id="knowledge-document-form"' in html
    assert 'id="knowledge-document-path-preview"' in html
    assert "function openKnowledgeDialog(" in js
    assert "function _knowledgeCategoryPaths(" in js
    assert "dispatchKnowledgeAction('create_category'" in js
    assert "dispatchKnowledgeAction('create_document'" in js
    assert "dispatchKnowledgeAction('rename_category'" in js
    assert "dispatchKnowledgeAction('delete_category'" in js
    assert "dispatchKnowledgeAction('delete_documents'" in js
    assert "dispatchKnowledgeAction('move_documents'" in js
    assert 'id="knowledge-import-input"' in html
    assert "function createKnowledgeDocument(" in js
    assert "function openKnowledgeDocumentEditor(" in js
    assert "documentPathPreview.textContent = options.category" in js
    assert "options.type === 'document'" in js
    assert "input.classList.toggle('hidden', options.type === 'select' || options.type === 'textarea' || options.type === 'document')" in js
    assert "function selectKnowledgeImportFiles(" in js
    assert "function importKnowledgeDocuments(" in js
    assert "function validateKnowledgeImportFiles(" in js
    assert "KNOWLEDGE_IMPORT_MAX_FILE_SIZE" in js
    assert "fetch('/api/knowledge/import'" in js
    assert "initKnowledgeImportDropZone()" in js

    knowledge_section = js[js.index("// Knowledge View"):js.index("function _hasFilterMatch")]
    assert "prompt(" not in knowledge_section
    assert "alert(" not in knowledge_section
    assert "if (path === 'index.md' || path === 'log.md') return '';" in knowledge_section


class UploadedFile:
    def __init__(self, filename, content):
        self.filename = filename
        self.value = content


def test_knowledge_import_handler_delegates_to_dispatch(tmp_path):
    from channel.web.web_channel import KnowledgeImportHandler

    dispatched = {"action": "import_documents", "code": 200, "message": "success",
                  "payload": {"imported": 2, "skipped": 0, "failed": 0}}
    params = {
        "target_category": "notes",
        "conflict_strategy": "rename",
        "files": [UploadedFile("a.md", b"# A"), UploadedFile("b.txt", b"B")],
    }

    with patch("channel.web.web_channel._require_auth", return_value="web:test-owner"), \
         patch("channel.web.web_channel.web.header"), \
         patch("channel.web.web_channel._raw_web_input", return_value=params), \
         patch("channel.web.web_channel._get_workspace_root", return_value=str(tmp_path)), \
         patch("agent.knowledge.service.KnowledgeService.dispatch", return_value=dispatched) as dispatch:
        response = json.loads(KnowledgeImportHandler().POST())

    dispatch.assert_called_once()
    action, payload = dispatch.call_args.args
    assert action == "import_documents"
    assert payload["target_category"] == "notes"
    assert payload["conflict_strategy"] == "rename"
    assert [f["filename"] for f in payload["files"]] == ["a.md", "b.txt"]
    assert response["status"] == "success"
    assert response["payload"]["imported"] == 2


def test_knowledge_import_handler_rejects_large_content_length(tmp_path):
    from channel.web.web_channel import KnowledgeImportHandler
    from agent.knowledge.service import KnowledgeService
    assert KnowledgeService.MAX_IMPORT_TOTAL_SIZE == 200 * 1024 * 1024

    with patch("channel.web.web_channel._require_auth", return_value="web:test-owner"), \
         patch("channel.web.web_channel.web.header"), \
         patch("channel.web.web_channel.web.ctx") as ctx:
        ctx.env = {"CONTENT_LENGTH": str(KnowledgeService.MAX_IMPORT_TOTAL_SIZE + 1)}
        response = json.loads(KnowledgeImportHandler().POST())

    assert response["status"] == "error"
    assert response["code"] == 413
    assert response["message"] == "import batch too large"


def test_citation_resolve_handler_uses_only_uri_and_trusted_service_identity(tmp_path):
    from channel.web.web_channel import KnowledgeCitationResolveHandler

    resolved = {
        "uri": "knowledge://doc/v/1/sections/sec/evidence/ev#bytes=0-5&content_hash=" + "a" * 64
        + "&quote_hash=" + "b" * 64 + "&source_ref_hash=" + "c" * 64 + "&citation_version=3",
        "citation_version": 3,
        "document_id": "doc",
        "document_version": 1,
        "section_id": "sec",
        "evidence_id": "ev",
        "source_ref": "knowledge/test.md",
        "source_ref_hash": "c" * 64,
        "byte_start": 0,
        "byte_end": 5,
        "content_hash": "a" * 64,
        "quote_hash": "b" * 64,
        "quote": "proof",
    }
    request = {"uri": resolved["uri"]}

    with patch("channel.web.web_channel._require_auth", return_value="web:test-owner") as require_auth, \
         patch("channel.web.web_channel.web.header"), \
         patch("channel.web.web_channel.web.data", return_value=json.dumps(request).encode()), \
         patch("channel.web.web_channel._get_workspace_root", return_value=str(tmp_path)), \
         patch("agent.knowledge.service.KnowledgeService.resolve_citation", return_value=resolved) as resolve:
        response = json.loads(KnowledgeCitationResolveHandler().POST())

    require_auth.assert_called_once_with()
    resolve.assert_called_once_with(resolved["uri"])
    assert response == {"status": "success", "code": 200, "citation": resolved}


def test_citation_resolve_handler_rejects_all_client_identity_claims():
    from channel.web.web_channel import KnowledgeCitationResolveHandler

    forbidden_claims = {
        "uri": "knowledge://doc/v/1/sections/sec/evidence/ev#bytes=0-1&content_hash=" + "a" * 64
        + "&quote_hash=" + "b" * 64 + "&source_ref_hash=" + "c" * 64 + "&citation_version=3",
        "tenant_id": "attacker-tenant",
        "actor_user_id": "admin",
        "session_id": "victim-session",
        "roles": ["admin"],
    }
    with patch("channel.web.web_channel._require_auth", return_value="web:test-owner"), \
         patch("channel.web.web_channel.web.header"), \
         patch("channel.web.web_channel.web.data", return_value=json.dumps(forbidden_claims).encode()), \
         patch("agent.knowledge.service.KnowledgeService.resolve_citation") as resolve:
        response = json.loads(KnowledgeCitationResolveHandler().POST())

    resolve.assert_not_called()
    assert response["status"] == "error"
    assert response["code"] == 400
    assert response["error_code"] == "invalid_citation_request"


def test_citation_resolve_handler_rejects_malformed_and_oversized_json():
    from channel.web.web_channel import KnowledgeCitationResolveHandler

    with patch("channel.web.web_channel._require_auth", return_value="web:test-owner"), \
         patch("channel.web.web_channel.web.header"), \
         patch("channel.web.web_channel.web.ctx") as ctx, \
         patch("channel.web.web_channel.web.data", return_value=b"{broken"), \
         patch("agent.knowledge.service.KnowledgeService.resolve_citation") as resolve:
        ctx.env = {"CONTENT_LENGTH": "7"}
        malformed = json.loads(KnowledgeCitationResolveHandler().POST())
    resolve.assert_not_called()
    assert malformed["code"] == 400
    assert malformed["error_code"] == "invalid_citation_request"

    with patch("channel.web.web_channel._require_auth", return_value="web:test-owner"), \
         patch("channel.web.web_channel.web.header"), \
         patch("channel.web.web_channel.web.ctx") as ctx, \
         patch("channel.web.web_channel.web.data") as read_body:
        ctx.env = {"CONTENT_LENGTH": "8193"}
        oversized = json.loads(KnowledgeCitationResolveHandler().POST())
    read_body.assert_not_called()
    assert oversized["code"] == 413
    assert oversized["error_code"] == "citation_request_too_large"


def test_session_citation_resolves_through_web_without_client_session_claim(tmp_path):
    from agent.knowledge import GovernedKnowledgeRuntime, KnowledgeWriteCommand
    from agent.memory.governance import IdentityContext, MemoryScope, Sensitivity
    from channel.web.web_channel import KnowledgeCitationResolveHandler

    owner_id = "web:" + "1" * 32
    identity = IdentityContext(
        tenant_id="tenant-local",
        actor_user_id=owner_id,
        roles=frozenset(),
        trace_id="web-session-citation-test",
        auth_source="web-password",
    )
    from agent.memory.conversation_store import ConversationStore
    conversation_store = ConversationStore(tmp_path / "conversation.db")
    conversation_store.claim_session("session-web-proof", owner_id)
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        runtime.write(
            identity,
            KnowledgeWriteCommand(
                content="# Session proof\nserver-capability-web-4172",
                title="Session proof",
                source_ref="knowledge/session/web-proof.md",
                collection_id="session",
                idempotency_key="web-session-citation",
                projection_path="session/web-proof.md",
                scope=MemoryScope.SESSION,
                session_id="session-web-proof",
                sensitivity=Sensitivity.PRIVATE,
            ),
        )
        citation = runtime.search(
            identity,
            "server-capability-web-4172",
            limit=5,
            session_id="session-web-proof",
        )[0].citation
    finally:
        runtime.close()

    assert "&session_binding=" in citation.uri
    request = {"uri": citation.uri}
    with patch(
        "channel.web.web_channel._require_auth", return_value=owner_id
    ), patch("channel.web.web_channel.web.header"), patch(
        "channel.web.web_channel.web.data",
        return_value=json.dumps(request).encode(),
    ), patch(
        "channel.web.web_channel._get_workspace_root", return_value=str(tmp_path)
    ), patch(
        "agent.memory.get_conversation_store", return_value=conversation_store
    ):
        response = json.loads(KnowledgeCitationResolveHandler().POST())

    assert response["status"] == "success", response
    assert response["citation"]["uri"] == citation.uri
    assert response["citation"]["quote"] == citation.quote

    conversation_store.delete_session("session-web-proof", owner_id)
    with patch(
        "channel.web.web_channel._require_auth", return_value=owner_id
    ), patch("channel.web.web_channel.web.header"), patch(
        "channel.web.web_channel.web.data",
        return_value=json.dumps(request).encode(),
    ), patch(
        "channel.web.web_channel._get_workspace_root", return_value=str(tmp_path)
    ), patch(
        "agent.memory.get_conversation_store", return_value=conversation_store
    ):
        expired = json.loads(KnowledgeCitationResolveHandler().POST())
    assert expired["status"] == "error"
    assert expired["code"] == 410
    assert expired["error_code"] == "citation_expired_or_invalid"


def test_session_citation_web_replay_by_other_principal_is_denied(tmp_path):
    from agent.knowledge import GovernedKnowledgeRuntime, KnowledgeWriteCommand
    from agent.memory.governance import IdentityContext, MemoryScope, Sensitivity
    from channel.web.web_channel import KnowledgeCitationResolveHandler

    owner_id = "web:" + "2" * 32
    identity = IdentityContext(
        tenant_id="tenant-local", actor_user_id=owner_id, roles=frozenset(),
        trace_id="citation-owner", auth_source="web-password",
    )
    runtime = GovernedKnowledgeRuntime(str(tmp_path))
    try:
        runtime.write(
            identity,
            KnowledgeWriteCommand(
                content="# Private\nweb-citation-replay-8821",
                title="Private",
                source_ref="knowledge/session/replay.md",
                collection_id="session",
                idempotency_key="web-session-replay",
                projection_path="session/replay.md",
                scope=MemoryScope.SESSION,
                session_id="session-owner",
                sensitivity=Sensitivity.PRIVATE,
            ),
        )
        uri = runtime.search(
            identity, "web-citation-replay-8821", session_id="session-owner"
        )[0].citation.uri
    finally:
        runtime.close()

    with patch(
        "channel.web.web_channel._require_auth",
        return_value="web:" + "3" * 32,
    ), patch("channel.web.web_channel.web.header"), patch(
        "channel.web.web_channel.web.data",
        return_value=json.dumps({"uri": uri}).encode(),
    ), patch(
        "channel.web.web_channel._get_workspace_root", return_value=str(tmp_path)
    ):
        response = json.loads(KnowledgeCitationResolveHandler().POST())

    assert response["status"] == "error"
    assert response["code"] == 403
    assert response["error_code"] == "citation_forbidden"
