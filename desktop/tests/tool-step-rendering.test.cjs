const test = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path')
const esbuild = require('esbuild')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')

// Real component, React and browser; synthetic JSON wire results. No backend,
// Electron transport or model is replaced or claimed verified by this test.
test('expanded tool card survives structured results and preserves text output', async () => {
  const build = await esbuild.build({
    stdin: {
      contents: `import React from 'react';
        import {createRoot} from 'react-dom/client';
        import {ToolStep} from './src/renderer/src/components/MessageSteps';
        const root = createRoot(document.getElementById('root'));
        window.renderStep = result => root.render(<ToolStep step={{type:'tool',
          name:'vision', status:result === undefined ? 'running' : 'success', result}}/>);
        window.renderStep(undefined);`,
      resolveDir: path.resolve(__dirname, '..'), loader: 'tsx',
    },
    bundle: true, write: false, platform: 'browser',
    define: { 'process.env.NODE_ENV': '"development"' },
  })
  const browser = await chromium.launch({
    headless: true, ...(process.env.BROWSER_CHANNEL ? {channel: process.env.BROWSER_CHANNEL} : {}),
  })
  try {
    const page = await browser.newPage()
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('http://tool-test.local/', route => route.fulfill({
      contentType: 'text/html', body: '<div id="root"></div>',
    }))
    await page.goto('http://tool-test.local/')
    await page.addScriptTag({content: build.outputFiles[0].text})
    await page.getByText('vision', {exact: true}).click()
    const result = {content:'test image', model:'vision-model', provider:'test', usage:{total_tokens:1}}
    for (const value of [result, ['a', {b:2}], 'plain output', 0, false, 'x'.repeat(4100)]) {
      await page.evaluate(value => window.renderStep(value), value)
      const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
      const expected = text.length > 4000 ? text.slice(0, 4000) + '\n… (truncated)' : text
      await page.waitForFunction(expected => document.querySelector('pre')?.textContent === expected, expected,
        {timeout: 3000}).catch(error => { throw new Error(`${error.message}; renderer errors: ${errors.join('; ')}`) })
      assert.ok(await page.locator('#root').evaluate(root => root.childElementCount > 0))
    }
    await page.evaluate(() => window.renderStep(null))
    await page.waitForFunction(() => !document.querySelector('pre'))
    assert.deepEqual(errors, [])
  } finally {
    await browser.close()
  }
})
