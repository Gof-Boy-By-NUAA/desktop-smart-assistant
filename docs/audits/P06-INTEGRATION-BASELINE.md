# P06 Integration Verified Baseline

本基线冻结当前源码、测试与本地制品证据，供后续从 Git commit 开始复核。它不是 release、stable 或 acceptance 基线。分支：`baseline/p06-integration-verified`。不创建发布标签。

基线提交父节点为 `42bf97b2c3bf8869cbb956105fe451b829fbb3c1`。既有安装包在基线提交之前由该父节点上的未提交源码构建；不得将父节点单独视为制品对应源码。本提交保留那些修改，并增加下面的指纹与验证入口。提交自身 SHA 由 `git rev-parse HEAD` 获取，不在文件中制造自引用。

## 状态和限制

| 项目 | 状态 | 范围 |
|---|---|---|
| SOURCE_FIXED | YES | 报告中已确认并修复的 P03–P07 项；不表示不存在其他问题 |
| LOCAL_TESTED | YES | 已记录的定向测试和本地回归 |
| PACKAGED | YES | 未签名 Windows x64 NSIS / win-unpacked |
| ARTIFACT_BOUND | YES | 下列指纹及实际内容比较；不是可重现构建证明 |
| INTEGRATION_VERIFIED | YES | 本地 Electron / IPC / 内置 Python / 存储；上游错误用本地 Stub |
| REAL_VENDOR_VERIFIED | NO | 真实供应商推理和账号能力尚未验证 |
| NSIS_INSTALL_VERIFIED | NO | 缺少隔离 Windows，NOT_RUN |
| NSIS_UNINSTALL_VERIFIED | NO | 缺少隔离 Windows，NOT_RUN |
| SIGNED_ARTIFACT | NO | Authenticode / SmartScreen / 信誉等 DEFERRED |
| ACCEPTANCE_VERIFIED | NO | 整体验收仍 PARTIAL |

P06 本地模型请求参数、配置和错误提示已验证；供应商原始错误没有直接出现在 UI，当前显示保守的 durable 失败终态。P08 未改变跨通道授权和可见性语义；其本地调查测试不代表需求已实现。已有 README 与此前产品修改原样纳入本基线，没有在整理过程中继续重构。

## 证据入口

- [P05–P08 续修报告](2026-09-23-p05-p08-fixes.md)
- [P06 当前续修报告](2026-09-24-p06-followup.md)
- [正式制品验收历史](2026-09-22-formal-artifact-acceptance.md)
- [制品与源码绑定](evidence/p06-baseline/artifact-binding.json)：458 个列明的源码、静态资源及构建输入；原始字节哈希和 CRLF→LF 规范化哈希同时保留。清单不是所有依赖的完整供应链证明。
- [精简验证结果](evidence/p06-baseline/results.json)：从 9 份本地 JSON 选取结果、失败原因、退出码和测试替身，记录原始文件 SHA-256。没有提交 profile、capability URL、用户数据库或完整日志。JSON 中 `evidencePath` 是原运行时位置，Git clone 后不会自动存在。
- [Python 环境记录](evidence/p06-baseline/python-environment.json)：当前包名和版本；不是安装锁，也不保证未来索引仍能提供相同 wheel。
- [只读校验器](../../scripts/verify-p06-baseline.cjs)：校验当前源码指纹；有原制品时同时比对制品 SHA、ASAR 与本地构建目录。不执行运行验收，输出明确标记 `runtime=NOT_RUN_BY_THIS_VERIFIER`。

NSIS：`SmartAssistant-Setup-2.1.3-x64.exe`

SHA-256：`52e1a55fd76c2d0f2075f7a7c279d12af989b2aefb58ce1ba4bd38e7601a9e05`

本机位置：`tmp/p06-followup-20260923/release/`。二进制不进 Git，也未上传 Release Assets。旧 `0a02007e...`、`a302949b...` 对应各自历史制品，不表示当前基线包。

## 验证与构建复现入口

以下命令在 Windows 项目根目录执行；需 Python 3.11、Node/npm 和项目依赖。`desktop/package-lock.json` 已跟踪；Python requirements 仍含范围约束，环境记录只能帮助复现，不能保证逐字节相同的 EXE。此前完整构建工具链包括 PyInstaller、Electron builder 和打包用 ripgrep；准备这些依赖的流程见 `desktop/build/`、桌面构建配置和已有 workflow，不在本轮更新依赖或 CI。

```powershell
# 仅核对源码，不启动服务
node scripts/verify-p06-baseline.cjs

.venv/Scripts/python.exe -m pytest tests/test_zhipu_p06.py tests/test_provider_catalog.py tests/test_models_handler.py tests/test_custom_provider_handlers.py tests/test_custom_provider.py -q
Push-Location desktop
node --test tests/p06-catalog-client.test.cjs tests/p06-web-catalog.test.cjs tests/p05-upload.test.cjs tests/p03-p04.test.cjs tests/markdown-security.test.cjs tests/main-broker-security.test.cjs tests/renderer-trust.test.cjs tests/web-markdown.test.cjs
node node_modules/typescript/bin/tsc --noEmit -p tsconfig.json
npm.cmd run build
Pop-Location

# 每次使用新的临时目录，保留旧验证记录
.venv/Scripts/python.exe -m PyInstaller desktop/build/smart-assistant-backend.spec --noconfirm --distpath desktop/build/dist --workpath tmp/p06-reproduction/pyinstaller-work
Push-Location desktop
# 在未配置签名凭据的独立构建环境中执行；禁止 publish。
$env:CSC_IDENTITY_AUTO_DISCOVERY='false'
node node_modules/electron-builder/cli.js --win --x64 --config electron-builder.win.js --config.directories.output=../tmp/p06-reproduction/release --publish never
Pop-Location

node desktop/tests/p06-models-artifact.cjs tmp/p06-reproduction/release/win-unpacked tmp/p06-reproduction/ui-after after
node desktop/tests/p03-p04-artifact.cjs tmp/p06-reproduction/release/win-unpacked tmp/p06-reproduction/p03-p04-after after
node desktop/tests/p05-p07-artifact.cjs tmp/p06-reproduction/release/win-unpacked tmp/p06-reproduction/p05-p07-after after

# 只适用于保留的原制品和对应 dist，不用于断言新构建一定同哈希。
node scripts/verify-p06-baseline.cjs tmp/p06-followup-20260923/release
```

真实供应商调用、安装、卸载需另行取得对应环境与授权，以上命令不替代这些验收。集成脚本使用本地 fault server/关闭端口及专属 profile，不使用真实 API key。

## 提交范围检查

Git 快照保留在忽略目录 `tmp/baseline-20260924/`：原 status、diff stat、binary diff、HEAD、分支和未跟踪文件清单。`tmp/`、安装包、构建输出、`.env*`、凭据文件、用户数据库和测试 profile 均排除。`desktop/build/` 中仅既有 spec、requirements、构建脚本等源码例外保留。

对候选树执行本地凭据模式检查，匹配项逐项核对为测试占位值、变量引用或说明文字；此类检查不能数学证明不存在秘密。已有 6.2 MB benchmark JSON 和图标/字体属于此前已跟踪内容，不是本轮生成日志或安装包。基线提交前再次检查 staged 文件名、大小、diff 和指纹。

最终 staged 树检查范围为 1,015 个文件：13 处文本模式命中均已人工核对为上述非凭据内容；16 个既有二进制图像/字体未按文本扫描。当前改动共 28 个 staged 文件，禁止的临时文件或敏感文件路径命中为 0。忽略规则验证同时确认 `desktop/build/` 的 spec 和 requirements 源码仍可跟踪。扫描不是完整 Git 历史泄露审计，也不替代后续独立仓库审查。

本次只建立和推送基线，不启动新的全面仓库审计，不运行发布 workflow，也不声称远端 CI 已通过。
