const assert = require('node:assert/strict')
const test = require('node:test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const compiledMain = require('./helpers/compiled-main.cjs')

function rendererModule(relative, bindings = {}, requireModule = require) {
  const source = fs.readFileSync(path.join(__dirname, '../src/renderer/src', relative), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
  const context = vm.createContext({ exports: {}, require: requireModule, Error, URL, Headers, Response, TextDecoder, Uint8Array, atob, crypto: globalThis.crypto, console, ...bindings })
  vm.runInContext(compiled, context)
  return context.exports
}

test('P03 resource protocol accepts only its exact origin and resource paths', () => {
  const { resourcePathFromUrl } = compiledMain(['resourcePathFromUrl'])
  for (const suffix of ['/preview/token/中文%20%23%25.md', '/file/token', '/preview/token/a.md?q=1']) {
    assert.equal(resourcePathFromUrl('smart-assistant://backend' + suffix), new URL('smart-assistant://backend' + suffix).pathname + new URL('smart-assistant://backend' + suffix).search)
  }
  for (const value of ['javascript:alert(1)', 'data:text/html,x', 'file:///C:/secret', 'other://backend/file/token', 'smart_assistant://backend/file/token', 'smart-assistant://evil/file/token', 'smart-assistant://backend:99/file/token', 'smart-assistant://user@backend/file/token', 'smart-assistant://backend/api/health', 'smart-assistant://backend/preview/../../config']) {
    assert.equal(resourcePathFromUrl(value), null, value)
  }
})

test('P03 renderer builds encoded resource URLs and rejects unrelated resource paths', () => {
  const client = rendererModule('api/client.ts').default
  assert.equal(client.getPreviewUrl('/preview/t/中文 空格%23%25.md'), 'smart-assistant://backend/preview/t/%E4%B8%AD%E6%96%87%20%E7%A9%BA%E6%A0%BC%23%25.md')
  for (const value of ['javascript:alert(1)', 'data:text/html,x', 'file:///C:/secret', 'other://backend/preview/x', '//evil/preview/x', '/api/health', '/preview/../../config']) {
    assert.equal(client.getPreviewUrl(value), '', value)
  }
})

function sessionStore(storage = new Map(), api = { getSessions: async () => ({ sessions: [], total: 0, page: 1, has_more: false }) }) {
  // 开发级状态测试使用 Fake localStorage 与 Stub API；正式边界另用完整制品验证。
  const localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }
  return rendererModule('store/sessionStore.ts', { localStorage }, name => name === '../api/client' ? { default: api } : require(name)).useSessionStore
}

test('P04 new and restored drafts are explicit; unknown saved IDs are not drafts', async () => {
  const storage = new Map(), first = sessionStore(storage)
  assert.equal(first.getState().draftId, first.getState().activeId)
  const restored = sessionStore(storage)
  assert.equal(restored.getState().draftId, first.getState().activeId)
  await restored.getState().loadSessions()
  assert.equal(restored.getState().error, null)
  const unknown = sessionStore(new Map([['cow_session_id', 'missing-session']]))
  assert.equal(unknown.getState().activeId, 'missing-session')
  assert.equal(unknown.getState().draftId, null)
})

test('P04 persisted draft becomes historical; session-list failure remains observable', async () => {
  const storage = new Map(), api = { getSessions: async () => { throw new Error('backend unavailable') } }
  const store = sessionStore(storage, api)
  const id = store.getState().activeId
  await store.getState().loadSessions()
  assert.equal(store.getState().error, 'backend unavailable')
  api.getSessions = async () => ({ sessions: [{ session_id: id }], total: 1, page: 1, has_more: false })
  await store.getState().loadSessions()
  assert.equal(store.getState().draftId, null)
  assert.equal(storage.has('cow_draft_session_id'), false)
  store.getState().newSession()
  assert.equal(store.getState().draftId, store.getState().activeId)
  store.getState().setActive(id)
  assert.equal(store.getState().draftId, null)
})

test('P04 empty successful history differs from missing session and failed request', async () => {
  let status = 200, payload = { status: 'success', messages: [], page: 1, has_more: false }
  const api = { backendRequest: async () => ({ status, bodyBase64: Buffer.from(JSON.stringify(payload)).toString('base64') }) }
  const client = rendererModule('api/client.ts', { window: { electronAPI: api } }).default
  assert.equal((await client.getHistory('empty')).messages.length, 0)
  payload = { status: 'error', message: 'session not found' }
  await assert.rejects(client.getHistory('missing'), /session not found/)
  status = 503; payload = { message: 'backend unavailable' }
  await assert.rejects(client.getHistory('existing'), /HTTP 503.*backend unavailable/)
})
