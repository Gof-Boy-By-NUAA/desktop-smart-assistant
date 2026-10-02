// Real Electron/BrowserWindow and production guards, isolated from user data.
// Fault injection only: control.json requests a real renderer termination or a
// finite busy loop. Native dialogs must be operated through desktop UI; no fake
// dialog API, synthetic event emission or model/network invocation is used.
const { app, BrowserWindow } = require('electron')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { setupWebContentsGuards } = require('../dist/main/renderer-guards.js')
const root = path.resolve(__dirname, '../..')
const owned = path.resolve(process.env.RC2_GUARD_EVIDENCE_DIR || '')
const mode = process.env.RC2_GUARD_TEST_MODE || 'interactive'
assert.ok(['interactive', 'responsive'].includes(mode))
assert.ok(owned.startsWith(path.join(root, 'tmp') + path.sep))
const profile = fs.mkdtempSync(path.join(owned, 'electron-profile-'))
const logs = path.join(profile, 'logs')
fs.mkdirSync(logs, {recursive:true})
app.setPath('userData', profile)
app.setPath('logs', logs)
let window, timer, deadline, disposeGuards, quitting = false, lastAction = null
const result = {pid:process.pid, electron:process.versions.electron, profile, mode, loads:[], events:[], checks:[]}
function save() {fs.writeFileSync(path.join(owned, 'native-electron-observation.json'), JSON.stringify(result, null, 2))}
async function finish(code) {
  if (quitting) return
  quitting = true
  clearInterval(timer); clearTimeout(deadline)
  if (disposeGuards) await disposeGuards()
  if (window && !window.isDestroyed()) window.destroy()
  result.exitCode = code; save(); app.exit(code)
}
app.whenReady().then(async () => {
  const fixture = path.join(profile, 'fixture.html')
  fs.writeFileSync(fixture, '<!doctype html><meta charset="utf-8"><title>RC2 Guard Owned Test</title><h1>RC2 专用恢复验证窗口</h1><p id="ready">真实 Electron 页面已加载</p>')
  window = new BrowserWindow({width:720,height:480,show:true,webPreferences:{contextIsolation:true,nodeIntegration:false}})
  disposeGuards = setupWebContentsGuards(window, () => quitting)
  window.webContents.on('render-process-gone', (_event, details) => {result.events.push({event:'render-process-gone',...details}); save()})
  for (const event of ['unresponsive','responsive']) window.webContents.on(event, () => {result.events.push({event}); save()})
  window.webContents.on('did-finish-load', () => {
    if (quitting || window.isDestroyed()) return
    result.loads.push({url:window.webContents.getURL(),rendererPid:window.webContents.getOSProcessId()}); save()
  })
  window.on('closed', () => {if (!quitting) finish(1)})
  await window.loadFile(fixture, {hash:'/session/owned-fixture'})
  console.log(JSON.stringify({ownedElectronPid:process.pid, profile, ready:true}))
  timer = setInterval(() => {
    const control = path.join(owned, 'native-control.json')
    if (!fs.existsSync(control)) return
    const {id,action} = JSON.parse(fs.readFileSync(control, 'utf8'))
    if (id === lastAction) return
    lastAction = id
    if (action === 'crash') window.webContents.forcefullyCrashRenderer()
    else if (action === 'hang') {
      window.webContents.executeJavaScript('(() => {const until=Date.now()+12000; while(Date.now()<until){}; return true})()')
        .catch(() => {result.events.push({event:'injected-hang-interrupted'}); save()})
    } else if (action === 'finish') {
      try {
        assert.ok(result.events.some(e => e.event === 'unresponsive'))
        assert.ok(result.events.some(e => e.event === 'responsive'))
        assert.ok(result.loads.every(l => l.url.endsWith('#/session/owned-fixture')))
        const records = fs.readFileSync(path.join(logs, 'renderer-health.jsonl'), 'utf8').trim().split('\n').map(JSON.parse)
        if (mode === 'interactive') {
          assert.ok(result.events.some(e => e.event === 'render-process-gone'))
          assert.ok(result.loads.length >= 2)
          assert.notEqual(result.loads[0].rendererPid, result.loads[1].rendererPid)
          assert.ok(records.some(e => e.event === 'user-requested-reload'))
          assert.ok(records.some(e => e.event === 'render-process-gone' && typeof e.exitCode === 'number'))
          result.checks.push('native renderer loss logged with reason/exitCode', 'explicit native reload creates new renderer and preserves hash')
        } else {
          assert.equal(result.loads.length, 1)
          assert.equal(records.some(e => e.event === 'user-requested-reload'), false)
          assert.equal(result.events.some(e => e.event === 'render-process-gone'), false)
          result.checks.push('finite native hang resumes without reload or renderer kill')
        }
        result.checks.push('native unresponsive/responsive observed')
        console.log(JSON.stringify({status:'passed',checks:result.checks,testDoubles:'none; isolated fixture and controlled fault injection'}))
        finish(0)
      } catch (error) {console.error(error.stack); finish(1)}
    } else {console.error('UNKNOWN_TEST_CONTROL_ACTION'); finish(1)}
  }, 250)
  deadline = setTimeout(() => {console.error('NATIVE_UI_VALIDATION_TIMEOUT'); finish(1)}, 600000)
}).catch(error => {console.error(error.stack); finish(1)})
