import { useState, useEffect, useCallback, useRef } from 'react'

// This is an opaque custom-protocol origin, not a network endpoint. The
// renderer never learns or probes the backend's loopback port; Electron main
// owns a per-launch pinned-TLS channel and exposes only narrow IPC methods.
const BACKEND_ORIGIN = 'smart_assistant://backend'

interface BackendState {
  status: 'connecting' | 'ready' | 'error'
  generation?: number
  error?: string
}

export function useBackend() {
  const [state, setState] = useState<BackendState>({ status: 'connecting' })
  const generationRef = useRef<number | undefined>(undefined)

  useEffect(() => {
    let cancelled = false
    const api = window.electronAPI

    const applyStatus = (data: { status: string; generation?: number; error?: string }) => {
      if (cancelled) return
      if (data.status === 'ready') {
        generationRef.current = data.generation
        setState({ status: 'ready', generation: data.generation })
        return
      }
      if (data.status === 'starting') {
        setState({ status: 'connecting', generation: data.generation })
        return
      }
      // A backend exit after a previous success is a real loss of the trust
      // boundary, not a transient renderer polling failure. Fail closed and
      // force a fresh main-process launch identity before future requests.
      generationRef.current = undefined
      setState({ status: 'error', error: data.error })
    }

    if (!api) {
      // The desktop UI must not downgrade to a direct loopback fetch when the
      // preload bridge is absent (for example a hostile/incorrect renderer).
      setState({ status: 'error', error: 'Trusted desktop bridge unavailable' })
      return () => { cancelled = true }
    }

    const offStatus = api.onBackendStatus(applyStatus)
    api
      .getBackendStatus()
      .then((status) => applyStatus({ status }))
      .catch(() => applyStatus({ status: 'error', error: 'Backend status unavailable' }))

    return () => {
      cancelled = true
      offStatus()
    }
  }, [])

  const restart = useCallback(async () => {
    setState((prev) => ({ ...prev, status: 'connecting', error: undefined }))
    if (!window.electronAPI) {
      setState({ status: 'error', error: 'Trusted desktop bridge unavailable' })
      return
    }
    await window.electronAPI.restartBackend()
  }, [])

  return { ...state, baseUrl: BACKEND_ORIGIN, restart }
}
