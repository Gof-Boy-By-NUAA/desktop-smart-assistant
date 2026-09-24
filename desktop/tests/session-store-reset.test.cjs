const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')

// Execute the actual store. Storage, Zustand state container and unused API
// are isolated doubles; this does not verify Electron or backend logout.
function loadStore() {
  const values = new Map([
    ['cow_session_id', 'old-user-session'],
    ['cow_draft_session_id', 'old-user-session'],
  ])
  const localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
  function create(initialize) {
    let state
    const setState = update => { state = { ...state, ...(typeof update === 'function' ? update(state) : update) } }
    const getState = () => state
    state = initialize(setState, getState)
    return { getState, setState }
  }
  let tick = 1000
  const context = vm.createContext({
    exports: {}, localStorage,
    Date: { now: () => ++tick },
    Math: Object.assign(Object.create(Math), { random: () => 0.25 }),
    require: name => {
      if (name === 'zustand') return { create }
      if (name === '../api/client') return { default: new Proxy({}, { get() { throw new Error('reset must not call backend') } }) }
      throw new Error(`Unexpected import: ${name}`)
    },
  })
  const source = fs.readFileSync(path.join(__dirname, '../src/renderer/src/store/sessionStore.ts'), 'utf8')
  vm.runInContext(ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, context)
  const store = context.exports.useSessionStore
  store.setState({ sessions: [{ session_id: 'old-user-session', title: 'Old user' }], total: 9, page: 3, hasMore: true, loading: true, error: 'old error' })
  return { store, localStorage }
}

test('logout reset invalidates the old active ID and persists a fresh local draft', () => {
  const { store, localStorage } = loadStore()
  assert.equal(store.getState().activeId, 'old-user-session')
  store.getState().reset()
  const state = store.getState()
  assert.equal(typeof state.activeId, 'string')
  assert.ok(state.activeId.length > 0)
  assert.notEqual(state.activeId, 'old-user-session')
  assert.equal(state.draftId, state.activeId)
  assert.equal(localStorage.getItem('cow_session_id'), state.activeId)
  assert.equal(localStorage.getItem('cow_draft_session_id'), state.draftId)
})

test('logout reset clears all renderer session metadata', () => {
  const { store } = loadStore()
  store.getState().reset()
  const state = store.getState()
  assert.deepEqual(Array.from(state.sessions), [])
  assert.equal(state.total, 0)
  assert.equal(state.page, 1)
  assert.equal(state.hasMore, false)
  assert.equal(state.loading, false)
  assert.equal(state.error, null)
})

test('consecutive resets rotate the draft ID with deterministic time and randomness', () => {
  const { store, localStorage } = loadStore()
  store.getState().reset()
  const first = store.getState().activeId
  store.getState().reset()
  const second = store.getState().activeId
  assert.notEqual(second, first)
  assert.notEqual(second, 'old-user-session')
  assert.equal(store.getState().draftId, second)
  assert.equal(localStorage.getItem('cow_session_id'), second)
  assert.equal(localStorage.getItem('cow_draft_session_id'), second)
})
