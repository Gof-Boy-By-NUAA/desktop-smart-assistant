// External driving only: no product modules, IPC handlers, storage, or auth are replaced.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const os = require('node:os')
const crypto = require('node:crypto')
const net = require('node:net')
const { spawnSync } = require('node:child_process')
const { _electron } = require('playwright')

const baseline = 'd60e99f5c0d87f651239ca9840881cc8be1eddbc'
const root = path.resolve(__dirname, '../..')
const runId = `a01-${new Date().toISOString().replace(/[:.]/g, '-')}-${crypto.randomBytes(4).toString('hex')}`
const runtime = path.join(root, 'acceptance/tmp', runId)
const evidence = path.join(root, 'acceptance/evidence', runId)
const data = path.join(runtime, 'data')
const home = path.join(runtime, 'home')
const profile = path.join(runtime, 'electron-profile')
const python = path.join(root, '.venv/Scripts/python.exe')
let password = crypto.randomBytes(32).toString('hex')
const secrets = [password]
const marker = `A01 local fixture ${runId}`
const reply = `A01 stored response ${runId}`
const sessionId = `a01_${crypto.randomBytes(12).toString('hex')}`
const checks = {}
const observations = []
const manifest = {
  schema_version: 1, baseline_sha: baseline, platform: process.platform,
  architecture: process.arch, windows_version: os.release(), node_version: process.version,
  started_at: new Date().toISOString(), test: 'real-electron-logout-e2e',
  official_driver_reference: 'https://playwright.dev/docs/api/class-electron',
}
const result = {
  status: 'running', passed: false, checks, observations, test_doubles: [],
  fixture: 'Real ConversationStore.append_messages in isolated SQLite, owned by the password-authenticated device. No model request.',
  limitations: { real_provider_verified: false, nsis_install_verified: false,
    multi_principal_isolation_verified: false, customer_environment_verified: false,
    fault_injection_e2e: 'NOT_RUN', transport_timeout_verified: false },
}
let app, page, backendPid
let ownedPids = []
let mainLog = '', rendererLog = ''
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
function redact(value) {
  return String(value).split(/\r?\n/).map(line =>
    /password|bearer|subject.?token|api.?key|cookie|authorization|v3\.[0-9a-f]+\./i.test(line) || secrets.some(secret => line.includes(secret))
      ? '[credential-bearing line omitted]' : line).join('\n')
}
function git(...args) {
  const r = spawnSync('git', ['-c', `safe.directory=${root.replaceAll('\\', '/')}`, ...args], { cwd: root, encoding: 'utf8' })
  assert.equal(r.status, 0, 'git command failed')
  return r.stdout.trim()
}
function command(exe, args, options = {}) {
  const r = spawnSync(exe, args, { cwd: root, encoding: 'utf8', windowsHide: true, ...options })
  if (r.status !== 0) throw new Error(redact(`${exe} failed: ${r.stderr || r.error || r.stdout}`))
  return r.stdout.trim()
}
async function until(fn, timeout = 30000) {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    const value = await fn()
    if (value) return value
    await sleep(200)
  }
  throw new Error('Condition timed out')
}
function alive(pid) { try { process.kill(pid, 0); return true } catch { return false } }
function descendants(pid) {
  return JSON.parse(command('powershell.exe', ['-NoProfile', '-Command',
    `ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_Process -Filter "ParentProcessId = ${Number(pid)}" | Select-Object ProcessId,Name,ExecutablePath)`]) || '[]')
}
function processTree(pid) {
  const children = descendants(pid)
  return children.flatMap(child => [child, ...processTree(child.ProcessId)])
}
async function api(route) {
  return page.evaluate(async route => {
    const response = await window.electronAPI.backendRequest({ path: route, method: 'GET' })
    return { status: response.status, body: JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(response.bodyBase64), c => c.charCodeAt(0)))) }
  }, route)
}
async function shot(name) { await page.screenshot({ path: path.join(evidence, 'screenshots', name), fullPage: true }) }
async function login(firstLoad = false) {
  await page.locator('input[type=password]').waitFor({ state: 'visible', timeout: 120000 })
  await page.locator('input[type=password]').fill(password)
  await page.locator('button[type=submit]').click()
  await page.locator('#chat-input').waitFor({ state: 'visible' })
  // Fresh renderer loads offer setup; the already-dismissed same renderer does not.
  if (firstLoad) await page.getByRole('button', { name: /^(Skip|跳过)$/ }).click()
  const auth = await api('/auth/check')
  assert.equal(auth.body.authenticated, true)
  assert.equal(auth.body.auth_required, true)
  return auth
}
function fileHashes(dir) {
  const entries = []
  for (const name of fs.readdirSync(dir)) {
    const file = path.join(dir, name)
    if (fs.statSync(file).isDirectory()) entries.push(...fileHashes(file))
    else entries.push({ file: path.relative(root, file).replaceAll('\\', '/'), sha256: crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') })
  }
  return entries
}

async function main() {
  assert.equal(process.platform, 'win32')
  assert.equal(git('rev-parse', `${baseline}^{commit}`), baseline)
  assert.equal(git('diff', baseline, '--', '.', ':(exclude)acceptance'), '', 'Product tree differs from frozen baseline')
  manifest.harness_head = git('rev-parse', 'HEAD')
  manifest.branch = git('branch', '--show-current')
  assert.equal(manifest.branch, 'acceptance/windows-real-boundary-d60e99f')
  manifest.harness_sha256 = crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex')
  manifest.build_files = fileHashes(path.join(root, 'desktop/dist'))
  manifest.electron_executable_sha256 = crypto.createHash('sha256').update(fs.readFileSync(path.join(root, 'desktop/node_modules/electron/dist/electron.exe'))).digest('hex')
  for (const port of [5173, 5174, 5175, 5176]) {
    const open = await new Promise(resolve => {
      const s = net.connect({ host: '127.0.0.1', port })
      s.once('connect', () => { s.destroy(); resolve(true) })
      s.once('error', () => resolve(false)); s.setTimeout(1000, () => { s.destroy(); resolve(false) })
    })
    assert.equal(open, false, 'Vite port occupied; refusing to test unrelated renderer')
  }
  for (const dir of [data, home, profile, path.join(runtime, 'temp'), path.join(home, 'AppData/Roaming'), path.join(home, 'AppData/Local'), path.join(evidence, 'screenshots')]) fs.mkdirSync(dir, { recursive: true })
  const env = {}
  for (const key of ['SystemRoot', 'WINDIR', 'SystemDrive', 'COMSPEC', 'PATH', 'PATHEXT', 'USERNAME', 'COMPUTERNAME', 'PROCESSOR_ARCHITECTURE']) {
    if (process.env[key]) env[key] = process.env[key]
  }
  Object.assign(env, { HOME: home, USERPROFILE: home, APPDATA: path.join(home, 'AppData/Roaming'),
    LOCALAPPDATA: path.join(home, 'AppData/Local'), TEMP: path.join(runtime, 'temp'), TMP: path.join(runtime, 'temp'),
    COW_DATA_DIR: data, COW_HOME: data, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' })
  fs.writeFileSync(path.join(data, 'config.json'), JSON.stringify({ channel_type: 'web', web_password: password,
    agent: false, agent_workspace: path.join(data, 'workspace'), self_evolution_enabled: false, knowledge: false,
    cow_lang: 'en', open_ai_api_key: '', deepseek_api_key: '', zhipu_ai_api_key: '' }))
  manifest.python_version = command(python, ['--version'], { env })
  manifest.playwright_version = require('playwright/package.json').version
  app = await _electron.launch({ executablePath: path.join(root, 'desktop/node_modules/electron/dist/electron.exe'),
    args: [path.join(root, 'desktop/dist/main/index.js'), `--user-data-dir=${profile}`], cwd: root, env, timeout: 120000 })
  app.process().stdout.on('data', d => { mainLog += redact(d) })
  app.process().stderr.on('data', d => { mainLog += redact(d) })
  manifest.launcher_pid = app.process().pid
  const mainState = await app.evaluate(({ app }) => ({ pid: process.pid, userData: app.getPath('userData'), version: process.versions.electron, packaged: app.isPackaged }))
  manifest.electron_pid = mainState.pid
  assert.equal(path.resolve(mainState.userData), path.resolve(profile))
  manifest.electron_version = mainState.version
  page = await app.firstWindow({ timeout: 120000 })
  page.on('console', m => { rendererLog += redact(`${m.type()}: ${m.text()}\n`) })
  page.on('pageerror', e => { rendererLog += redact(`pageerror: ${e.message}\n`) })
  await page.waitForLoadState('domcontentloaded')
  assert.ok(page.url().startsWith('file:') && page.url().includes('/desktop/dist/renderer/index.html'))
  await until(() => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().some(w => w.isVisible())))
  checks.real_electron_started = true
  await page.waitForFunction(async () => window.electronAPI && await window.electronAPI.getBackendStatus() === 'ready', null, { timeout: 120000 })
  const children = descendants(mainState.pid)
  const backend = children.find(p => /python/i.test(p.Name))
  assert.ok(backend, 'Python must be a real child of Electron')
  backendPid = backend.ProcessId
  manifest.python_pid = backendPid
  manifest.python_executable = backend.ExecutablePath
  manifest.child_processes = processTree(mainState.pid)
  ownedPids = manifest.child_processes.map(child => child.ProcessId)
  checks.real_python_backend_ready = true
  const unauth = await api('/auth/check')
  assert.equal(unauth.body.auth_required, true)
  assert.equal(unauth.body.authenticated, false)
  observations.push({ stage: 'before_login', auth: unauth })
  await page.locator('input[type=password]').waitFor({ state: 'visible' })
  await shot('01-login.png')
  observations.push({ stage: 'authenticated', auth: await login(true) })
  checks.real_login_succeeded = true
  await shot('02-authenticated.png')

  // Only the non-secret owner ID leaves main. The capability never enters the renderer or evidence.
  const owner = await app.evaluate(({ safeStorage }, encrypted) => {
    const token = safeStorage.decryptString(Buffer.from(encrypted, 'base64'))
    const parts = token.split('.')
    if (parts.length !== 3 || !/^[a-f0-9]{32}$/.test(parts[1])) throw new Error('Unexpected device subject format')
    return `web:${parts[1]}`
  }, fs.readFileSync(path.join(profile, 'desktop-subject-token.bin')).toString('base64'))
  // Fixture uses the existing local persistence implementation, never fabricated HTTP responses.
  command(python, ['-c', [
    'import json,sys', 'from pathlib import Path', 'from agent.memory.conversation_store import ConversationStore',
    'v=json.load(sys.stdin)', 's=ConversationStore(Path(v["db"]))',
    's.append_messages(v["session"], [{"role":"user","content":v["marker"]},{"role":"assistant","content":v["reply"]}], channel_type="web", owner_id=v["owner"])',
    's.rename_session(v["session"], "A01 stored session", owner_id=v["owner"])',
  ].join('\n')], { env, input: JSON.stringify({ db: path.join(data, 'workspace/memory/long-term/index.db'), session: sessionId, marker, reply, owner }) })
  await page.reload()
  await page.locator('#chat-input').waitFor({ state: 'visible' })
  await page.getByRole('button', { name: /^(Skip|跳过)$/ }).click()
  await page.getByText('A01 stored session', { exact: true }).click()
  await page.getByText(marker, { exact: true }).waitFor({ state: 'visible' })
  await page.getByText(reply, { exact: true }).waitFor({ state: 'visible' })
  const oldActive = await page.evaluate(() => localStorage.getItem('cow_session_id'))
  assert.equal(oldActive, sessionId)
  checks.old_session_created = true
  observations.push({ stage: 'before_logout', old_active_session_id: oldActive, protected: await api('/api/sessions') })
  await shot('03-before-logout.png')

  await page.getByTitle(/^(More|更多)$/).click()
  await page.getByText(/^(Sign out|退出登录)$/).click()
  checks.logout_via_ui = true
  await page.locator('input[type=password]').waitFor({ state: 'visible' })
  checks.login_gate_restored = true
  assert.equal(await page.locator('#chat-input').count(), 0)
  assert.equal(await page.getByText(marker, { exact: true }).count(), 0)
  assert.equal(await page.getByText(reply, { exact: true }).count(), 0)
  const newActive = await page.evaluate(() => localStorage.getItem('cow_session_id'))
  assert.notEqual(newActive, oldActive)
  checks.old_active_session_invalidated = true
  const revoked = await api('/auth/check')
  const protectedResult = await api('/api/sessions')
  assert.equal(revoked.body.auth_required, true)
  assert.equal(revoked.body.authenticated, false)
  assert.equal(protectedResult.status, 401)
  checks.backend_auth_revoked = true
  checks.main_no_old_bearer_privilege = true
  observations.push({ stage: 'after_logout', new_active_session_id: newActive, auth: revoked, protected: protectedResult })
  await shot('04-after-logout.png')
  await login()
  assert.equal(await page.evaluate(() => localStorage.getItem('cow_session_id')), newActive)
  assert.equal(await page.getByText(marker, { exact: true }).count(), 0)
  assert.equal(await page.getByText(reply, { exact: true }).count(), 0)
  checks.old_renderer_chat_cleared = true
  checks.relogin_succeeded = true
  // Same password/device principal may explicitly reload its own durable history.
  const sessions = await api('/api/sessions')
  assert.equal(sessions.status, 200)
  assert.ok(sessions.body.sessions.some(s => s.session_id === sessionId))
  observations.push({ stage: 'relogin', auth: await api('/auth/check'), active_session_id: newActive, own_history_visible: true })
  await shot('05-relogin.png')

  // Real backend rejection, not a network/mock substitution: rotate the isolated
  // password through the normal protected config API, then click UI Sign out.
  await page.getByText('A01 stored session', { exact: true }).click()
  await page.getByText(marker, { exact: true }).waitFor({ state: 'visible' })
  password = crypto.randomBytes(32).toString('hex')
  secrets.push(password)
  const rotated = await page.evaluate(async value => {
    const response = await window.electronAPI.backendRequest({ path: '/config', method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ updates: { web_password: value } }) })
    // Do not return the echoed configuration/secret to the driver or evidence.
    const body = JSON.parse(atob(response.bodyBase64))
    return { http_status: response.status, status: body.status }
  }, password)
  assert.equal(rotated.http_status, 200)
  assert.equal(rotated.status, 'success')
  const rejected = page.waitForEvent('pageerror', { predicate: e => /HTTP 401/.test(e.message), timeout: 15000 })
  await page.getByTitle(/^(More|更多)$/).click()
  await page.getByText(/^(Sign out|退出登录)$/).click()
  const logoutError = await rejected
  await page.locator('input[type=password]').waitFor({ state: 'visible' })
  const faultActive = await page.evaluate(() => localStorage.getItem('cow_session_id'))
  assert.notEqual(faultActive, sessionId)
  assert.equal(await page.getByText(marker, { exact: true }).count(), 0)
  const faultAuth = await api('/auth/check')
  const faultProtected = await api('/api/sessions')
  assert.equal(faultAuth.body.authenticated, false)
  assert.equal(faultProtected.status, 401)
  await shot('06-rejected-logout.png')
  await login()
  assert.equal(await page.evaluate(() => localStorage.getItem('cow_session_id')), faultActive)
  assert.equal(await page.getByText(marker, { exact: true }).count(), 0)
  assert.equal(await page.getByText(reply, { exact: true }).count(), 0)
  await shot('07-rejected-logout-relogin.png')
  checks.rejected_logout_fail_closed = true
  result.limitations.fault_injection_e2e = 'VERIFIED_HTTP_401_AFTER_PASSWORD_ROTATION'
  observations.push({ stage: 'rejected_logout', injected_failure: 'normal isolated password rotation invalidates existing bearer',
    observed_error: logoutError.message, auth: faultAuth, protected: faultProtected,
    new_active_session_id: faultActive, relogin_with_new_password: true })
  result.status = 'completed'
  result.passed = true
}

(async () => {
  try { await main() } catch (error) {
    result.status = 'failed'; result.error = redact(error.stack || error)
    if (page && !page.isClosed()) {
      try { await shot('failure.png') } catch (captureError) { result.screenshot_error = redact(captureError.message) }
    }
  } finally {
    if (app) {
      const electronPid = manifest.electron_pid || app.process().pid
      try {
        await app.close()
        await until(() => !alive(electronPid) && ownedPids.every(pid => !alive(pid)) && (!backendPid || !alive(backendPid)), 20000)
        checks.owned_processes_exited = true
      } catch (error) { result.cleanup_error = redact(error.message); result.passed = false }
    }
    // Delete only this invocation's uniquely created runtime directory after successful shutdown.
    if (result.passed) {
      const expected = path.resolve(root, 'acceptance/tmp') + path.sep
      assert.ok(path.resolve(runtime).startsWith(expected) && path.basename(runtime) === runId)
      try { fs.rmSync(runtime, { recursive: true, force: false }); checks.runtime_data_removed = true }
      catch (error) { result.cleanup_error = redact(error.message); result.passed = false }
    }
    if (!result.passed) result.runtime_retained = runtime
    if (!result.passed) result.status = 'failed'
    fs.mkdirSync(evidence, { recursive: true })
    manifest.finished_at = new Date().toISOString()
    fs.writeFileSync(path.join(evidence, 'manifest.json'), JSON.stringify(manifest, null, 2))
    fs.writeFileSync(path.join(evidence, 'result.json'), JSON.stringify(result, null, 2))
    fs.writeFileSync(path.join(evidence, 'main.log'), mainLog)
    fs.writeFileSync(path.join(evidence, 'renderer.log'), rendererLog + '\n[external driver observations]\n' + JSON.stringify(observations, null, 2))
    const hashes = fileHashes(evidence)
    fs.writeFileSync(path.join(evidence, 'sha256sums.txt'), hashes.map(v => `${v.sha256}  ${path.relative(evidence, path.join(root, v.file)).replaceAll('\\', '/')}`).join('\n') + '\n')
    console.log(JSON.stringify({ passed: result.passed, evidence, checks, error: result.error, cleanup_error: result.cleanup_error }))
    process.exitCode = result.passed ? 0 : 1
  }
})()
