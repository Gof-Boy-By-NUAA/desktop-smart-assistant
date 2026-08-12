const assert = require('node:assert/strict')
const fs = require('node:fs')
const fsp = require('node:fs/promises')
const Module = require('node:module')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

function loadMainHelpers() {
  const originalLoad = Module._load
  const electron = {
    app: {
      isPackaged: false,
      setName() {},
      requestSingleInstanceLock: () => true,
      on() {},
      whenReady: () => new Promise(() => {}),
      getPath: () => os.tmpdir(),
      getVersion: () => 'test',
      getLocale: () => 'en',
      getSystemLocale: () => 'en',
      quit() {},
    },
    BrowserWindow: class BrowserWindow {},
    shell: {},
    ipcMain: { handle() {}, on() {} },
    dialog: {},
    nativeImage: {},
    protocol: { registerSchemesAsPrivileged() {}, handle() {} },
    safeStorage: {},
    Menu: {},
    Tray: class Tray {},
    net: {},
  }
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === 'electron') return electron
    if (request === 'electron-updater') return { autoUpdater: {} }
    return originalLoad.call(this, request, parent, isMain)
  }
  try {
    return require('../dist/main/index.js')
  } finally {
    Module._load = originalLoad
  }
}

const { createDesktopStreamRegistry, createDesktopSubjectTokenStore } = loadMainHelpers()

test('encrypted subject token storage is atomic, reloadable, and erasable', async (t) => {
  const directory = await fsp.mkdtemp(path.join(os.tmpdir(), 'smart-assistant-subject-token-'))
  t.after(() => fsp.rm(directory, { recursive: true, force: true }))
  const reports = []
  const secureStorage = {
    isEncryptionAvailable: () => true,
    encryptString: (token) => Buffer.from(`encrypted:${Buffer.from(token, 'utf8').toString('base64')}`, 'utf8'),
    decryptString: (value) => Buffer.from(value.toString('utf8').slice('encrypted:'.length), 'base64').toString('utf8'),
  }
  const store = createDesktopSubjectTokenStore(directory, secureStorage, (message) => reports.push(message))

  await store.save('subject-one')
  const tokenPath = path.join(directory, 'desktop-subject-token.bin')
  assert.equal((await fsp.readFile(tokenPath, 'utf8')).includes('subject-one'), false)
  assert.equal(await store.load(), 'subject-one')

  await store.save('subject-two')
  assert.equal(await store.load(), 'subject-two')
  assert.deepEqual((await fsp.readdir(directory)).filter((name) => name.endsWith('.tmp')), [])
  assert.deepEqual(reports, [])

  await store.forget()
  await assert.rejects(() => fsp.access(tokenPath))
})

test('unavailable secure storage fails closed and reports an observable error', async (t) => {
  const directory = await fsp.mkdtemp(path.join(os.tmpdir(), 'smart-assistant-subject-unavailable-'))
  t.after(() => fsp.rm(directory, { recursive: true, force: true }))
  const reports = []
  const store = createDesktopSubjectTokenStore(directory, {
    isEncryptionAvailable: () => false,
    encryptString: () => { throw new Error('must not encrypt') },
    decryptString: () => { throw new Error('must not decrypt') },
  }, (message) => reports.push(message))

  assert.equal(await store.load(), null)
  await assert.rejects(() => store.save('subject-token'), /Secure storage is unavailable/)
  assert.equal(reports.length, 2)
  assert.ok(reports.every((message) => /Secure storage is unavailable/.test(message)))
})

test('opening SSE streams are cancellable and never reattach after cancellation or generation loss', () => {
  const registry = createDesktopStreamRegistry()
  const events = []
  const sender = { id: 17, isDestroyed: () => false, send: (_channel, payload) => events.push(payload) }
  let current = true
  const pending = registry.begin('17:stream', 'stream', sender, () => current)

  registry.close('17:stream')
  let lateCloseCount = 0
  assert.equal(registry.attach(pending, () => { lateCloseCount += 1 }), false)
  assert.equal(lateCloseCount, 1)
  assert.deepEqual(registry.counts(), { active: 0, opening: 0 })
  assert.deepEqual(events, [])

  const stale = registry.begin('17:stale', 'stale', sender, () => current)
  current = false
  let staleCloseCount = 0
  assert.equal(registry.attach(stale, () => { staleCloseCount += 1 }), false)
  assert.equal(staleCloseCount, 1)
  assert.deepEqual(registry.counts(), { active: 0, opening: 0 })
})

test('SSE terminal callbacks are emitted once and restart closes pending streams once', () => {
  const registry = createDesktopStreamRegistry()
  const events = []
  const sender = { id: 18, isDestroyed: () => false, send: (_channel, payload) => events.push(payload) }
  const active = registry.begin('18:active', 'active', sender, () => true)
  assert.equal(registry.attach(active, () => {}), true)
  registry.finish(active, { kind: 'error', error: 'network failure' })
  registry.finish(active, { kind: 'closed' })
  assert.deepEqual(events, [{ streamId: 'active', kind: 'error', error: 'network failure' }])

  const pending = registry.begin('18:pending', 'pending', sender, () => true)
  registry.closeAll('Backend is restarting')
  let lateCloseCount = 0
  assert.equal(registry.attach(pending, () => { lateCloseCount += 1 }), false)
  assert.equal(lateCloseCount, 1)
  assert.deepEqual(events.slice(1), [{
    streamId: 'pending', kind: 'closed', error: 'Backend is restarting',
  }])
})

test('main process keeps subject deletion behind the explicit trusted IPC', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'src', 'main', 'index.ts'), 'utf8')
  assert.match(source, /function clearDesktopAuthentication\(\)\s*\{\s*clearDesktopBearer\(\)/)
  assert.match(source, /ipcMain\.handle\('desktop-forget-device-identity', async \(event\) =>/)
  assert.match(source, /await restoreDesktopSubjectToken\(\)/)
  assert.match(source, /await getDesktopSubjectTokenStore\(\)\.save\(payload\.subject_token\)/)
})
