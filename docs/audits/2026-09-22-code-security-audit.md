# 2026-09-22 代码、Bug 与安全审计

## 结论与边界

首轮审计状态为 **PARTIAL / DEV_VERIFIED**。用户随后授权仅续修 P01/P02；这两项现已达到 **COMPLETED / INTEGRATION_VERIFIED**，详见 [两项续修验证报告](2026-09-22-two-fixes-verification.md)。下文 F01–F15 及全套 Python 结果保留首轮证据时间点，不表示本轮重新执行了全项目审计。

首轮修复 15 项确认问题；原待授权的 2 项已在授权后修复。没有 commit、push、发布、部署，也没有访问第三方生产目标进行漏洞验证。

事实来源是本次当前工作树和实际执行结果。审查的应用仓库为 `C:\Users\UnlimitedPower\Documents\桌面智能助手\SmartAssistant`，外层启动脚本指向该目录。已重新读取外层及应用目录的 AGENTS.md 和 engineering/testing/runtime/python/research/language 六份专项规则；全库发现优先使用 Jev，后续按候选路径精确检查。

初始工作树已经包含用户的录屏 Skill 方案修改，以及未跟踪的 AGENTS.md、.agent-rules、docs/归档。这些内容没有被本次审计修改、回滚或恢复。`tmp/audit-20260922-*` 是本次证据及可复核候选制品；本次新增安全复现使用合成凭据和临时文件。

## 已确认并修复

所有本节问题证据等级均为 CONFIRMED。严重程度为结合已证实调用路径的审计判断，不是 CVSS 评分。验证命令编号见后面的“实际验证”。每个 Python 修复的回归用例均包含在 V01 完整运行中。

### F01 静态资源同前缀目录越界读取｜高

- 根因：`AssetsHandler.GET` 用字符串 startswith 判断目录归属，`static_private` 被误当作 `static` 内部路径。
- 修改文件：`channel/web/web_channel.py`、`tests/test_static_assets_boundary.py`。
- 修复：对静态根及目标执行 realpath，再用 commonpath 判断归属。
- 直接证据：本地真实 HTTP 请求 `/assets/../static_private/private.txt` 在修复前返回 200 和临时私有标记，修复后返回 404；正常资源仍返回 200。未读取任何真实私密文件。
- 验证：V01；针对性记录 `tmp/audit-20260922-assets-green.log`，51 passed、1 skipped。相关红灯记录 `assets-red.log`。
- 等级：INTEGRATION_VERIFIED，仅覆盖临时目录与本地 HTTP；Windows 符号链接创建受限，该用例 skipped，不宣称 symlink 动态验证通过。

### F02 Web 异常页面泄露局部变量｜高

- 根因：web.py 默认调试错误页在嵌入式启动路径中未关闭，可把栈及局部变量返回客户端。
- 修改文件：`channel/web/web_channel.py`、`tests/test_web_error_privacy.py`。
- 修复：显式设置 `web.config.debug = False`。
- 直接证据：独立 Python 进程加载生产 Web 模块后，用受控故障处理器触发异常；旧响应包含合成 secret，修复后 500 响应为 `internal server error`。未使用真实 secret；不是声称所有业务异常均已触发。
- 验证：V01；`tmp/audit-20260922-error-privacy-green.log`，43 passed；红灯记录 `error-privacy-red.log`。
- 等级：INTEGRATION_VERIFIED，仅限实际框架错误处理及本地 HTTP 边界；异常处理器是故障注入。

### F03 Unicode 密码及畸形签名触发认证异常｜中

- 根因：`hmac.compare_digest(str, str)` 不支持非 ASCII 字符；密码和签名未正确处理该输入。
- 修改文件：`channel/web/web_channel.py`、`tests/test_web_auth_unicode.py`。
- 修复：密码用 UTF-8 bytes 常量时间比较；签名先验证 64 位小写十六进制格式；无效 Unicode 编码拒绝。
- 直接证据：旧实现中文密码登录和非 ASCII 签名用例失败；新实现中文密码登录成功、错误密码拒绝、畸形 bearer/subject 不再导致 500。
- 验证：V01；`tmp/audit-20260922-auth-green.log`，与频道等回归合计 52 passed；红灯记录 `regression-red.log`。
- 等级：INTEGRATION_VERIFIED，真实 HTTP/签名/处理器，使用临时测试配置。

### F04 Windows 备份恢复允许嵌入盘符路径｜高

- 根因：ZIP 成员 `workspace/D:/audit-probe.txt` 通过原有绝对路径、反斜杠和 `..` 检查，但 Windows 路径组合中的 `D:` 会改变目标驱动器语义。
- 修改文件：`cli/commands/backup.py`、`tests/test_cli_backup.py`。
- 修复：拒绝任何路径分段中带 drive 的成员。
- 直接证据：当前 Windows Path 组合显示路径脱离预期根；原 `_validate_archive` 接受该 ZIP，新实现抛出 unsafe archive path。未执行恶意归档解压，也未向根目录外写文件。
- 验证：V01；实际运行 `python -m pytest -q tests/test_cli_backup.py`，5 passed，记录 `tmp/audit-20260922-backup-green.log`；红灯记录 `backup-red.log`。
- 等级：DEV_VERIFIED，验证真实归档解析和 Windows 路径语义，未做破坏性利用。

### F05 SSRF 公网过滤遗漏共享地址段｜中

- 根因：仅检查 is_private 等属性；100.64.0.0/10 同时不是 private，也不是 global。
- 修改文件：`agent/tools/utils/url_safety.py`、`tests/test_ssrf_shared_addresses.py`。
- 修复：启用 SSRF 防护时增加 `not ip.is_global` 拒绝条件，保留显式关闭策略的行为。
- 直接证据：旧实现接受 100.64.0.1 与 100.127.255.254；修复后均拒绝，公网字面地址仍允许。仅做数字地址解析，没有向这些地址发送请求。
- 验证：V01、V07；`tmp/audit-20260922-ssrf-green.log`，43 passed；红灯记录 `ssrf-red.log` 为 2 failed、2 passed。
- 等级：DEV_VERIFIED；不代表已验证整个 DNS/网络出口隔离机制。

### F06 MCP INFO 日志写出完整工具参数｜高

- 根因：McpTool.execute 无条件将 params 格式化到 INFO 日志，参数可包含 API key 或私有文档。
- 修改文件：`agent/tools/mcp/mcp_tool.py`、`tests/test_mcp_failure_paths.py`。
- 修复：日志只保留服务器和工具名。
- 直接证据：合成凭据标记在旧日志中可见，修复后该执行日志不再包含标记；不声称已对第三方错误正文做全面脱敏。
- 验证：V01 中 `test_mcp_arguments_are_not_logged`；相关工具回归 32 passed，`tmp/audit-20260922-tool-green.log`。
- 等级：DEV_VERIFIED，使用受控 MCP 对端。

### F07 MCP 失败被包装为成功｜中

- 根因：client 将异常变成普通文本返回，tool 再以 ToolResult.success 包装；未处理 JSON-RPC error 与 result.isError。
- 修改文件：`agent/tools/mcp/mcp_client.py`、`tests/test_mcp_failure_paths.py`。
- 修复：传输错误向上抛出，协议错误和工具错误显式抛出，再由已有 ToolResult 错误边界处理。
- 直接证据：受控对端断开、返回 RPC error、返回 isError 三条旧路径误报 success；修复后均 error，正常结果仍 success。
- 验证：V01；MCP 与检索回归合计 23 passed，`tmp/audit-20260922-mcp-green.log`；红灯记录 `mcp-red.log`。
- 等级：DEV_VERIFIED；真实管道/子进程，外部协议对端为 Stub。

### F08 MCP 初始化失败遗留子进程｜中

- 根因：初始化异常返回 False 时未关闭已启动进程，强制 kill 后也没有等待回收。
- 修改文件：`agent/tools/mcp/mcp_client.py`、`tests/test_mcp_failure_paths.py`。
- 修复：失败初始化在 finally 关闭进程，kill 后 wait。
- 直接证据：受控对端拒绝握手，旧实现保留活动进程；修复后进程不存在或 poll 已结束。
- 验证：V01 中 `test_failed_handshake_reaps_child`；`tmp/audit-20260922-mcp-green.log` 23 passed。
- 等级：DEV_VERIFIED；真实子进程生命周期，握手错误来自 Stub。

### F09 MCP 持续通知可延长请求超时｜中

- 根因：每读取一行通知都重新获得完整超时，未约束整个请求的时限。
- 修改文件：`agent/tools/mcp/mcp_client.py`、`tests/test_mcp_failure_paths.py`。
- 修复：使用 monotonic 绝对截止时间，将剩余时间交给每次读取。
- 直接证据：配置 1 秒、持续通知 2.5 秒时，旧实现耗时约 2.53 秒仍继续等待；新实现在用例的 2 秒容差内失败。
- 验证：V01 中 `test_notifications_do_not_reset_request_deadline`；`tmp/audit-20260922-mcp-green.log` 23 passed。
- 等级：DEV_VERIFIED；不表示同步 stdin.write 的所有阻塞情形都已验证。

### F10 频道停止后保留失效主频道引用｜中

- 根因：从 dict pop 后再取同名对象比较，无法识别被删除的主频道；停止全部也不清理。
- 修改文件：`app.py`、`tests/test_channel_manager_primary.py`。
- 修复：在 pop 循环内比较已取出的频道对象并清理引用。
- 直接证据：停止主频道、停止全部的旧引用断言失败；修复后清空，停止其他频道仍保留主频道。
- 验证：V01 中三个频道测试；`tmp/audit-20260922-auth-green.log` 52 passed。
- 等级：DEV_VERIFIED；频道以 object 占位，仅验证引用生命周期，不代替真实频道服务停止。

### F11 Vision 未获得 Agent 工作目录｜中

- 根因：初始化器向文件工具注入工作目录时漏掉 vision，相对图片路径错误地基于启动目录解析。
- 修改文件：`bridge/agent_initializer.py`、`tests/test_tool_initialization_audit.py`。
- 修复：vision 加入现有 file_config 注入名单。
- 直接证据：真实 Vision._resolve_path 对临时工作目录中的相对文件返回错误位置；修复后返回正确路径。
- 验证：V01；`tmp/audit-20260922-tool-green.log` 32 passed；实际根因红灯记录 `vision-red.log`。
- 等级：DEV_VERIFIED；隔离自动发现/MCP 加载，未调用外部视觉模型。

### F12 通用工具加载器错误构造 governed Knowledge 工具｜低

- 根因：Knowledge 工具要求 runtime/identity；通用发现路径仍尝试无参数构造，产生初始化错误日志。
- 修改文件：`agent/tools/tool_manager.py`、`tests/test_tool_initialization_audit.py`。
- 修复：两条通用加载路径跳过这五类工具，继续由已有身份初始化路径注入，不新建 fallback。
- 直接证据：受限真实类发现触发 constructor 缺参错误；修复后通用工具正常加载且不再错误构造 Knowledge 类。
- 验证：V01；`tmp/audit-20260922-tool-green.log` 32 passed，并执行 governed knowledge 既有回归。
- 等级：DEV_VERIFIED；不将“没有初始化错误日志”扩展为外部知识源可用证明。

### F13 无扩展名文档 URL 被改写导致下载失败｜中

- 根因：Content-Type 识别 PDF 后给远端 URL 路径追加 .pdf，破坏真实路由和可能存在的签名参数。
- 修改文件：`agent/tools/web_fetch/web_fetch.py`、`tests/test_web_fetch_document_url.py`。
- 修复：保持原始请求 URL；扩展名只用于本地文件/解析器，重新请求前关闭原响应。
- 直接证据：本地 `/report?id=42` 返回真实 PDF，旧实现改请求 `/report.pdf?id=42` 得到 404；修复后原 URL 成功解析。
- 验证：V01；`tmp/audit-20260922-fetch-green.log` 21 passed；红灯记录 `fetch-red.log`。
- 等级：INTEGRATION_VERIFIED，真实本地 HTTP、requests、文件与 pypdf；本地服务为项目测试夹具，不代表互联网服务可用。

### F14 桌面 Markdown 链接识别依赖含 ReDoS 缺陷｜中

- 根因：桌面 markdown-it 启用了 linkify，锁定的 linkify-it 5.0.1 命中官方 GHSA-v245-v573-v5vm；重复 mailto 输入触发非线性处理。
- 修改文件：`desktop/package-lock.json`、`desktop/tests/markdown-security.test.cjs`。
- 修复：仅将 linkify-it 升至同一主版本补丁 5.0.2，package.json 不变。
- 直接证据：当前实际渲染路径可达；小样本旧耗时随输入翻倍约成倍平方增长；官方补丁版本已安装且与锁文件一致；再次 npm audit 中 linkify-it 条目已消失。
- 验证：V02、V03、V04、V08；桌面 Node 合并测试 13 passed，完整构建、renderer 类型检查成功。
- 等级：DEV_VERIFIED；未对正式安装包或长时间 UI 压力做验收。
- 来源：[上游安全公告](https://github.com/markdown-it/linkify-it/security/advisories/GHSA-v245-v573-v5vm)、[5.0.2 发布](https://github.com/markdown-it/linkify-it/releases/tag/5.0.2)。

### F15 测试收集阶段污染真实 web 模块｜低

- 根因：三个测试文件在 web 尚未导入时全局注入不完整 sys.modules Stub，导致结果取决于收集顺序，并遮蔽真实框架行为。
- 修改文件：`tests/test_custom_provider_handlers.py`、`tests/test_models_handler.py`、`tests/test_scheduler_web_update.py`。
- 修复：导入项目已安装的真实 web 模块，保留原断言和各用例必要的局部 Mock。
- 直接证据：混合子集出现 fake web 缺少 config 的 collection error；去除全局替换后同一子集正常收集执行。
- 验证：V01；适配器/打包等子集 168 passed、1 条弃用警告，`tmp/audit-20260922-adapters-packaging-green.log`。
- 等级：DEV_VERIFIED。

## 原待授权项：本轮已修复

这两项的“已确认但未修复”为 NONE。以下保留根因，当前验证以续修报告为准。

### P01 Electron 开发模式构建文件被 IPC 信任检查拒绝｜中

修复前，`desktop/src/main/index.ts:isTrustedRendererUrl` 在 isDev 时只接受 localhost Vite HTTP URL，但 createWindow 的开发模式回退路径实际 loadFile 构建 HTML，`npm run dev` 也是 build 后直接 electron。首轮真实编译函数验证确认了谓词矛盾。

已授权实施一行分支修复：开发 HTTP URL 走原 Vite 校验；确切的内置 file URL 走已有路径校验；保留 mainWindow.webContents 身份检查。真实 Electron/preload/IPC/本地 Python 后端验证通过，其他窗口和无关文件被拒绝。CONFIRMED，修复验证通过，INTEGRATION_VERIFIED。

### P02 Web 静态 Markdown 依赖保留同一链接识别性能缺陷｜中

修复前 `channel/web/static/vendor/markdown-it/markdown-it.min.js` 为 13.0.1，实际 Web 渲染启用 linkify。首轮有限样本和上游公告确认了链接识别性能缺陷，未进行无限输入或破坏性阻塞。

已核对跨主版本变更，并从当前锁定的 markdown-it 14.2.0 + linkify-it 5.0.2 重新构建资源。20 个样本在真实 createMd 规则下输出一致；实际 Web 页面加载、DOM 链接/知识引用、HTML 转义和有界性能验证通过。增加可复现构建及全部打包依赖许可证，未修改 console.js。CONFIRMED，修复验证通过，INTEGRATION_VERIFIED。

当前命令、结果和证据边界见续修报告；首轮 pending-probes 仅保留为修复前历史证据。

## 未确认但值得关注

- **INFERRED：依赖公告的项目影响。** npm 官方审计补丁后返回 35 个受影响包条目（3 critical / 25 high / 4 moderate / 3 low），包括 Electron、electron-builder、Vite 及传递依赖。条目可能涉及同一漏洞传播链，不能当作 35 个可利用漏洞。没有为清空告警自动跨主版本升级。
- **INFERRED：aiohttp 约束。** 对 requirements 与桌面 requirements 中实际安装的 28 个公开包版本查询 OSV，aiohttp 3.9.5 命中公告；依赖约束仍 `<3.10`。未确认这些公告在当前真实入口中的可达性，没有猜测修改。9 个列出的可选依赖本机未安装，其当前运行行为 UNKNOWN。原始/摘要结果见 `tmp/audit-20260922-python-dependencies*.json`；公告可能包含别名，不按记录数计算独立漏洞。
- **INFERRED：SSRF 检查与实际连接间存在 DNS 再解析窗口。** 代码先 getaddrinfo 校验，随后 requests 建立连接；未做受控 DNS 重绑定复现，因此未将其列为已确认漏洞或修改网络传输层。

## 实际验证

以下以 SmartAssistant 为 cwd；`python` 命令均实际使用 `.venv/Scripts/python.exe -X utf8`。最终全套运行将 COW_DATA_DIR 指向本次专用 `tmp/audit-20260922-final-data`，不使用用户工作区数据。

| 编号 | 实际命令 | 结果 / 证据 |
|---|---|---|
| V01 | `.venv/Scripts/python.exe -X utf8 -m pytest -q tests --tb=short --junitxml=tmp/audit-20260922-verified-pytest.xml` | 1107 passed、39 skipped、89 subtests passed、1 warning，0 failed，262.26 秒；日志 `tmp/audit-20260922-verified-pytest.log` |
| V02 | 在 desktop 执行 `node --test tests/trusted-backend.test.cjs tests/main-broker-security.test.cjs tests/web-queue-ui.test.cjs tests/markdown-security.test.cjs`，子进程 PATH 使用项目 .venv | 13 passed、0 failed、0 skipped；`tmp/audit-20260922-desktop-final.log` |
| V03 | 在 desktop 执行 `npm.cmd run build` | Vite renderer 与 tsc main 构建成功；`tmp/audit-20260922-desktop-build-verified.log`；非正式安装包 |
| V04 | 在 desktop 执行 `./node_modules/.bin/tsc.cmd --noEmit -p tsconfig.json` | exit 0；`tmp/audit-20260922-renderer-typecheck.log` |
| V05 | 对当前 Git 跟踪的 Python 文件逐个 `ast.parse` | 463 个、0 错误；`tmp/audit-20260922-python-syntax.json`，新增用例由 pytest 覆盖 |
| V06 | `python -m benchmarks.security.web_boundary --output benchmarks/results/web-boundary-security.json`，然后 `python -m benchmarks.security.verify --report benchmarks/results/web-boundary-security.json --output benchmarks/results/web-boundary-security-verification.json` | 30 项本地边界检查及独立重放通过；保留 production/customer 未验证限制 |
| V07 | `python -m pytest -q tests/test_ssrf_shared_addresses.py tests/test_security_ssrf_web_fetch.py tests/test_security_ssrf_browser_navigate.py tests/test_security_ssrf_path_traversal.py --tb=short` | 43 passed；`tmp/audit-20260922-ssrf-green.log` |
| V08 | 在 desktop 执行 `npm.cmd audit --json --cache ../tmp/audit-20260922-npm-cache` | 成功取得官方结果，因剩余公告 exit 1；原始 `tmp/audit-20260922-npm-audit-after.json`；不能标记依赖审计全绿 |

最初全套运行发现两个失败，均由于源码变更使既有 Web 边界报告指纹过期；真实边界检查无失败。已实际重跑 V06 更新两份本地报告，而非手改 PASS；两项所属测试子集随后 20 passed。生产部署、客户验收、外部受保护验证仍保持 NO/ABSENT/NOT_RUN。

已执行的其他分组回归包括：governed knowledge/memory/SQLite/scheduler/SSE 354 passed、3 skipped、77 subtests；工具/取消/并发/文件/Bash 186 passed、10 skipped。这些与 V01 有重叠，不相加宣称覆盖率。

## 测试替身与真实边界

- MCP：本次用可控 Python 子进程作为协议 Stub，实际使用 stdin/stdout、进程等待和超时。不能证明第三方 MCP 服务可用。
- HTTP：本地 WSGI/http.server 夹具提供测试路由/文档；实际 socket、生产处理器、HMAC、requests、pypdf 与临时文件均真实执行。认证配置及资源根目录被注入测试值。
- 异常页：合成异常处理器及合成 secret；未触碰用户凭据。
- Vision/发现：隔离 load_tools、__all__ 和 MCP 发现；调用真实初始化器和工具路径方法，未调用视觉模型。
- ChannelManager：object 占位频道，只验证对象引用。
- 既有 Python/Node 套件：使用 monkeypatch/unittest.mock、假 SDK/LLM/Bridge、伪 Electron 存储和事件对象、受控 HTTP/TLS 服务、故障注入等；通过不等同于真实第三方供应商、桌面 GUI 或生产可用。
- 真实本地集成范围：临时 SQLite 事务/并发/持久化回放、文件边界、HTTP 认证及错误处理；Node 测试实际启动项目 WebChannel 的桌面认证传输路径。PostgreSQL、第三方 LLM/MCP/SaaS、生产 TLS 代理、正式安装/更新及客户环境未验证。
- 39 项 skipped 保留原状：29 项需要 POSIX shell/process，9 项无法在当前 Windows 权限下创建符号链接，1 项需要 SQLite 3.44+ 的 FTS5 integrity_check 行为。没有转换成 PASS。

## 审查覆盖范围

| 模块/边界 | 实际检查和验证深度 |
|---|---|
| 启动、配置与生命周期 | 外层 start_demo.bat、app.py、ChannelManager、config.py 环境/配置/持久化、AgentInitializer；启动和引用回归 |
| Web/API/SSE | 路由、认证、Host/Origin、会话归属、能力 URL、上传/资源、日志/配置接口、SSE journal、取消/steer；本地 HTTP 与边界重放 |
| Agent、文件及执行工具 | agent/protocol 循环/取消、Bridge、read/write/edit/bash/background/browser/vision/web_fetch、安全检查；定向代码和既有工具回归 |
| 持久化与共享状态 | ConversationStore、governed knowledge/memory repository/service/runtime、scheduler TaskStore；临时 SQLite、身份、事务/并发和回放测试 |
| MCP/LLM/RAG/插件/Skill | MCP stdio/SSE/http/OAuth、工具装配、provider 适配器、retrieval、Skill 归档与插件加载；本地或替身验证，无第三方生产调用 |
| Electron/Desktop | main/preload/renderer、PythonBackend 控制管道与 TLS/HMAC、relay/updater、Markdown、IPC 信任、打包脚本；Node/真实本地后端测试、编译，未启动完整 GUI |
| 依赖/供应链/交付 | package manifests/lock、Python requirements、构建/安装/更新代码、release evidence 门禁；npm/OSV 公告、构建和源码指纹重放，未构建正式安装包或执行更新发布 |
| 测试与实现一致性 | 新增红转绿用例、清除全局框架 Stub 污染、全套及分组回归、skip 保留、过期报告拒绝逻辑 |

“全面”在本次表示按项目入口和各主要信任边界系统推进；没有逐行或所有输入空间覆盖证明。未发现新问题的部分只能表述为：在上述实际检查范围和验证深度内没有形成其他 CONFIRMED 结论。
