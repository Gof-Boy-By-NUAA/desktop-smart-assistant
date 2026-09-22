# 正式桌面制品验收（2026-09-22）

## 续验范围与当前阻碍（用户调整范围后）

本轮目标调整为：**同一未签名 Windows x64 正式 NSIS 制品的实际安装、启动、运行、再次启动和卸载验收**。以下既有运行记录保留其原有边界，不替代安装后运行证据。

本轮重新读取了当前 AGENTS.md、相关专项规则及本报告，并重新检查工作树、安装包 SHA-256 和源文件/ASAR 对应关系：安装包仍为下述哈希；1003 个已记录源文件无变化，六项 ASAR 构建内容及冻结后端 Web Markdown bundle 比对一致。未重建、修改安装包、Product ID 或安装行为。

### DEFERRED（明确不构成本轮阻碍，均未通过验收）

- Authenticode 代码签名。
- Windows SmartScreen 发布者/文件信誉。
- 公网下载后的 reputation 行为。
- 签名后的 NSIS / EXE 行为。
- Microsoft Store 分发。

### 隔离环境实际检查

- 宿主机：Windows 11 企业版，版本 `10.0.26200`，`HypervisorPresent=true`。
- 未找到 `WindowsSandbox.exe`、`wsb.exe` 命令或 System32 对应程序；当前用户 WindowsSandbox Appx 查询返回 NONE。
- Hyper-V 模块存在，`vmms`、`vmcompute` 服务运行。但在工具沙箱外执行只读 `Get-VM` 仍返回当前身份无管理权限；不能据此声称机器没有虚拟机。
- `Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM` 要求管理员提升，因此功能启用状态为 UNKNOWN，不能声称已禁用或不可安装。
- 未在 PATH 或已检查的常见安装位置找到 VirtualBox、VMware、QEMU 管理程序；这不等于对所有磁盘做过穷尽搜索。
- 尚未提供可访问的干净 Windows VM 或独立测试机。已请求环境及连接方式，不请求密码/API key。
- 宿主机原产品登记仍指向 `SmartAssistant-P2-ed68a64`，未运行安装器或卸载器，未修改权限、启用系统功能或动用原用户数据。

**当前阻碍是缺少可访问的隔离 Windows 执行环境，不是上述 DEFERRED 项。**

### 本轮安装验收清单

以下均为 **NOT_RUN / 环境阻碍**，不能用后文 win-unpacked 结果改判：

| 编号 | 必须经过的真实边界 |
|---|---|
| 1–4 | NSIS 安装完成、目标路径、文件完整性、桌面和开始菜单快捷方式 |
| 5–8 | 从已安装 EXE/快捷方式启动、Electron、内置 Python、Web/UI |
| 9–11 | 已安装制品 IPC 权限、Markdown、核心本地 API |
| 12–14 | 正常退出、再次启动、子进程与端口清理 |
| 15–16 | 正式卸载；核查安装目录、快捷方式、注册表、后台进程、临时资源 |
| 17 | 区分卸载应删除的程序文件与应保留的用户数据，并做实际前后比对 |

报告已记录的桌面 Markdown 预览失败、首次空会话历史错误仍是本轮范围内未解决项，未因调整验收范围而撤销，也未当作安装后新执行结果。真实模型调用单列为未验证：未提供 API key，不执行付费调用。

本轮测试替身：NONE；本轮未执行安装或运行测试。状态保持 **PARTIAL**；本轮安装/卸载边界验证等级为 **NONE**，既有制品集成记录仍仅覆盖其原有范围。**未达到 ACCEPTANCE_VERIFIED**。

## 正式制品

- 制品：`tmp/formal-acceptance-20260922-df13e42d/release/SmartAssistant-Setup-2.1.3-x64.exe`，Windows x64 NSIS，186,835,139 字节，未签名。
- SHA-256：`3b36b580728ca7e94bb655996abc39c339fbc7e7e189a048ea47ae5be385bfeb`。
- 同次构建生成 `release/win-unpacked`。实际运行的是其中的 `SmartAssistant.exe`，不是 source tree 或 dev mode。
- 当前 AGENTS.md、专项规则、完整审计报告及两项续修验证报告均已重新读取。构建前检查了工作树与差异，保留原有修改。
- 构建使用当前 `smart-assistant-backend.spec`、`electron-builder.win.js`，Python 3.11.8、Electron 33.4.11、electron-builder 25.1.8，禁用签名凭证与发布。依赖版本保存在 `build-python-freeze.txt`。
- 1003 个源文件构建前后哈希一致；ASAR 主进程、preload、Python 管理器、renderer HTML/JS/CSS 与本次构建输出逐项一致；冻结后端中的 Web Markdown bundle 与当前源文件一致。详见证据目录 `artifact-binding.json`、`source-hashes.json`。

## 实际执行的验收项目

| 项目 | 实际结果 |
|---|---|
| PyInstaller 独立后端构建 | PASS；生成内置 EXE 与依赖目录 |
| `npm.cmd run build` | PASS；renderer 与 main 构建完成 |
| `node node_modules/typescript/bin/tsc --noEmit -p tsconfig.json` | PASS |
| `node scripts/build-web-markdown.cjs --check` | PASS |
| Windows NSIS 构建，`--publish never` | PASS；生成上述安装包 |
| 实际 NSIS 安装 | NOT_RUN；安全预检发现同产品 ID 的现有安装，详见失败项 |
| 制品启动 | PASS；`app.isPackaged=true`，入口位于 `resources/app.asar` |
| 内置 Python 启动 | PASS；实际子进程为制品内 `smart-assistant-backend.exe`；启动环境 PATH 未包含 Python/venv，可信通道 ready |
| 数据隔离 | PASS；实际 home、Electron userData、AppData 与 Python 工作区均位于任务专用 profile |
| Electron UI 加载 | 已加载正式 renderer 和首次使用界面；存在下述历史提示、Markdown 预览异常，不能判整体 UI 验收通过 |
| IPC 修复 | PASS；主窗口正式文件可访问后端，其他窗口、主窗口错误文件、伪造 Authorization 请求头均被拒绝 |
| 核心本地 API | `/api/health`、`/api/sessions`、`/api/tools`、`/api/skills`、`/api/workspace/tree`、`/chat` 和 Markdown 静态资源均返回 200，检查了真实响应内容 |
| readiness | 503；存储、工作区、磁盘、队列和 Agent 初始化为 true，只有未配置模型密钥的 `chat_model_config=false` |
| 冻结后端 Web UI / Markdown | PASS；实际加载 Console；markdown-it 14.2.0 + linkify-it 5.0.2；链接截断、知识引用、HTML 转义通过；16000 次 `mailto:` 样本约 9 ms |
| 桌面 Markdown 文件预览 | FAIL；真实工作区预览显示 `Failed to fetch` |
| 正常退出 | PASS；通过 Electron `app.quit()` 触发原有退出处理，主进程退出码 0 |
| 进程与端口清理 | PASS；无任务主进程、后端或探针残留；后端、调试器、临时 Web 转发端口均已关闭 |

## 实际结果

运行证据位于 `tmp/formal-acceptance-20260922-df13e42d/`：

- `runtime.json`、`runtime-processes.json`：实际 EXE、ASAR、内置后端进程及退出码。
- `runtime-status.js.result.json`、`artifact-checks.js.result.json`：制品身份、UI、IPC 正负向检查。最初 API 检查脚本读取了不存在的 `body` 字段，响应内容以随后正确解码 `bodyBase64` 的 `api-details.js.result.json` 为准，不能使用首次记录的零字节字段判断接口内容。
- `web-artifact-check.js.result.json`：真实冻结后端资源、Web Console 和 Markdown 样本结果。
- `preview-failure-check.js.result.json`：后端预览 200/156 字节，但浏览器 URL 构造与 fetch 失败。
- `cleanup-check.json`、`temp-cleanup.json`、`install-final-state.json`：原数据、快捷方式、旧安装及清理结果。

未修改生产代码，未 commit、push、发布或部署。临时 venv、PyInstaller 中间目录、运行 profile、临时 ripgrep 资源及中间后端输出已清理。保留正式制品、验收脚本/日志、快捷方式备份和本次桌面构建输出。

## 失败项

1. **实际安装被安全条件阻止，未执行。** 当前用户注册表存在产品 GUID `609d45a8-19b2-5637-a4eb-24dc35d484e7`，对应旧安装 `C:\Users\UnlimitedPower\AppData\Local\Programs\SmartAssistant-P2-ed68a64`。当前 NSIS 的 `installSection.nsh` 在复制新程序前无条件调用 `uninstallOldVersion`；`installUtil.nsh` 会执行原卸载器。因此仅指定独立 `/D` 路径仍可能卸载旧程序，超出本次仅备份/恢复快捷方式的授权。未删除或替换登记项来绕过检查。后续需要无该产品登记的隔离 Windows 测试环境。
2. **桌面 Markdown 预览失败，CONFIRMED。** 用正式 UI 打开隔离工作区的 Markdown 文件，显示“预览失败: Failed to fetch”。同一文件通过正式 IPC 调用后端预览为 200。桌面生成的 `smart_assistant://backend/...` 在 Chromium 中 `new URL()` 抛出 `Invalid URL`，fetch 抛出 `Failed to fetch`。没有修改实现或用替代渲染器把该项改判为 PASS。
3. **首次空会话出现历史加载失败提示，CONFIRMED 运行现象。** 全新 profile 会话列表为空，界面仍显示“会话记录加载失败”；冻结后端同期记录 `History API error: session not found`。仅记录验收异常，未展开新的全面审计。

## 测试替身

- 目标组件未使用 Mock/Stub/Fake：运行实际 Electron 制品、ASAR、内置 Python、真实 IPC、固定证书 TLS 和真实本地存储。
- Web Console 验收使用任务专用回环 GET 转发适配器：浏览器请求经正式主窗口 IPC 转给实际冻结后端，原样转发真实响应；不返回预设成功结果。该适配器不是正式安装后的用户入口，不能证明安装入口可用。
- 使用临时 profile、Markdown 文件和不可信页面作为确定性输入；通过仅回环的主进程 inspector 操作真实窗口与调用正常退出。没有关闭 IPC 校验或替换业务实现。

## 未覆盖边界

- NSIS 实际安装/卸载、安装后启动、新快捷方式及默认用户目录运行。未用 win-unpacked 运行结果替代这些证据。
- 代码签名、SmartScreen 发布者/文件信誉、公网下载信誉、签名后制品行为和 Microsoft Store 分发为 **DEFERRED**，不属于本轮验收。干净 Windows 主机上的安装、运行和卸载仍需实际验证；跨版本升级兼容性不在本轮范围。
- 未配置真实模型凭证，未验证实际 LLM 对话、第三方服务或在线更新；外部更新请求未成功，未下载/安装更新。
- 正常退出由 `app.quit()` 触发，未验证人工点击托盘菜单退出。桌面 Markdown 预览尚未通过。
- 初始两个快捷方式已备份且从未被替换；最终内容哈希、属性、时间戳全部一致，因此无需重写恢复。原 `.cow` 与 SmartAssistant 用户数据的 196 个目录/文件记录前后一致，旧安装及其登记仍保留。没有遗留测试安装、活跃子进程或监听端口。

## 最终任务状态

**PARTIAL**。正式制品已生成并完成部分真实运行验收；实际安装未执行，且发现正式制品 UI 路径失败。

## 最终验证等级

**INTEGRATION_VERIFIED（仅限已通过的真实制品组件边界）**。

**未达到 ACCEPTANCE_VERIFIED**，不能宣称正式安装包整体验收通过。

---

## P03 / P04 定向修复及新制品验证（2026-09-22，当前有效结论）

本节追加新证据；前述旧制品的 P03/P04 FAIL 及安装未执行记录保持不变。本次只修复 P03/P04，没有继续调查安装环境，没有修改 NSIS、Product ID、注册表、签名、更新器或真实模型配置，没有 commit、push、发布或部署。

### 制品与源码绑定

证据目录：`tmp/p03-p04-20260922-459e2ac7/`，以下证据路径均相对该目录。

| 制品 | SHA-256 |
|---|---|
| 新 `release/SmartAssistant-Setup-2.1.3-x64.exe`，186,835,499 字节，未签名 | `0a02007eef1d7a5add5bd104ed609002864f49f63f326fc296a41bf225aa0cc5` |
| 新 `release/win-unpacked/SmartAssistant.exe` | `0164b96c382b9c72b09583fb73a72dbe8eed90665df905e41405c66ca7ebd826` |
| 新 `release/win-unpacked/resources/app.asar` | `15167fc083e4c29612995030492c068b4ddc4ec8063723dc9495516d2a7c9288` |
| 内置后端 `smart-assistant-backend.exe` | `47efdfae401eee9815953523f68a46e17fce9549847503f50866d700b4b59182` |

旧 NSIS 哈希复核仍为 `3b36b580728ca7e94bb655996abc39c339fbc7e7e189a048ea47ae5be385bfeb`，旧包没有获得本次 PASS。新包、新 ASAR、启动 EXE 与后端哈希在集成测试后再次核对一致，详见 `new-artifact-hashes.json`。

`artifact-binding.json`：当前 dist 中 62 个文件与新 ASAR 逐项一致；对原制品的 1003 项源码清单重新计算，仅本节列出的六个目标源码文件发生变化。Python 生产源码及后端资源未变，本次复用已核对哈希的原冻结后端作为打包输入，未重新执行 PyInstaller；真实运行仍由新桌面制品启动该内置后端。`baseline.diff` 与当前 diff 比对确认无关已跟踪修改保持不变。

### P03

**结论：CONFIRMED，已修复，当前新制品验证通过；INTEGRATION_VERIFIED。**

根因：项目已明确设计内部资源协议，由 Electron 主进程把 `/file/`、`/preview/` capability 资源转发到固定 TLS 后端；通用 API 走受信任 IPC。原 scheme `smart_assistant` 含下划线，Chromium URL 解析失败；renderer CSP 也未声明该内部资源来源。[Electron protocol 文档](https://www.electronjs.org/docs/latest/api/protocol) 的 scheme 规则与本次 Chromium `Invalid URL` 实测相符。Web Console 原有相对 HTTP 路径无需改动。

最小修改：

- `desktop/src/main/index.ts`：注册及处理合法的 `smart-assistant`；仅精确 host `backend`、无用户信息/端口、`/file/` 或 `/preview/` 路径进入协议处理。后端原有 capability 校验、GET/HEAD 限制及 IPC 主窗口信任规则保留。
- `desktop/src/renderer/src/api/client.ts`、`desktop/src/renderer/src/hooks/useBackend.ts`：同步内部 origin。
- `desktop/src/renderer/index.html`：仅在 connect/img/media 来源中加入 `smart-assistant://backend`，没有开启未知 scheme 通用放行或 bypassCSP。

直接证据：

- `before/results.json`：本轮重新运行旧完整制品，Markdown 无标题且显示预览失败；同一文件正式 IPC 预览 HTTP 200，Chromium URL 构造抛出 `Invalid URL`，状态 `REPRODUCED`。
- `after-5/results.json`：新完整制品真实 renderer 显示 Markdown 标题、中文加粗和代码高亮，原始 script 未执行。`normal.md` 与 `中文 路径 #100% & [样本].md` 均通过。
- 内部 preview/raw 资源实际 fetch 均为 200 且内容匹配；允许的 HTML 预览链接实际打开资源窗口，正文正确且没有 `electronAPI`。
- Markdown 中 javascript/data/file 链接未生成危险 anchor；javascript/data/未知 scheme 的 fetch 被拒绝。错误 host 被拒绝，通用 `/api/health` 及无效 capability 均非成功响应。
- 不可信 BrowserWindow 即使装载真实 preload，其后端 IPC 请求仍被拒绝，不能把内部资源协议用作通用 API 代理。协议仍支持持有效 capability 的资源读取；未把这种既有设计描述为完全禁止所有窗口读取所有资源。
- 单元测试另覆盖 file/旧 scheme/自定义 scheme/用户信息/端口/路径穿越等被资源解析或 URL 生成入口拒绝。`file:` 拒绝结论限于预览 URL 和 Markdown 链接，未宣称 file-origin 页面所有原生 `fetch(file:)` 都被全局禁用。

### P04

**结论：CONFIRMED，已修复，当前新制品验证通过；INTEGRATION_VERIFIED。**

根因：前端创建的新 session ID 尚未写入持久化存储，却立即请求历史。后端对不存在的 session 返回真实错误是正确行为；前端没有区分本地草稿与已有会话，且历史客户端未显式检查业务 status。

最小修改：

- `desktop/src/renderer/src/store/sessionStore.ts`：记录与 active ID 精确匹配的本地草稿标记；重启保留草稿状态；会话列表确认持久化后移除标记；选择历史或新建会话时正确更新标记。
- `desktop/src/renderer/src/pages/ChatPage.tsx`：仅明确的本地草稿跳过历史读取，未知 ID 仍由真实后端核验。
- `desktop/src/renderer/src/api/client.ts`：历史业务错误抛出后端信息，成功空结果与真正失败保持区分。

直接证据：`before/results.json` 在本轮旧包新 profile 中再次复现零 session 却显示“会话记录加载失败”。`after-5/results.json` 使用真实 Electron → IPC → 内置 Python → SQLite，结果如下：

| 状态 | 当前实际结果 |
|---|---|
| 全新 profile，0 session | 正常欢迎空状态，没有历史加载错误 |
| 空草稿退出后再次启动 | 仍为 0 session，无错误 |
| 草稿随后成为持久化会话 | 自动清除草稿标记并显示真实历史 |
| 已有历史 | API 返回 2 条消息，UI 显示 `P04_HISTORY_OK` |
| 已存在且查询成功但 0 消息 | API success + 空数组，UI 无错误 |
| session 不存在 | API error `session not found`，UI 仍显示历史加载失败 |
| 真实 SQLite 请求故障 | 仅在任务数据库重命名 messages 表；API error `no such table: messages`，UI 显示错误；finally 恢复表 |
| 故障恢复后点击重试 | 恢复历史显示，错误消失 |
| 再次退出及启动 | 已有历史仍正常显示 |
| 真实后端进程停止 | 仅终止测试桌面端持有的后端子进程；IPC 拒绝请求，UI 显示“初始化失败 / Trusted backend stopped unexpectedly”，未伪装为空历史 |

会话由测试脚本通过真实 ConversationStore 写入隔离 SQLite；不需要真实模型调用，也未伪造外部模型成功。最终这组验证四次启动的主进程均正常退出，exit code 为 0。

### 本轮执行的命令及结果

命令中 `B` 表示上述证据目录；除注明外 cwd 为项目根目录。

| 验证命令 | 实际结果 |
|---|---|
| `node desktop/tests/p03-p04-artifact.cjs tmp/formal-acceptance-20260922-df13e42d/release/win-unpacked B/before before` | 两项修复前失败重新复现；`before/results.json` |
| desktop 目录：`npm.cmd run build` | exit 0；`build.log`；保留 Vite 大 chunk 提示，不改无关代码 |
| desktop 目录：`node node_modules/typescript/bin/tsc --noEmit -p tsconfig.json` | exit 0 |
| desktop 目录：`node --test tests/p03-p04.test.cjs tests/renderer-trust.test.cjs tests/main-broker-security.test.cjs tests/markdown-security.test.cjs tests/web-markdown.test.cjs` | 20 PASS，0 FAIL，0 skipped；`targeted-tests.log` |
| desktop 目录：`node node_modules/electron-builder/cli.js --win --x64 --config electron-builder.win.js --config.directories.output=B/release --publish never` | exit 0；新 NSIS 与 win-unpacked；`packaging.log`。仅对子进程清除签名凭据并禁用自动签名发现，无签名/发布操作 |
| `node desktop/tests/p03-p04-artifact.cjs B/release/win-unpacked B/after-5 after` | exit 0，PASSED；完整真实调用路径，无组件替身 |
| `node --check desktop/tests/p03-p04-artifact.cjs`、`.venv/Scripts/python.exe -m py_compile desktop/tests/p04-history-fixture.py` | PASS |
| `node B/bind-artifact.cjs`、`git diff --check` | PASS，62 项制品内容一致、无关 diff 保持原样 |

测试脚本纠错不算产品修复：早期集成脚本使用单独 assistant 消息，真实历史分组正确返回空；随后补齐 user/assistant 会话。另修正了把 file-origin 原生 fetch 当作预览 URL 边界、把后端已停止状态期望为 error、以及把 Markdown MIME 导航期望为 HTML 展示的测试假设。最终完整结果以 `after-5/results.json` 为准；较早 `after-2` 至 `after-4` 的失败记录保留，不计为 PASS。`after-1/results.json` 是追加调查时写入的诊断结果，首轮结果被该诊断覆盖，不作为通过证据。

### 测试替身、隔离和清理

- 开发级测试：Fake localStorage（Map），Stub session/IPC 响应，已有主进程测试的 Electron、updater、窗口/后端对象替身；VM 运行实际提取或编译的函数。这些结果仅是 DEV_VERIFIED。
- 最终制品集成：Mock/Stub/Fake 组件 **NONE**。真实 SQLite 使用人工会话数据；故障注入为重命名任务数据库表和终止该测试应用自己的后端，不涉及真实用户数据库或模型。
- 运行环境：新正式 `win-unpacked/SmartAssistant.exe`，`app.isPackaged=true`，加载新 ASAR；profile/home/userData/Temp 为任务独立目录，应用 PATH 不含 Python/venv，后端由制品启动。主进程 inspector 仅用于测试驱动和读取真实 UI/API，不替换生产处理逻辑。
- `process-cleanup-check.json`：任务制品进程及所属监听无残留；`temporary-cleanup.json`：本轮 before/after 测试 profile、任务复制的中间后端目录和 rg.exe 已清理。保留新旧制品、测试脚本、日志、结果及桌面构建输出。宿主机现有安装、注册表和快捷方式未作修改。

### 剩余阻碍与 DEFERRED

- **NSIS 实际安装/卸载：BLOCKED / NOT_RUN — 缺少隔离 Windows 环境。** 本轮没有尝试解决该环境，也没有使用 win-unpacked 结果替代安装后验收。
- **DEFERRED**：Authenticode；SmartScreen / 公网 reputation / 签名后行为；Microsoft Store；真实模型调用。

### 本轮最终状态

- **P03/P04 修复任务：COMPLETED。** 两项均达到 CONFIRMED + 修复验证通过；验证等级 **INTEGRATION_VERIFIED**。
- **项目正式制品验收：PARTIAL。** 实际 NSIS 安装和卸载尚未执行，整体 **不标记 ACCEPTANCE_VERIFIED**。