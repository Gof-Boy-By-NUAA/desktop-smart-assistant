const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

const REPO_ROOT = path.resolve(__dirname, '..', '..')
const CONSOLE_SOURCE = fs.readFileSync(
  path.join(REPO_ROOT, 'channel', 'web', 'static', 'js', 'console.js'),
  'utf8'
)

function loadQueueHelpers(translations) {
  const start = CONSOLE_SOURCE.indexOf('function formatQueuedExecutionStatus(queuePosition)')
  const end = CONSOLE_SOURCE.indexOf('function addLoadingIndicator()', start)
  assert.notEqual(start, -1, 'queued status formatter must be present')
  assert.notEqual(end, -1, 'queued status helper must precede loading indicator')

  const sandbox = {
    Number,
    String,
    t: (key) => translations[key] || key,
  }
  vm.createContext(sandbox)
  vm.runInContext(
    `${CONSOLE_SOURCE.slice(start, end)}\nthis.formatQueuedExecutionStatus = formatQueuedExecutionStatus;\nthis.setLoadingExecutionState = setLoadingExecutionState;`,
    sandbox,
    { filename: 'console-queue-helpers.js' }
  )
  return sandbox
}

function classList() {
  const values = new Set()
  return {
    add(...names) { names.forEach((name) => values.add(name)) },
    remove(...names) { names.forEach((name) => values.delete(name)) },
    contains(name) { return values.has(name) },
  }
}

function loadingElement() {
  const dots = { classList: classList() }
  const status = { classList: classList(), textContent: '' }
  status.classList.add('hidden')
  return {
    dots,
    status,
    element: {
      dataset: {},
      querySelector(selector) {
        if (selector === '.loading-dots') return dots
        if (selector === '.loading-status') return status
        return null
      },
    },
  }
}

test('queued Web response visibly replaces generation dots without declaring completion', () => {
  const { setLoadingExecutionState } = loadQueueHelpers({
    execution_queued: 'Queued; waiting for the earlier task…',
    execution_queued_position: 'Queued at position {position}; waiting for the earlier task…',
  })
  const { element, dots, status } = loadingElement()

  setLoadingExecutionState(element, 'queued', 2)

  assert.equal(dots.classList.contains('hidden'), true)
  assert.equal(status.classList.contains('hidden'), false)
  assert.equal(status.textContent, 'Queued at position 2; waiting for the earlier task…')
  assert.equal(element.dataset.executionState, 'queued')

  // A non-queued response is intentionally ignored by this function: it must
  // not be able to overwrite a durable queued declaration with a fake success.
  setLoadingExecutionState(element, 'completed', 1)
  assert.equal(status.textContent, 'Queued at position 2; waiting for the earlier task…')
  assert.equal(element.dataset.executionState, 'queued')
})
