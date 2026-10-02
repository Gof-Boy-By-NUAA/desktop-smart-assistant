const test = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')

// Development-level race/lifecycle tests: Electron dialog/window and filesystem
// are explicit doubles. Native events and native button behavior are verified
// separately by renderer-recovery.integration.cjs plus desktop UI interaction.
function harness() {
  const wc = new EventEmitter()
  const win = new EventEmitter()
  let destroyed = false, quitting = false
  let reloads = 0, crashes = 0
  const records = [], dialogs = [], deferred = []
  let quitRequests = 0
  wc.isDestroyed = win.isDestroyed = () => destroyed
  win.webContents = wc
  wc.reload = () => { reloads++ }
  wc.forcefullyCrashRenderer = () => { crashes++; wc.emit('render-process-gone', {}, {reason:'killed', exitCode:1}) }
  const exports = {}
  const fakeApp = new EventEmitter()
  fakeApp.getPath = () => '/owned-test-logs'
  fakeApp.quit = () => {quitRequests++}
  const fakeElectron = {
    app: fakeApp,
    dialog: {showMessageBox: (_win, options) => new Promise(resolve => {
      dialogs.push({options, resolve})
      options.signal.addEventListener('abort', () => {
        if (!fakeElectron.delayAbort) resolve({response:1})
      })
    })},
  }
  const fakeFs = {mkdirSync() {}, appendFileSync(_file, line) { records.push(JSON.parse(line)) }}
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../dist/main/renderer-guards.js'), 'utf8'), {
    exports, require: name => name === 'electron' ? fakeElectron : name === 'fs' ? fakeFs : require(name),
    console: {info() {}, error() {}}, AbortController,
    setImmediate: action => {deferred.push(action); return action},
    clearImmediate: action => {const at=deferred.indexOf(action); if(at>=0) deferred.splice(at,1)},
  })
  const dispose = exports.setupWebContentsGuards(win, () => quitting)
  return {wc, win, records, dialogs, fakeFs, fakeApp, fakeElectron, dispose,
    destroy() {destroyed = true; wc.emit('destroyed')},
    quit() {quitting = true; fakeApp.emit('before-quit', {preventDefault() {}})},
    flush() {while (deferred.length) deferred.shift()()},
    get reloads() {return reloads}, get crashes() {return crashes},
    get quitRequests() {return quitRequests},
  }
}
const settle = () => new Promise(resolve => setImmediate(resolve))

test('dead renderer stays stopped until explicit reload; log contains only whitelisted fields', async () => {
  const h = harness()
  h.wc.emit('render-process-gone', {}, {reason:'oom', exitCode:9, private:'DO_NOT_LOG'})
  assert.equal(h.reloads, 0)
  assert.equal(h.dialogs.length, 1)
  assert.equal(h.dialogs[0].options.defaultId, 1)
  assert.equal(h.dialogs[0].options.cancelId, 1)
  assert.equal(JSON.stringify(h.records).includes('DO_NOT_LOG'), false)
  assert.equal(h.records[0].reason, 'oom')
  assert.equal(h.records[0].exitCode, 9)
  h.dialogs[0].resolve({response:0})
  await settle()
  assert.equal(h.reloads, 0, 'reload is deferred out of the event handler')
  h.flush()
  assert.equal(h.reloads, 1)
  assert.equal(h.crashes, 0)
})

test('waiting is bounded to one prompt per failure and responsive invalidates a stale choice', async () => {
  const h = harness()
  h.wc.emit('unresponsive')
  h.wc.emit('unresponsive')
  assert.equal(h.dialogs.length, 1)
  h.wc.emit('responsive')
  h.dialogs[0].resolve({response:0})
  await settle(); h.flush()
  assert.equal(h.reloads, 0)
  h.wc.emit('unresponsive')
  assert.equal(h.dialogs.length, 2)
  h.dialogs[1].resolve({response:1})
  await settle(); h.flush()
  h.wc.emit('unresponsive')
  assert.equal(h.dialogs.length, 2)
  assert.equal(h.reloads, 0)
})

test('explicit hung recovery terminates once and does not open a second crash prompt', async () => {
  const h = harness()
  h.wc.emit('unresponsive')
  h.dialogs[0].resolve({response:0})
  await settle(); h.flush(); await settle()
  assert.equal(h.crashes, 1)
  assert.equal(h.reloads, 1)
  assert.equal(h.dialogs.length, 1)
  assert.ok(h.records.some(r => r.event === 'render-process-gone'))
})

test('destroying or quitting the window prevents a deferred reload', async () => {
  for (const end of ['destroy', 'quit']) {
    const h = harness()
    h.wc.emit('render-process-gone', {}, {reason:'crashed', exitCode:2})
    h.dialogs[0].resolve({response:0})
    await settle()
    h[end](); h.flush()
    assert.equal(h.reloads, 0)
  }
})

test('a recovered or newly loaded page cancels an accepted but deferred hung restart', async () => {
  for (const event of ['responsive', 'did-finish-load']) {
    const h = harness()
    h.wc.emit('unresponsive')
    h.dialogs[0].resolve({response:0})
    await settle()
    h.wc.emit(event)
    h.flush()
    assert.equal(h.reloads, 0)
    assert.equal(h.crashes, 0)
  }
})

test('log write failure is exposed in the recovery prompt', () => {
  const h = harness()
  h.fakeFs.appendFileSync = () => {throw new Error('injected filesystem denial')}
  h.wc.emit('render-process-gone', {}, {reason:'crashed', exitCode:2})
  assert.match(h.dialogs[0].options.detail, /日志写入失败/)
})

test('closed, destroyed and before-quit dispose only owned listeners and invalidate late events', async () => {
  for (const end of ['closed', 'destroyed', 'before-quit']) {
    const h = harness()
    const otherListener = () => {}
    h.wc.on('responsive', otherListener)
    h.wc.emit('render-process-gone', {}, {reason:'crashed', exitCode:2})
    const lateGone = h.wc.listeners('render-process-gone')[0]
    const lateResponsive = h.wc.listeners('responsive')[0]
    const readsAfterDispose = () => {throw new Error('native object accessed after dispose')}
    if (end === 'closed') h.win.emit('closed')
    else if (end === 'destroyed') h.destroy()
    else h.quit()
    h.wc.isDestroyed = h.win.isDestroyed = readsAfterDispose
    const logsAtDispose = h.records.length
    assert.equal(h.dialogs[0].options.signal.aborted, true)
    assert.equal(h.wc.listenerCount('render-process-gone'), 0)
    assert.deepEqual(h.wc.listeners('responsive'), [otherListener])
    assert.equal(h.wc.listenerCount('destroyed'), 0)
    assert.equal(h.win.listenerCount('closed'), 0)
    assert.equal(h.fakeApp.listenerCount('before-quit'), 1, 'only the native-close barrier remains')
    h.dispose(); lateGone({}, {reason:'oom', exitCode:9}); lateResponsive()
    await settle(); h.flush()
    assert.equal(h.fakeApp.listenerCount('before-quit'), 0)
    assert.equal(h.records.length, logsAtDispose)
    assert.equal(h.reloads, 0)
    assert.equal(h.crashes, 0)
  }
})

test('before-quit waits for the native dialog result, not just its abort signal', async () => {
  const h = harness()
  h.fakeElectron.delayAbort = true
  h.wc.emit('render-process-gone', {}, {reason:'crashed', exitCode:2})
  let prevented = false
  h.fakeApp.emit('before-quit', {preventDefault() {prevented=true}})
  assert.equal(prevented, true)
  assert.equal(h.dialogs[0].options.signal.aborted, true)
  await settle()
  assert.equal(h.quitRequests, 0, 'abort has not completed the native dialog')
  let secondPrevented = false
  h.fakeApp.emit('before-quit', {preventDefault() {secondPrevented=true}})
  assert.equal(secondPrevented, true, 'repeated quit cannot bypass the pending close')
  h.dialogs[0].resolve({response:1})
  await settle()
  assert.equal(h.quitRequests, 1)
  assert.equal(h.reloads, 0)
  assert.equal(h.fakeApp.listenerCount('before-quit'), 0)
})

test('explicit disposal also blocks quit until the native dialog settles', async () => {
  const h = harness()
  h.fakeElectron.delayAbort = true
  h.wc.emit('unresponsive')
  const closed = h.dispose()
  let prevented = false
  h.fakeApp.emit('before-quit', {preventDefault() {prevented=true}})
  assert.equal(prevented, true)
  assert.equal(h.quitRequests, 0)
  h.dialogs[0].resolve({response:1})
  await closed
  assert.equal(h.quitRequests, 1)
  assert.equal(h.fakeApp.listenerCount('before-quit'), 0)
})
