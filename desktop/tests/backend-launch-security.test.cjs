const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')

const { PythonBackend } = require('../dist/main/python-manager.js')
const REPO_ROOT = path.resolve(__dirname, '..', '..')
const TEMP_PARENT = path.join(REPO_ROOT, 'tmp', 'delivery-defense')
const TEMP_PREFIX = 'backend-launch-security-'
const PATH_PYTHON_NAME = process.platform === 'win32' ? 'python.exe' : 'python3'
const BUNDLED_NAME = process.platform === 'win32' ? 'smart-assistant-backend.exe' : 'smart-assistant-backend'

// 验证目标是当前编译实现的路径校验和选择逻辑；fixture 只作为真实文件系统数据，绝不执行。
// SHELL 白名单测试不证明 Linux 登录 shell 已执行，fixture 文件也不证明 Python 可运行。
function withEnvironment(values, callback) {
  const previous = new Map(Object.keys(values).map((key) => [key, process.env[key]]))
  try {
    for (const [key, value] of Object.entries(values)) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
    return callback()
  } finally {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
  }
}

function withFixture(callback) {
  fs.mkdirSync(TEMP_PARENT, { recursive: true })
  const directory = fs.mkdtempSync(path.join(TEMP_PARENT, TEMP_PREFIX))
  try {
    return callback(directory)
  } finally {
    // 只递归清理当前测试创建的唯一目录，先验证解析后的绝对边界。
    const resolved = path.resolve(directory)
    assert.equal(path.dirname(resolved), path.resolve(TEMP_PARENT))
    assert.ok(path.basename(resolved).startsWith(TEMP_PREFIX))
    fs.rmSync(resolved, { recursive: true, force: true })
    assert.equal(fs.existsSync(resolved), false, 'fixture cleanup left an owned directory')
  }
}

function file(filename) {
  fs.mkdirSync(path.dirname(filename), { recursive: true })
  fs.writeFileSync(filename, 'path-validation fixture; never execute\n', { flag: 'wx', mode: 0o700 })
  return filename
}

function backendRoot(directory) {
  const root = path.join(directory, '后端 目录 with spaces & symbols')
  fs.mkdirSync(root)
  return root
}

test('login shell selection returns only the fixed approved paths', () => {
  const backend = new PythonBackend(REPO_ROOT)
  for (const approved of ['/bin/bash', '/bin/zsh', '/bin/sh']) {
    withEnvironment({ SHELL: approved }, () => {
      assert.equal(backend.selectLoginShell(), approved)
    })
  }
  for (const unapproved of [
    undefined, '', 'bash', '/usr/bin/bash', '/tmp/bash',
    '/bin/bash -c echo injected', '/bin/bash;echo injected',
    '/bin/bash$(echo injected)', '/bin/bash\necho injected',
    'C:\\Windows\\System32\\cmd.exe',
  ]) {
    withEnvironment({ SHELL: unapproved }, () => {
      assert.equal(backend.selectLoginShell(), '/bin/sh', `unexpected shell selection for ${JSON.stringify(unapproved)}`)
    })
  }
})

test('development launch accepts absolute fixed interpreter filenames with spaces and Chinese characters', () => {
  withFixture((directory) => {
    const backend = new PythonBackend(backendRoot(directory))
    for (const name of ['python', 'python.exe', 'python3', 'python3.exe']) {
      const command = file(path.join(directory, '解释器 with spaces & symbols', name))
      assert.equal(backend.validateLaunchCommand(command, false), path.normalize(command))
    }
  })
})

test('development launch rejects relative paths, command text, suffixes, missing files, directories and NUL', () => {
  withFixture((directory) => {
    const backend = new PythonBackend(backendRoot(directory))
    const script = file(path.join(directory, 'python.cmd'))
    const batch = file(path.join(directory, 'python.bat'))
    const arbitrary = file(path.join(directory, 'other.exe'))
    const directoryCommand = path.join(directory, 'directory', 'python.exe')
    fs.mkdirSync(directoryCommand, { recursive: true })
    for (const command of [
      '', 'python', 'python3', path.join('relative', 'python.exe'),
      script, batch, arbitrary, directoryCommand,
      path.join(directory, 'missing', 'python.exe'),
      `${path.join(directory, 'python.exe')} --version`,
      `${path.join(directory, 'python.exe')}\u0000extra`,
    ]) {
      assert.throws(() => backend.validateLaunchCommand(command, false), undefined, `accepted invalid command ${JSON.stringify(command)}`)
    }
  })
})

test('bundled launch accepts the two supported layouts and retains the absolute path', () => {
  withFixture((directory) => {
    const root = backendRoot(directory)
    const backend = new PythonBackend(root)
    for (const command of [
      file(path.join(root, BUNDLED_NAME)),
      file(path.join(root, 'smart-assistant-backend', BUNDLED_NAME)),
    ]) {
      assert.equal(backend.validateLaunchCommand(command, true), path.normalize(command))
    }
  })
})

test('bundled launch rejects an outside file, unsupported layout, script suffix and directory', () => {
  withFixture((directory) => {
    const root = backendRoot(directory)
    const backend = new PythonBackend(root)
    const outside = file(path.join(directory, 'outside', BUNDLED_NAME))
    const otherLayout = file(path.join(root, 'other-directory', BUNDLED_NAME))
    const script = file(path.join(root, `${BUNDLED_NAME}.cmd`))
    const directoryCommand = path.join(root, 'smart-assistant-backend', BUNDLED_NAME)
    fs.mkdirSync(directoryCommand, { recursive: true })
    for (const command of [outside, otherLayout, script, directoryCommand, BUNDLED_NAME]) {
      assert.throws(() => backend.validateLaunchCommand(command, true), undefined, `accepted invalid bundled command ${command}`)
    }
  })
})

test('bundled launch rejects a real directory junction or symlink escaping the backend root', () => {
  withFixture((directory) => {
    const root = backendRoot(directory)
    const backend = new PythonBackend(root)
    const outsideDirectory = path.join(directory, 'outside backend root')
    const outsideCommand = file(path.join(outsideDirectory, BUNDLED_NAME))
    const link = path.join(root, 'smart-assistant-backend')
    fs.symlinkSync(outsideDirectory, link, process.platform === 'win32' ? 'junction' : 'dir')
    try {
      const escapedCommand = path.join(link, BUNDLED_NAME)
      assert.equal(fs.realpathSync(escapedCommand), fs.realpathSync(outsideCommand))
      assert.throws(() => backend.validateLaunchCommand(escapedCommand, true))
    } finally {
      fs.unlinkSync(link)
    }
  })
})

test('development launch retains a real venv file symlink instead of the versioned target path', { skip: process.platform === 'win32' }, () => {
  withFixture((directory) => {
    const root = backendRoot(directory)
    const backend = new PythonBackend(root)
    const target = file(path.join(directory, 'system interpreter', 'python3.12'))
    const command = path.join(root, '.venv', 'bin', 'python')
    fs.mkdirSync(path.dirname(command), { recursive: true })
    fs.symlinkSync(target, command, 'file')
    try {
      assert.notEqual(fs.realpathSync(command), command)
      assert.equal(backend.validateLaunchCommand(command, false), path.normalize(command))
    } finally {
      fs.unlinkSync(command)
    }
  })
})

test('Python discovery prefers existing venv candidates in order before PATH', () => {
  withFixture((directory) => {
    const root = backendRoot(directory)
    const candidates = [
      file(path.join(root, '.venv', 'bin', 'python')),
      file(path.join(root, '.venv', 'Scripts', 'python.exe')),
      file(path.join(root, 'venv', 'bin', 'python')),
      file(path.join(root, 'venv', 'Scripts', 'python.exe')),
    ]
    const pathCommand = file(path.join(directory, 'PATH interpreter', PATH_PYTHON_NAME))
    withEnvironment({ PATH: path.dirname(pathCommand), SHELL: '/bin/sh' }, () => {
      for (const candidate of candidates) {
        assert.equal(new PythonBackend(root).findPython(), path.normalize(candidate))
        fs.unlinkSync(candidate)
      }
      assert.equal(new PythonBackend(root).findPython(), path.normalize(pathCommand))
    })
  })
})

test('Python discovery skips venv directories and uses a regular file from PATH', () => {
  withFixture((directory) => {
    const root = backendRoot(directory)
    fs.mkdirSync(path.join(root, '.venv', 'bin', 'python'), { recursive: true })
    const pathCommand = file(path.join(directory, 'PATH interpreter', PATH_PYTHON_NAME))
    withEnvironment({ PATH: path.dirname(pathCommand), SHELL: '/bin/sh' }, () => {
      assert.equal(new PythonBackend(root).findPython(), path.normalize(pathCommand))
    })
  })
})

test('Python discovery uses absolute PATH entries in order and never chooses cmd or bat launchers', () => {
  withFixture((directory) => {
    const root = backendRoot(directory)
    const relativeCommand = file(path.join(directory, 'relative PATH interpreter', PATH_PYTHON_NAME))
    const launcherDirectory = path.join(directory, 'script launchers')
    file(path.join(launcherDirectory, 'python.cmd'))
    file(path.join(launcherDirectory, 'python.bat'))
    file(path.join(launcherDirectory, 'python3.cmd'))
    file(path.join(launcherDirectory, 'python3.bat'))
    fs.mkdirSync(path.join(launcherDirectory, PATH_PYTHON_NAME))
    const first = file(path.join(directory, 'first absolute interpreter', PATH_PYTHON_NAME))
    const second = file(path.join(directory, 'second absolute interpreter', PATH_PYTHON_NAME))
    const relativeEntry = path.relative(process.cwd(), path.dirname(relativeCommand))
    assert.equal(path.isAbsolute(relativeEntry), false)
    withEnvironment({
      PATH: [relativeEntry, launcherDirectory, path.dirname(first), path.dirname(second)].join(path.delimiter),
      SHELL: '/bin/sh',
    }, () => {
      const backend = new PythonBackend(root)
      const resolvedEntries = backend.resolveEnvPath().split(path.delimiter)
      assert.ok(resolvedEntries.includes(path.dirname(first)))
      assert.ok(resolvedEntries.indexOf(path.dirname(first)) < resolvedEntries.indexOf(path.dirname(second)))
      assert.equal(backend.findPython(), path.normalize(first))
      fs.unlinkSync(first)
      assert.equal(new PythonBackend(root).findPython(), path.normalize(second))
    })
  })
})

test('POSIX Python discovery skips a non-executable PATH file and chooses the next executable', { skip: process.platform === 'win32' }, () => {
  withFixture((directory) => {
    const root = backendRoot(directory)
    const first = file(path.join(directory, 'non-executable interpreter', 'python3'))
    fs.chmodSync(first, 0o600)
    const second = file(path.join(directory, 'executable interpreter', 'python3'))
    withEnvironment({ PATH: [path.dirname(first), path.dirname(second)].join(path.delimiter), SHELL: '/bin/sh' }, () => {
      assert.equal(new PythonBackend(root).findPython(), path.normalize(second))
    })
  })
})

test('Windows Python discovery returns no command when PATH only has scripts or relative entries', { skip: process.platform !== 'win32' }, () => {
  withFixture((directory) => {
    const root = backendRoot(directory)
    const launcherDirectory = path.join(directory, 'script launchers')
    file(path.join(launcherDirectory, 'python.cmd'))
    file(path.join(launcherDirectory, 'python.bat'))
    const relativeCommand = file(path.join(directory, 'relative interpreter', 'python.exe'))
    const relativeEntry = path.relative(process.cwd(), path.dirname(relativeCommand))
    withEnvironment({ PATH: [launcherDirectory, relativeEntry].join(path.delimiter) }, () => {
      assert.equal(new PythonBackend(root).findPython(), '')
    })
  })
})
