// 实际完整制品验收：不替换 Electron、IPC、后端、协议或存储。
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { spawn, spawnSync } = require('node:child_process')

const [artifact, output, phase = 'after'] = process.argv.slice(2).map((v, i) => i < 2 ? path.resolve(v) : v)
if (!artifact || !output) throw new Error('Usage: node p06-models-artifact.cjs <win-unpacked> <owned-output> [before|after]')
if (fs.existsSync(path.join(output, 'results.json'))) throw new Error('Use a fresh output directory to preserve earlier evidence')
fs.mkdirSync(output, { recursive: true })
const home = path.join(output, 'profile')
for (const sub of ['AppData/Roaming', 'AppData/Local', 'Temp', 'electron']) fs.mkdirSync(path.join(home, sub), { recursive: true })
const result = { phase, artifact, checks: {}, runs: [], testDoubles: 'NONE', faultInjection: [] }
const save = () => fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(result, null, 2))
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
let child, rpc, closed, upstream

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
  // Local fault servers must bypass the host's Windows proxy settings.
  env.NO_PROXY = 'localhost,127.0.0.1'
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
  const skipped = await renderer(`(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='跳过');if(b){b.click();return true}return false})()`)
  // Wait for the first-run language save before issuing configuration writes.
  if (skipped) await until(() => logs.includes('Language switched to:'), 'Onboarding language saved')
  assert.equal((await request('/api/health')).data.status, 'ok')
}
async function quit() {
  if (!child || child.exitCode !== null) return
  await main(`(()=>{setTimeout(()=>process.mainModule.require('electron').app.quit(),100);return true})()`)
  rpc.close(); rpc = null
  const code = await Promise.race([closed, sleep(15000).then(() => { throw new Error('Normal exit timeout') })])
  assert.equal(code, 0); child = null
}
const card = "[...document.querySelectorAll('h3')].find(e=>e.textContent.trim()==='主模型')?.closest('.rounded-card')";
const menu = "[...document.querySelectorAll('div')].find(e=>e.style.position==='fixed'&&e.className.includes('max-h-64'))";
async function openPicker(index){
 await renderer(`(()=>{const c=${card};if(!c)throw Error('Main model card absent');c.querySelectorAll('button')[${index}].click();return true})()`);
 return until(()=>renderer(`(()=>{const m=${menu};return m?Array.from(m.children).map(e=>e.innerText.trim()):null})()`),'Model menu visible');
}
async function choose(value){
 await renderer(`(()=>{const m=${menu};const e=Array.from(m.children).find(e=>e.innerText.trim()===${JSON.stringify(value)});if(!e)throw Error('Option absent');e.click();return true})()`);
}
async function saveModel(expectedProvider,expectedModel){
 await renderer(`(()=>{const c=${card};[...c.querySelectorAll('button')].find(e=>e.textContent.trim()==='保存').click();return true})()`);
 await until(async()=>{const c=(await request('/api/models')).data.capabilities.chat;return c.current_provider===expectedProvider&&c.current_model===expectedModel},'Actual model configuration saved');
}
async function settings(){
 await renderer("location.hash='/settings?tab=models'");
 await until(()=>renderer(`Boolean(${card})`),'Main model settings');
}
async function run(){
 await start();
 const action=async data=>(await request('/api/models',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)})).data;
 assert.equal((await action({action:'set_provider',provider_id:'zhipu',api_key:'p06-local-only',api_base:'http://127.0.0.1:9/v4'})).status,'success');
 assert.equal((await action({action:'set_capability',capability:'chat',provider_id:'zhipu',model:'glm-5.2'})).status,'success');
 await settings();
 const models=await openPicker(1);
 result.checks.zhipuOptions=models;
 if(phase==='before'){
  assert.equal(models.includes('glm-5.3'),false);
  result.status='REPRODUCED';return;
 }
 for(const model of ['glm-5.3','glm-5.3-flash','glm-5.3-flashx'])assert.ok(models.includes(model));
 await choose('glm-5.3');await saveModel('zhipu','glm-5.3');
 assert.equal(await renderer(`document.body.innerText.includes('此模型始终开启思考')`),true);
 result.checks.newModelSelected=true;
 await openPicker(1);await choose('自定义');
 await renderer(`(()=>{const e=(${card}).querySelector('input');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'p06-not-a-real-model');e.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
 await sleep(100);await saveModel('zhipu','p06-not-a-real-model');
 result.checks.customInputSaved=true;
 await quit();await start();await settings();
 assert.equal(await renderer(`(${card}).innerText.includes('p06-not-a-real-model')`),true);
 result.checks.customRestart=true;
 await renderer("location.hash='/'");
 await until(()=>renderer("!!document.querySelector('textarea')"),'Chat after settings');
 assert.equal((await action({action:'set_provider',provider_id:'openai',api_key:'p06-local-only',api_base:'https://127.0.0.1:9/v1'})).status,'success');
 await settings();await openPicker(0);await choose('OpenAI');
 await until(()=>renderer("document.body.innerText.includes('模型发现失败')"),'Actual discovery connection error displayed');
 result.checks.discoveryFailureVisible=true;
 const options=await openPicker(1);assert.ok(options.includes('自定义'));assert.ok(options.length>1);
 await choose('自定义');
 await renderer(`(()=>{const e=(${card}).querySelector('input');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'p06-openai-custom');e.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
 await sleep(100);await saveModel('openai','p06-openai-custom');
 result.checks.customAfterFailure=true;
 await openPicker(0);await choose('智谱AI');await saveModel('zhipu','glm-5.3');
 result.checks.providerSwitch=true;
 await quit();await start();await settings();
 assert.equal(await renderer(`(${card}).innerText.includes('glm-5.3')`),true);
 result.checks.finalRestart=true;
 // The upstream deliberately rejects every request. It only records the real
 // packaged backend/SDK wire payload and never simulates model success.
 const capturedRequests=[];
 result.testDoubles='Local upstream returns HTTP 404 MODEL_NOT_FOUND; no model success or quality claim';
 result.faultInjection.push('Dummy credentials in isolated profile; closed localhost discovery port; local model rejection server');
 upstream=require('node:http').createServer((req,res)=>{
  let body='';req.on('data',b=>body+=b);req.on('end',()=>{
   capturedRequests.push({url:req.url,body:JSON.parse(body)});
   res.writeHead(404,{'content-type':'application/json'});
   res.end(JSON.stringify({error:{message:'P06_MODEL_NOT_FOUND',type:'invalid_request_error',code:'model_not_found'}}));
  });
 });
 await new Promise(resolve=>upstream.listen(0,'127.0.0.1',resolve));
 assert.equal((await action({action:'set_provider',provider_id:'zhipu',api_key:'p06-local-only',api_base:'http://127.0.0.1:'+upstream.address().port+'/v4'})).status,'success');
 await renderer("location.hash='/'");
 await until(()=>renderer("!!document.querySelector('textarea')"),'Chat ready for real SDK request');
 await renderer(`(()=>{const e=document.querySelector('textarea');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(e,'P06_WIRE');e.dispatchEvent(new Event('input',{bubbles:true}));return true})()`);
 await sleep(100);
 await renderer(`document.querySelector('textarea').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}))`);
 const captured=await until(()=>capturedRequests.find(r=>r.body.stream===true),'Packaged backend streaming SDK request',45000);
 assert.equal(captured.body.model,'glm-5.3');
 assert.equal(captured.body.thinking.type,'enabled');
 assert.equal(captured.body.reasoning_effort,'low');
 result.checks.wire={path:captured.url,model:captured.body.model,thinking:captured.body.thinking,reasoning_effort:captured.body.reasoning_effort};
 try {
  // Web terminal delivery uses durable settlement, not the raw vendor string.
  await until(()=>renderer("document.body.innerText.includes('执行结果不确定（Agent execution interrupted before durable completion；请求编号 ')") ,'Durable failure shown',60000);
 } finally {
  result.checks.errorUi=await renderer('document.body.innerText');
 }
 result.checks.modelErrorVisible=true;
 result.checks.vendorErrorDetailVisible=result.checks.errorUi.includes('P06_MODEL_NOT_FOUND');
 result.testDoubles='Local upstream returns HTTP 404 MODEL_NOT_FOUND; no model success or quality claim';
 result.faultInjection.push('Only dummy credentials in isolated profile; discovery connects to closed localhost port; no external model call');
 result.status='PASSED';
}
run().catch(error=>{result.status='FAILED';result.error=error.stack;process.exitCode=1}).finally(async()=>{
 try{await quit()}catch(error){result.cleanupError=String(error);result.status='FAILED';process.exitCode=1;if(child?.exitCode===null)child.kill()}
 if(upstream)await new Promise(resolve=>upstream.close(resolve));
 save();console.log(JSON.stringify(result));
});
