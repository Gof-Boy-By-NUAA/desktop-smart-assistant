"""Executable regression tests for the read-only delivery evidence journey.

The assertions intentionally cover both API failure semantics and the Web
normalizer.  A source-string check alone would not prove that a malformed
response remains blocked, so the browser helper is run under Node as well.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import mock_open, patch

import pytest

from benchmarks.evidence.release_manifest import REPORTS
from channel.web import web_channel


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "channel/web/static/js/release-evidence.js"
CONSOLE = ROOT / "channel/web/static/js/console.js"
CHAT = ROOT / "channel/web/chat.html"

EXPECTED_HARD_DENIALS = [
    "FDE_CASE_EVIDENCE",
    "TARGET_CUSTOMER_ACCEPTANCE",
    "CUSTOMER_ATTESTATION",
    "CUSTOMER_TEST_EXECUTION",
    "SKILLS_GOLD_DATASET_VALID",
    "SKILLS_PRODUCTION_GATE_ELIGIBLE",
    "SKILLS_LOCAL_REPORT_CONTRACT",
    "GIT_COMMIT_BOUND_EVIDENCE",
    "REMOTE_CI_REQUIRED_CHECKS",
    "BRANCH_PROTECTION",
    "SIGNED_RELEASE_ARTIFACT",
    "REPRODUCIBLE_BUILD",
    "DOCKER_BUILD",
    "INSTALLER_SMOKE_TEST",
    "MIGRATION_ROLLBACK_TEST",
    "72H_SOAK",
    "PRODUCTION_ALERT_FIRE_TEST",
    "SESSION_CITATION_UI_CLOSED_LOOP",
    "KNOWLEDGE_LOCAL_RECOMPUTATION_VERIFIED",
    "KNOWLEDGE_INDEPENDENT_VERIFICATION",
    "EXTERNAL_VERIFIER_ATTESTATION",
    "SESSION_CITATION_PRODUCTION_VERIFIED",
]

EXPECTED_REQUIRED_CONDITIONS = [
    "all_formal_reports_present",
    "retrieval_formal_gate",
    "knowledge_formal_gate",
    "memory_formal_gate",
    "skills_local_report_contract",
    "skills_pinned_dataset",
    "skills_formal_gate",
    "fde_case_evidence",
    "customer_acceptance",
    "git_commit_bound_evidence",
    "clean_release_tree",
    "reproducible_build",
    "docker_build",
    "installer_smoke_test",
    "migration_rollback_test",
    "soak_and_alert_test",
    "session_citation_closed_loop",
    "external_verifier_attestation",
]


def _handler_response(
    manifest: dict,
    verification: dict,
    *,
    context: SimpleNamespace | None = None,
) -> tuple[dict, SimpleNamespace]:
    context = context or SimpleNamespace(status=None)
    with patch.object(web_channel, "_require_auth", return_value="web:test-owner"), \
         patch.object(web_channel.web, "header") as header, \
         patch.object(web_channel.web, "ctx", context), \
         patch.object(web_channel.os.path, "isfile", return_value=True), \
         patch("builtins.open", mock_open(read_data=json.dumps(manifest))), \
         patch("benchmarks.evidence.release_manifest.verify_manifest", return_value=verification):
        response = json.loads(web_channel.ReleaseEvidenceHandler().GET())
    header.assert_any_call("Cache-Control", "no-store")
    return response, context


def test_release_evidence_invalid_integrity_is_http_422_and_cannot_pass():
    response, context = _handler_response(
        {},
        {"passed": False, "integrity_passed": False, "checks": [{"name": "tampered", "passed": False}]},
    )

    assert context.status == "422 Unprocessable Entity"
    assert response["status"] == "invalid_evidence"
    assert response["passed"] is False
    assert response["hard_denials"] == {"FDE_CASE_EVIDENCE": "ABSENT"}
    assert response["verification"]["integrity_passed"] is False


def test_release_evidence_runtime_failure_is_http_500_and_cannot_pass():
    context = SimpleNamespace(status=None)
    with patch.object(web_channel, "_require_auth", return_value="web:test-owner"), \
         patch.object(web_channel.web, "header"), \
         patch.object(web_channel.web, "ctx", context), \
         patch.object(web_channel.os.path, "isfile", return_value=True), \
         patch("builtins.open", side_effect=OSError("read denied")):
        response = json.loads(web_channel.ReleaseEvidenceHandler().GET())

    assert context.status == "500 Internal Server Error"
    assert response == {
        "status": "invalid_evidence",
        "passed": False,
        "hard_denials": {"FDE_CASE_EVIDENCE": "ABSENT"},
        "message": "release evidence could not be verified",
    }


def test_release_evidence_completed_response_exposes_integrity_not_a_synthetic_pass():
    manifest = {
        "passed": False,
        "hard_denials": {"FDE_CASE_EVIDENCE": "ABSENT"},
        "required_conditions": {"customer_acceptance": False},
        "reports": {"customer_acceptance": {"status": "NOT_RUN", "passed": False}},
    }
    response, context = _handler_response(
        manifest,
        {"passed": False, "integrity_passed": True, "checks": []},
    )

    assert context.status is None
    assert response["status"] == "completed"
    assert response["passed"] is False
    assert response["verification"] == {
        "passed": False,
        "integrity_passed": True,
        "checks": [],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for Web delivery contract tests")
def test_web_delivery_normalizer_rejects_missing_unknown_and_contradictory_pass_claims():
    script = f"""
const assert = require('assert');
const helper = require({json.dumps(str(HELPER))});
const hard = Object.fromEntries(helper.EXPECTED_HARD_DENIALS.map((key) => [key, 'YES']));
const required = Object.fromEntries(helper.EXPECTED_REQUIRED_CONDITIONS.map((key) => [key, true]));
const reports = Object.fromEntries(helper.EXPECTED_REPORTS.map((key) => [key, {{ status: 'completed', passed: true, fresh: true }}]));
const valid = {{
  status: 'completed', passed: true, hard_denials: hard,
  required_conditions: required, reports,
  verification: {{ passed: true, integrity_passed: true, checks: [] }},
}};
assert.strictEqual(helper.normalizeEvidence(valid).passed, true);

const missingDenial = structuredClone(valid);
delete missingDenial.hard_denials.FDE_CASE_EVIDENCE;
assert.strictEqual(helper.normalizeEvidence(missingDenial).passed, false);
assert.strictEqual(helper.normalizeEvidence(missingDenial).status, 'invalid_evidence');

const unknownGate = structuredClone(valid);
unknownGate.required_conditions.FORGED_GATE = true;
assert.strictEqual(helper.normalizeEvidence(unknownGate).passed, false);
assert.strictEqual(helper.normalizeEvidence(unknownGate).status, 'invalid_evidence');

const contradictory = structuredClone(valid);
contradictory.required_conditions.customer_acceptance = false;
assert.strictEqual(helper.normalizeEvidence(contradictory).passed, false);
assert.strictEqual(helper.normalizeEvidence(contradictory).status, 'invalid_evidence');

const invalid = helper.normalizeEvidence({{ status: 'invalid_evidence', passed: false, message: 'tampered' }});
assert.strictEqual(invalid.passed, false);
assert.deepStrictEqual(invalid.hardDenials, {{ FDE_CASE_EVIDENCE: 'ABSENT' }});
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_web_and_desktop_delivery_views_are_reachable_read_only_and_xss_safe():
    chat = CHAT.read_text(encoding="utf-8")
    console = CONSOLE.read_text(encoding="utf-8")
    desktop_page = (ROOT / "desktop/src/renderer/src/pages/DeliveryPage.tsx").read_text(encoding="utf-8")
    desktop_client = (ROOT / "desktop/src/renderer/src/api/client.ts").read_text(encoding="utf-8")

    assert 'data-view="delivery"' in chat
    assert 'id="view-delivery"' in chat
    assert chat.index('assets/js/release-evidence.js') < chat.index('assets/js/console.js')
    assert "delivery: { group: 'nav_monitor', page: 'menu_delivery' }" in console
    assert "fetch('/api/release/evidence'" in console
    assert "cache: 'no-store'" in console
    assert "response.status !== 422 && response.status !== 500" in console
    assert "else if (viewId === 'delivery') void loadDeliveryEvidence();" in console
    assert "this.request('/api/release/evidence', { cache: 'no-store' })" not in desktop_client
    assert "acceptedErrorStatuses.includes(res.status)" in desktop_client
    assert "[422, 500]" in desktop_client
    assert "HARD_DENIAL_FIELDS" in desktop_page
    assert "REQUIRED_CONDITION_FIELDS" in desktop_page
    assert "verification.integrityPassed" in desktop_page

    file_branch = console[
        console.index("} else if (item.type === 'file') {"):
        console.index("} else if (item.type === 'artifact') {")
    ]
    assert "fileEl.innerHTML" not in file_branch
    assert "document.createTextNode(` ${fileName}`)" in file_branch
    assert "fileEl.rel = 'noopener noreferrer'" in file_branch
    assert "const fileUrl = _toWebUrl(item.content);" in file_branch
    assert "function _safeFileDisplayName" in console
    assert "function _toWebUrl" in console
    assert "javascript:" not in file_branch.lower()

    # Keep the browser schema in lockstep with the authoritative Python
    # release manifest report map. A mismatch must block in the UI, not pass.
    helper_source = HELPER.read_text(encoding="utf-8")
    for report in REPORTS:
        assert f"'{report}'" in helper_source


def test_citation_ui_never_claims_verified_before_resolution():
    web_console = CONSOLE.read_text(encoding="utf-8")
    desktop_markdown = (
        ROOT / "desktop/src/renderer/src/components/Markdown.tsx"
    ).read_text(encoding="utf-8")
    desktop_bubble = (
        ROOT / "desktop/src/renderer/src/components/MessageBubble.tsx"
    ).read_text(encoding="utf-8")

    assert "label.content = '📎 Source'" in web_console
    assert "label.content = '📎 Source'" in desktop_markdown
    assert "label.content = '📎 Verified source'" not in web_console
    assert "label.content = '📎 Verified source'" not in desktop_markdown
    assert "citationView.state === 'resolved'" in desktop_bubble
    assert "Verification failed / 核验失败" in desktop_bubble
    assert "title.textContent = 'Source / 来源'" in web_console
    assert "title.textContent = 'Verified source / 已核验来源'" in web_console
    assert "title.textContent = 'Verification failed / 核验失败'" in web_console


def test_desktop_logs_do_not_claim_live_after_stream_failure():
    logs_page = (
        ROOT / "desktop/src/renderer/src/pages/LogsPage.tsx"
    ).read_text(encoding="utf-8")
    i18n = (ROOT / "desktop/src/renderer/src/i18n.ts").read_text(
        encoding="utf-8"
    )

    assert "type LogConnectionState = 'connecting' | 'live' | 'disconnected'" in logs_page
    assert "es.onerror" in logs_page
    assert "setConnectionState('disconnected')" in logs_page
    assert "connectionState === 'live'" in logs_page
    assert "setConnectionAttempt((value) => value + 1)" in logs_page
    assert "logs_disconnected" in i18n
    assert "logs_retry" in i18n
