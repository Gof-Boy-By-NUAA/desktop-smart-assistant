# 白屏根因定论与修复（React 渲染异常）

日期：2026-09-29；2026-09-30 修正证据定性。开发收尾基线为 `master` @ `2cf708e`，已包含白屏修复。本报告纠正此前未经进程退出证据支持的“renderer 死亡”表述；逐帧与现场证据见宿主机既有 `archive/rc2-evidence-2026-09/rc2-whitescreen-analysis-20260928/`。

## 根因（CONFIRMED）

已确认的缺陷是未捕获的 React 渲染异常：将复杂对象当作 React child 渲染，导致根 DOM 清空卸载。它解释了工具卡片展开后的白屏路径；应用级抛错与系统进程崩溃必须分开判断：

1. 已记录的 vision 工具结果是结构化 JSON 对象，包含 `content`、`model`、`provider`、`usage` 键；
2. `desktop/src/renderer/src/components/MessageSteps.tsx` 的 `ToolStep` 把 `step.result` 直接作为 React child 渲染，未先转成显示文本；
3. React 抛出 `Objects are not valid as a React child`，根 DOM 清空卸载。该证据没有记录 renderer PID 退出、`render-process-gone` 或系统崩溃转储，不能将其定性为 renderer 进程死亡；
4. 复现证据：`docs/audits/evidence/white-screen-20260929/repro.json`（修复前真实 `ToolStep` + React + 无头 Edge，3 条异常记录，`rootChildren: 0`）。

## 修复

- `MessageSteps.tsx`：渲染前把非字符串 `result` 规范化为 `JSON.stringify(result, null, 2)`，截断逻辑基于规范化文本；`null`/空值不渲染 Output 块。
- `chatStore.ts`（同批收尾修改）：历史回放与 `cancelled` 终态事件统一剥离取消标记并调用 `finishStream()` 释放回合，SSE/requestId 保留到权威终态事件为止。

## 验证（2026-09-29）

| 项目 | 结果 |
|---|---|
| 回归测试 `desktop/tests/tool-step-rendering.test.cjs`（真实 React + `ToolStep` + 无头 Edge；对象/数组/字符串/0/false/超长截断/null 七类输入） | RED：修复前 exit 1，复现同一 React 崩溃；GREEN：修复后 1 passed、0 failed、exit 0 |
| `tsc --noEmit -p desktop/tsconfig.json` | exit 0（同日独立复核再次 exit 0） |
| 后端目标测试 `test_rc2_agent_stream_cleanup / test_rc2_secret_response_logging / test_web_session_security / test_durable_web_execution_ledger` | 当时记录 99 passed；后续已在测试内隔离设置 `COW_DATA_DIR`，不能将早期环境要求当成当前执行前提 |

## 范围限制

- `INSTALLER_REBUILT=YES`、`INSTALLED_APP_UPDATED=YES`：2026-09-29 15:22 启动构建，构建、宿主机卸载与安装退出码均为 0。安装器位于 `C:\A05-Acceptance\Transfer\SmartAssistant-Setup-2.1.3-rc.2-x64.exe`，SHA256 为 `6D82F4E6CA43A0B354D2065F8362F4B170AC7A9B987C2FA69581E3DE34C7BDF3`；2026-09-30 解包检查已确认修复进入制品。该检查不证明修复后的人工桌面验收通过。
- Electron 主进程的 `render-process-gone` 兜底（自动 reload / crashReporter 落盘）仍缺失，属于独立防御层后续项，未在本轮实现。
- 浏览器回归使用合成的 JSON wire 结果，不验证真实模型调用，也不验证安装制品内的 Electron。

证据：`docs/audits/evidence/white-screen-20260929/fix-verification.txt` 保留原始 RED/GREEN/TYPECHECK 开发验证记录。其中 `INSTALLER_REBUILT=NO` 与当前已确认的制品状态不符，保留原文以供追溯，不将其当作当前交付状态，也不单凭该字段推定记录时间。真实构建与安装记录位于宿主机既有 `archive/rc2-evidence-2026-09/white-screen-install-20260929-152216/`；2026-09-28 白屏现场证据同样保留在该既有归档树。
