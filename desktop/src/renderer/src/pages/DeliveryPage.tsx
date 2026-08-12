import React, { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, ClipboardCheck, Loader2, RefreshCw, ShieldAlert } from 'lucide-react'
import apiClient from '../api/client'
import { t } from '../i18n'

type Evidence = {
  status?: unknown
  passed?: unknown
  message?: unknown
  hard_denials?: unknown
  required_conditions?: unknown
  reports?: unknown
  verification?: unknown
}

type Report = { status?: string; passed?: boolean; fresh?: boolean }
type Verification = { passed: boolean; integrityPassed: boolean }

// Keep this display contract deliberately stricter than an arbitrary API
// response. A backend/frontend schema mismatch becomes BLOCKED instead of a
// visually convincing local PASS.
const HARD_DENIAL_FIELDS = [
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
] as const

const REQUIRED_CONDITION_FIELDS = [
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
] as const

const REPORT_FIELDS = [
  'retrieval_comparison',
  'retrieval_verification',
  'knowledge_comparison',
  'knowledge_verification',
  'memory_outbox',
  'skills_selection',
  'customer_acceptance',
  'web_boundary_security',
  'web_boundary_verification',
] as const

const DENIAL_STATES = new Set(['YES', 'NO', 'ABSENT', 'NOT_RUN'])

const isRecord = (value: unknown): value is Record<string, unknown> => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
)

const hasExactKeys = (value: unknown, expected: readonly string[]): value is Record<string, unknown> => {
  if (!isRecord(value)) return false
  const actual = Object.keys(value).sort()
  const sortedExpected = [...expected].sort()
  return actual.length === sortedExpected.length && actual.every((key, index) => key === sortedExpected[index])
}

const stringRecord = (value: unknown): Record<string, string> => {
  if (!isRecord(value)) return {}
  return Object.fromEntries(Object.entries(value).filter(([, item]) => typeof item === 'string')) as Record<string, string>
}

const booleanRecord = (value: unknown): Record<string, boolean> => {
  if (!isRecord(value)) return {}
  return Object.fromEntries(Object.entries(value).filter(([, item]) => typeof item === 'boolean')) as Record<string, boolean>
}

const reportRecord = (value: unknown): Record<string, Report> => {
  if (!isRecord(value)) return {}
  const result: Record<string, Report> = {}
  for (const [name, item] of Object.entries(value)) {
    if (!isRecord(item) || typeof item.status !== 'string' || typeof item.passed !== 'boolean') continue
    if (item.fresh !== undefined && typeof item.fresh !== 'boolean') continue
    result[name] = { status: item.status, passed: item.passed, fresh: item.fresh as boolean | undefined }
  }
  return result
}

const verificationRecord = (value: unknown): Verification => {
  if (!isRecord(value)) return { passed: false, integrityPassed: false }
  return {
    passed: value.passed === true,
    integrityPassed: value.integrity_passed === true,
  }
}

const isCompleteEvidenceContract = (
  data: Evidence | null,
  hardDenials: Record<string, string>,
  required: Record<string, boolean>,
  reports: Record<string, Report>,
  verification: Verification,
): boolean => {
  if (data?.status !== 'completed' || typeof data.passed !== 'boolean') return false
  if (!hasExactKeys(data.hard_denials, HARD_DENIAL_FIELDS)
      || !hasExactKeys(data.required_conditions, REQUIRED_CONDITION_FIELDS)
      || !hasExactKeys(data.reports, REPORT_FIELDS)
      || !isRecord(data.verification)) return false
  if (!HARD_DENIAL_FIELDS.every((key) => DENIAL_STATES.has(hardDenials[key]))) return false
  if (!REQUIRED_CONDITION_FIELDS.every((key) => typeof required[key] === 'boolean')) return false
  if (!REPORT_FIELDS.every((key) => reports[key] !== undefined)) return false
  if (typeof data.verification.passed !== 'boolean' || typeof data.verification.integrity_passed !== 'boolean') return false

  const allConditionsPassed = Object.values(required).every((value) => value === true)
  const allDenialsLifted = Object.values(hardDenials).every((value) => value === 'YES')
  return verification.integrityPassed
    && data.passed === allConditionsPassed
    && verification.passed === data.passed
    && (!data.passed || allDenialsLifted)
}

const DeliveryPage: React.FC = () => {
  const [data, setData] = useState<Evidence | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setData(null)
    try {
      const response = await apiClient.getReleaseEvidence()
      setData(isRecord(response) ? response as Evidence : {
        status: 'invalid_evidence',
        passed: false,
        message: 'release evidence response is not an object',
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (loading) {
    return <div className="flex-1 flex items-center justify-center text-content-tertiary"><Loader2 size={18} className="animate-spin mr-2" />{t('delivery_loading')}</div>
  }

  if (error) {
    return <DeliveryError message={error} onRetry={load} />
  }

  const hardDenials = stringRecord(data?.hard_denials)
  const required = booleanRecord(data?.required_conditions)
  const reports = reportRecord(data?.reports)
  const verification = verificationRecord(data?.verification)
  const completeContract = isCompleteEvidenceContract(data, hardDenials, required, reports, verification)
  const releasePassed = completeContract
    && data?.passed === true
    && verification.passed
    && verification.integrityPassed
    && Object.values(required).every((value) => value === true)
    && Object.values(hardDenials).every((value) => value === 'YES')
  const status = typeof data?.status === 'string' ? data.status : 'invalid_evidence'
  const message = typeof data?.message === 'string' ? data.message : ''
  const visibleHardDenials = completeContract
    ? hardDenials
    : status === 'not_available' ? { FDE_CASE_EVIDENCE: 'ABSENT' } : {}

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-6 py-5">
      <div className="max-w-5xl mx-auto space-y-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <ClipboardCheck size={21} className="text-accent" />
              <h1 className="text-xl font-bold text-content">{t('delivery_title')}</h1>
            </div>
            <p className="text-sm text-content-tertiary mt-1">{t('delivery_desc')}</p>
          </div>
          <button onClick={() => void load()} className="inline-flex items-center gap-1.5 px-3 py-2 rounded-btn border border-default text-sm text-content-secondary hover:bg-surface-2 cursor-pointer">
            <RefreshCw size={14} />{t('delivery_refresh')}
          </button>
        </div>

        <section className={`rounded-xl border p-4 ${releasePassed ? 'border-accent/40 bg-accent-soft' : 'border-danger/30 bg-danger-soft'}`}>
          <div className="flex items-start gap-3">
            {releasePassed ? <CheckCircle2 size={20} className="text-accent mt-0.5" /> : <ShieldAlert size={20} className="text-danger mt-0.5" />}
            <div>
              <h2 className="font-semibold text-content">{releasePassed ? t('delivery_passed') : t('delivery_blocked')}</h2>
              <p className="text-sm text-content-secondary mt-1">{releasePassed ? t('delivery_passed_desc') : t('delivery_blocked_desc')}</p>
              {!releasePassed && <p className="text-xs text-danger mt-2">{
                message || (status === 'not_available' ? t('delivery_manifest_absent') : t('delivery_invalid'))
              }</p>}
            </div>
          </div>
        </section>

        <section className="rounded-xl border border-default bg-surface overflow-hidden">
          <SectionTitle title={t('delivery_verification')} />
          <div className="divide-y divide-default">
            <Row label={t('delivery_integrity')} value={verification.integrityPassed ? 'PASS' : 'BLOCKED'} passed={verification.integrityPassed} />
            <Row label={t('delivery_release_result')} value={releasePassed ? 'PASS' : 'BLOCKED'} passed={releasePassed} />
            <Row label={t('delivery_response_status')} value={status} passed={status === 'completed' && releasePassed} />
          </div>
        </section>

        <EvidenceSection title={t('delivery_formal_gates')} hasRows={completeContract && Object.keys(required).length > 0}>
          {Object.entries(required).map(([name, passed]) => (
            <Row key={name} label={name} value={passed ? 'PASS' : 'BLOCKED'} passed={passed} />
          ))}
        </EvidenceSection>

        <EvidenceSection title={t('delivery_reports')} hasRows={completeContract && Object.keys(reports).length > 0}>
          {Object.entries(reports).map(([name, report]) => (
            <Row key={name} label={name} value={`${report.status || 'ABSENT'}${report.fresh === false ? ` · ${t('delivery_stale')}` : ''}`} passed={report.passed === true && report.fresh !== false} />
          ))}
        </EvidenceSection>

        <EvidenceSection title={t('delivery_hard_denials')} hasRows={Object.keys(visibleHardDenials).length > 0}>
          {Object.entries(visibleHardDenials).map(([name, value]) => (
            <Row key={name} label={name} value={value} passed={value === 'YES'} />
          ))}
        </EvidenceSection>
      </div>
    </div>
  )
}

const SectionTitle: React.FC<{ title: string }> = ({ title }) => (
  <h2 className="px-4 py-3 text-sm font-semibold text-content border-b border-default">{title}</h2>
)

const EvidenceSection: React.FC<{ title: string; hasRows: boolean; children: React.ReactNode }> = ({ title, hasRows, children }) => (
  <section className="rounded-xl border border-default bg-surface overflow-hidden">
    <SectionTitle title={title} />
    <div className="divide-y divide-default">
      {hasRows ? children : <div className="px-4 py-3 text-sm text-danger">{t('delivery_no_details')}</div>}
    </div>
  </section>
)

const Row: React.FC<{ label: string; value: string; passed: boolean }> = ({ label, value, passed }) => (
  <div className="flex items-center justify-between gap-4 px-4 py-2.5 text-sm">
    <span className="font-mono text-xs text-content-secondary break-all">{label}</span>
    <span className={`flex items-center gap-1.5 font-mono text-xs ${passed ? 'text-accent' : 'text-danger'}`}>
      {!passed && <AlertTriangle size={13} />}{value}
    </span>
  </div>
)

const DeliveryError: React.FC<{ message: string; onRetry: () => void }> = ({ message, onRetry }) => (
  <div className="flex-1 flex flex-col items-center justify-center px-6 text-center">
    <ShieldAlert size={24} className="text-danger mb-3" />
    <h2 className="text-lg font-semibold text-content mb-2">{t('delivery_load_error')}</h2>
    <p className="text-sm text-danger max-w-lg break-words mb-4">{message}</p>
    <button onClick={onRetry} className="px-4 py-2 rounded-btn bg-accent text-accent-contrast hover:bg-accent-hover text-sm font-medium cursor-pointer">{t('delivery_retry')}</button>
  </div>
)

export default DeliveryPage
