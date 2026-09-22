# 两项授权续修验证

范围仅为原审计 P01 Electron 开发模式 IPC、P02 Web Markdown 性能缺陷。

| 项目 | 证据等级 | 修复验证 | 验证等级 |
|---|---|---|---|
| P01 IPC 信任规则 | CONFIRMED | 通过 | INTEGRATION_VERIFIED |
| P02 Web Markdown | CONFIRMED | 通过 | INTEGRATION_VERIFIED |

**本轮任务状态：COMPLETED。验证等级：INTEGRATION_VERIFIED。** 未 commit、push、发布或部署；没有升级其他依赖，没有修改原有 console.js 或其他业务模块。不是正式安装包、更新发布或生产环境验收。

## 当前规则与初始状态

重新读取了以下当前文件：

- `C:\Users\UnlimitedPower\Documents\桌面智能助手\AGENTS.md`
- `C:\Users\UnlimitedPower\Documents\桌面智能助手\SmartAssistant\AGENTS.md`
- 上述两个目录中的 `.agent-rules/engineering.md`、`testing.md`、`runtime.md`、`research.md`、`language.md`；内容逐份比对一致。
- `C:\Users\UnlimitedPower\Documents\桌面智能助手\SmartAssistant\docs\audits\2026-09-22-code-security-audit.md`

先检查外层与嵌套仓库 git status，再保存定向文件初始 SHA-256。首轮审计修改和用户已有文档/规则文件均保留。本次没有读取历史记忆来替代当前文件。

## P01：最小权限修复

- 根因：开发模式过早返回 Vite URL 判断，阻断同一启动流程中的内置构建 HTML。
- 生产修改：`desktop/src/main/index.ts` 一行，将开发分支限定为 `isDev && parsed.protocol === 'http:'`。
- 内置 file URL 必须继续等于 `dist/renderer/index.html` 的规范化绝对路径；没有使用目录前缀、任意 file URL 或任意 localhost 的白名单。
- 原有 `sender === mainWindow.webContents` 检查保持不变。开发 Vite 端口名单未扩大，打包模式仍不接受 Vite。
- 修复前定向测试：2 passed、2 failed，失败为开发内置文件和对应主窗口被错误拒绝；日志 `tmp/audit-followup-ipc-red.log`。
- 修复后单元检查覆盖开发/打包模式、确切文件、错误文件/相邻目录、远程文件、非法 scheme、Vite 端口及窗口身份。
- 真实集成：隐藏 Electron 窗口加载实际构建文件，实际 preload → ipcRenderer → 当前编译的 setupIPC/权限检查 → PythonBackend 认证 TLS → 真实 WebChannel `/api/health`，返回 200 和正确正文。其他窗口加载同一文件仍被拒绝；同一窗口加载无关临时文件也被拒绝。

## P02：跨主版本兼容性与最小升级

先读取 [markdown-it 14.2.0 变更记录](https://raw.githubusercontent.com/markdown-it/markdown-it/14.2.0/CHANGELOG.md) 和 [linkify-it 安全公告](https://github.com/markdown-it/linkify-it/security/advisories/GHSA-v245-v573-v5vm)，再检查实际 package.json、锁文件和 console.js 调用。

- 14.0 改为 ESM，保留 CJS；核心 API 签名未变，emoji 插件例外与项目无关。
- 现代浏览器要求及 CommonMark 更新会影响部分边缘行为；图片 alt、Unicode 和实体解析也有修复。没有承诺所有输入与旧版逐字相同。
- 当前 Web 使用全局 `window.markdownit`、core.ruler、Token、renderer 规则；没有内部模块导入或 emoji 插件。
- 新包从**已安装且与锁文件一致**的 markdown-it 14.2.0 + linkify-it 5.0.2 构建为浏览器 IIFE，保留同名全局 API。不能直接复制上游预构建文件并假设它含 5.0.2。
- `console.js` 无须兼容性修改；20 个实际 createMd 样本输出一致，涵盖中文 URL、知识引用、文件 URL、表格、列表、强调、实体、HTML 转义及安全链接。
- 新增 `desktop/scripts/build-web-markdown.cjs`、`markdown-it/LICENSES.txt`，更新 vendor README。构建检查所有实际打包依赖的版本与锁文件相符；许可证包含 entities 的 BSD-2-Clause 等，未统一假称全部 MIT。
- 定向测试确认当前静态文件能够精确重建，保留 link/citation 安全属性、代码高亮和 HTML 转义。
- 真实 Electron Chromium 浏览器加载由真实 WebChannel 返回的 `/chat`、console.js 和静态包，检查实际 DOM 的链接、知识引用及无脚本注入。16000 次 mailto 文本有界样本耗时约 11.2 ms。

### 有限性能复核

同一机器、Windows x64、Node 26.4.0、并发 1，实际 createMd；每版本/尺寸预热 1 次、采样 5 次，使用 performance.now。没有替代 parser 或 linkify。

| 文本字节数 | 旧版中位数 ms | 新版中位数 ms | 旧版 p95 ms | 新版 p95 ms |
|---|---:|---:|---:|---:|
| 14000 | 6.03 | 1.03 | 6.23 | 1.26 |
| 28000 | 21.16 | 1.12 | 21.66 | 1.50 |
| 56000 | 82.03 | 2.36 | 82.96 | 2.82 |
| 112000 | 329.67 | 5.01 | 332.57 | 5.30 |

原始样本见 `tmp/audit-followup-compatibility.json`。这是有界回归证据，不推断整个系统吞吐、并发性能或所有恶意输入的复杂度。

## 实际验证命令与结果

以下命令 cwd 为 `SmartAssistant/desktop`，除最后注明的一条。

| 命令 | 实际结果 |
|---|---|
| `npm.cmd run build` | renderer Vite + main tsc 均成功，exit 0；`tmp/audit-followup-build.log` |
| `./node_modules/.bin/tsc.cmd --noEmit -p tsconfig.json` | exit 0；`tmp/audit-followup-typecheck.log` |
| `node --test tests/renderer-trust.test.cjs tests/web-markdown.test.cjs tests/markdown-security.test.cjs tests/main-broker-security.test.cjs tests/trusted-backend.test.cjs tests/web-queue-ui.test.cjs` | 21 passed、0 failed、0 skipped；`tmp/audit-followup-node.log`。子进程 PATH 指向项目 .venv |
| `node tests/run-scoped-electron.cjs` | 实际 Electron 33.4.11 / Chromium 130.0.6723.191；4 项边界检查通过，Electron exit 0、清理成功；`tmp/audit-followup-electron-verified.log` |
| `node scripts/build-web-markdown.cjs --check` | 静态包和许可证与当前锁定依赖生成结果一致，exit 0 |
| 根目录 `node tmp/audit-followup-compatibility.cjs` | 20 样本无输出差异；记录有界性能样本，exit 0 |

仅生成器锁版本检查在首轮 21 项通过后补充，最终再次执行相关 10 项定向测试及 `--check`；包内容没有变化，既有构建/集成证据仍对应同一制品。

## 替身与证据边界

- 单元权限用例使用 URL 和 webContents 身份占位对象，只证明谓词；最终窗口身份和 IPC 交互另由真实 Electron 覆盖。
- 集成 harness 从**当前编译 index.js** 按 AST 取出原始函数，不复制或改写权限判断、IPC handler 或 proxy；注入真实 BrowserWindow、Electron IPC 和 PythonBackend。未运行完整产品入口、托盘、更新器或用户配置，因此不声称完整产品启动/安装验收。
- Web 页面验证使用本机随机端口 HTTP 转发器，原样转发本次真实 Python 后端响应；没有用固定 HTML/JSON 代替 WebChannel 或渲染规则。目标权限、preload、IPC、TLS、Markdown 和 DOM 均实际执行。
- 既有 main-broker/trusted-backend 回归仍含受控错误服务器、伪进程事件和安全存储替身；这些通过不代表被替换对象可用。
- 浏览器配置、后端数据与临时文件均使用本任务独立目录，无真实凭据。第一次集成的 Windows 缓存句柄清理失败已修正为父进程等待 Electron 退出后清理；遗留的两个任务目录也已确认归属并清理。
- 未重新执行完整 Python 套件，没有把上轮 1107 passed 当成本轮结果；没有验收正式安装包或生产环境。

## 最终结果

两项均为 **CONFIRMED + 修复验证通过**；本轮已确认未修复项为 **NONE**。任务 **COMPLETED / INTEGRATION_VERIFIED**，限定于上述两项及实际本地组件边界。
