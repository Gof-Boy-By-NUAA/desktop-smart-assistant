const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const test = require('node:test')
const { buildWebMarkdown } = require('../scripts/build-web-markdown.cjs')
const root = path.resolve(__dirname, '../..')
const source = fs.readFileSync(path.join(root, 'channel/web/static/js/console.js'), 'utf8')
const createMd = source.slice(source.indexOf('const FALLBACK_HLJS ='), source.indexOf('const md = createMd();'))
const vendor = path.join(root, 'channel/web/static/vendor/markdown-it')

function renderer(bundle) {
  const context = vm.createContext({})
  vm.runInContext('window = globalThis;', context)
  vm.runInContext(fs.readFileSync(path.join(root, 'channel/web/static/vendor/highlightjs/highlight.min.js'), 'utf8'), context)
  vm.runInContext(bundle, context)
  vm.runInContext(createMd + '\nmd = createMd();', context)
  return context.md
}

const md = renderer(fs.readFileSync(path.join(vendor, 'markdown-it.min.js'), 'utf8'))

test('发布的 Web 包可从锁定的已修复依赖精确重建', () => {
  for (const [name, content] of Object.entries(buildWebMarkdown())) {
    assert.ok(fs.readFileSync(path.join(vendor, name), 'utf8') === content, `Stale Web asset: ${name}`)
  }
})

test('真实 createMd 保留链接、知识引用和安全属性', () => {
  const link = md.render('**https://example.com**，中文')
  assert.match(link, /href="https:\/\/example.com"/)
  assert.match(link, /target="_blank" rel="noopener noreferrer"/)
  assert.ok(link.includes('中文'))
  const citation = md.render('来源 knowledge://document?id=abc&citation_version=3')
  assert.match(citation, /class="knowledge-citation-link" data-governed-citation="v3"/)
  assert.match(citation, /knowledge:\/\/document\?id=abc&amp;citation_version=3/)
  assert.doesNotMatch(md.render('[危险](javascript:alert(1))'), /href="javascript:/)
  assert.match(md.render('<script>alert(1)</script>'), /&lt;script&gt;/)
  assert.match(md.render('```js\nconst n = 1\n```'), /hljs-keyword/)
  assert.match(md.render('| a | b |\n|---|---|\n| 1 | 2 |'), /<table>/)
})

test('新版本正确处理图片 alt 中 HTML 文本和 Unicode 分隔符', () => {
  assert.match(md.render('![a <b>x</b>](https://example.com/a.png)'), /alt="a &lt;b&gt;x&lt;\/b&gt;"/)
  assert.match(md.render('中文 *强调* 😀'), /<em>强调<\/em>/)
})

test('重复 mailto 输入在有界时间内完成且不生成链接', () => {
  // 有限恶意样本；超时由 VM 强制限制，不让测试永久占用 CPU。
  const context = vm.createContext({ md })
  const html = vm.runInContext('md.render("mailto:".repeat(16000))', context, { timeout: 2000 })
  assert.doesNotMatch(html, /<a /)
})

module.exports = { renderer }
