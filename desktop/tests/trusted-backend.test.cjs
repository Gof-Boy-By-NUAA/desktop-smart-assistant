const assert = require('node:assert/strict')
const fs = require('node:fs')
const fsp = require('node:fs/promises')
const http = require('node:http')
const https = require('node:https')
const { EventEmitter } = require('node:events')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { PythonBackend } = require('../dist/main/python-manager.js')
const REPO_ROOT = path.resolve(__dirname, '..', '..')

function waitFor(check, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve, reject) => {
    const tick = () => {
      try {
        const value = check()
        if (value) return resolve(value)
      } catch (error) {
        return reject(error)
      }
      if (Date.now() >= deadline) return reject(new Error('timed out waiting for condition'))
      setTimeout(tick, 50)
    }
    tick()
  })
}

function listen(server, port) {
  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(port, '127.0.0.1', () => {
      server.removeListener('error', reject)
      resolve()
    })
  })
}

function close(server) {
  return new Promise((resolve) => server.close(() => resolve()))
}

async function removeTemp(directory) {
  let lastError
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      await fsp.rm(directory, { recursive: true, force: true })
      return
    } catch (error) {
      lastError = error
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
  }
  throw lastError
}

const FAKE_BACKEND = String.raw`
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, ${JSON.stringify(REPO_ROOT)})
from cheroot.ssl.builtin import BuiltinSSLAdapter
from cheroot import wsgi
from channel.web.desktop_protocol import (
    DesktopRequestAuthMiddleware,
    create_ephemeral_tls_material,
    make_ready_frame,
    read_bootstrap_credentials,
    read_control_frame,
    write_control_frame,
)

def app(environ, start_response):
    request_marker = os.environ.get('COW_TRUSTED_TEST_REQUEST_FILE')
    if request_marker:
        Path(request_marker).write_text('received', encoding='ascii')
    if environ.get('PATH_INFO', '').startswith('/preview/'):
        payload = json.dumps({'status': 'ok', 'request_uri': environ.get('REQUEST_URI')}, ensure_ascii=True).encode('ascii')
    else:
        payload = b'{"status":"ok"}'
    start_response('200 OK', [('Content-Type', 'application/json'), ('Content-Length', str(len(payload)))])
    return [payload]

fd = int(os.environ['COW_DESKTOP_CONTROL_FD'])
launch_id, secret = read_bootstrap_credentials(read_control_frame(fd))
material = create_ephemeral_tls_material(launch_id)
server = wsgi.Server(('127.0.0.1', 0), DesktopRequestAuthMiddleware(app, launch_id, secret), server_name='localhost')
server.ssl_adapter = BuiltinSSLAdapter(material.certificate_path, material.private_key_path)
material.cleanup()
server.prepare()
port = int(server.socket.getsockname()[1])
if os.environ.get('COW_TRUSTED_TEST_PORT_FILE'):
    Path(os.environ['COW_TRUSTED_TEST_PORT_FILE']).write_text(str(port), encoding='ascii')
if os.environ.get('COW_TRUSTED_TEST_CERT_FILE'):
    Path(os.environ['COW_TRUSTED_TEST_CERT_FILE']).write_text(material.certificate_pem, encoding='ascii')
control_certificate = material.certificate_pem
if os.environ.get('COW_TRUSTED_TEST_WRONG_CERT') == '1':
    wrong_material = create_ephemeral_tls_material(launch_id)
    control_certificate = wrong_material.certificate_pem
    wrong_material.cleanup()
write_control_frame(fd, make_ready_frame(launch_id, secret, port, control_certificate))
server.serve()
`

test('trusted Python backend rejects direct callers and does not reuse a stopped endpoint', async (t) => {
  const temp = await fsp.mkdtemp(path.join(os.tmpdir(), 'smart-assistant-trusted-backend-'))
  const appPath = path.join(temp, 'app.py')
  const portFile = path.join(temp, 'port')
  const certFile = path.join(temp, 'cert.pem')
  await fsp.writeFile(appPath, FAKE_BACKEND, 'utf8')
  const previousPortFile = process.env.COW_TRUSTED_TEST_PORT_FILE
  const previousCertFile = process.env.COW_TRUSTED_TEST_CERT_FILE
  process.env.COW_TRUSTED_TEST_PORT_FILE = portFile
  process.env.COW_TRUSTED_TEST_CERT_FILE = certFile

  const backend = new PythonBackend(temp)
  let stopped = false
  const backendErrors = []
  backend.on('error', (error) => { backendErrors.push(String(error)) })
  backend.on('log', (line) => { process.stderr.write(`[fake-backend] ${line}\n`) })
  backend.on('stopped', () => { stopped = true })
  try {
    await backend.start()
    assert.deepEqual(backendErrors, [])
    assert.equal(backend.getStatus(), 'ready')
    const port = Number(await waitFor(() => fs.existsSync(portFile) ? fs.readFileSync(portFile, 'utf8') : ''))
    const certificate = await fsp.readFile(certFile, 'utf8')

    const unauthenticated = await new Promise((resolve, reject) => {
      const request = https.request({
        hostname: '127.0.0.1', port, path: '/api/health',
        ca: certificate, rejectUnauthorized: true, servername: 'localhost',
      }, (response) => { response.resume(); response.on('end', () => resolve(response.statusCode)) })
      request.once('error', reject)
      request.end()
    })
    assert.equal(unauthenticated, 401)

    const trusted = await backend.request({ path: '/api/health', method: 'GET' })
    assert.equal(trusted.status, 200)
    assert.deepEqual(JSON.parse(trusted.body.toString('utf8')), { status: 'ok' })

    const rawTarget = '/preview/%E4%B8%AD%E6%96%87%20file.txt?name=%E4%B8%AD%E6%96%87%20file.txt'
    const encodedPreview = await backend.request({ path: rawTarget, method: 'GET' })
    assert.equal(encodedPreview.status, 200)
    assert.equal(JSON.parse(encodedPreview.body.toString('utf8')).request_uri, rawTarget)

    // Reuse headers signed for the original raw target, but change the actual
    // HTTPS request line. Cheroot must verify REQUEST_URI rather than decoded
    // PATH_INFO and reject this before the app sees it.
    const signed = backend.buildRequestOptions({ path: rawTarget, method: 'GET' }, false)
    const tamperedStatus = await new Promise((resolve, reject) => {
      const request = https.request({ ...signed.options, path: '/preview/%E4%B8%AD%E6%96%87%20other.txt?name=%E4%B8%AD%E6%96%87%20file.txt' }, (response) => {
        response.resume()
        response.on('end', () => resolve(response.statusCode))
      })
      request.once('error', reject)
      request.end()
    })
    assert.equal(tamperedStatus, 401)

    backend.stop()
    await waitFor(() => stopped && backend.getStatus() === 'stopped')
    let attackerRequests = 0
    const attacker = http.createServer((_request, response) => {
      attackerRequests += 1
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end('{"status":"ok"}')
    })
    await listen(attacker, port)
    try {
      await assert.rejects(() => backend.request({ path: '/api/health', method: 'GET' }), /unavailable/)
      assert.equal(attackerRequests, 0)
    } finally {
      await close(attacker)
    }
  } finally {
    backend.stop()
    if (previousPortFile === undefined) delete process.env.COW_TRUSTED_TEST_PORT_FILE
    else process.env.COW_TRUSTED_TEST_PORT_FILE = previousPortFile
    if (previousCertFile === undefined) delete process.env.COW_TRUSTED_TEST_CERT_FILE
    else process.env.COW_TRUSTED_TEST_CERT_FILE = previousCertFile
    await removeTemp(temp)
  }
})

function delayedExitChild(delayMs, forceExit = false) {
  const child = new EventEmitter()
  child.exitCode = null
  child.signalCode = null
  child.killed = false
  child.kill = (signal) => {
    if (signal === 'SIGTERM' && !forceExit) {
      setTimeout(() => {
        child.signalCode = 'SIGTERM'
        child.emit('exit', null, 'SIGTERM')
      }, delayMs)
    } else if (signal === 'SIGKILL') {
      setTimeout(() => {
        child.signalCode = 'SIGKILL'
        child.emit('exit', null, 'SIGKILL')
      }, 0)
    }
    child.killed = true
    return true
  }
  return child
}

test('restart waits for the exact old child exit and old generation cannot clear the replacement state', async () => {
  const temp = await fsp.mkdtemp(path.join(os.tmpdir(), 'smart-assistant-restart-race-'))
  const appPath = path.join(temp, 'app.py')
  await fsp.writeFile(appPath, FAKE_BACKEND, 'utf8')
  const backend = new PythonBackend(temp, { shutdownTimeoutMs: 1_500 })
  const oldProcess = delayedExitChild(700)
  const oldChild = backend.createChild(oldProcess, 1)
  backend.process = oldProcess
  backend.activeChild = oldChild
  backend.generation = 1
  backend.status = 'ready'
  backend.endpoint = {
    port: 1,
    certificatePem: 'old',
    certificateFingerprint: Buffer.alloc(32),
    launchId: 'desktop_launch_id_123456',
    secret: Buffer.alloc(32, 7),
    generation: 1,
  }
  oldProcess.on('exit', (code, signal) => backend.handleExit(oldChild, code, signal))
  const startedAt = Date.now()
  try {
    await backend.restart()
    assert.ok(Date.now() - startedAt >= 650, 'replacement started before old child actually exited')
    assert.equal(backend.getGeneration(), 2)
    assert.equal(backend.getStatus(), 'ready')
    assert.equal((await backend.request({ path: '/api/health', method: 'GET' })).status, 200)
    await new Promise((resolve) => setTimeout(resolve, 100))
    assert.equal(backend.getStatus(), 'ready')
    assert.equal((await backend.request({ path: '/api/health', method: 'GET' })).status, 200)
  } finally {
    await backend.stop().catch(() => {})
    await removeTemp(temp)
  }
})

test('forced termination is observable and does not treat proc.killed as exit proof', async () => {
  const backend = new PythonBackend(REPO_ROOT, { shutdownTimeoutMs: 50 })
  const stuckProcess = delayedExitChild(0, true)
  const stuckChild = backend.createChild(stuckProcess, 1)
  backend.process = stuckProcess
  backend.activeChild = stuckChild
  backend.generation = 1
  backend.status = 'ready'
  const logs = []
  backend.on('log', (line) => logs.push(String(line)))
  stuckProcess.on('exit', (code, signal) => backend.handleExit(stuckChild, code, signal))

  await backend.stop()

  assert.equal(backend.getStatus(), 'stopped')
  assert.ok(logs.some((line) => line.includes('force killing')), 'force-kill fallback was not observable')
  await assert.rejects(() => backend.request({ path: '/api/health', method: 'GET' }), /unavailable/)
})

test('trusted startup refuses a control-pipe certificate that does not own the TLS listener', async () => {
  const temp = await fsp.mkdtemp(path.join(os.tmpdir(), 'smart-assistant-trusted-mismatch-'))
  const appPath = path.join(temp, 'app.py')
  const portFile = path.join(temp, 'port')
  const certFile = path.join(temp, 'cert.pem')
  const requestFile = path.join(temp, 'request-observed')
  await fsp.writeFile(appPath, FAKE_BACKEND, 'utf8')
  const previous = {
    port: process.env.COW_TRUSTED_TEST_PORT_FILE,
    cert: process.env.COW_TRUSTED_TEST_CERT_FILE,
    request: process.env.COW_TRUSTED_TEST_REQUEST_FILE,
    wrong: process.env.COW_TRUSTED_TEST_WRONG_CERT,
  }
  process.env.COW_TRUSTED_TEST_PORT_FILE = portFile
  process.env.COW_TRUSTED_TEST_CERT_FILE = certFile
  process.env.COW_TRUSTED_TEST_REQUEST_FILE = requestFile
  process.env.COW_TRUSTED_TEST_WRONG_CERT = '1'
  const backend = new PythonBackend(temp, { startupTimeoutMs: 2_000 })
  const errors = []
  backend.on('error', (error) => { errors.push(String(error)) })
  try {
    await backend.start()
    assert.notEqual(backend.getStatus(), 'ready')
    assert.ok(errors.length > 0)
    assert.equal(fs.existsSync(requestFile), false)
  } finally {
    backend.stop()
    await waitFor(() => backend.getStatus() === 'stopped', 10_000).catch(() => {})
    for (const [name, value] of Object.entries(previous)) {
      const key = `COW_TRUSTED_TEST_${name === 'port' ? 'PORT_FILE' : name === 'cert' ? 'CERT_FILE' : name === 'request' ? 'REQUEST_FILE' : 'WRONG_CERT'}`
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
    await removeTemp(temp)
  }
})

test('the real SmartAssistant WebChannel starts only through the authenticated desktop transport', async () => {
  const dataRoot = await fsp.mkdtemp(path.join(os.tmpdir(), 'smart-assistant-real-desktop-'))
  const previous = {
    data: process.env.COW_DATA_DIR,
    channel: process.env.CHANNEL_TYPE,
  }
  process.env.COW_DATA_DIR = dataRoot
  process.env.CHANNEL_TYPE = 'web'
  const backend = new PythonBackend(REPO_ROOT, { startupTimeoutMs: 30_000 })
  const errors = []
  backend.on('error', (error) => { errors.push(String(error)) })
  try {
    await backend.start()
    assert.equal(backend.getStatus(), 'ready', errors.join('\n'))
    const health = await backend.request({ path: '/api/health', method: 'GET' })
    assert.equal(health.status, 200)
    assert.deepEqual(JSON.parse(health.body.toString('utf8')), { status: 'ok' })
  } finally {
    backend.stop()
    await waitFor(() => backend.getStatus() === 'stopped', 10_000).catch(() => {})
    if (previous.data === undefined) delete process.env.COW_DATA_DIR
    else process.env.COW_DATA_DIR = previous.data
    if (previous.channel === undefined) delete process.env.CHANNEL_TYPE
    else process.env.CHANNEL_TYPE = previous.channel
    await removeTemp(dataRoot)
  }
})
