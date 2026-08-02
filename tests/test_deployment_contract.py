"""Static release-boundary checks that fail closed before packaging."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_context_excludes_local_secrets_and_runtime_state():
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required = {
        ".git",
        ".env",
        ".env.*",
        ".preview_secret",
        "knowledge/.system/citation-capability.key",
        "config.json",
        "config.yaml",
        "client_config.json",
        "plugins.json",
        "user_datas.pkl",
        "**/.dev.vars",
        "logs",
        "tmp",
        "workspace",
        "local",
        "desktop/node_modules",
        "benchmarks/.cache",
        "benchmarks/results",
    }
    assert required <= patterns


def test_production_dockerfile_uses_copy_and_supported_python_contract():
    dockerfile = (ROOT / "docker/Dockerfile.latest").read_text(encoding="utf-8")
    assert "ARG PYTHON_IMAGE=python:3.11-slim-bookworm" in dockerfile
    assert "FROM ${PYTHON_IMAGE}" in dockerfile
    assert "ADD " not in dockerfile
    assert "COPY . ${BUILD_PREFIX}" in dockerfile


def test_retrieval_ci_runs_independent_verifier_and_uploads_both_reports():
    workflow = (ROOT / ".github/workflows/test-retrieval.yml").read_text(
        encoding="utf-8"
    )
    assert "python -m benchmarks.retrieval.verify" in workflow
    assert "cmrc2018-comparison.json" in workflow
    assert "cmrc2018-comparison-verification.json" in workflow


def test_knowledge_ci_runs_independent_verifier_and_uploads_both_reports():
    workflow = (ROOT / ".github/workflows/test-governed-knowledge.yml").read_text(
        encoding="utf-8"
    )
    assert "python -m benchmarks.knowledge.compare" in workflow
    assert "python -m benchmarks.knowledge.verify" in workflow
    assert "cmrc2018-knowledge-comparison.json" in workflow
    assert "cmrc2018-knowledge-comparison-verification.json" in workflow


def test_web_boundary_ci_runs_independent_replay_and_uploads_both_reports():
    workflow = (ROOT / ".github/workflows/test-web-boundary.yml").read_text(
        encoding="utf-8"
    )
    assert "python -m benchmarks.security.web_boundary" in workflow
    assert "python -m benchmarks.security.verify" in workflow
    assert "web-boundary-security.json" in workflow
    assert "web-boundary-security-verification.json" in workflow


def test_release_workflow_cannot_build_without_verified_release_manifest():
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "release-preflight:" in workflow
    assert "benchmarks.evidence.release_manifest" in workflow
    assert "--verify" in workflow
    assert '"$RUNNER_TEMP/release-evidence-manifest.json"' in workflow
    assert "manifest_generate.outcome" in workflow
    assert "manifest_verify.outcome" in workflow
    assert "release-evidence-${{ github.sha }}" in workflow
    assert "needs: release-preflight" in workflow


def test_desktop_multipart_requests_share_bearer_authenticated_transport():
    client = (
        ROOT / "desktop/src/renderer/src/api/client.ts"
    ).read_text(encoding="utf-8")
    assert "private async authenticatedFetch" in client
    assert "headers.set('Authorization', `Bearer ${this.authToken}`)" in client
    for endpoint in ("/upload", "/api/knowledge/import", "/api/voice/asr"):
        assert f"this.authenticatedFetch('{endpoint}'" in client
        assert f"fetch(`${{this.baseUrl}}{endpoint}`" not in client


def test_desktop_sse_uses_request_bound_ticket_not_bearer_query_string():
    client = (ROOT / "desktop/src/renderer/src/api/client.ts").read_text(encoding="utf-8")
    desktop_store = (ROOT / "desktop/src/renderer/src/store/chatStore.ts").read_text(encoding="utf-8")
    server = (ROOT / "channel/web/web_channel.py").read_text(encoding="utf-8-sig")
    assert "'/stream/ticket'" in client
    assert "&ticket=${encodeURIComponent(ticket.ticket)}" in client
    assert "after_event_id: afterEventId" in client
    assert "token=${encodeURIComponent(this.authToken)}" not in client[client.index("async createSSEStream"):client.index("async deleteMessage")]
    assert 'getattr(params, "token", "")' in server
    assert "after_event_id" in server[server.index("class StreamTicketHandler"):server.index("class ChatHandler")]
    assert "SSE_RECONNECT_ATTEMPTS = 3" in desktop_store
    assert "createSSEStream(requestId, lastEventId)" in desktop_store
    assert "void reconnect(next)" in desktop_store
    assert "isStreamInterrupted: true" in desktop_store


def test_file_and_log_stream_urls_never_carry_login_bearers():
    client = (ROOT / "desktop/src/renderer/src/api/client.ts").read_text(encoding="utf-8")
    desktop_store = (ROOT / "desktop/src/renderer/src/store/chatStore.ts").read_text(encoding="utf-8")
    web_console = (ROOT / "channel/web/static/js/console.js").read_text(encoding="utf-8")
    server = (ROOT / "channel/web/web_channel.py").read_text(encoding="utf-8-sig")

    assert "withToken(" not in client
    assert "getServeFileUrl" not in client
    assert "/api/file?path=" not in web_console
    assert "payload.url || payload.path" in web_console
    assert "getServeFileUrl" not in desktop_store
    assert "url.startsWith('/file/')" in desktop_store
    assert "attachment_urls" in desktop_store
    assert "_get_query_token" not in server
    assert "_decorate_history_file_capabilities" in server
    assert "'/api/logs/ticket'" in server
    assert "_consume_log_stream_ticket(ticket)" in server
    assert "'/api/logs/ticket'" in client
    assert "/api/logs?ticket=${encodeURIComponent(ticket.ticket)}" in client


def test_cancel_ui_requires_backend_or_sse_confirmation_before_cancelled_label():
    desktop_store = (
        ROOT / "desktop/src/renderer/src/store/chatStore.ts"
    ).read_text(encoding="utf-8")
    web_console = (
        ROOT / "channel/web/static/js/console.js"
    ).read_text(encoding="utf-8")
    assert "isCancelPending: true" in desktop_store
    assert "result.cancelled < 1" in desktop_store
    assert "Deliberately retain requestId" in desktop_store
    request_block = web_console[
        web_console.index("function requestCancel()"):
        web_console.index("// Button click is the only path", web_console.index("function requestCancel()"))
    ]
    assert "??????" in request_block
    assert "???????" in request_block
    assert "??????" in request_block
    assert "???' : 'Cancelled" not in request_block


def test_auth_check_failure_is_fail_closed_in_desktop_and_web():
    app = (ROOT / "desktop/src/renderer/src/App.tsx").read_text(encoding="utf-8")
    web_console = (
        ROOT / "channel/web/static/js/console.js"
    ).read_text(encoding="utf-8")
    assert "setAuthState('unavailable')" in app
    assert "authState === 'unavailable'" in app
    auth_tail = web_console[web_console.rindex("fetch('/auth/check')"):]
    assert "Authentication uncertainty must never be treated as authorization" in auth_tail
    catch_block = auth_tail[auth_tail.index("}).catch(() => {"):auth_tail.index("requestAnimationFrame")]
    assert "initApp();" not in catch_block
    assert "showLoginScreen();" in catch_block


def test_desktop_logout_revokes_backend_credential_and_clears_renderer_state():
    app = (ROOT / "desktop/src/renderer/src/App.tsx").read_text(encoding="utf-8")
    client = (ROOT / "desktop/src/renderer/src/api/client.ts").read_text(encoding="utf-8")
    sessions = (ROOT / "desktop/src/renderer/src/store/sessionStore.ts").read_text(encoding="utf-8")
    chat = (ROOT / "desktop/src/renderer/src/store/chatStore.ts").read_text(encoding="utf-8")
    nav = (ROOT / "desktop/src/renderer/src/layout/NavRail.tsx").read_text(encoding="utf-8")
    assert "await apiClient.authLogout()" in app
    assert "useChatStore.getState().reset()" in app
    assert "useSessionStore.getState().reset()" in app
    assert "this.request<ApiResult>('/auth/logout'" in client
    assert "this.setAuthToken(null)" in client
    assert "localStorage.removeItem(ACTIVE_KEY)" in sessions
    assert "set({ sessions: {} })" in chat
    assert "menu_logout" in nav


def test_authenticated_web_agent_never_runs_after_persistence_precondition_failure():
    source = (ROOT / "bridge/agent_bridge.py").read_text(encoding="utf-8")
    precondition = source.index(
        '"authenticated Web user message was not durably persisted"'
    )
    run_stream = source.index("response = agent.run_stream(")
    assert precondition < run_stream
    assert '"authenticated Web response was not durably persisted"' in source
    assert "self.clear_session(session_id)" in source[run_stream:]


def test_delivery_evidence_surface_is_read_only_and_fail_closed():
    source = (ROOT / "channel/web/web_channel.py").read_text(encoding="utf-8-sig")
    start = source.index("class ReleaseEvidenceHandler:")
    end = source.index("class McpOAuthCallbackHandler:", start)
    handler = source[start:end]
    assert "_require_auth()" in handler
    assert '"passed": bool(manifest.get(' in handler
    assert '"FDE_CASE_EVIDENCE": "ABSENT"' in handler
    assert "verify_manifest" in handler
    assert "'/api/release/evidence', 'ReleaseEvidenceHandler'" in source
