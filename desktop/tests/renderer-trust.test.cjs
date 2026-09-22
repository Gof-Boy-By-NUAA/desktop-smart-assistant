const assert = require('node:assert/strict')
const path = require('node:path')
const { pathToFileURL } = require('node:url')
const test = require('node:test')
const compiledMain = require('./helpers/compiled-main.cjs')
const built = pathToFileURL(path.resolve(__dirname, '../dist/renderer/index.html')).href

for (const isDev of [true, false]) {
  test(`只信任确切的内置文件，开发模式=${isDev}`, () => {
    const context = compiledMain(['isTrustedRendererUrl'], { isDev })
    assert.equal(context.isTrustedRendererUrl(built), true)
    assert.equal(context.isTrustedRendererUrl(built + '#/chat'), true)
    for (const candidate of [
      built.replace('index.html', 'other.html'), built + '/extra',
      built.replace('/renderer/', '/renderer-private/'),
      'file://remote-host/share/index.html', 'https://example.com',
      'data:text/html,hello', 'javascript:void(0)', 'not a URL',
    ]) assert.equal(context.isTrustedRendererUrl(candidate), false, candidate)
  })
}

test('仅开发模式保留原有 localhost Vite 端口白名单', () => {
  const development = compiledMain(['isTrustedRendererUrl'])
  const packaged = compiledMain(['isTrustedRendererUrl'], { isDev: false })
  for (const port of [5173, 5174, 5175, 5176]) {
    assert.equal(development.isTrustedRendererUrl(`http://localhost:${port}/`), true)
    assert.equal(packaged.isTrustedRendererUrl(`http://localhost:${port}/`), false)
  }
  for (const candidate of ['http://localhost:5177/', 'http://127.0.0.1:5173/',
    'http://localhost.evil:5173/', 'https://localhost:5173/']) {
    assert.equal(development.isTrustedRendererUrl(candidate), false, candidate)
  }
})

test('同一文件 URL 不授予其他 webContents 权限', () => {
  const context = compiledMain(['isTrustedRendererUrl', 'isTrustedRenderer'])
  const primary = { getURL: () => built }
  context.mainWindow = { webContents: primary }
  assert.equal(context.isTrustedRenderer(primary), true)
  assert.equal(context.isTrustedRenderer({ getURL: () => built }), false)
  primary.getURL = () => built.replace('index.html', 'other.html')
  assert.equal(context.isTrustedRenderer(primary), false)
})
