const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const url = require('node:url')
const ts = require('typescript')

// 从当前构建取原始函数，不复制或重写待测权限判断。
module.exports = function compiledMain(names, bindings = {}) {
  const directory = path.resolve(__dirname, '../../dist/main')
  const source = fs.readFileSync(path.join(directory, 'index.js'), 'utf8')
  const parsed = ts.createSourceFile('index.js', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
  const functions = names.map(name => {
    const found = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name)
    if (!found) throw new Error(`Missing compiled function: ${name}`)
    return found.getText(parsed)
  })
  const context = vm.createContext({
    URL, Buffer, Uint8Array, console,
    path_1: { default: path }, url_1: url, __dirname: directory,
    isDev: true, VITE_DEV_PORTS: [5173, 5174, 5175, 5176], mainWindow: null,
    ...bindings,
  })
  vm.runInContext(functions.join('\n'), context)
  return context
}
