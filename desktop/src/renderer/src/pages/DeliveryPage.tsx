import React, { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, ClipboardCheck, Loader2, RefreshCw, ShieldAlert } from 'lucide-react'
import apiClient from '../api/client'
import { t } from '../i18n'

type Evidence = {
  status?: string
  passed?: boolean
  message?: string
  hard_denials?: Record<string, string>
  required_conditions?: Record<string, boolean>
  reports?: Record<string, { status?: string; passed?: boolean; fresh?: boolean; limitations?: Record<string, unknown> }>
  verification?: { passed?: boolean; checks?: Array<{ name?: string; passed?: boolean }> }
}

const DeliveryPage: React.FC = () => {
  const [data, setData] = useState<Evidence | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await apiClient.getReleaseEvidence() as Evidence)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setData(null)
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

  const hardDenials = data?.hard_denials || {}
  const required = data?.required_conditions || {}
  const reports = data?.reports || {}
  const releasePassed = data?.passed === true && data?.verification?.passed === true

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
              {data?.status === 'not_available' && <p className="text-xs text-danger mt-2">{data.message || t('delivery_manifest_absent')}</p>}
            </div>
          </div>
        </section>

        <section className="rounded-xl border border-default bg-surface overflow-hidden">
          <SectionTitle title={t('delivery_formal_gates')} />
          <div className="divide-y divide-default">
            {Object.entries(required).map(([name, passed]) => (
              <Row key={name} label={name} value={passed ? 'PASS' : 'BLOCKED'} passed={passed} />
            ))}
          </div>
        </section>

        <section className="rounded-xl border border-default bg-surface overflow-hidden">
          <SectionTitle title={t('delivery_reports')} />
          <div className="divide-y divide-default">
            {Object.entries(reports).map(([name, report]) => (
              <Row key={name} label={name} value={`${report.status || 'ABSENT'}${report.fresh === false ? ' · STALE' : ''}`} passed={report.passed === true && report.fresh !== false} />
            ))}
          </div>
        </section>

        <section className="rounded-xl border border-default bg-surface overflow-hidden">
          <SectionTitle title={t('delivery_hard_denials')} />
          <div className="divide-y divide-default">
            {Object.entries(hardDenials).map(([name, value]) => (
              <Row key={name} label={name} value={value} passed={value === 'YES'} />
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}

const SectionTitle: React.FC<{ title: string }> = ({ title }) => (
  <h2 className="px-4 py-3 text-sm font-semibold text-content border-b border-default">{title}</h2>
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
