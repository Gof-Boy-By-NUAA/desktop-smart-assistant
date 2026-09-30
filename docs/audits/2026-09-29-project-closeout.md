# 项目收尾状态（2026-09-29）

本文档记录项目开发结束时的最终状态、验证证据与已知未竟事项。开发工作于 2026-09-29 停止，此后仓库进入冻结状态。

## 最终状态

- 开发收尾基线：`master` @ `2cf708e334a600d59e42831f83efe63a6a215016`，经 PR #5 rebase 合并；本次文档与 CI 整改通过独立 PR 交付。
- 最终版本：`2.1.3-rc.2`（`cli/VERSION`）。
- 规范发布工具链：Windows/Desktop Release Toolchain V1（Node 20.20.2 / Python 3.11.9 / Electron 33.4.11 / electron-builder 25.1.8 / PyInstaller 6.22.3，见 README）。
- 真实 RC2 制品：2026-09-29 15:22 启动构建，15:26 生成 `C:\A05-Acceptance\Transfer\SmartAssistant-Setup-2.1.3-rc.2-x64.exe`，SHA256 为 `6D82F4E6CA43A0B354D2065F8362F4B170AC7A9B987C2FA69581E3DE34C7BDF3`。构建、宿主机卸载及安装记录的退出码均为 0。
- 2026-09-30 直接解包该安装器，提取 `resources/app.asar` 的 SHA256 为 `5979F8B8AD66D8895EC209032F9B79C6373A4F5A363B11BB98BF8863D0EEC792`；其中已包含工具结果转显示文本的白屏修复代码。解包与代码检查不等于人工桌面验收通过。该制品来自当时含未提交修复的工作树，不能声称它由后续 master 提交构建。

## 最终验证

| 项目 | 结果 |
|---|---|
| 全量回归（干净环境复跑，2026-09-29，venv-b01r2-regression，`COW_DATA_DIR` 隔离） | **1137 passed / 38 skipped / 92 subtests passed / 0 failed，exit 0**（22:29）；证据 `docs/audits/evidence/closeout-20260929/` 与宿主机 `archive/rc2-evidence-2026-09/final-full-regression-20260929-r2/` |
| 同日首次全量回归 | 3 failed / 1134 passed；首次失败原因尚未确认，后续单独重跑 3/3 与全量验证已通过。并发归档与失败同时发生不能证明因果关系；保留首次失败记录 |
| 桌面端 `tsc --noEmit -p desktop/tsconfig.json` | exit 0（2026-09-29 两次独立复核） |
| 白屏根因回归 `desktop/tests/tool-step-rendering.test.cjs`（真实 React + ToolStep + 无头 Edge） | RED：修复前复现 React 异常与根 DOM 清空；GREEN：修复后 1 passed / 0 failed。此前仅在本地通过，尚未接入 CI；本次整改增加 CI 步骤，实际结果以该 PR 的执行记录为准，见 [白屏报告](2026-09-29-white-screen-root-cause-and-fix.md) |
| RC2 目标测试（stream cleanup / secret logging / session security / durable ledger） | 99 passed |
| 开发收尾基线 `2cf708e` 的 GitHub 检查 | **10 项 SUCCESS / 2 项 SKIPPED（Docker 构建按策略跳过）**。两个 `build-image` job 仅允许上游 `zhayujie/SmartAssistant` 仓库运行；SKIPPED 不作为 PASS，未覆盖当时尚未接入的白屏回归 |

## 已知未竟事项

1. **白屏修复后的人工验收待记录**：修复已进入上述安装制品，并已安装到宿主机；本记录不宣称修复后人工上传图片验收通过。
2. **Electron 主进程无 `render-process-gone` 处理与 `crashReporter`**：实际进程退出的恢复与诊断属于独立后续项。当前白屏复现证明应用级 React 渲染异常，不能据此确认 renderer 进程死亡。
3. **外部干净机（VM）完整安装验收未完成**：A05 启动观察与外围拒绝检查已通过；完整安装、重启、卸载及数据保留验收仍待完成，不能将其混同。执行时需重新核实 VM 访问权限与状态。
4. **安装器代码签名验收未完成**：上述制品构建日志记录 `signing skipped (no signtool/credentials)`，不能宣称正式签名链路通过。
5. **Python 3.13 与 Node 26 未通过发布验证**（README 已声明，升级属后续独立任务）。
6. A05 复核结论：R-001（外部 401 探针）与 R-004（观测时序）为验收工具问题、非产品缺陷；R-002（取消生命周期竞态窗口）与 R-003（错误路径密钥日志）在本批已修复并附回归测试，见 [A05 取证报告](2026-09-26-a05-rc1-forensic-investigation.md)。

## 归档索引

- 仓库内：`docs/audits/`（历次审计与验收报告）、`docs/audits/evidence/`（机器可读证据）、`docs/归档/`（冻结不实现方案：录屏沉淀技能，含 zip 与 SHA256）。
- 宿主机 `archive/`（不入库）：`rc2-evidence-2026-09/`（RC2 期间全部运行证据精选，含两轮最终全量回归完整输出）、`audit-scripts-2026-09/`（各批次证据生成脚本）、`output-2026-09/`（早期 UI 截图与 PDF）、`research/`（上游调研材料）、`pre-cleanup-2026-09-18/`、`reports-2026-09-18/`、`delivery-prep-AI_SNAPSHOT_20260902/`。
- 历史工作树（acceptance-35b312f / b01-r2 / b01-r2-rc1 / fix-current / product-d60e99f / rc2-remediation）已于收尾时移除，其唯一状态保存在对应分支：`codex/acceptance-evidence-35b312f`、`hardening/b01-r2-toolchain-freeze`、`acceptance/windows-real-boundary-d60e99f`、`rc2/a05-remediation` 等，随收尾一并推送远端。
- 当前恢复说明：[仓库内规范副本](../../archive/final-state-20260929/RESTORE.md)。宿主机已有归档中的同名说明同步更新；历史 bundle 是较早快照，不能冒充最终 master 备份。
