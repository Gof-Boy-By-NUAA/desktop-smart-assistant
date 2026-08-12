import { ChildProcess, spawn, execFileSync } from 'child_process'
import { EventEmitter } from 'events'
import path from 'path'
import os from 'os'
import fs from 'fs'
import * as crypto from 'crypto'
import * as https from 'https'
import { StringDecoder } from 'string_decoder'

// Writable data dir for the packaged app (config.json, run.log, user data).
// Source/dev runs keep using the repository CWD instead.
const COW_DATA_DIR = path.join(os.homedir(), '.cow')
const DESKTOP_CONTROL_FD = 3
const CONTROL_FRAME_MAX_BYTES = 4096
const MAX_REQUEST_BYTES = 64 * 1024 * 1024
const MAX_RESPONSE_BYTES = 64 * 1024 * 1024
const MAX_SSE_EVENT_BYTES = 1024 * 1024
const STARTUP_TIMEOUT_MS = 120_000
const SHUTDOWN_TIMEOUT_MS = 5_000
const MAX_ORIGIN_FORM_BYTES = 8192

export interface PythonBackendOptions {
  /** Test-only/diagnostic override; production uses the bounded default. */
  startupTimeoutMs?: number
  /** Test-only/diagnostic override; production waits five seconds per signal. */
  shutdownTimeoutMs?: number
}

export interface TrustedBackendRequest {
  path: string
  method?: string
  headers?: Record<string, string>
  body?: Buffer | Uint8Array | string
  timeoutMs?: number
}

export interface TrustedBackendResponse {
  status: number
  statusText: string
  headers: Record<string, string>
  body: Buffer
}

export interface TrustedBackendStream {
  close(): void
}

interface TrustedEndpoint {
  port: number
  certificatePem: string
  certificateFingerprint: Buffer
  launchId: string
  secret: Buffer
  generation: number
}

interface BackendChild {
  process: ChildProcess
  generation: number
  stopRequested: boolean
  transportFenced: boolean
  exited: boolean
  exitCode: number | null
  exitSignal: NodeJS.Signals | null
  exitPromise: Promise<void>
  resolveExit: () => void
  termination: Promise<void> | null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function jsonFrame(value: Record<string, unknown>): Buffer {
  const payload = Buffer.from(JSON.stringify(value), 'utf8')
  if (payload.length < 1 || payload.length > CONTROL_FRAME_MAX_BYTES) {
    throw new Error('invalid desktop control frame length')
  }
  return payload
}

function writeControlFrame(stream: NodeJS.WritableStream, value: Record<string, unknown>): void {
  const payload = jsonFrame(value)
  const prefix = Buffer.allocUnsafe(4)
  prefix.writeUInt32BE(payload.length, 0)
  if (!stream.write(Buffer.concat([prefix, payload]))) {
    // The frame is <= 4 KiB, so pipe backpressure is transient. The child is
    // already listening on the other end; keeping the write queued is safe.
  }
}

function readControlFrame(stream: NodeJS.ReadableStream, timeoutMs: number): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0)
    let settled = false
    const finish = (callback: () => void) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      stream.removeListener('data', onData)
      stream.removeListener('error', onError)
      stream.removeListener('end', onEnd)
      stream.removeListener('close', onEnd)
      callback()
    }
    const fail = (error: Error) => finish(() => reject(error))
    const onError = (error: Error) => fail(error)
    const onEnd = () => fail(new Error('desktop control pipe closed before ready'))
    const onData = (chunk: Buffer | string) => {
      buffer = Buffer.concat([buffer, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)])
      if (buffer.length < 4) return
      const length = buffer.readUInt32BE(0)
      if (length < 1 || length > CONTROL_FRAME_MAX_BYTES) {
        fail(new Error('invalid desktop control frame length'))
        return
      }
      if (buffer.length < length + 4) return
      if (buffer.length !== length + 4) {
        fail(new Error('unexpected extra desktop control data'))
        return
      }
      try {
        const raw = buffer.subarray(4).toString('utf8')
        const value: unknown = JSON.parse(raw)
        if (!isRecord(value) || JSON.stringify(value) !== raw) throw new Error('noncanonical desktop control frame')
        finish(() => resolve(value))
      } catch {
        fail(new Error('invalid desktop control frame'))
      }
    }
    const timer = setTimeout(() => fail(new Error('desktop backend bootstrap timed out')), timeoutMs)
    stream.on('data', onData)
    stream.once('error', onError)
    stream.once('end', onEnd)
    stream.once('close', onEnd)
  })
}

function secureEqualHex(actual: string, expected: string): boolean {
  if (!/^[0-9a-f]{64}$/.test(actual) || !/^[0-9a-f]{64}$/.test(expected)) return false
  return crypto.timingSafeEqual(Buffer.from(actual, 'hex'), Buffer.from(expected, 'hex'))
}

function validateReadyFrame(
  value: Record<string, unknown>,
  expectedLaunchId: string,
  secret: Buffer,
): { port: number; certificatePem: string } {
  const keys = Object.keys(value).sort()
  const expectedKeys = ['certificate', 'launch_id', 'port', 'proof', 'type']
  if (keys.length !== expectedKeys.length || keys.some((key, index) => key !== expectedKeys[index])) {
    throw new Error('invalid desktop ready schema')
  }
  const launchId = value.launch_id
  const port = value.port
  const certificatePem = value.certificate
  const proof = value.proof
  if (
    value.type !== 'ready' ||
    typeof launchId !== 'string' ||
    typeof port !== 'number' ||
    !Number.isInteger(port) ||
    port < 1 ||
    port > 65535 ||
    typeof certificatePem !== 'string' ||
    certificatePem.length < 64 ||
    certificatePem.length > CONTROL_FRAME_MAX_BYTES ||
    !certificatePem.startsWith('-----BEGIN CERTIFICATE-----') ||
    !certificatePem.trimEnd().endsWith('-----END CERTIFICATE-----') ||
    typeof proof !== 'string' ||
    !crypto.timingSafeEqual(Buffer.from(launchId), Buffer.from(expectedLaunchId))
  ) {
    throw new Error('invalid desktop ready frame')
  }
  const proofInput = JSON.stringify({
    certificate_sha256: crypto.createHash('sha256').update(certificatePem, 'utf8').digest('hex'),
    launch_id: expectedLaunchId,
    port,
    type: 'ready',
  })
  const expectedProof = crypto.createHmac('sha256', secret).update(proofInput, 'utf8').digest('hex')
  if (!secureEqualHex(proof, expectedProof)) throw new Error('desktop ready proof verification failed')
  return { port, certificatePem }
}

function normalizeOriginForm(rawTarget: string): string {
  if (
    typeof rawTarget !== 'string' ||
    rawTarget.length < 1 ||
    rawTarget.length > MAX_ORIGIN_FORM_BYTES ||
    !rawTarget.startsWith('/') ||
    rawTarget.startsWith('//') ||
    rawTarget.endsWith('?') ||
    rawTarget.includes('#') ||
    /[^\x21-\x7e]/.test(rawTarget)
  ) {
    throw new Error('invalid backend path')
  }
  for (let index = 0; index < rawTarget.length; index += 1) {
    if (rawTarget[index] === '%') {
      const escape = rawTarget.slice(index + 1, index + 3)
      if (!/^[0-9A-Fa-f]{2}$/.test(escape)) throw new Error('invalid backend path encoding')
      index += 2
    }
  }
  try {
    // Keep the exact ASCII spelling for HMAC and the request line, but reject
    // percent escapes that cannot represent one UTF-8 URI. This matches the
    // backend's raw REQUEST_URI validation without decoding either side.
    decodeURIComponent(rawTarget)
  } catch {
    throw new Error('invalid backend path encoding')
  }
  return rawTarget
}

function canonicalRequestMac(
  method: string,
  pathAndQuery: string,
  body: Buffer,
  timestamp: number,
  nonce: string,
  endpoint: TrustedEndpoint,
): string {
  const question = pathAndQuery.indexOf('?')
  const pathPart = question === -1 ? pathAndQuery : pathAndQuery.slice(0, question)
  const queryPart = question === -1 ? '' : pathAndQuery.slice(question + 1)
  // This key order matches desktop_protocol._canonical_json(sort_keys=True).
  const payload = JSON.stringify({
    body_sha256: crypto.createHash('sha256').update(body).digest('hex'),
    launch_id: endpoint.launchId,
    method,
    nonce,
    path: pathPart,
    query: queryPart,
    timestamp,
    version: 1,
  })
  return crypto.createHmac('sha256', endpoint.secret).update(payload, 'utf8').digest('hex')
}

class SseFrameParser {
  private decoder = new StringDecoder('utf8')
  private pending = ''
  private dataLines: string[] = []
  private eventId: string | undefined

  constructor(private readonly emit: (data: string, eventId?: string) => void) {}

  push(chunk: Buffer): void {
    this.pending += this.decoder.write(chunk)
    if (Buffer.byteLength(this.pending, 'utf8') > MAX_SSE_EVENT_BYTES) throw new Error('SSE frame too large')
    let newline = this.pending.indexOf('\n')
    while (newline !== -1) {
      const rawLine = this.pending.slice(0, newline)
      this.pending = this.pending.slice(newline + 1)
      this.consumeLine(rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine)
      newline = this.pending.indexOf('\n')
    }
  }

  finish(): void {
    const tail = this.decoder.end()
    if (tail) this.pending += tail
    if (this.pending) this.consumeLine(this.pending.endsWith('\r') ? this.pending.slice(0, -1) : this.pending)
    this.pending = ''
    this.dispatch()
  }

  private consumeLine(line: string): void {
    if (line === '') {
      this.dispatch()
      return
    }
    if (line.startsWith(':')) return
    const separator = line.indexOf(':')
    const field = separator === -1 ? line : line.slice(0, separator)
    const value = separator === -1 ? '' : line.slice(separator + 1).replace(/^ /, '')
    if (field === 'data') {
      this.dataLines.push(value)
      if (Buffer.byteLength(this.dataLines.join('\n'), 'utf8') > MAX_SSE_EVENT_BYTES) {
        throw new Error('SSE event too large')
      }
    } else if (field === 'id' && !value.includes('\u0000')) {
      this.eventId = value
    }
  }

  private dispatch(): void {
    if (!this.dataLines.length) return
    const data = this.dataLines.join('\n')
    this.dataLines = []
    this.emit(data, this.eventId)
  }
}

export class PythonBackend extends EventEmitter {
  private process: ChildProcess | null = null
  private activeChild: BackendChild | null = null
  private backendPath: string
  private status: 'stopped' | 'starting' | 'stopping' | 'ready' | 'error' = 'stopped'
  private endpoint: TrustedEndpoint | null = null
  private generation = 0
  private resolvedPath: string | null = null
  private readonly startupTimeoutMs: number
  private readonly shutdownTimeoutMs: number

  constructor(backendPath: string, options: PythonBackendOptions = {}) {
    super()
    this.backendPath = backendPath
    const requestedTimeout = options.startupTimeoutMs ?? STARTUP_TIMEOUT_MS
    this.startupTimeoutMs = Number.isInteger(requestedTimeout)
      ? Math.min(Math.max(requestedTimeout, 1_000), STARTUP_TIMEOUT_MS)
      : STARTUP_TIMEOUT_MS
    const requestedShutdownTimeout = options.shutdownTimeoutMs ?? SHUTDOWN_TIMEOUT_MS
    this.shutdownTimeoutMs = Number.isInteger(requestedShutdownTimeout)
      ? Math.min(Math.max(requestedShutdownTimeout, 50), SHUTDOWN_TIMEOUT_MS)
      : SHUTDOWN_TIMEOUT_MS
  }

  getStatus(): string {
    return this.status
  }

  getGeneration(): number {
    return this.generation
  }

  private clearEndpoint(): void {
    if (this.endpoint) this.endpoint.secret.fill(0)
    this.endpoint = null
  }

  private isCurrent(child: BackendChild): boolean {
    return this.activeChild === child && this.process === child.process && this.generation === child.generation
  }

  private clearEndpointFor(child: BackendChild): void {
    if (this.isCurrent(child) && this.endpoint?.generation === child.generation) this.clearEndpoint()
  }

  private createChild(process: ChildProcess, generation: number): BackendChild {
    let resolveExit: () => void = () => {}
    const child: BackendChild = {
      process,
      generation,
      stopRequested: false,
      transportFenced: false,
      exited: false,
      exitCode: null,
      exitSignal: null,
      exitPromise: new Promise<void>((resolve) => { resolveExit = resolve }),
      resolveExit: () => resolveExit(),
      termination: null,
    }
    return child
  }

  /**
   * Lifecycle failures are application events, not Node's unhandled-error
   * mechanism. A caller that has not subscribed must not crash Electron while
   * the backend is being fenced; it still receives the diagnostic via `log`.
   */
  private reportError(message: string): void {
    if (this.listenerCount('error') > 0) this.emit('error', message)
    else this.emit('log', `Backend error: ${message}`)
  }

  /**
   * A TLS/MAC channel error is not a recoverable renderer transport glitch:
   * after a child crash an attacker may have rebound the old loopback port.
   * Fence this generation before another request can carry a login password or
   * bearer, kill the stale child, and require an explicit fresh launch.
   */
  private fenceTransport(generation: number, error: Error): void {
    const child = this.activeChild
    if (!child || child.generation !== generation || !this.isCurrent(child) || child.stopRequested || child.transportFenced) return
    child.transportFenced = true
    this.clearEndpointFor(child)
    this.status = 'error'
    this.reportError(`Trusted backend transport failed: ${error.message}`)
    void this.terminateChild(child, 'transport fence').catch((terminationError) => {
      if (this.isCurrent(child)) this.reportError(`Trusted backend fence could not confirm child exit: ${String(terminationError)}`)
    })
  }

  /** Build the PATH the backend should run with. */
  private resolveEnvPath(): string {
    if (this.resolvedPath !== null) return this.resolvedPath
    const sep = path.delimiter
    const existing = process.env.PATH || ''
    const parts: string[] = existing ? existing.split(sep) : []
    const rgDir = path.join(path.dirname(this.backendPath), 'bin')
    const rgExe = process.platform === 'win32' ? 'rg.exe' : 'rg'
    if (fs.existsSync(path.join(rgDir, rgExe))) parts.unshift(rgDir)
    if (process.platform !== 'win32') {
      try {
        const shell = process.env.SHELL || '/bin/zsh'
        const out = execFileSync(shell, ['-ilc', 'echo -n "__PATH__$PATH"'], {
          encoding: 'utf8', timeout: 5000, stdio: ['ignore', 'pipe', 'ignore'],
        })
        const marker = out.lastIndexOf('__PATH__')
        if (marker !== -1) {
          const shellPath = out.slice(marker + '__PATH__'.length).trim()
          if (shellPath) parts.push(...shellPath.split(sep))
        }
      } catch { /* fall through to common paths */ }
      const home = os.homedir()
      parts.push(path.join(home, '.local/bin'), '/usr/local/bin', '/opt/homebrew/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin')
    }
    const seen = new Set<string>()
    this.resolvedPath = parts.filter((item) => {
      const value = item.trim()
      if (!value || seen.has(value)) return false
      seen.add(value)
      return true
    }).join(sep)
    return this.resolvedPath
  }

  private findBundledBackend(): string | null {
    const exeName = process.platform === 'win32' ? 'smart-assistant-backend.exe' : 'smart-assistant-backend'
    for (const candidate of [
      path.join(this.backendPath, 'smart-assistant-backend', exeName),
      path.join(this.backendPath, exeName),
    ]) {
      if (fs.existsSync(candidate)) return candidate
    }
    return null
  }

  private findPython(): string {
    for (const candidate of [
      path.join(this.backendPath, '.venv', 'bin', 'python'),
      path.join(this.backendPath, '.venv', 'Scripts', 'python.exe'),
      path.join(this.backendPath, 'venv', 'bin', 'python'),
      path.join(this.backendPath, 'venv', 'Scripts', 'python.exe'),
    ]) {
      if (fs.existsSync(candidate)) return candidate
    }
    return process.platform === 'win32' ? 'python' : 'python3'
  }

  async start(): Promise<void> {
    if (this.status === 'ready' || this.status === 'starting') return
    const previousChild = this.activeChild
    if (previousChild) {
      try {
        await this.stopChild(previousChild, 'start replacement')
      } catch (error) {
        if (this.isCurrent(previousChild)) {
          this.clearEndpointFor(previousChild)
          this.status = 'error'
          this.reportError(`Previous backend did not exit: ${String(error)}`)
        }
        return
      }
    }
    const generation = this.generation + 1
    this.generation = generation
    this.status = 'starting'
    this.clearEndpoint()
    this.emit('starting', { generation })

    const bundled = this.findBundledBackend()
    const dataDir = bundled ? COW_DATA_DIR : this.backendPath
    let command: string
    let args: string[]
    let cwd: string
    if (bundled) {
      command = bundled
      args = []
      try { fs.mkdirSync(COW_DATA_DIR, { recursive: true }) } catch { /* Python also ensures it */ }
      cwd = COW_DATA_DIR
      this.emit('log', `Starting bundled backend: ${bundled} (cwd=${cwd})`)
    } else {
      command = this.findPython()
      const appPath = path.join(this.backendPath, 'app.py')
      if (!fs.existsSync(appPath)) {
        this.status = 'error'
        this.reportError(`app.py not found at ${appPath}`)
        return
      }
      args = [appPath]
      cwd = this.backendPath
      this.emit('log', `Starting Python backend: ${command} ${appPath}`)
    }

    const launchId = crypto.randomBytes(24).toString('base64url')
    const secret = crypto.randomBytes(32)
    let childProcess: ChildProcess
    try {
      childProcess = spawn(command, args, {
        cwd,
        env: {
          ...process.env,
          PATH: this.resolveEnvPath(),
          PYTHONUNBUFFERED: '1',
          COW_DESKTOP: '1',
          // Port 0 is atomically allocated by the kernel. Its value never enters
          // renderer state; the parent receives it only over fd 3 with a proof.
          COW_WEB_PORT: '0',
          COW_DESKTOP_CONTROL_FD: String(DESKTOP_CONTROL_FD),
          ...(bundled ? { COW_DATA_DIR } : {}),
        },
        stdio: ['pipe', 'pipe', 'pipe', 'pipe'],
      })
    } catch (error) {
      this.status = 'error'
      this.reportError(`Failed to start Python: ${error instanceof Error ? error.message : String(error)}`)
      return
    }
    const child = this.createChild(childProcess, generation)
    this.process = childProcess
    this.activeChild = child

    childProcess.stdout?.on('data', (data: Buffer) => {
      for (const line of data.toString().split('\n').filter(Boolean)) this.emit('log', line)
    })
    childProcess.stderr?.on('data', (data: Buffer) => {
      for (const line of data.toString().split('\n').filter(Boolean)) this.emit('log', line)
    })
    childProcess.on('exit', (code, signal) => this.handleExit(child, code, signal))
    childProcess.on('error', (error) => {
      if (this.isCurrent(child)) void this.failStartup(child, new Error(`Failed to start Python: ${error.message}`))
    })

    const control = childProcess.stdio[DESKTOP_CONTROL_FD] as unknown as NodeJS.ReadWriteStream | null
    if (!control) {
      await this.failStartup(child, new Error('desktop control pipe is unavailable'))
      return
    }
    try {
      const readyFrame = readControlFrame(control, this.startupTimeoutMs)
      writeControlFrame(control, { launch_id: launchId, secret: secret.toString('base64url'), type: 'bootstrap' })
      // Keep the duplex control handle open until the child exits. On Windows
      // ending the writable half can surface a `close` event on the shared
      // PipeWrap before the child-to-parent ready frame is delivered.
      const ready = validateReadyFrame(await readyFrame, launchId, secret)
      if (!this.isCurrent(child) || child.transportFenced || child.stopRequested) throw new Error('desktop backend startup was superseded')
      const certificateFingerprint = new crypto.X509Certificate(ready.certificatePem).raw
      this.endpoint = {
        port: ready.port,
        certificatePem: ready.certificatePem,
        certificateFingerprint: crypto.createHash('sha256').update(certificateFingerprint).digest(),
        launchId,
        secret,
        generation,
      }
      await this.waitForTrustedReady(child)
      if (!this.isCurrent(child) || child.transportFenced || child.stopRequested) throw new Error('desktop backend startup was superseded')
      this.status = 'ready'
      this.emit('log', 'Backend ready on a pinned private desktop channel')
      this.emit('ready', { generation })
    } catch (error) {
      await this.failStartup(child, error instanceof Error ? error : new Error('desktop backend startup failed'))
    }
  }

  private async failStartup(child: BackendChild, error: Error): Promise<void> {
    if (!this.isCurrent(child)) return
    this.clearEndpointFor(child)
    this.status = 'error'
    this.reportError(error.message)
    try {
      await this.terminateChild(child, 'startup failure')
    } catch (terminationError) {
      if (this.isCurrent(child)) this.reportError(`Failed to confirm backend startup child exit: ${String(terminationError)}`)
    }
  }

  private childHasExited(child: BackendChild): boolean {
    if (!child.exited && (child.process.exitCode !== null || child.process.signalCode !== null)) {
      this.handleExit(child, child.process.exitCode, child.process.signalCode)
    }
    return child.exited
  }

  private handleExit(child: BackendChild, code: number | null, signal: NodeJS.Signals | null): void {
    if (child.exited) return
    child.exited = true
    child.exitCode = code
    child.exitSignal = signal
    child.resolveExit()
    // A delayed exit belongs to the process instance that emitted it.  It must
    // never clear a newer generation's endpoint, secret, or public state.
    if (!this.isCurrent(child)) return
    const previous = this.status
    this.clearEndpointFor(child)
    this.process = null
    this.activeChild = null
    this.status = 'stopped'
    const outcome = signal ? `signal ${signal}` : `code ${code}`
    this.emit('log', `Python process exited with ${outcome}`)
    if (!child.stopRequested && !child.transportFenced && previous === 'ready') {
      this.reportError(`Backend exited unexpectedly (${outcome})`)
    } else if (!child.stopRequested && !child.transportFenced && previous === 'starting') {
      this.reportError(`Backend exited during startup (${outcome})`)
    }
    this.emit('stopped', { generation: child.generation, unexpected: !child.stopRequested && !child.transportFenced })
  }

  private async waitForChildExit(child: BackendChild, timeoutMs: number): Promise<boolean> {
    if (this.childHasExited(child)) return true
    return new Promise((resolve) => {
      const timer = setTimeout(() => resolve(this.childHasExited(child)), timeoutMs)
      child.exitPromise.then(() => {
        clearTimeout(timer)
        resolve(true)
      })
    })
  }

  private async terminateChild(child: BackendChild, reason: string): Promise<void> {
    if (child.termination) return child.termination
    child.stopRequested = true
    child.termination = (async () => {
      if (this.childHasExited(child)) return
      try { child.process.kill('SIGTERM') } catch { /* exit races are confirmed below */ }
      if (await this.waitForChildExit(child, this.shutdownTimeoutMs)) return
      // ChildProcess.killed only means a signal was sent.  It is not evidence
      // of exit, so check exitCode/signalCode/event before and after SIGKILL.
      if (this.childHasExited(child)) return
      this.emit('log', `Backend generation ${child.generation} did not exit after ${reason}; force killing it`)
      try { child.process.kill('SIGKILL') } catch { /* exit races are confirmed below */ }
      if (await this.waitForChildExit(child, this.shutdownTimeoutMs)) return
      throw new Error(`backend generation ${child.generation} did not exit after forced termination`)
    })()
    return child.termination
  }

  private async stopChild(child: BackendChild, reason: string): Promise<void> {
    if (this.isCurrent(child)) {
      this.clearEndpointFor(child)
      this.status = 'stopping'
    }
    await this.terminateChild(child, reason)
  }

  private async waitForTrustedReady(child: BackendChild): Promise<void> {
    const deadline = Date.now() + this.startupTimeoutMs
    let lastError: Error | null = null
    while (Date.now() < deadline && this.isCurrent(child) && this.status === 'starting') {
      try {
        const response = await this.sendRequest({ path: '/api/health', method: 'GET', timeoutMs: 2000 }, true)
        const body = JSON.parse(response.body.toString('utf8')) as { status?: unknown }
        if (response.status === 200 && body.status === 'ok') return
        lastError = new Error('trusted health response was invalid')
      } catch (error) {
        lastError = error instanceof Error ? error : new Error('trusted health probe failed')
      }
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    throw lastError || new Error('backend failed to become ready')
  }

  private getEndpoint(allowStarting: boolean): TrustedEndpoint {
    if (!this.endpoint || (this.status !== 'ready' && !(allowStarting && this.status === 'starting'))) {
      throw new Error('trusted backend is unavailable')
    }
    if (!this.activeChild || this.endpoint.generation !== this.generation || !this.isCurrent(this.activeChild)) {
      throw new Error('trusted backend is unavailable')
    }
    return this.endpoint
  }

  private buildRequestOptions(input: TrustedBackendRequest, allowStarting: boolean) {
    const endpoint = this.getEndpoint(allowStarting)
    const method = String(input.method || 'GET').toUpperCase()
    if (!/^[A-Z]{1,16}$/.test(method)) throw new Error('invalid backend method')
    const requestPath = normalizeOriginForm(input.path)
    const body = Buffer.isBuffer(input.body)
      ? input.body
      : input.body instanceof Uint8Array
        ? Buffer.from(input.body)
        : typeof input.body === 'string'
          ? Buffer.from(input.body, 'utf8')
          : Buffer.alloc(0)
    if (body.length > MAX_REQUEST_BYTES) throw new Error('desktop request body exceeds limit')

    const headers: Record<string, string> = {}
    for (const [name, value] of Object.entries(input.headers || {})) {
      const normalized = name.toLowerCase()
      if (
        !/^[A-Za-z0-9-]{1,128}$/.test(name) ||
        typeof value !== 'string' ||
        value.length > 8192 ||
        /[\r\n]/.test(value) ||
        ['host', 'cookie', 'content-length', 'connection', 'transfer-encoding'].includes(normalized) ||
        normalized.startsWith('x-cow-desktop-')
      ) {
        throw new Error('forbidden backend request header')
      }
      headers[name] = value
    }
    const timestamp = Math.floor(Date.now() / 1000)
    const nonce = crypto.randomBytes(24).toString('base64url')
    headers['Content-Length'] = String(body.length)
    headers['X-Cow-Desktop-Launch-Id'] = endpoint.launchId
    headers['X-Cow-Desktop-Timestamp'] = String(timestamp)
    headers['X-Cow-Desktop-Nonce'] = nonce
    headers['X-Cow-Desktop-Mac'] = canonicalRequestMac(method, requestPath, body, timestamp, nonce, endpoint)

    return {
      endpoint,
      body,
      options: {
        protocol: 'https:',
        hostname: '127.0.0.1',
        port: endpoint.port,
        method,
        path: requestPath,
        headers,
        ca: endpoint.certificatePem,
        rejectUnauthorized: true,
        servername: 'localhost',
        agent: false,
        checkServerIdentity: (_hostname, certificate) => {
          if (!certificate.raw) return new Error('backend certificate is missing')
          const actual = crypto.createHash('sha256').update(certificate.raw).digest()
          return crypto.timingSafeEqual(actual, endpoint.certificateFingerprint)
            ? undefined
            : new Error('backend certificate pin mismatch')
        },
      } satisfies https.RequestOptions,
    }
  }

  private sendRequest(input: TrustedBackendRequest, allowStarting = false): Promise<TrustedBackendResponse> {
    const { endpoint, body, options } = this.buildRequestOptions(input, allowStarting)
    const timeoutMs = input.timeoutMs ?? 30_000
    return new Promise((resolve, reject) => {
      let settled = false
      const settle = (callback: () => void) => {
        if (settled) return
        settled = true
        callback()
      }
      const request = https.request(options, (response) => {
        const chunks: Buffer[] = []
        let size = 0
        response.on('data', (chunk: Buffer) => {
          size += chunk.length
          if (size > MAX_RESPONSE_BYTES) {
            request.destroy(new Error('desktop response exceeds limit'))
            return
          }
          chunks.push(Buffer.from(chunk))
        })
        response.on('end', () => {
          const headers: Record<string, string> = {}
          for (const [name, value] of Object.entries(response.headers)) {
            headers[name] = Array.isArray(value) ? value.join(', ') : String(value ?? '')
          }
          settle(() => resolve({
            status: response.statusCode || 0,
            statusText: response.statusMessage || '',
            headers,
            body: Buffer.concat(chunks),
          }))
        })
        response.on('error', (error) => settle(() => {
          this.fenceTransport(endpoint.generation, error)
          reject(error)
        }))
      })
      request.setTimeout(timeoutMs, () => request.destroy(new Error('desktop backend request timed out')))
      request.on('error', (error) => settle(() => {
        this.fenceTransport(endpoint.generation, error)
        reject(error)
      }))
      request.write(body)
      request.end()
    })
  }

  async request(input: TrustedBackendRequest): Promise<TrustedBackendResponse> {
    return this.sendRequest(input)
  }

  async openSse(
    path: string,
    handlers: {
      message: (data: string, eventId?: string) => void
      error: (error: Error) => void
      closed: () => void
    },
  ): Promise<TrustedBackendStream> {
    const { endpoint, body, options } = this.buildRequestOptions({ path, method: 'GET', timeoutMs: 30_000 }, false)
    return new Promise((resolve, reject) => {
      let explicitClose = false
      let settled = false
      const request = https.request(options, (response) => {
        if ((response.statusCode || 0) < 200 || (response.statusCode || 0) >= 300) {
          response.resume()
          if (!settled) {
            settled = true
            reject(new Error(`desktop SSE request failed with HTTP ${response.statusCode || 0}`))
          }
          return
        }
        const parser = new SseFrameParser(handlers.message)
        response.on('data', (chunk: Buffer) => {
          try {
            parser.push(Buffer.from(chunk))
          } catch (error) {
            request.destroy(error instanceof Error ? error : new Error('invalid SSE response'))
          }
        })
        response.on('error', (error) => {
          if (!explicitClose) {
            this.fenceTransport(endpoint.generation, error)
            handlers.error(error)
          }
        })
        response.on('end', () => {
          try { parser.finish() } catch { /* terminating stream is already an error */ }
          if (!explicitClose) handlers.closed()
        })
        if (!settled) {
          settled = true
          resolve({
            close: () => {
              explicitClose = true
              request.destroy()
            },
          })
        }
      })
      request.setTimeout(30_000, () => request.destroy(new Error('desktop SSE connection timed out')))
      request.on('error', (error) => {
        if (!settled) {
          settled = true
          this.fenceTransport(endpoint.generation, error)
          reject(error)
        } else if (!explicitClose) {
          this.fenceTransport(endpoint.generation, error)
          handlers.error(error)
        }
      })
      request.write(body)
      request.end()
    })
  }

  async stop(): Promise<void> {
    const child = this.activeChild
    if (!child) {
      this.clearEndpoint()
      this.status = 'stopped'
      return
    }
    try {
      await this.stopChild(child, 'stop')
    } catch (error) {
      if (this.isCurrent(child)) {
        this.clearEndpointFor(child)
        this.status = 'error'
        this.reportError(`Failed to stop backend: ${String(error)}`)
      }
      throw error
    }
  }

  async restart(): Promise<void> {
    // Do not replace a child after merely sending it a signal.  A delayed old
    // exit callback would otherwise erase the new generation's capability.
    await this.stop()
    await this.start()
  }
}
