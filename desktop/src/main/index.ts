import { app, BrowserWindow, shell, ipcMain, dialog, nativeImage, protocol, safeStorage } from 'electron'
import { openExternalSafely } from './external-url'
import path from 'path'
import fs from 'fs'
import { fileURLToPath } from 'url'
import http from 'http'
import { PythonBackend } from './python-manager'
import { buildAppMenu } from './menu'
import { createTray, destroyTray } from './tray'
import { initUpdater, checkForUpdates, startDownload, quitAndInstall, setUpdateLanguage } from './updater'
import { setupThemeIPC, loadAppConfig } from './themes'
import { setupHttpRelayIPC } from './http-relay'

// Force the product name so the Dock/menu shows the app name even in dev mode,
// where the default Electron binary would otherwise report "Electron". The name
// can be overridden by the bundled app-config (appName); defaults to SmartAssistant.
app.setName(loadAppConfig()?.appName || 'SmartAssistant')

let mainWindow: BrowserWindow | null = null
let pythonBackend: PythonBackend | null = null
// True once the user explicitly quits (menu/tray), so close-to-tray is bypassed.
let isQuitting = false

const isDev = !app.isPackaged
const VITE_DEV_PORTS = [5173, 5174, 5175, 5176]


// Register before app readiness. This origin is served only by Electron main
// after it verifies an owner/path-bound backend capability over pinned TLS.
protocol.registerSchemesAsPrivileged([
  {
    scheme: 'smart_assistant',
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      corsEnabled: true,
      stream: true,
    },
  },
])

let desktopAuthToken: string | null = null
let desktopSubjectToken: string | null = null
type DesktopStreamSender = Pick<Electron.WebContents, 'id' | 'isDestroyed' | 'send'>

interface DesktopStream {
  key: string
  streamId: string
  sender: DesktopStreamSender
  isCurrent: () => boolean
  cancelled: boolean
  terminal: boolean
  close?: () => void
}

type SafeStorageAdapter = Pick<typeof safeStorage, 'isEncryptionAvailable' | 'encryptString' | 'decryptString'>
type SubjectTokenFilesystem = Pick<typeof fs.promises, 'mkdir' | 'readFile' | 'rename' | 'unlink' | 'writeFile'>

const SUBJECT_TOKEN_FILENAME = 'desktop-subject-token.bin'

/**
 * Keeps the server-signed device identity out of the renderer while allowing
 * it to survive an application or trusted-backend restart. The replacement is
 * renamed into place only after its complete encrypted value is written.
 */
export function createDesktopSubjectTokenStore(
  userDataPath: string,
  storage: SafeStorageAdapter,
  reportFailure: (message: string) => void,
  fileSystem: SubjectTokenFilesystem = fs.promises,
) {
  const tokenPath = path.join(userDataPath, SUBJECT_TOKEN_FILENAME)
  const unavailable = 'Secure storage is unavailable; this device identity cannot be used.'

  const secureStorageAvailable = () => {
    if (storage.isEncryptionAvailable()) return true
    reportFailure(unavailable)
    return false
  }

  return {
    async load(): Promise<string | null> {
      if (!secureStorageAvailable()) return null
      let encrypted: Buffer
      try {
        encrypted = await fileSystem.readFile(tokenPath)
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null
        reportFailure('Unable to read the encrypted device identity.')
        return null
      }
      try {
        const token = storage.decryptString(encrypted)
        if (!token) {
          reportFailure('The encrypted device identity is invalid and was not used.')
          return null
        }
        return token
      } catch {
        reportFailure('The encrypted device identity could not be decrypted and was not used.')
        return null
      }
    },

    async save(token: string): Promise<void> {
      if (!token || !secureStorageAvailable()) {
        throw new Error(unavailable)
      }
      const temporaryPath = `${tokenPath}.${process.pid}.${Date.now()}.tmp`
      try {
        const encrypted = storage.encryptString(token)
        await fileSystem.mkdir(userDataPath, { recursive: true })
        await fileSystem.writeFile(temporaryPath, encrypted, { mode: 0o600 })
        await fileSystem.rename(temporaryPath, tokenPath)
      } catch {
        try { await fileSystem.unlink(temporaryPath) } catch { /* no temporary file to remove */ }
        reportFailure('Unable to save the encrypted device identity.')
        throw new Error('Unable to save the encrypted device identity.')
      }
    },

    async forget(): Promise<void> {
      try {
        await fileSystem.unlink(tokenPath)
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
          reportFailure('Unable to forget the encrypted device identity.')
          throw new Error('Unable to forget the encrypted device identity.')
        }
      }
    },
  }
}

/**
 * Tracks established and opening SSE connections. A pending connection remains
 * cancellable until attach() rechecks its backend generation and sender trust.
 */
export function createDesktopStreamRegistry() {
  const active = new Map<string, DesktopStream>()
  const opening = new Map<string, DesktopStream>()

  const remove = (stream: DesktopStream) => {
    if (active.get(stream.key) === stream) active.delete(stream.key)
    if (opening.get(stream.key) === stream) opening.delete(stream.key)
  }
  const closeHandle = (stream: DesktopStream) => {
    try { stream.close?.() } catch { /* connection already closed */ }
  }
  const send = (stream: DesktopStream, payload: Record<string, unknown>) => {
    if (!stream.sender.isDestroyed()) {
      stream.sender.send('desktop-backend-stream', { streamId: stream.streamId, ...payload })
    }
  }
  const cancel = (stream: DesktopStream, reason?: string, notify = false) => {
    if (stream.terminal) {
      // A close may arrive while openSse is unresolved. attach() installs the
      // handle later and reaches this branch to close that late connection.
      closeHandle(stream)
      return
    }
    stream.cancelled = true
    stream.terminal = true
    remove(stream)
    closeHandle(stream)
    if (notify) send(stream, { kind: 'closed', ...(reason ? { error: reason } : {}) })
  }

  return {
    begin(key: string, streamId: string, sender: DesktopStreamSender, isCurrent: () => boolean): DesktopStream {
      if (active.has(key) || opening.has(key)) throw new Error('Backend stream already exists')
      const stream: DesktopStream = { key, streamId, sender, isCurrent, cancelled: false, terminal: false }
      opening.set(key, stream)
      return stream
    },
    attach(stream: DesktopStream, close: () => void): boolean {
      stream.close = close
      if (stream.cancelled || stream.terminal || opening.get(stream.key) !== stream || !stream.isCurrent()) {
        cancel(stream)
        return false
      }
      opening.delete(stream.key)
      active.set(stream.key, stream)
      return true
    },
    message(stream: DesktopStream, payload: Record<string, unknown>) {
      if (stream.cancelled || stream.terminal) return
      if (!stream.isCurrent()) {
        cancel(stream)
        return
      }
      send(stream, payload)
    },
    finish(stream: DesktopStream, payload: Record<string, unknown>) {
      if (stream.cancelled || stream.terminal) return
      if (!stream.isCurrent()) {
        cancel(stream)
        return
      }
      stream.terminal = true
      remove(stream)
      send(stream, payload)
    },
    close(key: string) {
      const stream = active.get(key) ?? opening.get(key)
      if (stream) cancel(stream)
    },
    abortOpening(stream: DesktopStream) {
      if (opening.get(stream.key) === stream) cancel(stream)
    },
    closeAll(reason?: string) {
      for (const stream of new Set([...opening.values(), ...active.values()])) {
        cancel(stream, reason, true)
      }
    },
    counts() {
      return { active: active.size, opening: opening.size }
    },
  }
}

const desktopStreams = createDesktopStreamRegistry()
let desktopSubjectTokenStore: ReturnType<typeof createDesktopSubjectTokenStore> | null = null
let desktopIdentityFailureShown = false

function reportDesktopIdentityFailure(message: string) {
  console.error(`[security] ${message}`)
  const window = mainWindow
  if (window && !window.isDestroyed()) {
    window.webContents.send('desktop-authentication-error', { error: message })
  }
  if (!desktopIdentityFailureShown) {
    desktopIdentityFailureShown = true
    try {
      dialog.showErrorBox('Secure device identity unavailable', message)
    } catch { /* app is already shutting down */ }
  }
}

function getDesktopSubjectTokenStore() {
  if (!desktopSubjectTokenStore) {
    desktopSubjectTokenStore = createDesktopSubjectTokenStore(
      app.getPath('userData'),
      safeStorage,
      reportDesktopIdentityFailure,
    )
  }
  return desktopSubjectTokenStore
}

async function restoreDesktopSubjectToken() {
  desktopSubjectToken = await getDesktopSubjectTokenStore().load()
}

function clearDesktopBearer() {
  desktopAuthToken = null
}

function clearDesktopAuthentication() {
  clearDesktopBearer()
}

async function forgetDesktopDeviceIdentity() {
  clearDesktopBearer()
  desktopSubjectToken = null
  await getDesktopSubjectTokenStore().forget()
}

function closeDesktopStreams(reason?: string) {
  desktopStreams.closeAll(reason)
}

let desktopBackendGeneration = 0

function currentDesktopStream(
  backend: PythonBackend,
  generation: number,
  sender: Electron.WebContents,
): boolean {
  return pythonBackend === backend &&
    backend.getStatus() === 'ready' &&
    desktopBackendGeneration === generation &&
    isTrustedRenderer(sender)
}

function updateDesktopBackendGeneration(generation: number) {
  if (Number.isSafeInteger(generation) && generation >= 0) {
    desktopBackendGeneration = generation
  }
}

function streamKey(sender: Electron.WebContents, streamId: string): string {
  return `${sender.id}:${streamId}`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isTrustedRendererUrl(rawUrl: string): boolean {
  try {
    const parsed = new URL(rawUrl)
    if (isDev) {
      return parsed.protocol === 'http:' && parsed.hostname === 'localhost' && VITE_DEV_PORTS.includes(Number(parsed.port))
    }
    if (parsed.protocol !== 'file:') return false
    const expected = path.resolve(__dirname, '../renderer/index.html')
    return path.resolve(fileURLToPath(parsed)) === expected
  } catch {
    return false
  }
}

function isTrustedRenderer(sender: Electron.WebContents): boolean {
  return sender === mainWindow?.webContents && isTrustedRendererUrl(sender.getURL())
}

function parseRendererRequest(raw: unknown): {
  path: string
  method: string
  headers: Record<string, string>
  body?: string | Uint8Array
} {
  if (!isRecord(raw) || typeof raw.path !== 'string' || raw.path.length > 8192) {
    throw new Error('Invalid backend request')
  }
  const method = typeof raw.method === 'string' ? raw.method.toUpperCase() : 'GET'
  if (!/^[A-Z]{1,16}$/.test(method)) throw new Error('Invalid backend request method')
  const headers: Record<string, string> = {}
  if (raw.headers !== undefined) {
    if (!isRecord(raw.headers)) throw new Error('Invalid backend request headers')
    for (const [name, value] of Object.entries(raw.headers)) {
      const lower = name.toLowerCase()
      if (
        typeof value !== 'string' ||
        !/^[A-Za-z0-9-]{1,128}$/.test(name) ||
        /[\r\n]/.test(value) ||
        ['authorization', 'host', 'cookie', 'content-length', 'connection', 'transfer-encoding'].includes(lower) ||
        lower.startsWith('x-cow-desktop-')
      ) {
        throw new Error('Forbidden backend request header')
      }
      headers[name] = value
    }
  }
  if (raw.body === undefined) return { path: raw.path, method, headers }
  if (typeof raw.body === 'string') return { path: raw.path, method, headers, body: raw.body }
  if (raw.body instanceof Uint8Array) return { path: raw.path, method, headers, body: raw.body }
  throw new Error('Invalid backend request body')
}

function sanitizedResponseHeaders(headers: Record<string, string>): Record<string, string> {
  const safe: Record<string, string> = {}
  for (const [name, value] of Object.entries(headers)) {
    const lower = name.toLowerCase()
    // `/auth/login` sets an HttpOnly bearer cookie for normal browsers. The
    // desktop response must never send that credential across renderer IPC.
    if (lower === 'set-cookie' || lower === 'authorization' || lower === 'proxy-authenticate') continue
    safe[name] = value
  }
  return safe
}

async function proxyDesktopRequest(raw: unknown) {
  const request = parseRendererRequest(raw)
  if (!pythonBackend || pythonBackend.getStatus() !== 'ready') {
    throw new Error('Trusted backend is unavailable')
  }

  const parsed = new URL(request.path, 'https://smart_assistant.invalid')
  if (parsed.origin !== 'https://smart_assistant.invalid') throw new Error('Invalid backend request path')
  const route = parsed.pathname
  let body = request.body
  if (route === '/auth/login') {
    if (typeof body !== 'string') throw new Error('Invalid login request')
    let login: Record<string, unknown>
    try { login = JSON.parse(body) as Record<string, unknown> } catch { throw new Error('Invalid login request') }
    if (!isRecord(login) || Object.keys(login).some((key) => key !== 'password') || typeof login.password !== 'string') {
      throw new Error('Invalid login request')
    }
    body = JSON.stringify({
      password: login.password,
      ...(desktopSubjectToken ? { subject_token: desktopSubjectToken } : {}),
    })
  }

  const headers = { ...request.headers }
  if (desktopAuthToken && route !== '/auth/login') headers.Authorization = `Bearer ${desktopAuthToken}`
  try {
    const response = await pythonBackend.request({ ...request, headers, body })
    let responseBody = response.body
    if (route === '/auth/login') {
      let payload: Record<string, unknown> | null = null
      try {
        payload = JSON.parse(response.body.toString('utf8')) as Record<string, unknown>
      } catch {
        // Preserve a malformed/error body; it cannot become an authenticated
        // desktop success because no bearer was captured.
      }
      if (payload?.status === 'success' && typeof payload.token === 'string') {
        if (typeof payload.subject_token !== 'string' || !payload.subject_token) {
          clearDesktopBearer()
          desktopSubjectToken = null
          reportDesktopIdentityFailure('The trusted backend returned an invalid device identity.')
          throw new Error('Trusted backend returned an invalid device identity.')
        }
        try {
          await getDesktopSubjectTokenStore().save(payload.subject_token)
        } catch {
          // Do not leave an older device identity on disk after failing to
          // commit its replacement: that could select the wrong owner after
          // a later restart. Authentication remains fail-closed.
          clearDesktopBearer()
          desktopSubjectToken = null
          try { await getDesktopSubjectTokenStore().forget() } catch { /* already reported */ }
          throw new Error('Unable to securely persist the device identity.')
        }
        desktopSubjectToken = payload.subject_token
        desktopAuthToken = payload.token
        delete payload.token
        delete payload.subject_token
        responseBody = Buffer.from(JSON.stringify(payload), 'utf8')
      }
    }
    return {
      status: response.status,
      statusText: response.statusText,
      headers: sanitizedResponseHeaders(response.headers),
      bodyBase64: responseBody.toString('base64'),
    }
  } finally {
    // A failed/revoked logout must never leave a renderer-triggered request
    // with a bearer that could be replayed after a backend restart.
    if (route === '/auth/logout') clearDesktopAuthentication()
  }
}

function resourcePathFromUrl(rawUrl: string): string | null {
  try {
    const parsed = new URL(rawUrl)
    if (
      parsed.protocol !== 'smart_assistant:' ||
      parsed.hostname !== 'backend' ||
      (!parsed.pathname.startsWith('/file/') && !parsed.pathname.startsWith('/preview/'))
    ) return null
    return `${parsed.pathname}${parsed.search}`
  } catch {
    return null
  }
}

function setupBackendProtocol() {
  protocol.handle('smart_assistant', async (request) => {
    const resourcePath = resourcePathFromUrl(request.url)
    if (!resourcePath || (request.method !== 'GET' && request.method !== 'HEAD') || !pythonBackend) {
      return new Response('Not found', { status: 404 })
    }
    try {
      const response = await pythonBackend.request({ path: resourcePath, method: request.method })
      return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers: sanitizedResponseHeaders(response.headers),
      })
    } catch {
      // Certificate pin/MAC failure is intentionally indistinguishable from an
      // unavailable resource to an untrusted renderer document.
      return new Response('Trusted backend unavailable', { status: 503 })
    }
  })
}

function openBackendResourceWindow(url: string) {
  if (!resourcePathFromUrl(url)) return
  const resourceWindow = new BrowserWindow({
    width: 1100,
    height: 800,
    show: true,
    webPreferences: {
      // Generated previews/files are untrusted document content. They receive
      // neither the app preload bridge nor Node/Electron privileges; resource
      // bytes still flow only through the main-process protocol handler.
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  })
  resourceWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  void resourceWindow.loadURL(url)
}

function probePort(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(`http://localhost:${port}`, (res) => {
      resolve(res.statusCode !== undefined)
    })
    req.on('error', () => resolve(false))
    req.setTimeout(500, () => { req.destroy(); resolve(false) })
  })
}

async function findViteDevServer(): Promise<string | null> {
  for (const port of VITE_DEV_PORTS) {
    if (await probePort(port)) {
      return `http://localhost:${port}`
    }
  }
  return null
}

function getIconPath(ext: string = 'png'): string | undefined {
  const iconFile = `icon.${ext}`
  const iconPath = isDev
    ? path.resolve(__dirname, '../../resources', iconFile)
    : path.join(process.resourcesPath, iconFile)
  if (fs.existsSync(iconPath)) return iconPath
  return undefined
}

const isMac = process.platform === 'darwin'
const isWin = process.platform === 'win32'

// Persisted window bounds
const windowStateFile = () => path.join(app.getPath('userData'), 'window-state.json')

function loadWindowState(): { width: number; height: number; x?: number; y?: number } {
  try {
    const raw = fs.readFileSync(windowStateFile(), 'utf-8')
    const s = JSON.parse(raw)
    if (typeof s.width === 'number' && typeof s.height === 'number') return s
  } catch {
    /* first run or unreadable */
  }
  return { width: 1280, height: 800 }
}

function saveWindowState() {
  if (!mainWindow || mainWindow.isDestroyed()) return
  if (mainWindow.isMinimized() || mainWindow.isFullScreen()) return
  const b = mainWindow.getBounds()
  try {
    fs.writeFileSync(windowStateFile(), JSON.stringify(b))
  } catch {
    /* ignore */
  }
}

function createWindow() {
  const state = loadWindowState()

  mainWindow = new BrowserWindow({
    width: state.width,
    height: state.height,
    x: state.x,
    y: state.y,
    minWidth: 900,
    minHeight: 600,
    // macOS: native traffic lights inset into our custom titlebar.
    // Windows: fully frameless; we render custom window controls in-app.
    titleBarStyle: isMac ? 'hiddenInset' : 'hidden',
    trafficLightPosition: isMac ? { x: 14, y: 16 } : undefined,
    frame: isMac ? undefined : false,
    backgroundColor: '#0e0e10',
    icon: getIconPath(),
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  const persist = () => saveWindowState()
  mainWindow.on('resize', persist)
  mainWindow.on('move', persist)
  mainWindow.on('maximize', emitMaximizeState)
  mainWindow.on('unmaximize', emitMaximizeState)

  const rendererHtml = path.join(__dirname, '../renderer/index.html')

  if (isDev) {
    findViteDevServer().then((devUrl) => {
      if (devUrl) {
        console.log(`[Electron] Loading Vite dev server: ${devUrl}`)
        mainWindow?.loadURL(devUrl)
        mainWindow?.webContents.openDevTools()
      } else if (fs.existsSync(rendererHtml)) {
        console.log('[Electron] Vite dev server not found, loading built files')
        mainWindow?.loadFile(rendererHtml)
      } else {
        console.error('[Electron] No renderer available. Run "npm run build:renderer" first.')
      }
    })
  } else {
    mainWindow.loadFile(rendererHtml)
  }

  // Surface renderer-side console output and load failures to the main-process
  // stdout. Without this, "stuck on initializing" hangs are invisible from the
  // terminal because all renderer logs stay in the (closed) devtools.
  mainWindow.webContents.on('console-message', (_e, level, message, line, sourceId) => {
    console.log(`[renderer:${level}] ${message} (${sourceId}:${line})`)
  })
  mainWindow.webContents.on('did-fail-load', (_e, code, desc, url) => {
    console.error(`[renderer] did-fail-load ${code} ${desc} ${url}`)
  })
  // A renderer navigation to an arbitrary origin would otherwise retain the
  // preload bridge and become a confused deputy for trusted backend IPC.
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (isTrustedRendererUrl(url)) return
    event.preventDefault()
    void openExternalSafely(url)
  })

  mainWindow.once('ready-to-show', () => {
    mainWindow?.show()
  })

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (resourcePathFromUrl(url)) {
      openBackendResourceWindow(url)
      return { action: 'deny' }
    }
    void openExternalSafely(url)
    return { action: 'deny' }
  })

  // Close-to-tray: hide the window instead of destroying it, so the tray's
  // "Show" can bring it back. Only a real Quit (menu/tray/Cmd+Q) destroys it.
  mainWindow.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault()
      mainWindow?.hide()
    }
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

function getBackendPath(): string {
  if (isDev) {
    return path.resolve(__dirname, '../../..')
  }
  return path.join(process.resourcesPath, 'backend')
}

async function startBackend() {
  const backendPath = getBackendPath()
  pythonBackend = new PythonBackend(backendPath)

  pythonBackend.on('starting', (event: { generation: number }) => {
    updateDesktopBackendGeneration(event.generation)
    clearDesktopAuthentication()
    closeDesktopStreams('Backend is restarting')
    mainWindow?.webContents.send('backend-status', { status: 'starting', generation: event.generation })
  })

  pythonBackend.on('ready', (event: { generation: number }) => {
    updateDesktopBackendGeneration(event.generation)
    console.log(`[backend] ready on trusted generation ${event.generation}`)
    mainWindow?.webContents.send('backend-status', { status: 'ready', generation: event.generation })
  })

  pythonBackend.on('error', (error: string) => {
    clearDesktopAuthentication()
    closeDesktopStreams('Trusted backend error')
    // Mirror to the main-process stdout too: otherwise backend startup errors
    // are only visible in the renderer devtools, making `npm run dev` hangs
    // impossible to diagnose from the terminal.
    console.error(`[backend] error: ${error}`)
    mainWindow?.webContents.send('backend-status', { status: 'error', error })
  })

  pythonBackend.on('stopped', (event: { generation: number; unexpected: boolean }) => {
    updateDesktopBackendGeneration(event.generation)
    clearDesktopAuthentication()
    closeDesktopStreams('Trusted backend stopped')
    mainWindow?.webContents.send('backend-status', {
      status: 'stopped',
      generation: event.generation,
      ...(event.unexpected ? { error: 'Trusted backend stopped unexpectedly' } : {}),
    })
  })

  pythonBackend.on('log', (line: string) => {
    console.log(`[backend] ${line}`)
    mainWindow?.webContents.send('backend-log', line)
  })

  await pythonBackend.start()
}

function setupIPC() {
  ipcMain.handle('get-backend-status', () => {
    return pythonBackend?.getStatus() ?? 'stopped'
  })

  ipcMain.handle('restart-backend', async () => {
    clearDesktopAuthentication()
    closeDesktopStreams('Backend is restarting')
    await pythonBackend?.restart()
    return true
  })

  ipcMain.handle('desktop-backend-request', async (event, request) => {
    if (!isTrustedRenderer(event.sender)) throw new Error('Untrusted renderer')
    return proxyDesktopRequest(request)
  })

  ipcMain.handle('desktop-forget-device-identity', async (event) => {
    if (!isTrustedRenderer(event.sender)) throw new Error('Untrusted renderer')
    await forgetDesktopDeviceIdentity()
    return true
  })

  ipcMain.handle('desktop-backend-stream-open', async (event, raw) => {
    if (!isTrustedRenderer(event.sender) || !isRecord(raw)) throw new Error('Untrusted renderer')
    const streamId = raw.streamId
    const path = raw.path
    if (
      typeof streamId !== 'string' ||
      !/^[A-Za-z0-9_-]{12,128}$/.test(streamId) ||
      typeof path !== 'string' ||
      path.length > 8192 ||
      !pythonBackend ||
      pythonBackend.getStatus() !== 'ready'
    ) throw new Error('Invalid backend stream')
    const parsed = new URL(path, 'https://smart_assistant.invalid')
    if (parsed.origin !== 'https://smart_assistant.invalid' || !['/stream', '/api/logs'].includes(parsed.pathname)) {
      throw new Error('Invalid backend stream path')
    }
    const key = streamKey(event.sender, streamId)
    const backend = pythonBackend
    const generation = desktopBackendGeneration
    const streamState = desktopStreams.begin(key, streamId, event.sender, () =>
      currentDesktopStream(backend, generation, event.sender),
    )
    try {
      const stream = await backend.openSse(`${parsed.pathname}${parsed.search}`, {
        message: (data, lastEventId) => desktopStreams.message(
          streamState,
          { kind: 'message', data, ...(lastEventId ? { lastEventId } : {}) },
        ),
        error: (error) => {
          desktopStreams.finish(streamState, { kind: 'error', error: error.message })
        },
        closed: () => {
          desktopStreams.finish(streamState, { kind: 'closed' })
        },
      })
      desktopStreams.attach(streamState, () => stream.close())
    } finally {
      // attach() or cancellation removes its own state. This remains only as
      // a defensive cleanup if PythonBackend.openSse rejects before resolving.
      desktopStreams.abortOpening(streamState)
    }
  })

  ipcMain.handle('desktop-backend-stream-close', async (event, streamId: unknown) => {
    if (!isTrustedRenderer(event.sender) || typeof streamId !== 'string') return
    const key = streamKey(event.sender, streamId)
    desktopStreams.close(key)
  })

  ipcMain.handle('select-directory', async () => {
    const result = await dialog.showOpenDialog({
      properties: ['openDirectory'],
    })
    return result.canceled ? null : result.filePaths[0]
  })

  ipcMain.handle('select-file', async (_event, filters?: Electron.FileFilter[]) => {
    const result = await dialog.showOpenDialog({
      properties: ['openFile'],
      filters: filters || [{ name: 'All Files', extensions: ['*'] }],
    })
    return result.canceled ? null : result.filePaths[0]
  })

  // Open a local file with the OS default app; falls back to revealing it in
  // the file manager when no handler exists. Returns '' on success.
  ipcMain.handle('open-path', async (_event, targetPath: string) => {
    if (!targetPath) return 'empty path'
    const err = await shell.openPath(targetPath)
    if (err) shell.showItemInFolder(targetPath)
    return err
  })

  // Custom window controls (used by Windows frameless titlebar)
  ipcMain.handle('window-minimize', () => mainWindow?.minimize())
  ipcMain.handle('window-maximize', () => {
    if (!mainWindow) return false
    if (mainWindow.isMaximized()) mainWindow.unmaximize()
    else mainWindow.maximize()
    return mainWindow.isMaximized()
  })
  ipcMain.handle('window-close', () => mainWindow?.close())
  ipcMain.handle('window-is-maximized', () => mainWindow?.isMaximized() ?? false)

  // Current app version, shown in the NavRail footer.
  ipcMain.handle('get-app-version', () => app.getVersion())

  // Auto-update controls (renderer-driven: check, then opt-in download/install).
  // The renderer passes its current UI language so downloads can be routed to
  // the China CDN mirror (zh) or R2 (others).
  ipcMain.handle('update-check', (_event, lang?: string) => {
    setUpdateLanguage(lang)
    // This channel is only hit by an explicit "check for update" click, so the
    // panel should re-open even if the version was previously dismissed.
    checkForUpdates(true)
  })
  ipcMain.handle('update-download', (_event, lang?: string) => {
    setUpdateLanguage(lang)
    startDownload()
  })
  ipcMain.handle('update-install', () => {
    // Let the window actually close so the app can fully quit — otherwise the
    // close-to-tray handler preventDefault()s it, the process stays alive, and
    // Squirrel.Mac can't swap the app bundle (the update silently no-ops and
    // relaunching still shows the old version).
    isQuitting = true
    quitAndInstall()
  })

  // Synchronous OS locale lookup (e.g. "zh-CN", "en-US"). Used by the renderer
  // to pick a sensible default UI language on first run before any paint.
  ipcMain.on('get-system-locale', (event) => {
    event.returnValue = app.getLocale() || app.getSystemLocale?.() || ''
  })
}

function emitMaximizeState() {
  const max = mainWindow?.isMaximized() ?? false
  mainWindow?.webContents.send('window-maximize-changed', max)
}

// Single-instance lock: focus the existing window instead of opening a second app.
const gotTheLock = app.requestSingleInstanceLock()
if (!gotTheLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.show()
      mainWindow.focus()
    }
  })
}

app.on('before-quit', () => {
  clearDesktopAuthentication()
  closeDesktopStreams('Application is closing')
  pythonBackend?.stop()
})

app.whenReady().then(async () => {
  // Set Dock icon on macOS (PNG is most reliable for nativeImage)
  if (process.platform === 'darwin') {
    const pngPath = getIconPath('png')
    if (pngPath) {
      const icon = nativeImage.createFromPath(pngPath)
      if (!icon.isEmpty()) {
        app.dock.setIcon(icon)
        console.log('[Electron] Dock icon set:', pngPath)
      } else {
        console.warn('[Electron] Dock icon loaded but empty:', pngPath)
      }
    } else {
      console.warn('[Electron] Dock icon not found in resources/')
    }
  }

  setupIPC()
  setupThemeIPC()
  setupHttpRelayIPC()
  setupBackendProtocol()
  createWindow()
  await restoreDesktopSubjectToken()
  buildAppMenu(() => mainWindow)
  // No menu-bar tray on macOS — the Dock + window controls are enough there.
  // Keep the tray on Windows/Linux where minimizing to a tray icon is expected.
  if (!isMac) {
    createTray({
      getWindow: () => mainWindow,
      iconPath: getIconPath('png'),
      onQuit: () => {
        isQuitting = true
        app.quit()
      },
    })
  }
  await startBackend()

  // Wire auto-update: a first silent check a few seconds after launch (so it
  // doesn't compete with backend startup), then poll every 4 hours so a
  // long-running window still surfaces new releases. Both are automatic checks
  // (userInitiated=false): the panel auto-opens once per new version, and once
  // the user dismisses it these polls only keep the footer/menu dot lit rather
  // than re-popping the panel. autoDownload is off, so any update is opt-in.
  initUpdater(() => mainWindow)
  setTimeout(() => checkForUpdates(), 5000)
  const UPDATE_POLL_MS = 4 * 60 * 60 * 1000
  setInterval(() => checkForUpdates(), UPDATE_POLL_MS)

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow()
    } else {
      mainWindow?.show()
    }
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

app.on('before-quit', () => {
  isQuitting = true
  saveWindowState()
  destroyTray()
  pythonBackend?.stop()
})
