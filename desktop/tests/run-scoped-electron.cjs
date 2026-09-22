const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { spawn } = require('node:child_process')
const root = path.resolve(__dirname, '../..')
const temporary = fs.mkdtempSync(path.join(root, 'tmp/audit-followup-electron-'))
const env = { ...process.env, COW_AUDIT_ELECTRON_DIR: temporary }
delete env.ELECTRON_RUN_AS_NODE
const child = spawn(require('electron'), [path.join(__dirname, 'scoped-electron.integration.cjs')], {
  cwd: path.resolve(__dirname, '..'), env, stdio: 'inherit', windowsHide: true,
})
console.log(`Owned Electron test PID: ${child.pid}`)
function cleanup() {
  assert.ok(temporary.startsWith(path.join(root, 'tmp') + path.sep))
  // 仅处理本次 mkdtemp 创建的目录；短暂 Windows 文件锁由原生 rm 有界重试。
  fs.rmSync(temporary, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
  assert.equal(fs.existsSync(temporary), false)
}
child.once('error', error => { cleanup(); console.error(error); process.exitCode = 1 })
child.once('exit', (code, signal) => {
  cleanup()
  console.log(JSON.stringify({ electronExitCode: code, signal, cleanup: 'passed' }))
  process.exitCode = code === 0 && signal === null ? 0 : 1
})
