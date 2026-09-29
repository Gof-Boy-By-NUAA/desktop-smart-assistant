# 白屏根因定论与修复（renderer 崩溃）

日期：2026-09-29。工作树：`SmartAssistant-rc2-remediation`（分支 `rc2/a05-remediation`，基线 `2d38cb3` 加未提交修改）。本报告解决 2026-09-28 白屏分析遗留的「renderer 死亡、死因 UNKNOWN」问题（逐帧与系统级证据见宿主机 `archive/rc2-evidence-2026-09/rc2-whitescreen-analysis-20260928/`）：根因已定位并在源码层修复。

## 根因（CONFIRMED）

安装版 RC2 桌面端在触发 vision 工具后整窗白屏、重启前不可恢复，根因是 renderer 内未捕获的 React 渲染崩溃：

1. 后端 `models/zhipuai/zhipuai_bot.py` 将 vision 工具结果以结构化 JSON 对象（`content`、`model`、`provider`、`usage` 键）写入 tool step；
2. `desktop/src/renderer/src/components/MessageSteps.tsx` 的 `ToolStep` 把 `step.result` 直接作为 React child 渲染，未先转成显示文本；
3. React 抛出 `Objects are not valid as a React child`，renderer 进程崩溃，Electron 主进程没有 `render-process-gone` 处理与 `crashReporter`，窗口停留在永久白屏；
4. 复现证据：`docs/audits/evidence/white-screen-20260929/repro.json`（真实未改动的 `ToolStep` + React + 无头 Edge，3 次崩溃记录，`rootChildren: 0`）。

## 修复

- `MessageSteps.tsx`：渲染前把非字符串 `result` 规范化为 `JSON.stringify(result, null, 2)`，截断逻辑基于规范化文本；`null`/空值不渲染 Output 块。
- `chatStore.ts`（同批收尾修改）：历史回放与 `cancelled` 终态事件统一剥离取消标记并调用 `finishStream()` 释放回合，SSE/requestId 保留到权威终态事件为止。

## 验证（2026-09-29）

| 项目 | 结果 |
|---|---|
| 回归测试 `desktop/tests/tool-step-rendering.test.cjs`（真实 React + `ToolStep` + 无头 Edge；对象/数组/字符串/0/false/超长截断/null 七类输入） | RED：修复前 exit 1，复现同一 React 崩溃；GREEN：修复后 1 passed、0 failed、exit 0 |
| `tsc --noEmit -p desktop/tsconfig.json` | exit 0（同日独立复核再次 exit 0） |
| 后端目标测试 `test_rc2_agent_stream_cleanup / test_rc2_secret_response_logging / test_web_session_security / test_durable_web_execution_ledger` | 99 passed（运行时需设置 `COW_DATA_DIR`；该测试文件在第 582 行读取此变量写观测文件） |

## 范围限制

- `INSTALLER_REBUILT=NO`、`INSTALLED_APP_UPDATED=NO`：修复停留在源码层，未重建安装器，已安装的 RC2 制品仍会白屏，需按规范发布工具链重建后才进入制品。
- Electron 主进程的 `render-process-gone` 兜底（自动 reload / crashReporter 落盘）仍缺失，属于独立防御层后续项，未在本轮实现。
- 浏览器回归使用合成的 JSON wire 结果，不验证真实模型调用，也不验证安装制品内的 Electron。

证据：`docs/audits/evidence/white-screen-20260929/fix-verification.txt`（RED/GREEN/TYPECHECK 记录）；2026-09-28 白屏现场证据已存档于宿主机 `archive/rc2-evidence-2026-09/`（工作区收尾归档，不入库）。
