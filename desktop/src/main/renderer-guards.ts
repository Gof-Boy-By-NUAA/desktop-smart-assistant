import { app, BrowserWindow, dialog } from 'electron'
import fs from 'fs'
import path from 'path'

// No renderer URL, conversation, exception text or credentials enter this log.
function logRendererEvent(event: string, details: { reason?: string; exitCode?: number } = {}): boolean {
  const record = {
    timestamp: new Date().toISOString(), event,
    ...(details.reason ? { reason: details.reason } : {}),
    ...(Number.isInteger(details.exitCode) ? { exitCode: details.exitCode } : {}),
  }
  console.info(`[RendererGuard] ${JSON.stringify(record)}`)
  try {
    const directory = app.getPath('logs')
    fs.mkdirSync(directory, { recursive: true })
    fs.appendFileSync(path.join(directory, 'renderer-health.jsonl'), JSON.stringify(record) + '\n', 'utf8')
    return true
  } catch {
    console.error('[RendererGuard] DIAGNOSTIC_LOG_WRITE_FAILED')
    return false
  }
}

export function setupWebContentsGuards(window: BrowserWindow, isQuitting: () => boolean): () => Promise<void> {
  const wc = window.webContents
  // Capture the JS EventEmitter operations while the native objects are alive.
  const removeWebListener = wc.removeListener.bind(wc)
  const removeWindowListener = window.removeListener.bind(window)
  let disposed = false
  let disposalResult: Promise<void> | null = null
  let quitRequested = false
  let failure: 'gone' | 'unresponsive' | null = null
  let epoch = 0
  let offeredEpoch = -1
  let prompting = false
  let promptAbort: AbortController | null = null
  let promptResult: Promise<Electron.MessageBoxReturnValue> | null = null
  let expectedRestart = false
  let pendingRecovery = false
  let recoveryImmediate: ReturnType<typeof setImmediate> | null = null
  let logWritten = true

  const usable = () => !disposed && !isQuitting() && !window.isDestroyed() && !wc.isDestroyed()
  const cancelPrompt = () => promptAbort?.abort()

  function dispose(): Promise<void> {
    if (disposed) return disposalResult!
    disposed = true
    // Return the actual native dialog completion for owners performing an
    // explicit destroy()/exit(), which bypass Electron's normal quit sequence.
    if (promptResult) {
      app.on('before-quit', preventWhileClosing)
      disposalResult = promptResult.then(() => {}, () => {}).then(() => {
        app.removeListener('before-quit', preventWhileClosing)
        if (quitRequested) app.quit()
      })
    } else disposalResult = Promise.resolve()
    epoch += 1
    failure = null
    pendingRecovery = false
    expectedRestart = false
    if (recoveryImmediate !== null) clearImmediate(recoveryImmediate)
    recoveryImmediate = null
    removeWebListener('render-process-gone', onGone)
    removeWebListener('unresponsive', onUnresponsive)
    removeWebListener('responsive', onResponsive)
    removeWebListener('did-finish-load', onLoaded)
    removeWebListener('destroyed', dispose)
    removeWindowListener('closed', dispose)
    app.removeListener('before-quit', onBeforeQuit)
    const controller = promptAbort
    promptAbort = null
    controller?.abort()
    return disposalResult
  }

  function onBeforeQuit(event: Electron.Event): void {
    if (promptResult) preventWhileClosing(event)
    void dispose()
    // Windows closes the native message box asynchronously after abort().
    // Keep Electron's UI thread alive until its actual result has settled.
  }

  function preventWhileClosing(event: Electron.Event): void {
    event.preventDefault()
    quitRequested = true
  }

  function offerRecovery(): void {
    if (!usable() || !failure || prompting || pendingRecovery || offeredEpoch === epoch) return
    const promptEpoch = epoch
    offeredEpoch = epoch
    prompting = true
    const controller = new AbortController()
    promptAbort = controller
    const hung = failure === 'unresponsive'
    const nativeResult = dialog.showMessageBox(window, {
      type: 'warning', title: 'SmartAssistant 页面恢复',
      message: hung ? '页面暂时无响应' : '页面进程已停止',
      detail: '重载会重新打开当前页面，不会重新发送消息。尚未发送的输入可能丢失。' +
        (logWritten ? '诊断已保存在本地 renderer-health.jsonl。' : '本地诊断日志写入失败。'),
      buttons: ['重载当前会话', hung ? '继续等待' : '暂不重载'],
      defaultId: 1, cancelId: 1, noLink: true, signal: controller.signal,
    })
    promptResult = nativeResult
    void nativeResult.then(({ response }) => {
      if (response !== 0 || controller.signal.aborted || !usable() || epoch !== promptEpoch || !failure) return
      const forceRestart = failure === 'unresponsive'
      epoch += 1
      const recoveryEpoch = epoch
      pendingRecovery = true
      logRendererEvent('user-requested-reload')
      // Leave the event handler before reloading a dead renderer. There is no
      // automatic retry loop: every recovery requires an explicit user choice.
      recoveryImmediate = setImmediate(() => {
        if (disposed) return
        recoveryImmediate = null
        if (!usable() || epoch !== recoveryEpoch || !pendingRecovery || !failure) return
        pendingRecovery = false
        failure = null
        try {
          if (forceRestart) {
            expectedRestart = true
            wc.forcefullyCrashRenderer()
          }
          if (usable()) wc.reload()
        } catch {
          if (!usable()) return
          expectedRestart = false
          logRendererEvent('reload-failed')
          failure = 'gone'
          epoch += 1
          offerRecovery()
        }
      })
    }).catch(() => {
      if (!controller.signal.aborted && usable()) logRendererEvent('recovery-dialog-failed')
    }).finally(() => {
      if (promptResult === nativeResult) promptResult = null
      if (disposed) return
      prompting = false
      if (promptAbort === controller) promptAbort = null
      if (epoch !== promptEpoch) offerRecovery()
    })
  }

  function onGone(_event: Electron.Event, details: Electron.RenderProcessGoneDetails): void {
    if (!usable()) return
    logWritten = logRendererEvent('render-process-gone', details)
    if (expectedRestart) {
      expectedRestart = false
      return
    }
    pendingRecovery = false
    failure = 'gone'
    epoch += 1
    cancelPrompt()
    offerRecovery()
  }
  function onUnresponsive(): void {
    if (!usable()) return
    logWritten = logRendererEvent('unresponsive')
    if (failure === 'gone') return
    if (failure !== 'unresponsive') {
      failure = 'unresponsive'
      epoch += 1
    }
    offerRecovery()
  }
  function onResponsive(): void {
    if (!usable()) return
    logRendererEvent('responsive')
    if (failure === 'unresponsive') {
      failure = null
      pendingRecovery = false
      epoch += 1
      cancelPrompt()
    }
  }
  function onLoaded(): void {
    if (!usable()) return
    failure = null
    pendingRecovery = false
    expectedRestart = false
    epoch += 1
    cancelPrompt()
  }
  wc.on('render-process-gone', onGone)
  wc.on('unresponsive', onUnresponsive)
  wc.on('responsive', onResponsive)
  wc.on('did-finish-load', onLoaded)
  wc.once('destroyed', dispose)
  window.once('closed', dispose)
  app.once('before-quit', onBeforeQuit)
  return dispose
}
