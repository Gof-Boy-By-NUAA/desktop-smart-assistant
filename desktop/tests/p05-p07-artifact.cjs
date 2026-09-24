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
async function run() {
  await start()
  result.checks.initial = await renderer(`({error:document.body.innerText.includes('会话记录加载失败'),id:localStorage.getItem('cow_session_id')})`)
  await renderer(`(()=>{[...document.querySelectorAll('button[title="新对话"]')].at(-1).click();return true})()`)
  await sleep(2500)
  const draft = await renderer(`({error:document.body.innerText.includes('会话记录加载失败'),id:localStorage.getItem('cow_session_id'),draft:localStorage.getItem('cow_draft_session_id')})`)
  result.checks.newDraft = draft
  result.checks.history = (await request('/api/history?session_id='+encodeURIComponent(draft.id))).data
  await main(`(()=>{global.__uploadConsole=[];const w=process.mainModule.require('electron').BrowserWindow.getAllWindows()[0];w.webContents.on('console-message',(_e,level,message)=>{if(message.includes('Upload'))global.__uploadConsole.push(message)});return true})()`)
  await renderer(`(()=>{const input=document.querySelector('input[type="file"]');const dt=new DataTransfer();dt.items.add(new File(['P05_ATTACHMENT'],'测试 附件.md',{type:'text/markdown'}));input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));return true})()`)
  await sleep(1500)
  result.checks.attachment = {shown:await renderer(`document.body.innerText.includes('测试 附件.md')`),console:await main('global.__uploadConsole')}
  const tree=(await request('/api/workspace/tree')).data
  assert.ok(path.resolve(tree.root).startsWith(home+path.sep))
  const names=['test.md','测试.md','测试 文档.md','a b.md','中文 #100%.md','a&b.md','test[1].md']
  for(const name of names)fs.writeFileSync(path.join(tree.root,name),'# P07_CONTENT '+name+'\n<script>window.__p07xss=1</script>\n')
  const entries=(await request('/api/workspace/tree')).data.entries
  const linkLines=entries.filter(e=>names.includes(e.name)).flatMap(e=>[e.preview_url,e.raw_url].map(p=>'[P07_OPEN_'+names.indexOf(e.name)+'_'+(p.startsWith('/file/')?'file':'preview')+'](smart-assistant://backend'+p+')'))
  fs.writeFileSync(path.join(tree.root,'p07-links.md'),linkLines.join('\n\n'))
  await renderer(`(()=>{const file=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='文件');if(file)file.click();else document.querySelector('button[title="工作空间"]').click();})()`)
  await until(()=>renderer(`(()=>{const e=[...document.querySelectorAll('span')].find(e=>e.textContent==='p07-links.md');if(!e)return false;e.click();return true})()`),'P07 link fixture selected in actual workspace UI')
  await until(()=>renderer(`!![...document.querySelectorAll('a')].find(e=>e.textContent==='P07_OPEN_0_preview')`),'Actual Markdown links rendered')
  result.checks.windows=[]
  for(const name of names){
    const entry=entries.find(e=>e.name===name)
    assert.ok(entry,name)
    for (const resourcePath of [entry.preview_url, entry.raw_url]) {
    const r=await request(resourcePath)
    const resourceHeaders=await renderer(`fetch(${JSON.stringify("smart-assistant://backend"+resourcePath)}).then(r=>Object.fromEntries(r.headers))`)
    const linkLabel='P07_OPEN_'+names.indexOf(name)+'_'+(resourcePath.startsWith('/file/')?'file':'preview')
    await renderer(`(()=>{const a=[...document.querySelectorAll('a')].find(e=>e.textContent===${JSON.stringify(linkLabel)});if(!a)throw new Error('P07 link absent');a.click();return true})()`,true)
    await sleep(900)
    const windowState=await main(`(async()=>{const w=process.mainModule.require('electron').BrowserWindow.getAllWindows().find(w=>!w.webContents.getURL().includes('/dist/renderer/index.html'));if(!w)return {absent:true};if(!w.webContents.getURL()){w.destroy();return {url:'',text:'',blank:true}};const state={url:w.webContents.getURL(),...(await w.webContents.executeJavaScript("({text:document.body?.innerText||'',xss:window.__p07xss===1,ipc:typeof window.electronAPI!=='undefined'})"))};w.destroy();return state})()`)
    result.checks.windows.push({name,route:resourcePath.startsWith("/file/")?"file":"preview",backendStatus:r.status,resourceHeaders,...windowState});save()
    }
  }
  if(phase==='before'){
    assert.equal(draft.error,true)
    assert.equal(result.checks.attachment.shown,false)
    assert.ok(result.checks.windows.every(w=>w.backendStatus===200&&!w.text.includes('P07_CONTENT')))
    result.status='REPRODUCED'
  }else{
    assert.equal(draft.error,false)
    assert.equal(draft.id,draft.draft)
    assert.equal(result.checks.attachment.shown,true)
    for(const w of result.checks.windows){assert.ok(w.text.includes('P07_CONTENT '+w.name));assert.equal(w.xss,false);assert.equal(w.ipc,false)}

    const overview=(await request('/api/models')).data
    assert.equal(overview.status,'success')
    assert.ok(overview.providers.every(p=>!p.configured),'isolated profile must contain no real credentials')
    result.checks.catalog=[]
    for(const provider of overview.providers){
      const catalog=(await request('/api/models?catalog_provider='+encodeURIComponent(provider.id))).data
      assert.equal(catalog.status,'success')
      assert.ok(['no_key','not_supported'].includes(catalog.discovery))
      assert.equal(catalog.custom_model_allowed,true)
      for(const model of [provider.models[0], 'p06-custom-nonexistent-'+provider.id].filter(Boolean)){
        const save=(await request('/api/models',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({action:'set_capability',capability:'chat',provider_id:provider.id,model})})).data
        assert.equal(save.status,'success')
        const current=(await request('/api/models')).data.capabilities.chat
        assert.equal(current.current_provider,provider.id)
        assert.equal(current.current_model,model)
      }
      result.checks.catalog.push({provider:provider.id,discovery:catalog.discovery,customSaved:true,inference:'NOT_RUN'})
    }
    assert.equal((await request('/api/models?catalog_provider=p06-unknown')).data.status,'error')
    const failedProvider=await request('/api/models',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({action:'set_provider',provider_id:'openai',api_key:'p06-local-fault-only',api_base:'https://127.0.0.1:9/v1'})})
    assert.equal(failedProvider.data.status,'success')
    const failedCatalog=(await request('/api/models?catalog_provider=openai')).data
    assert.equal(failedCatalog.discovery,'failed')
    assert.equal(failedCatalog.source,'builtin')
    assert.ok(failedCatalog.models.length>0)
    result.checks.discoveryFailure={actualLocalConnectionFailure:true,fallback:failedCatalog.source,discovery:failedCatalog.discovery}
    result.faultInjection.push('Discovery request to closed local HTTPS port; no external provider calls')
    const saved=(await request('/api/models')).data.capabilities.chat
    await quit();await start()
    const restored=(await request('/api/models')).data.capabilities.chat
    assert.equal(restored.current_provider,saved.current_provider)
    assert.equal(restored.current_model,saved.current_model)
    result.checks.providerRestart=true
    // Real failure against an intentionally unavailable local endpoint; no
    // external inference, credentials or model success responses are involved.
    const action=async data=>(await request('/api/models',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)})).data
    assert.equal((await action({action:'set_provider',provider_id:'custom',api_base:'http://127.0.0.1:9/v1',api_key:'p05-local-fault-only'})).status,'success')
    assert.equal((await action({action:'set_capability',capability:'chat',provider_id:'custom',model:'p05-local-fault-only'})).status,'success')
    result.faultInjection.push('Custom provider in isolated profile points to closed localhost port 9; no external model call')
    await renderer(`(()=>{const el=document.querySelector('textarea');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(el,'P05_FIRST_MESSAGE');el.dispatchEvent(new Event('input',{bubbles:true}));return true})()`)
    await sleep(200)
    await renderer(`(()=>{document.querySelector('textarea').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}));return true})()`)
    const persisted=await until(async()=>{const h=(await request('/api/history?session_id='+encodeURIComponent(draft.id))).data;return h.status==='success'&&h.messages?.length?h:null},'First message durable',45000)
    result.checks.firstMessage={persisted:true,messages:persisted.messages.length}
    await quit();await start()
    await until(()=>renderer(`document.body.innerText.includes('P05_FIRST_MESSAGE')`),'First message after restart')
    assert.equal(await renderer(`document.body.innerText.includes('会话记录加载失败')`),false)
    result.checks.firstMessageRestart=true
    result.status='PASSED'
  }
}
run().catch(error => { result.status = 'FAILED'; result.error = error.stack; process.exitCode = 1 }).finally(async () => {
  try { await quit() } catch (error) { result.cleanupError = String(error); result.status = 'FAILED'; process.exitCode = 1; if (child?.exitCode === null) child.kill() }
  save(); console.log(JSON.stringify(result))
})
