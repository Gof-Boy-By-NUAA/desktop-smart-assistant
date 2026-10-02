const test = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path')
const http = require('node:http')
const esbuild = require('esbuild')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')

// Real React, ErrorBoundary, HashRouter, browser reload and browser clipboard API.
// Synthetic throwing child and a local request fixture inject the render failure.
// No Electron, backend, conversation delivery or LLM behavior is claimed verified.
test('root error boundary contains rendering failures and exposes only safe diagnostics', async t => {
  const canary = 'private-message-canary credential-canary https://private-url-canary/'
  const build = await esbuild.build({
    stdin: {
      contents: `import React from 'react';
        import {createRoot} from 'react-dom/client';
        import {HashRouter, useLocation} from 'react-router-dom';
        import ErrorBoundary from './src/renderer/src/components/ErrorBoundary';
        function ThrowingChild(props) {
          const error = new Error(props.secret);
          error.stack = 'private-stack-canary';
          throw error;
        }
        function HealthyChild() {
          const location = useLocation();
          return <div data-testid="healthy">正常会话 {location.pathname}
            <button onClick={() => fetch('/fixture-submit', {method:'POST', body:'synthetic request'})}>发送测试请求</button>
          </div>;
        }
        const root = createRoot(document.getElementById('root'));
        const render = failed => root.render(<React.StrictMode><ErrorBoundary><HashRouter>
          {failed ? <ThrowingChild secret={${JSON.stringify(canary)}}/> : <HealthyChild/>}
        </HashRouter></ErrorBoundary></React.StrictMode>);
        window.triggerRenderFailure = () => {
          sessionStorage.setItem('error-boundary-fixture-failed', '1');
          render(true);
        };
        render(sessionStorage.getItem('error-boundary-fixture-failed') === '1');`,
      resolveDir: path.resolve(__dirname, '..'), loader: 'tsx',
    },
    bundle: true, write: false, platform: 'browser',
    define: { 'process.env.NODE_ENV': '"development"' },
  })
  const requests = { documents: 0, submits: 0 }
  const server = http.createServer((request, response) => {
    if (request.url === '/bundle.js') {
      response.writeHead(200, {'Content-Type': 'application/javascript'})
      response.end(build.outputFiles[0].text)
    } else if (request.url === '/fixture-submit' && request.method === 'POST') {
      requests.submits += 1
      request.resume()
      response.writeHead(204)
      response.end()
    } else if (request.url === '/') {
      requests.documents += 1
      response.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'})
      response.end('<!doctype html><div id="root"></div><script src="/bundle.js"></script>')
    } else {
      response.writeHead(404)
      response.end()
    }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const origin = `http://127.0.0.1:${server.address().port}`
  let browser
  try {
    browser = await chromium.launch({
      headless: true, ...(process.env.BROWSER_CHANNEL ? {channel: process.env.BROWSER_CHANNEL} : {}),
    })
    t.diagnostic(`browser=${browser.version()}; secure loopback origin; native clipboard API; fault injection=synthetic child Error`)

    await t.test('healthy child and hash routing remain visible', async () => {
      const context = await browser.newContext()
      try {
        const page = await context.newPage()
        const errors = []
        page.on('pageerror', error => errors.push(error.message))
        await page.goto(`${origin}/#/session/healthy`)
        await page.getByTestId('healthy').waitFor()
        assert.match(await page.getByTestId('healthy').innerText(), /正常会话 \/session\/healthy/)
        assert.equal(await page.getByRole('heading', {name:'页面渲染异常'}).count(), 0)
        assert.deepEqual(errors, [])
      } finally {
        await context.close()
      }
    })

    await t.test('render failure leaves root visible, copies whitelisted data, and reloads the same hash without resubmitting', async () => {
      const context = await browser.newContext()
      try {
        await context.grantPermissions(['clipboard-read', 'clipboard-write'], {origin})
        const page = await context.newPage()
        const url = `${origin}/#/session/private-url-canary`
        await page.goto(url)
        await page.getByTestId('healthy').waitFor()
        const submitsBefore = requests.submits
        const response = page.waitForResponse(response => response.url() === `${origin}/fixture-submit`)
        await page.getByRole('button', {name:'发送测试请求'}).click()
        assert.equal((await response).status(), 204)
        assert.equal(requests.submits, submitsBefore + 1)
        await page.evaluate(() => window.triggerRenderFailure())
        await page.getByRole('heading', {name:'页面渲染异常'}).waitFor()
        assert.ok(await page.locator('#root').evaluate(root => root.childElementCount > 0))
        assert.equal(await page.getByTestId('healthy').count(), 0)
        const diagnostic = page.getByRole('textbox', {name:'安全诊断日志'})
        await page.waitForFunction(() => document.querySelector('textarea')?.value.length > 0)
        const safeText = await diagnostic.inputValue()
        assertSafeDiagnostic(safeText)
        assert.doesNotMatch(await page.locator('#root').innerText(), /private-|credential-canary/)
        await page.getByRole('button', {name:'复制诊断日志'}).click()
        await page.getByRole('status').filter({hasText:'诊断日志已复制。'}).waitFor()
        const clipboardText = await page.evaluate(() => navigator.clipboard.readText())
        assertSafeDiagnostic(clipboardText)
        // Native Windows clipboard may normalize LF to CRLF; verify the exact data.
        assert.deepEqual(JSON.parse(clipboardText), JSON.parse(safeText))

        const documentsBefore = requests.documents
        await Promise.all([
          page.waitForEvent('load'),
          page.getByRole('button', {name:'重载当前会话'}).click(),
        ])
        await page.getByRole('heading', {name:'页面渲染异常'}).waitFor()
        assert.equal(page.url(), url)
        assert.equal(requests.documents, documentsBefore + 1)
        assert.equal(requests.submits, submitsBefore + 1)
      } finally {
        await context.close()
      }
    })

    await t.test('native denied clipboard reports failure and keeps safe text available for manual copy', async () => {
      const context = await browser.newContext()
      let cdp
      let browserContextId
      try {
        const page = await context.newPage()
        cdp = await context.newCDPSession(page)
        browserContextId = (await cdp.send('Target.getTargetInfo')).targetInfo.browserContextId
        assert.ok(browserContextId)
        await cdp.send('Browser.setPermission', {
          permission: {name:'clipboard-read'}, setting:'denied', origin, browserContextId,
        })
        await cdp.send('Browser.setPermission', {
          permission: {name:'clipboard-write', allowWithoutSanitization:true},
          setting:'denied', origin, browserContextId,
        })
        await cdp.send('Browser.setPermission', {
          permission: {name:'clipboard-write', allowWithoutSanitization:false},
          setting:'denied', origin, browserContextId,
        })
        await page.goto(`${origin}/#/session/denied`)
        await page.getByTestId('healthy').waitFor()
        await page.evaluate(() => window.triggerRenderFailure())
        await page.getByRole('heading', {name:'页面渲染异常'}).waitFor()
        await page.getByRole('button', {name:'复制诊断日志'}).click()
        await page.getByRole('status').filter({hasText:'无法自动复制，请选择下方诊断日志后手动复制。'}).waitFor()
        assert.equal(await page.getByText('诊断日志已复制。', {exact:true}).count(), 0)
        const diagnostic = page.getByRole('textbox', {name:'安全诊断日志'})
        assertSafeDiagnostic(await diagnostic.inputValue())
        await diagnostic.selectText()
        assert.equal(await diagnostic.evaluate(element => element.selectionEnd - element.selectionStart), (await diagnostic.inputValue()).length)
      } finally {
        if (cdp) {
          await cdp.send('Browser.resetPermissions', {browserContextId})
          await cdp.detach()
        }
        await context.close()
      }
    })
  } finally {
    if (browser) await browser.close()
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
  }
})

function assertSafeDiagnostic(text) {
  const diagnostic = JSON.parse(text)
  assert.deepEqual(Object.keys(diagnostic).sort(), ['code', 'correlation_id', 'event', 'time'])
  assert.equal(diagnostic.event, 'renderer_render_error')
  assert.equal(diagnostic.code, 'REACT_RENDER_ERROR')
  assert.ok(Number.isFinite(Date.parse(diagnostic.time)))
  assert.match(diagnostic.correlation_id, /^[0-9a-f-]{36}$/)
  assert.doesNotMatch(text, /private-|credential-canary|synthetic request/)
}
