// 用真实 Electron、preload、IPC、PythonBackend 与 WebChannel 验证两项边界。
// 不运行产品托盘、更新器或用户配置；只提取当前编译文件中未改写的函数。
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const http = require('node:http')
const electron = require('electron')
const { app, BrowserWindow, session } = electron
const compiledMain = require('./helpers/compiled-main.cjs')
const { PythonBackend } = require('../dist/main/python-manager.js')
const root = path.resolve(__dirname, '../..')
const temporary = process.env.COW_AUDIT_ELECTRON_DIR
assert.ok(temporary && temporary.startsWith(path.join(root, 'tmp/audit-followup-electron-')))
fs.mkdirSync(path.join(temporary, 'userData'))
app.setPath('userData', path.join(temporary, 'userData'))
process.env.COW_DATA_DIR = temporary
process.env.CHANNEL_TYPE = 'web'
process.env.PATH = path.join(root, '.venv/Scripts') + path.delimiter + process.env.PATH
fs.writeFileSync(path.join(temporary, 'config.json'), JSON.stringify({ agent: false, model: '', agent_workspace: path.join(temporary, 'workspace') }))
app.on('window-all-closed', () => {})
const backend = new PythonBackend(root, { startupTimeoutMs: 30000 })
const backendErrors = []
backend.on('error', message => backendErrors.push(String(message)))
const windows = []
let server
const checks = []

app.whenReady().then(async () => {
  let exitCode = 1
  try {
    session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
      const parsed = new URL(details.url)
      callback({ cancel: !['file:', 'data:', 'devtools:'].includes(parsed.protocol) && parsed.hostname !== '127.0.0.1' })
    })
    await backend.start()
    assert.equal(backend.getStatus(), 'ready', backendErrors.join('\n'))
    const context = compiledMain([
      'isTrustedRendererUrl', 'isTrustedRenderer', 'isRecord', 'parseRendererRequest',
      'sanitizedResponseHeaders', 'proxyDesktopRequest', 'setupIPC',
    ], {
      electron_1: electron, isDev: !app.isPackaged, pythonBackend: backend,
      desktopAuthToken: null, desktopSubjectToken: null,
    })
    context.setupIPC()
    require('../dist/main/themes.js').setupThemeIPC()
    function makeWindow(preload = true) {
      const window = new BrowserWindow({ show: false, webPreferences: {
        contextIsolation: true, nodeIntegration: false,
        ...(preload ? { preload: path.resolve(__dirname, '../dist/main/preload.js') } : {}),
      } })
      windows.push(window)
      return window
    }
    const primary = makeWindow()
    context.mainWindow = primary
    const built = path.resolve(__dirname, '../dist/renderer/index.html')
    await primary.loadFile(built)
    const healthScript = `window.electronAPI.backendRequest({ path: '/api/health' })`
    const response = await primary.webContents.executeJavaScript(healthScript)
    assert.equal(response.status, 200)
    assert.deepEqual(JSON.parse(Buffer.from(response.bodyBase64, 'base64').toString()), { status: 'ok' })
    checks.push('built-file -> real preload -> guarded IPC -> pinned TLS -> real WebChannel health')

    const other = makeWindow()
    await other.loadFile(built)
    await assert.rejects(() => other.webContents.executeJavaScript(healthScript), /Untrusted renderer/)
    checks.push('same built URL in another webContents rejected')
    other.destroy()

    const unrelated = path.join(temporary, 'other.html')
    fs.writeFileSync(unrelated, '<!doctype html><title>Untrusted fixture</title>')
    await primary.loadFile(unrelated)
    await assert.rejects(() => primary.webContents.executeJavaScript(healthScript), /Untrusted renderer/)
    checks.push('same webContents on unrelated file rejected')
    primary.destroy()

    // 仅转发到本次真实后端；让浏览器加载实际 chat.html/console.js/vendor。
    server = http.createServer(async (request, reply) => {
      try {
        if (request.method !== 'GET') { reply.writeHead(405).end(); return }
        const result = await backend.request({ path: request.url, method: 'GET' })
        const headers = Object.fromEntries(Object.entries(result.headers).filter(([name]) =>
          !['transfer-encoding', 'content-length', 'connection'].includes(name.toLowerCase())))
        reply.writeHead(result.status, headers)
        reply.end(result.body)
      } catch {
        reply.writeHead(502).end()
      }
    })
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
    const web = makeWindow(false)
    await web.loadURL(`http://127.0.0.1:${server.address().port}/chat`)
    const markdown = await web.webContents.executeJavaScript(`(() => {
      if (typeof createMd !== 'function') throw new Error('real console.js did not load');
      const renderer = createMd();
      const html = renderer.render('**https://example.com**，中文\\n\\nknowledge://document?id=abc&citation_version=3\\n\\n<script>alert(1)</script>');
      const display = document.createElement('div'); display.innerHTML = html; document.body.appendChild(display);
      const citation = display.querySelector('[data-governed-citation="v3"]');
      const started = performance.now(); renderer.render('mailto:'.repeat(16000));
      return { html, link: display.querySelector('a').getAttribute('href'), citation: citation?.getAttribute('href'), scripts: display.querySelectorAll('script').length, ms: performance.now() - started };
    })()`)
    assert.equal(markdown.link, 'https://example.com')
    assert.equal(markdown.citation, 'knowledge://document?id=abc&citation_version=3')
    assert.equal(markdown.scripts, 0)
    assert.ok(markdown.ms < 2000, `bounded sample took ${markdown.ms}ms`)
    checks.push('real Web chat loads vendored bundle, custom citation/link rules and escaped HTML')
    console.log(JSON.stringify({ status: 'passed', electron: process.versions.electron, chrome: process.versions.chrome, checks, repeatedMailto: { count: 16000, ms: markdown.ms }, testDoubles: 'none for trust/IPC/TLS/renderer; loopback HTTP transport adapter serves real backend responses' }))
    exitCode = 0
  } catch (error) {
    console.error(error.stack || error)
  } finally {
    for (const window of windows) if (!window.isDestroyed()) window.destroy()
    if (server) {
      server.closeAllConnections()
      await new Promise(resolve => server.close(resolve))
    }
    await backend.stop()
    assert.equal(backend.getStatus(), 'stopped')
    // Chromium 进程退出前仍持有 Windows 缓存句柄；由父进程等待退出后清理。
    app.exit(exitCode)
  }
}).catch(error => { console.error(error.stack || error); app.exit(1) })
