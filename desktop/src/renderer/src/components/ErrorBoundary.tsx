import React from 'react'

interface SafeDiagnostic {
  event: 'renderer_render_error'
  code: 'REACT_RENDER_ERROR'
  time: string
  correlation_id: string
}

interface ErrorBoundaryState {
  hasError: boolean
  diagnostic: SafeDiagnostic | null
  copyStatus: 'idle' | 'copying' | 'copied' | 'failed'
}

export default class ErrorBoundary extends React.Component<React.PropsWithChildren, ErrorBoundaryState> {
  state: ErrorBoundaryState = { hasError: false, diagnostic: null, copyStatus: 'idle' }

  static getDerivedStateFromError(): Partial<ErrorBoundaryState> {
    return { hasError: true }
  }

  componentDidCatch(_error: Error, _info: React.ErrorInfo): void {
    // Build a whitelist instead of retaining error text, stack, props or session data.
    this.setState({
      diagnostic: {
        event: 'renderer_render_error',
        code: 'REACT_RENDER_ERROR',
        time: new Date().toISOString(),
        correlation_id: crypto.randomUUID(),
      },
    })
  }

  private copyDiagnostic = async (): Promise<void> => {
    if (!this.state.diagnostic || this.state.copyStatus === 'copying') return

    this.setState({ copyStatus: 'copying' })
    try {
      await navigator.clipboard.writeText(JSON.stringify(this.state.diagnostic, null, 2))
      this.setState({ copyStatus: 'copied' })
    } catch {
      this.setState({ copyStatus: 'failed' })
    }
  }

  render(): React.ReactNode {
    if (!this.state.hasError) return this.props.children

    const { diagnostic, copyStatus } = this.state
    return (
      <main className="flex min-h-screen items-center justify-center bg-base p-6 text-primary">
        <section aria-labelledby="render-error-heading" className="w-full max-w-xl rounded-xl border border-default bg-surface p-6">
          <h1 id="render-error-heading" className="text-xl font-semibold">页面渲染异常</h1>
          <p className="mt-3 text-sm text-secondary">可以重载当前会话，或复制诊断日志用于排查。</p>
          <div className="mt-5 flex flex-wrap gap-3">
            <button type="button" onClick={() => window.location.reload()} className="rounded-lg bg-accent px-4 py-2 text-sm text-white">
              重载当前会话
            </button>
            <button type="button" onClick={this.copyDiagnostic} disabled={!diagnostic || copyStatus === 'copying'} className="rounded-lg border border-default px-4 py-2 text-sm disabled:opacity-50">
              复制诊断日志
            </button>
          </div>
          <p role="status" className="mt-3 text-sm text-secondary">
            {copyStatus === 'copying' && '正在复制…'}
            {copyStatus === 'copied' && '诊断日志已复制。'}
            {copyStatus === 'failed' && '无法自动复制，请选择下方诊断日志后手动复制。'}
          </p>
          <textarea aria-label="安全诊断日志" readOnly value={diagnostic ? JSON.stringify(diagnostic, null, 2) : ''} className="mt-3 h-40 w-full resize-none rounded-lg border border-default bg-inset p-3 font-mono text-xs" />
        </section>
      </main>
    )
  }
}
