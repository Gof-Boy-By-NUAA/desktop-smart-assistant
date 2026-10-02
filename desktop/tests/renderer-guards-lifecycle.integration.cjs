// Real Electron 33: production guards, native message box and native renderer
// termination. Only the HTML fixture/fault are synthetic; no API doubles.
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
const root = path.resolve(__dirname, '../..')

if (!process.versions.electron) {
  const {test} = require('node:test')
  const {spawn} = require('node:child_process')
  const evidence = path.resolve(process.env.RC2_LIFECYCLE_EVIDENCE || '')
  assert.ok(evidence.startsWith(path.join(root, 'tmp') + path.sep))
  const modes = ['quit-active-dialog', 'double-quit-active-dialog', 'dispose-destroy-exit', 'dispose-close-exit', 'quit-no-dialog']
  for (const mode of modes) test(`native lifecycle: ${mode}`, async () => {
    const directory = fs.mkdtempSync(path.join(evidence, mode + '-'))
    const env = {...process.env, RC2_NATIVE_CASE:mode, RC2_NATIVE_CASE_DIR:directory}
    delete env.ELECTRON_RUN_AS_NODE
    const electron = path.join(__dirname, '../node_modules/electron/dist', process.platform === 'win32' ? 'electron.exe' : 'electron')
    const out = fs.openSync(path.join(directory, 'stdout.log'), 'w')
    const err = fs.openSync(path.join(directory, 'stderr.log'), 'w')
    const child = spawn(electron, [__filename], {env, stdio:['ignore', out, err], windowsHide:true})
    fs.closeSync(out); fs.closeSync(err)
    const code = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {child.kill(); reject(new Error(`Owned Electron timed out: ${directory}`))}, 30000)
      child.once('error', error => {clearTimeout(timeout); reject(error)})
      child.once('exit', (code, signal) => {
        clearTimeout(timeout)
        fs.writeFileSync(path.join(directory, 'exit.json'), JSON.stringify({code, signal}))
        resolve(code)
      })
    })
    assert.equal(code, 0, `Actual Electron exit code; evidence: ${directory}`)
    const record = JSON.parse(fs.readFileSync(path.join(directory, 'observation.json'), 'utf8'))
    assert.equal(record.validated, true)
    assert.equal(record.quitCode, 0)
  })
} else {
  const {app, BrowserWindow} = require('electron')
  const {setupWebContentsGuards} = require('../dist/main/renderer-guards.js')
  const directory = path.resolve(process.env.RC2_NATIVE_CASE_DIR)
  assert.ok(directory.startsWith(path.join(root, 'tmp') + path.sep))
  const mode = process.env.RC2_NATIVE_CASE
  const profile = path.join(directory, 'profile')
  const logs = path.join(profile, 'logs')
  fs.mkdirSync(logs, {recursive:true})
  app.setPath('userData', profile); app.setPath('logs', logs)
  const record = {pid:process.pid, electron:process.versions.electron, mode, events:[], validated:false}
  const save = () => fs.writeFileSync(path.join(directory, 'observation.json'), JSON.stringify(record, null, 2))
  let window, quitting = false, dispose
  const deadline = setTimeout(() => {record.timeout=true;save();app.exit(2)}, 20000)
  app.on('before-quit', () => {record.events.push('before-quit');save()})
  app.on('window-all-closed', () => {})
  app.on('quit', (_event, code) => {clearTimeout(deadline);record.quitCode=code;save()})
  app.whenReady().then(async () => {
    window = new BrowserWindow({width:640,height:360,show:true,webPreferences:{contextIsolation:true,nodeIntegration:false}})
    const wc = window.webContents
    const events = ['render-process-gone', 'unresponsive', 'responsive', 'did-finish-load']
    const existing = new Map(events.map(event => [event, wc.listeners(event)]))
    dispose = setupWebContentsGuards(window, () => quitting)
    const guardListeners = new Map(events.map(event => [event, wc.listeners(event).filter(listener => !existing.get(event).includes(listener))]))
    window.on('closed', () => {record.events.push('closed');save()})
    await window.loadURL('data:text/html,<h1>Owned Electron shutdown verification</h1>')
    if (mode !== 'quit-no-dialog') {
      const gone = new Promise(resolve => wc.once('render-process-gone', (_event, details) => {
        record.events.push({event:'render-process-gone', reason:details.reason, exitCode:details.exitCode});save();resolve()
      }))
      wc.forcefullyCrashRenderer()
      await gone
      const records = fs.readFileSync(path.join(logs, 'renderer-health.jsonl'), 'utf8').trim().split('\n').map(JSON.parse)
      assert.ok(records.some(r => r.event === 'render-process-gone' && r.reason === 'crashed'))
    }
    quitting = true
    if (mode.startsWith('dispose-')) {
      await dispose()
      for (const event of events) {
        assert.equal(wc.listeners(event).some(listener => guardListeners.get(event).includes(listener)), false, `guard listener remains: ${event}`)
      }
      record.validated = true; save()
      if (mode === 'dispose-destroy-exit') window.destroy()
      else window.close()
      app.exit(0)
    } else {
      record.validated = true; save()
      app.quit()
      if (mode === 'double-quit-active-dialog') app.quit()
    }
  }).catch(error => {record.error=error.stack;save();app.exit(1)})
}
