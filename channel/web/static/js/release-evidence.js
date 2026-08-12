/*
 * Fail-closed normalization for the read-only release-evidence endpoint.
 *
 * This file deliberately owns no approval action.  It only turns the server
 * response into a display model, and it treats an older, malformed, or
 * unexpected response as blocked rather than guessing that it is a PASS.
 * The UMD-style export keeps the contract executable in the Python/Node
 * regression suite without requiring a browser DOM.
 */
(function attachReleaseEvidenceContract(root) {
    'use strict';

    const EXPECTED_HARD_DENIALS = Object.freeze([
        'FDE_CASE_EVIDENCE',
        'TARGET_CUSTOMER_ACCEPTANCE',
        'CUSTOMER_ATTESTATION',
        'CUSTOMER_TEST_EXECUTION',
        'SKILLS_GOLD_DATASET_VALID',
        'SKILLS_PRODUCTION_GATE_ELIGIBLE',
        'SKILLS_LOCAL_REPORT_CONTRACT',
        'GIT_COMMIT_BOUND_EVIDENCE',
        'REMOTE_CI_REQUIRED_CHECKS',
        'BRANCH_PROTECTION',
        'SIGNED_RELEASE_ARTIFACT',
        'REPRODUCIBLE_BUILD',
        'DOCKER_BUILD',
        'INSTALLER_SMOKE_TEST',
        'MIGRATION_ROLLBACK_TEST',
        '72H_SOAK',
        'PRODUCTION_ALERT_FIRE_TEST',
        'SESSION_CITATION_UI_CLOSED_LOOP',
        'KNOWLEDGE_LOCAL_RECOMPUTATION_VERIFIED',
        'KNOWLEDGE_INDEPENDENT_VERIFICATION',
        'EXTERNAL_VERIFIER_ATTESTATION',
        'SESSION_CITATION_PRODUCTION_VERIFIED',
    ]);

    const EXPECTED_REQUIRED_CONDITIONS = Object.freeze([
        'all_formal_reports_present',
        'retrieval_formal_gate',
        'knowledge_formal_gate',
        'memory_formal_gate',
        'skills_local_report_contract',
        'skills_pinned_dataset',
        'skills_formal_gate',
        'fde_case_evidence',
        'customer_acceptance',
        'git_commit_bound_evidence',
        'clean_release_tree',
        'reproducible_build',
        'docker_build',
        'installer_smoke_test',
        'migration_rollback_test',
        'soak_and_alert_test',
        'session_citation_closed_loop',
        'external_verifier_attestation',
    ]);

    const EXPECTED_REPORTS = Object.freeze([
        'retrieval_comparison',
        'retrieval_verification',
        'knowledge_comparison',
        'knowledge_verification',
        'memory_outbox',
        'skills_selection',
        'customer_acceptance',
        'web_boundary_security',
        'web_boundary_verification',
    ]);

    const DENIAL_STATES = new Set(['YES', 'NO', 'ABSENT', 'NOT_RUN']);
    const MAX_TEXT_LENGTH = 512;
    const MAX_VERIFICATION_CHECKS = 512;

    function isRecord(value) {
        return value !== null && typeof value === 'object' && !Array.isArray(value)
            && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
    }

    function hasExactKeys(value, expectedKeys) {
        if (!isRecord(value)) return false;
        const actual = Object.keys(value).sort();
        const expected = Array.from(expectedKeys).sort();
        return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
    }

    function safeText(value, fallback) {
        return typeof value === 'string' && value.length <= MAX_TEXT_LENGTH ? value : fallback;
    }

    function invalidEvidence(message) {
        return {
            status: 'invalid_evidence',
            passed: false,
            message: safeText(message, 'release evidence could not be verified'),
            hardDenials: { FDE_CASE_EVIDENCE: 'ABSENT' },
            requiredConditions: {},
            reports: {},
            verification: { passed: false, integrityPassed: false },
        };
    }

    function normalizeHardDenials(value) {
        if (!hasExactKeys(value, EXPECTED_HARD_DENIALS)) return null;
        const result = {};
        for (const key of EXPECTED_HARD_DENIALS) {
            if (!DENIAL_STATES.has(value[key])) return null;
            result[key] = value[key];
        }
        return result;
    }

    function normalizeRequiredConditions(value) {
        if (!hasExactKeys(value, EXPECTED_REQUIRED_CONDITIONS)) return null;
        const result = {};
        for (const key of EXPECTED_REQUIRED_CONDITIONS) {
            if (typeof value[key] !== 'boolean') return null;
            result[key] = value[key];
        }
        return result;
    }

    function normalizeReports(value) {
        if (!hasExactKeys(value, EXPECTED_REPORTS)) return null;
        const result = {};
        for (const key of EXPECTED_REPORTS) {
            const item = value[key];
            if (!isRecord(item) || typeof item.status !== 'string'
                    || item.status.length > MAX_TEXT_LENGTH || typeof item.passed !== 'boolean') {
                return null;
            }
            if (Object.prototype.hasOwnProperty.call(item, 'fresh') && typeof item.fresh !== 'boolean') {
                return null;
            }
            result[key] = {
                status: item.status,
                passed: item.passed,
                fresh: item.fresh,
            };
        }
        return result;
    }

    function normalizeVerification(value) {
        if (!isRecord(value) || typeof value.passed !== 'boolean'
                || typeof value.integrity_passed !== 'boolean' || !Array.isArray(value.checks)
                || value.checks.length > MAX_VERIFICATION_CHECKS) {
            return null;
        }
        return {
            passed: value.passed,
            integrityPassed: value.integrity_passed,
        };
    }

    function normalizeEvidence(raw) {
        if (!isRecord(raw)) return invalidEvidence('release evidence response is not an object');

        const status = safeText(raw.status, '');
        if (status === 'not_available') {
            return {
                status,
                passed: false,
                message: safeText(raw.message, 'release evidence manifest is absent'),
                hardDenials: { FDE_CASE_EVIDENCE: 'ABSENT' },
                requiredConditions: {},
                reports: {},
                verification: { passed: false, integrityPassed: false },
            };
        }
        if (status !== 'completed') {
            return invalidEvidence(safeText(raw.message, 'release evidence could not be verified'));
        }

        const hardDenials = normalizeHardDenials(raw.hard_denials);
        const requiredConditions = normalizeRequiredConditions(raw.required_conditions);
        const reports = normalizeReports(raw.reports);
        const verification = normalizeVerification(raw.verification);
        if (!hardDenials || !requiredConditions || !reports || !verification
                || typeof raw.passed !== 'boolean') {
            return invalidEvidence('release evidence response is incomplete or has an unknown schema');
        }

        const allConditionsPassed = Object.values(requiredConditions).every((value) => value === true);
        const allDenialsLifted = Object.values(hardDenials).every((value) => value === 'YES');
        // The producer and verifier must agree.  A disagreement is evidence of
        // an invalid response, not a reason to choose the more optimistic value.
        if (raw.passed !== allConditionsPassed
                || verification.passed !== raw.passed
                || !verification.integrityPassed
                || (raw.passed && !allDenialsLifted)) {
            return invalidEvidence('release evidence response has contradictory gate results');
        }

        return {
            status: 'completed',
            passed: raw.passed === true && verification.passed === true
                && verification.integrityPassed === true && allConditionsPassed && allDenialsLifted,
            message: safeText(raw.message, ''),
            hardDenials,
            requiredConditions,
            reports,
            verification,
        };
    }

    const api = Object.freeze({
        EXPECTED_HARD_DENIALS,
        EXPECTED_REQUIRED_CONDITIONS,
        EXPECTED_REPORTS,
        normalizeEvidence,
    });
    root.CowDeliveryEvidence = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
}(typeof window !== 'undefined' ? window : globalThis));
