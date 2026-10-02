const assert = require('node:assert/strict')
const fs = require('node:fs')
const fsp = require('node:fs/promises')
const path = require('node:path')
const test = require('node:test')
const compiledMain = require('./helpers/compiled-main.cjs')

const REPO_ROOT = path.resolve(__dirname, '../..')
const TOKEN_FILENAME = 'desktop-subject-token.bin'

// Only Electron safeStorage and the backend transport are doubles. The tested
// validators come from the current compiled main and use the real Windows FS.
const secureStorage = {
  isEncryptionAvailable: () => true,
  encryptString: token => Buffer.from(`encrypted:${Buffer.from(token).toString('base64')}`),
  decryptString: value => Buffer.from(value.toString().slice('encrypted:'.length), 'base64').toString(),
}

const storageFunctions = compiledMain(['createDesktopSubjectTokenStore'], {
  fs_1: { default: fs }, process, SUBJECT_TOKEN_FILENAME: TOKEN_FILENAME,
})

async function fixture(t) {
  const parent = path.join(REPO_ROOT, 'tmp/delivery-defense')
  await fsp.mkdir(parent, { recursive: true })
  const directory = await fsp.mkdtemp(path.join(parent, 'input-boundaries-test-'))
  t.after(() => fsp.rm(directory, { recursive: true, force: true }))
  return directory
}

function newStore(directory, reports = []) {
  return storageFunctions.createDesktopSubjectTokenStore(directory, secureStorage, message => reports.push(message))
}

function newBroker(backend, bindings = {}) {
  return compiledMain([
    'isRecord', 'parseRendererRequest', 'sanitizedResponseHeaders',
    'clearDesktopBearer', 'clearDesktopAuthentication', 'resolveAllowedBackendPath', 'proxyDesktopRequest',
  ], {
    pythonBackend: backend, desktopAuthToken: 'main-bearer', desktopSubjectToken: 'device-subject',
    ...bindings,
  })
}

function recordingBackend() {
  const requests = []
  return {
    requests,
    getStatus: () => 'ready',
    invoke: async input => {
      requests.push(input)
      return { status: 200, statusText: 'OK', headers: {}, body: Buffer.from('{"status":"ok"}') }
    },
  }
}

test('token store rejects malformed or unrestricted roots before filesystem access', () => {
  for (const root of ['', '.', '../outside', 'relative/data', path.parse(REPO_ROOT).root, `${REPO_ROOT}\0suffix`, undefined]) {
    assert.throws(() => newStore(root), /Invalid device identity storage root/)
  }
})

test('real token file stays in a new absolute root and preserves save/load/replace/forget', async t => {
  const directory = await fixture(t)
  const userData = path.join(directory, 'new-user-data')
  const reports = []
  const store = newStore(userData, reports)
  assert.equal(await store.load(), null)
  await store.save('subject-one')
  const file = path.join(userData, TOKEN_FILENAME)
  assert.equal(path.dirname(await fsp.realpath(file)), await fsp.realpath(userData))
  assert.equal((await fsp.readFile(file)).includes(Buffer.from('subject-one')), false)
  assert.equal(await store.load(), 'subject-one')
  await store.save('subject-two')
  assert.equal(await newStore(userData).load(), 'subject-two')
  assert.deepEqual(await fsp.readdir(userData), [TOKEN_FILENAME])
  await store.forget()
  assert.equal(await store.load(), null)
  assert.deepEqual(reports, [])
})

test('token store rejects a real junction at the token filename without touching its target', async t => {
  const directory = await fixture(t)
  const userData = path.join(directory, 'user-data')
  const outside = path.join(directory, 'outside')
  await fsp.mkdir(userData)
  await fsp.mkdir(outside)
  await fsp.writeFile(path.join(outside, 'sentinel'), 'untouched')
  const tokenPath = path.join(userData, TOKEN_FILENAME)
  await fsp.symlink(outside, tokenPath, process.platform === 'win32' ? 'junction' : 'dir')
  assert.equal((await fsp.lstat(tokenPath)).isSymbolicLink(), true)
  const reports = []
  const store = newStore(userData, reports)
  assert.equal(await store.load(), null)
  await assert.rejects(() => store.save('replacement'), /Unable to save/)
  await assert.rejects(() => store.forget(), /Unable to forget/)
  assert.equal(await fsp.readFile(path.join(outside, 'sentinel'), 'utf8'), 'untouched')
  assert.equal((await fsp.lstat(tokenPath)).isSymbolicLink(), true)
  assert.equal(reports.length, 3)
})

test('an established userData junction works, then changing its target fails closed', async t => {
  const directory = await fixture(t)
  const first = path.join(directory, 'first')
  const second = path.join(directory, 'second')
  const userData = path.join(directory, 'user-data')
  await fsp.mkdir(first)
  await fsp.mkdir(second)
  const linkType = process.platform === 'win32' ? 'junction' : 'dir'
  await fsp.symlink(first, userData, linkType)
  const store = newStore(userData)
  await store.save('original-device')
  assert.equal(await store.load(), 'original-device')
  await fsp.unlink(userData)
  await fsp.symlink(second, userData, linkType)
  await newStore(second).save('other-device')
  assert.equal(await store.load(), null)
  await assert.rejects(() => store.save('replacement'), /Unable to save/)
  await assert.rejects(() => store.forget(), /Unable to forget/)
  assert.equal(await newStore(first).load(), 'original-device')
  assert.equal(await newStore(second).load(), 'other-device')
})

test('broker rejects URL, protocol, encoding and route attacks before transport dispatch', async () => {
  const backend = recordingBackend()
  const broker = newBroker(backend)
  for (const target of [
    '', 'api/config', 'https://smart_assistant.invalid/auth/login',
    'http://127.0.0.1/api/config', 'file:///etc/passwd', 'data:text/plain,hello',
    '//evil.invalid/api/config', '//user:pass@smart_assistant.invalid/api/config',
    '/\\evil.invalid/api/config', '/api\\config', '/api/config#fragment',
    '/api/config\r\nHost:evil.invalid', '/api/config?', '/api/config?x=%GG',
    '/api/%2f../auth/login', '/api/%5c../auth/login', '/api/%00config',
    '/api/%FF', '/auth/%6cogin', '/auth/login/extra', '/api-evil/config', '/unlisted',
  ]) {
    await assert.rejects(() => broker.proxyDesktopRequest({ path: target }), /Invalid backend request/)
  }
  assert.equal(backend.requests.length, 0)
})

test('broker dispatches canonical paths with main-owned bearer and preserves encoded resources/tickets', async () => {
  const backend = recordingBackend()
  const broker = newBroker(backend)
  for (const target of [
    '/api/old/../models?catalog_provider=%E4%B8%AD%E6%96%87', '/message', '/poll', '/cancel', '/config', '/upload',
    '/stream/ticket', '/api/logs/ticket', '/auth/check', '/file/signed%20file', '/preview/%E4%B8%AD%E6%96%87',
  ]) await broker.proxyDesktopRequest({ path: target })
  assert.equal(backend.requests[0].path, '/api/models?catalog_provider=%E4%B8%AD%E6%96%87')
  assert.equal(backend.requests[9].path, '/file/signed%20file')
  assert.ok(backend.requests.every(request => request.headers.Authorization === 'Bearer main-bearer'))
  assert.ok(backend.requests.every(request => request.method === 'GET'))
})

test('canonical login keeps bearer/subject private and persists the returned identity to a real file', async t => {
  const directory = await fixture(t)
  const store = newStore(directory)
  const backend = recordingBackend()
  backend.invoke = async input => {
    backend.requests.push(input)
    return {
      status: 200, statusText: 'OK', headers: { 'set-cookie': 'private', 'Content-Type': 'application/json' },
      body: Buffer.from(JSON.stringify({ status: 'success', token: 'new-main-bearer', subject_token: 'new-device-subject' })),
    }
  }
  const broker = newBroker(backend, { getDesktopSubjectTokenStore: () => store })
  const response = await broker.proxyDesktopRequest({ path: '/api/../auth/login', method: 'POST', body: '{"password":"test-password"}' })
  assert.equal(backend.requests[0].path, '/auth/login')
  assert.equal(backend.requests[0].headers.Authorization, undefined)
  assert.deepEqual(JSON.parse(backend.requests[0].body), { password: 'test-password', subject_token: 'device-subject' })
  assert.deepEqual(JSON.parse(Buffer.from(response.bodyBase64, 'base64')), { status: 'success' })
  assert.equal(response.headers['set-cookie'], undefined)
  assert.equal(await store.load(), 'new-device-subject')
  assert.equal(broker.desktopAuthToken, 'new-main-bearer')
  assert.equal(broker.desktopSubjectToken, 'new-device-subject')
})

test('renderer cannot inject auth headers or login identity and logout clears bearer after failure', async () => {
  const backend = recordingBackend()
  const broker = newBroker(backend)
  await assert.rejects(() => broker.proxyDesktopRequest({ path: '/api/config', headers: { Authorization: 'attacker' } }), /Forbidden backend request header/)
  await assert.rejects(() => broker.proxyDesktopRequest({ path: '/auth/login', method: 'POST', body: '{"password":"test","subject_token":"attacker"}' }), /Invalid login request/)
  assert.equal(backend.requests.length, 0)
  backend.invoke = async () => { throw new Error('injected transport failure') }
  await assert.rejects(() => broker.proxyDesktopRequest({ path: '/auth/logout', method: 'POST' }), /injected transport failure/)
  assert.equal(broker.desktopAuthToken, null)
})

test('broker selects only registered literal routes and constructs encoded dynamic segments', async () => {
  const backend = recordingBackend()
  const broker = newBroker(backend)
  for (const target of ['/api/config', '/api/unlisted', '/api/sessions/id/unknown', '/api/sessions/id/clear_context/extra', '/preview/a//b']) {
    await assert.rejects(() => broker.proxyDesktopRequest({path:target}), /Invalid backend request/)
  }
  assert.equal(backend.requests.length, 0)
  for (const target of [
    '/api/sessions/session_abc-1', '/api/sessions/%E4%B8%AD%E6%96%87/generate_title',
    '/api/sessions/session%3Aone/clear_context', '/file/folder/signed%20file',
    '/api/history?session_id=a%26b%3Dc%23d&page=1&empty=&key=%20&key=+&key=x',
  ]) await broker.proxyDesktopRequest({path:target})
  assert.equal(backend.requests[0].path, '/api/sessions/session_abc-1')
  assert.equal(backend.requests[1].path, '/api/sessions/%E4%B8%AD%E6%96%87/generate_title')
  assert.equal(backend.requests[2].path, '/api/sessions/session%3Aone/clear_context')
  assert.equal(backend.requests[3].path, '/file/folder/signed%20file')
  assert.equal(backend.requests[4].path, '/api/history?session_id=a%26b%3Dc%23d&page=1&empty=&key=%20&key=+&key=x')
})
