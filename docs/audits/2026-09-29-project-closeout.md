# 项目收尾状态（2026-09-29）

本文档记录项目开发结束时的最终状态、验证证据与已知未竟事项。开发工作于 2026-09-29 停止，此后仓库进入冻结状态。

## 最终状态

- 最终分支：`master`（自 `2fc8065` 起快进包含全部后续工作；`rc2/a05-remediation` 为收尾前最后的工作分支）。
- 最终版本：`2.1.3-rc.2`（`cli/VERSION`）。
- 规范发布工具链：Windows/Desktop Release Toolchain V1（Node 20.20.2 / Python 3.11.9 / Electron 33.4.11 / electron-builder 25.1.8 / PyInstaller 6.22.3，见 README）。
- 最后一次冻结制品：RC1 安装器 SHA256 `965598c2…`（源 `2d38cb3`）。收尾批次（含白屏修复）**未重建安装器**。

## 最终验证

| 项目 | 结果 |
|---|---|
| 全量回归（干净环境复跑，2026-09-29，venv-b01r2-regression，`COW_DATA_DIR` 隔离） | **1137 passed / 38 skipped / 92 subtests passed / 0 failed，exit 0**（22:29）；证据 `docs/audits/evidence/closeout-20260929/` 与宿主机 `archive/rc2-evidence-2026-09/final-full-regression-20260929-r2/` |
| 同日首次全量回归 | 3 failed / 1134 passed；3 个失败（customer acceptance / durable ledger 崩溃窗口用例）与宿主机大流量归档拷贝并发，单独复跑 3/3 通过，判定为环境性失败，已由干净复跑取代 |
| 桌面端 `tsc --noEmit -p desktop/tsconfig.json` | exit 0（2026-09-29 两次独立复核） |
| 白屏根因回归 `desktop/tests/tool-step-rendering.test.cjs`（真实 React + ToolStep + 无头 Edge） | RED：修复前复现 renderer 崩溃；GREEN：修复后 1 passed / 0 failed，见 [白屏报告](2026-09-29-white-screen-root-cause-and-fix.md) |
| RC2 目标测试（stream cleanup / secret logging / session security / durable ledger） | 99 passed |

## 已知未竟事项

1. **白屏修复未进入安装制品**：`MessageSteps.tsx` 修复与 `render-process-gone` 兜底建议停留在源码层，已安装的 RC2 制品仍会白屏；恢复需按规范工具链重建安装器。
2. **Electron 主进程无 `render-process-gone` 处理与 `crashReporter`**：renderer 崩溃后无自动恢复/落盘，属独立防御层后续项（白屏根因报告有说明）。
3. **外部干净机（VM）安装验收未完成**：A05 外部验收进行到探针脚本就绪阶段，宿主 Hyper-V 需要管理员 `Get-VM` 权限，项目结束时未取得；探针脚本已归档于 `acceptance/a05/`。
4. **安装器代码签名链路未打通**：无管理员权限时 winCodeSign/Go 侧 rcedit 符号链接特权缺失（B01 记录），正式签名构建未完成。
5. **Python 3.13 与 Node 26 未通过发布验证**（README 已声明，升级属后续独立任务）。
6. A05 复核结论：R-001（外部 401 探针）与 R-004（观测时序）为验收工具问题、非产品缺陷；R-002（取消生命周期竞态窗口）与 R-003（错误路径密钥日志）在本批已修复并附回归测试，见 [A05 取证报告](2026-09-26-a05-rc1-forensic-investigation.md)。

## 归档索引

- 仓库内：`docs/audits/`（历次审计与验收报告）、`docs/audits/evidence/`（机器可读证据）、`docs/归档/`（冻结不实现方案：录屏沉淀技能，含 zip 与 SHA256）。
- 宿主机 `archive/`（不入库）：`rc2-evidence-2026-09/`（RC2 期间全部运行证据精选，含两轮最终全量回归完整输出）、`audit-scripts-2026-09/`（各批次证据生成脚本）、`output-2026-09/`（早期 UI 截图与 PDF）、`research/`（上游调研材料）、`pre-cleanup-2026-09-18/`、`reports-2026-09-18/`、`delivery-prep-AI_SNAPSHOT_20260902/`。
- 历史工作树（acceptance-35b312f / b01-r2 / b01-r2-rc1 / fix-current / product-d60e99f / rc2-remediation）已于收尾时移除，其唯一状态保存在对应分支：`codex/acceptance-evidence-35b312f`、`hardening/b01-r2-toolchain-freeze`、`acceptance/windows-real-boundary-d60e99f`、`rc2/a05-remediation` 等，随收尾一并推送远端。
