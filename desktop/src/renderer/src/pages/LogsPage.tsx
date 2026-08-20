import React, { useState, useEffect, useRef } from 'react'
import { RefreshCw } from 'lucide-react'
import { t } from '../i18n'
import apiClient, { type BackendEventSource } from '../api/client'

interface LogsPageProps {
  baseUrl: string
}

type LogConnectionState = 'connecting' | 'live' | 'disconnected'

const LogsPage: React.FC<LogsPageProps> = ({ baseUrl }) => {
  const [logs, setLogs] = useState<string[]>([])
  const [autoScroll, setAutoScroll] = useState(true)
  const [connectionState, setConnectionState] = useState<LogConnectionState>('connecting')
  const [connectionAttempt, setConnectionAttempt] = useState(0)
  const [lastEventAt, setLastEventAt] = useState<Date | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    apiClient.setBaseUrl(baseUrl)
    setConnectionState('connecting')
    let closed = false
    let es: BackendEventSource | null = null

    void apiClient.createLogStream().then((stream) => {
      if (closed) {
        stream.close()
        return
      }
      es = stream
      es.onmessage = (event) => {
        setConnectionState('live')
        setLastEventAt(new Date())
        try {
          const data = JSON.parse(event.data)
          if (data.type === 'init' && data.content) {
            setLogs(data.content.split('\n').filter(Boolean))
          } else if (data.type === 'line' && data.content) {
            setLogs((prev) => {
              const next = [...prev, data.content]
              if (next.length > 2000) return next.slice(-1500)
              return next
            })
          }
        } catch { /* ignore */ }
      }
      es.onerror = () => {
        if (closed) return
        setConnectionState('disconnected')
        es?.close()
        es = null
      }
    }).catch(() => {
      if (!closed) setConnectionState('disconnected')
    })

    return () => {
      closed = true
      es?.close()
    }
  }, [baseUrl, connectionAttempt])

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [logs, autoScroll])

  const handleScroll = () => {
    if (!containerRef.current) return
    const { scrollTop, scrollHeight, clientHeight } = containerRef.current
    setAutoScroll(scrollHeight - scrollTop - clientHeight < 50)
  }

  const getLogColor = (line: string) => {
    if (line.includes('ERROR') || line.includes('error')) return 'text-red-400'
    if (line.includes('WARNING') || line.includes('warn')) return 'text-amber-400'
    if (line.includes('DEBUG')) return 'text-slate-500'
    return 'text-slate-300'
  }

  return (
    <div className="flex-1 overflow-y-auto p-6">
      <div className="max-w-5xl mx-auto">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h2 className="text-xl font-bold text-slate-800 dark:text-slate-100">{t('logs_title')}</h2>
            <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">{t('logs_desc')}</p>
          </div>
        </div>

        {/* Terminal-style log viewer */}
        <div className="bg-slate-900 rounded-xl border border-slate-700 overflow-hidden shadow-lg">
          {/* Terminal header */}
          <div className="flex items-center gap-2 px-4 py-2.5 bg-slate-800 border-b border-slate-700">
            <div className="flex gap-1.5">
              <span className="w-3 h-3 rounded-full bg-red-500/80" />
              <span className="w-3 h-3 rounded-full bg-amber-500/80" />
              <span className="w-3 h-3 rounded-full bg-emerald-500/80" />
            </div>
            <span className="text-xs text-slate-400 ml-2 font-mono">run.log</span>
            <div className="flex-1" />
            <div className="flex items-center gap-1.5">
              <span className={`w-2 h-2 rounded-full ${
                connectionState === 'live'
                  ? 'bg-emerald-500 animate-pulse'
                  : connectionState === 'disconnected'
                    ? 'bg-red-500'
                    : 'bg-amber-500 animate-pulse'
              }`} />
              <span className="text-xs text-slate-400">
                {connectionState === 'live'
                  ? t('logs_live')
                  : connectionState === 'disconnected'
                    ? t('logs_disconnected')
                    : t('logs_connecting_short')}
              </span>
              {connectionState === 'disconnected' && (
                <button
                  type="button"
                  onClick={() => setConnectionAttempt((value) => value + 1)}
                  className="ml-1 inline-flex items-center gap-1 rounded border border-slate-600 px-1.5 py-0.5 text-xs text-slate-300 hover:bg-slate-700"
                >
                  <RefreshCw size={11} />
                  {t('logs_retry')}
                </button>
              )}
            </div>
          </div>

          {/* Log content */}
          <div
            ref={containerRef}
            onScroll={handleScroll}
            className="p-4 overflow-y-auto font-mono text-xs leading-relaxed whitespace-pre-wrap break-all"
            style={{ height: 'calc(100vh - 272px)' }}
          >
            {logs.length > 0 ? (
              logs.map((line, i) => (
                <div key={i} className={getLogColor(line)}>{line}</div>
              ))
            ) : (
              <p className={connectionState === 'disconnected' ? 'text-red-400' : 'text-slate-500'}>
                {connectionState === 'disconnected' ? t('logs_unavailable') : t('logs_connecting')}
              </p>
            )}
            {lastEventAt && (
              <p className="mt-3 text-[11px] text-slate-600">
                {t('logs_last_event')}: {lastEventAt.toLocaleTimeString()}
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export default LogsPage
