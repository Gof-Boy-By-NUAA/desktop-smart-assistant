// 实际完整制品验收：不替换 Electron、IPC、后端、协议或存储。
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { spawn, spawnSync } = require('node:child_process')

const [artifact, output, phase = 'after'] = process.argv.slice(2).map((v, i) => i < 2 ? path.resolve(v) : v)
if (!artifact || !output) throw new Error('Usage: node p03-p04-artifact.cjs <win-unpacked> <owned-output> [before|after]')
if (fs.existsSync(path.join(output, 'results.json'))) throw new Error('Use a fresh output directory to preserve earlier evidence')
fs.mkdirSync(output, { recursive: true })
const home = path.join(output, 'profile')
for (const sub of ['AppData/Roaming', 'AppData/Local', 'Temp', 'electron']) fs.mkdirSync(path.join(home, sub), { recursive: true })
const result = { phase, artifact, checks: {}, runs: [], testDoubles: 'NONE', faultInjection: [] }
const save = () => fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(result, null, 2))
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
let child, rpc, closed

async function connect(url) {
  const socket = new WebSocket(url)
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }) })
  let seq = 0
  const pending = new Map()
  socket.addEventListener('message', e => {
    const msg = JSON.parse(e.data), waiter = pending.get(msg.id)
    if (!waiter) return
    pending.delete(msg.id); clearTimeout(waiter.timer)
    if (msg.error || msg.result?.exceptionDetails) waiter.reject(new Error(JSON.stringify(msg)))
    else waiter.resolve(msg.result.result.value)
  })
  return {
    eval(expression) { return new Promise((resolve, reject) => { const id = ++seq; const timer = setTimeout(() => { pending.delete(id); reject(new Error('Inspector evaluation timeout')) }, 20000); pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method: 'Runtime.evaluate', params: { expression, awaitPromise: true, returnByValue: true } })) }) },
    close() { socket.close() },
  }
}
const main = expression => rpc.eval(expression)
const renderer = (expression, userGesture = false) => main(`process.mainModule.require('electron').BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().includes('/dist/renderer/index.html')).webContents.executeJavaScript(${JSON.stringify(expression)},${userGesture})`)
const request = async (route, extra = {}) => {
  const r = await renderer(`window.electronAPI.backendRequest(${JSON.stringify({ path: route, ...extra })})`)
  const text = Buffer.from(r.bodyBase64, 'base64').toString('utf8')
  let data; try { data = JSON.parse(text) } catch {}
  return { status: r.status, text, data }
}
async function until(check, description, timeout = 20000) {
  const start = Date.now(); let value, error
  while (Date.now() - start < timeout) {
    try { value = await check(); if (value) return value } catch (e) { error = e }
    await sleep(150)
  }
  throw new Error(`${description}: timeout; ${error || JSON.stringify(value)}`)
}
async function start() {
  const env = { ...process.env, USERPROFILE: home, HOME: home, TEMP: path.join(home, 'Temp'), TMP: path.join(home, 'Temp'), PATH: `${process.env.SystemRoot}\\System32;${process.env.SystemRoot}` }
  for (const key of Object.keys(env)) if (/^(COW_|PYTHONPATH$|PYTHONHOME$|VIRTUAL_ENV$|ELECTRON_RUN_AS_NODE$)/i.test(key)) delete env[key]
  const number = result.runs.length + 1
  const log = fs.createWriteStream(path.join(output, `runtime-${number}.log`))
  child = spawn(path.join(artifact, 'SmartAssistant.exe'), ['--inspect=127.0.0.1:0', `--user-data-dir=${path.join(home, 'electron')}`, '--disable-background-timer-throttling', '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1'], { env, cwd: home, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
  const run = { pid: child.pid, number }; result.runs.push(run); save()
  closed = new Promise(resolve => child.once('exit', (code, signal) => { run.exitCode = code; run.signal = signal; log.end(); save(); resolve(code) }))
  let inspector, logs = ''
  for (const stream of [child.stdout, child.stderr]) stream.on('data', b => { log.write(b); logs += b.toString(); inspector ||= logs.match(/Debugger listening on (ws:\/\/127\.0\.0\.1:\d+\/[^\s]+)/)?.[1] })
  await until(() => inspector, 'Main inspector', 30000)
  rpc = await connect(inspector)
  await until(() => renderer('window.electronAPI?.getBackendStatus()').then(s => s === 'ready'), 'Bundled backend ready', 120000)
  await until(() => renderer(`document.body.innerText.includes('新对话')`), 'React UI')
  const identity = await main(`({packaged:process.mainModule.require('electron').app.isPackaged,path:process.mainModule.require('electron').app.getAppPath(),home:process.mainModule.require('os').homedir(),userData:process.mainModule.require('electron').app.getPath('userData')})`)
  assert.equal(identity.packaged, true); assert.equal(identity.home, home); assert.equal(identity.userData, path.join(home, 'electron'))
  run.identity = identity
  await renderer(`(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='跳过');if(b)b.click();})()`)
  assert.equal((await request('/api/health')).data.status, 'ok')
}
async function quit() {
  if (!child || child.exitCode !== null) return
  await main(`(()=>{setTimeout(()=>process.mainModule.require('electron').app.quit(),100);return true})()`)
  rpc.close(); rpc = null
  const code = await Promise.race([closed, sleep(15000).then(() => { throw new Error('Normal exit timeout') })])
  assert.equal(code, 0); child = null
}
async function preview(name) {
  await renderer(`(()=>{const file=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='文件');if(file)file.click();else document.querySelector('button[title="工作空间"]').click();})()`)
  await until(() => renderer(`(()=>{const el=[...document.querySelectorAll('span')].find(e=>e.textContent===${JSON.stringify(name)});if(!el)return false;el.click();return true;})()`), 'Workspace file')
  return until(() => renderer(`(()=>{const text=document.body.innerText;return text.includes('P03_MARKDOWN_OK')||text.includes('预览失败')?{text:text.slice(-900),heading:!![...document.querySelectorAll('h1')].find(e=>e.textContent==='P03_MARKDOWN_OK'),strong:!![...document.querySelectorAll('strong')].find(e=>e.textContent==='中文加粗'),highlight:!!document.querySelector('code .hljs-keyword'),xss:window.__p03xss===1,historyError:text.includes('会话记录加载失败')}:null})()`), 'Markdown preview outcome')
}

async function reloadSession(id) {
  await renderer(`localStorage.setItem('cow_session_id',${JSON.stringify(id)});localStorage.removeItem('cow_draft_session_id')`)
  await main(`new Promise(resolve=>{const w=process.mainModule.require('electron').BrowserWindow.getAllWindows()[0];w.webContents.once('did-finish-load',()=>resolve(true));w.reload();})`)
  await until(() => renderer(`document.body.innerText.includes('新对话')`), 'Reloaded session UI')
}
function fixture(mode, draftId) {
  const repo = path.resolve(__dirname, '../..')
  const db = path.join(home, '.cow/workspace/memory/long-term/index.db')
  const p = spawnSync(path.join(repo, '.venv/Scripts/python.exe'), [path.join(__dirname, 'p04-history-fixture.py'), db, mode, ...(draftId ? [draftId] : [])], { cwd: repo, env: { ...process.env, PYTHONUTF8: '1' }, windowsHide: true, encoding: 'utf8', timeout: 30000 })
  if (p.status !== 0) throw new Error(`Fixture ${mode}: ${p.stderr || p.stdout}`)
}
async function extendedChecks(names, resources) {
  result.checks.specialPath = await preview(names[1])
  assert.equal(result.checks.specialPath.heading, true)
  result.checks.dangerousAnchors = await renderer(`document.querySelectorAll('a[href^="javascript:"],a[href^="data:"],a[href^="file:"]').length`)
  assert.equal(result.checks.dangerousAnchors, 0)
  const selected = resources.find(e => e.name === names[1])
  const signedPaths = [selected.preview_url, selected.raw_url]
  result.checks.internalResources = await renderer(`Promise.all(${JSON.stringify(signedPaths)}.map(async p=>{const r=await fetch('smart-assistant://backend'+p);return {status:r.status,content:(await r.text()).includes('P03_MARKDOWN_OK')}}))`)
  for (const r of result.checks.internalResources) { assert.equal(r.status, 200); assert.equal(r.content, true) }
  const htmlResource = resources.find(e => e.name === 'linked.html')
  await renderer(`window.open(${JSON.stringify('smart-assistant://backend' + htmlResource.preview_url)},'_blank');true`, true)
  result.checks.internalLinkWindow = await until(() => main(`(async()=>{const w=process.mainModule.require('electron').BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('smart-assistant://backend/preview/'));if(!w)return false;return w.webContents.executeJavaScript("document.body.innerText.includes('P03_INTERNAL_LINK')&&typeof window.electronAPI==='undefined'")})()`), 'Internal preview link opens without IPC privileges')
  await main(`(()=>{for(const w of process.mainModule.require('electron').BrowserWindow.getAllWindows())if(w.webContents.getURL().startsWith('smart-assistant://'))w.close();return true})()`)
  result.checks.invalidResources = await renderer(`Promise.all(['smart-assistant://backend/api/health','smart-assistant://wrong/preview/x','smart-assistant://backend/preview/not-a-cap/file.md','javascript:alert(1)','data:text/plain,denied','other://backend/file/x'].map(async url=>{try{const r=await fetch(url);return {url,status:r.status,ok:r.ok}}catch{return {url,rejected:true}}}))`)
  for (const r of result.checks.invalidResources) assert.ok(r.rejected || !r.ok, r.url)
  result.checks.untrustedWindow = await main(`(async()=>{const e=process.mainModule.require('electron');const p=process.mainModule.require('path');const w=new e.BrowserWindow({show:false,webPreferences:{preload:p.join(e.app.getAppPath(),'dist/main/preload.js'),contextIsolation:true,nodeIntegration:false}});try{await w.loadURL('data:text/html,<title>Untrusted test window</title>');return await w.webContents.executeJavaScript("(async()=>{let ipc;try{await window.electronAPI.backendRequest({path:'/api/health'});ipc=false}catch(e){ipc=String(e).includes('Untrusted renderer')}let resource;try{resource=!(await fetch('smart-assistant://backend/api/health')).ok}catch{resource=true}return {ipcDenied:ipc,apiResourceDenied:resource}})()");}finally{w.destroy()}})()`)
  assert.equal(result.checks.untrustedWindow.ipcDenied, true)
  assert.equal(result.checks.untrustedWindow.apiResourceDenied, true)

  await quit(); await start()
  result.checks.restartEmpty = { total: (await request('/api/sessions')).data.total, historyError: await renderer(`document.body.innerText.includes('会话记录加载失败')`) }
  assert.equal(result.checks.restartEmpty.total, 0); assert.equal(result.checks.restartEmpty.historyError, false)
  const draftId = await renderer(`localStorage.getItem('cow_draft_session_id')`)
  await quit(); fixture('seed', draftId); await start()
  await until(() => renderer(`!![...document.querySelectorAll('h1')].find(e=>e.textContent==='P04_DRAFT_PERSISTED')`), 'Draft that became durable must load real history')
  result.checks.draftPersisted = await renderer(`localStorage.getItem('cow_draft_session_id')===null`)
  assert.equal(result.checks.draftPersisted, true)
  await reloadSession('p04-existing')
  await until(() => renderer(`!![...document.querySelectorAll('h1')].find(e=>e.textContent==='P04_HISTORY_OK')`), 'Persisted history rendered')
  result.checks.existingHistory = { apiMessages: (await request('/api/history?session_id=p04-existing')).data.messages.length, rendered: true }
  assert.equal(result.checks.existingHistory.apiMessages, 2)
  await reloadSession('p04-empty')
  const empty = await request('/api/history?session_id=p04-empty')
  assert.equal(empty.data.status, 'success'); assert.equal(empty.data.messages.length, 0)
  await sleep(500)
  result.checks.existingEmpty = { apiSuccess: true, uiError: await renderer(`document.body.innerText.includes('会话记录加载失败')`) }
  assert.equal(result.checks.existingEmpty.uiError, false)

  await reloadSession('p04-missing')
  await until(() => renderer(`document.body.innerText.includes('会话记录加载失败')`), 'Missing session remains an error')
  result.checks.missing = { api: (await request('/api/history?session_id=p04-missing')).data, uiError: true }
  assert.equal(result.checks.missing.api.status, 'error')
  fixture('break'); result.faultInjection.push('Renamed messages table in owned real SQLite database; restored in finally')
  try {
    await reloadSession('p04-existing')
    await until(() => renderer(`document.body.innerText.includes('会话记录加载失败')`), 'Real database failure remains an error')
    result.checks.databaseFailure = { api: (await request('/api/history?session_id=p04-existing')).data, uiError: true }
    assert.equal(result.checks.databaseFailure.api.status, 'error')
  } finally { fixture('restore') }
  await renderer(`(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent==='重试');if(!b)throw new Error('Retry absent');b.click()})()`)
  await until(() => renderer(`!![...document.querySelectorAll('h1')].find(e=>e.textContent==='P04_HISTORY_OK')&&!document.body.innerText.includes('会话记录加载失败')`), 'Real error recovery')
  result.checks.recovery = true
  await quit(); await start()
  await until(() => renderer(`!![...document.querySelectorAll('h1')].find(e=>e.textContent==='P04_HISTORY_OK')`), 'Existing history after relaunch')
  result.checks.restartHistory = true
  const backendPid = await main(`(()=>{const h=process._getActiveHandles().find(h=>typeof h.spawnfile==='string'&&h.spawnfile===process.mainModule.require('path').join(process.resourcesPath,'backend/smart-assistant-backend/smart-assistant-backend.exe'));if(!h)throw new Error('Owned backend child not found');const pid=h.pid;h.kill();return pid})()`)
  result.faultInjection.push('Terminated only the backend child of the owned test app, PID '+backendPid)
  await until(() => renderer(`window.electronAPI.getBackendStatus().then(s=>s==='stopped')`), 'Backend loss observable')
  result.checks.backendUnavailable = await renderer(`(async()=>{let rejected=false;try{await window.electronAPI.backendRequest({path:'/api/history?session_id=p04-existing'})}catch{rejected=true}return {rejected,text:document.body.innerText}})()`)
  assert.equal(result.checks.backendUnavailable.rejected, true)
  assert.ok(result.checks.backendUnavailable.text.includes('Trusted backend stopped unexpectedly'))
  assert.ok(!result.checks.backendUnavailable.text.includes('有什么可以帮你的'))
}

async function run() {
  await start()
  const sessions = await request('/api/sessions')
  assert.equal(sessions.data.total, 0)
  // 等待初始化请求完成；不是以 sleep 证明后端可用，ready/health 已真实通过。
  await sleep(700)
  const initial = await renderer(`({historyError:document.body.innerText.includes('会话记录加载失败'),active:localStorage.getItem('cow_session_id'),draft:localStorage.getItem('cow_draft_session_id')})`)
  result.checks.initial = initial
  const tree = await request('/api/workspace/tree')
  assert.ok(path.resolve(tree.data.root).startsWith(home + path.sep))
  const names = ['normal.md', '中文 路径 #100% & [样本].md']
  fs.writeFileSync(path.join(tree.data.root, 'linked.html'), '<!doctype html><title>Internal resource</title><h1>P03_INTERNAL_LINK</h1>')
  for (const name of names) fs.writeFileSync(path.join(tree.data.root, name), '# P03_MARKDOWN_OK\n\n**中文加粗**\n\n```js\nconst answer = 42;\n```\n\n[bad](javascript:alert(1))\n\n[bad-data](data:text/html,evil)\n\n[bad-file](file:///C:/Windows/win.ini)\n\n<script>window.__p03xss=1</script>\n')
  result.checks.preview = await preview(names[0])
  const resources = (await request('/api/workspace/tree')).data.entries
  const entry = resources.find(e => e.name === names[0])
  result.checks.backendPreview = { status: (await request(entry.preview_url)).status }
  result.checks.url = await renderer(`(()=>{try{return {valid:true,href:new URL('${phase === 'before' ? 'smart_assistant' : 'smart-assistant'}://backend/preview/x/y').href}}catch(e){return {valid:false,error:String(e)}}})()`)
  if (phase === 'before') {
    assert.equal(initial.historyError, true)
    assert.equal(result.checks.preview.heading, false)
    assert.equal(result.checks.backendPreview.status, 200)
    assert.equal(result.checks.url.valid, false)
  } else {
    assert.equal(initial.historyError, false)
    assert.equal(result.checks.preview.heading, true)
    assert.equal(result.checks.preview.strong, true)
    assert.equal(result.checks.preview.highlight, true)
    assert.equal(result.checks.preview.xss, false)
    await extendedChecks(names, resources)
  }
  result.status = phase === 'before' ? 'REPRODUCED' : 'PASSED'
}
run().catch(error => { result.status = 'FAILED'; result.error = error.stack; process.exitCode = 1 }).finally(async () => {
  try { await quit() } catch (error) { result.cleanupError = String(error); result.status = 'FAILED'; process.exitCode = 1; if (child?.exitCode === null) child.kill() }
  save(); console.log(JSON.stringify(result))
})

