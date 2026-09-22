const assert = require('node:assert/strict')
const test = require('node:test')
const semver = require('semver')
const MarkdownIt = require('markdown-it')

test('installed and locked linkify-it include GHSA-v245-v573-v5vm fix', () => {
  const installed = require('linkify-it/package.json').version
  const locked = require('../package-lock.json').packages['node_modules/linkify-it'].version
  assert.ok(semver.gte(installed, '5.0.2'))
  assert.equal(installed, locked)
})

test('markdown linkification keeps ordinary links and escapes raw HTML', () => {
  const md = new MarkdownIt({ html: false, linkify: true })
  assert.match(md.render('https://example.com'), /href="https:\/\/example.com"/)
  assert.match(md.render('<script>alert(1)</script>'), /&lt;script&gt;/)
  assert.doesNotMatch(md.render('mailto:'.repeat(8000)), /<a /)
})
