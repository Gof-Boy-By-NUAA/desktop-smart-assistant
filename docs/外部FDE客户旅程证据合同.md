# 外部 FDE 客户旅程证据合同

## 目的和严格边界

本合同定义目标客户在**签名发布制品**上执行 FDE 用户旅程后，如何把可验证证据交给受保护的 release runner。它不是测试夹具、演示脚本或本地 PASS 开关。

在没有客户签名 evidence、外部 trust root 和受保护的 trust-root SHA pin 时：

    FDE_CASE_EVIDENCE=ABSENT
    TARGET_CUSTOMER_ACCEPTANCE=NO
    CUSTOMER_ATTESTATION=ABSENT
    CUSTOMER_TEST_EXECUTION=NOT_RUN

不得把本仓库测试私钥、临时 JSON、同仓文件或环境变量样例用于生产验收。

## 外部输入

release runner 必须从仓库外部的受控位置提供以下三个值：

| 输入 | 环境变量 | 约束 |
| --- | --- | --- |
| 客户 FDE evidence | `SMART_ASSISTANT_FDE_EVIDENCE_PATH` | 外部常规 JSON 文件；不得位于 checkout 内、不得为符号链接。 |
| 客户 trust root | `SMART_ASSISTANT_FDE_TRUST_ROOT_PATH` | 外部常规 JSON 文件；包含允许的 customer Ed25519 signer。 |
| trust-root SHA-256 pin | `SMART_ASSISTANT_FDE_TRUST_ROOT_SHA256` | 由受保护 runner/密钥管理系统提供的 64 位小写 SHA-256；不得由同一 PR 写入。 |

解析器位于 `benchmarks/evidence/fde_case.py`。缺任一输入、路径位于 checkout、信任根不匹配 pin、字段多余/缺失、签名无效、journey 缺失、commit 或 source fingerprint 不匹配，都会得到 `status=ABSENT`、`INCOMPLETE_EXTERNAL_INPUT` 或 `INVALID`，并且 `passed=false`。

即使原始签名 evidence 的 `status=VERIFIED`，release manifest 也只会在 checkout 干净、所有源码已跟踪且 `commit_bound=true` 时令 `release_passed=true`。客户证据不能为未提交的工作树、临时改动或本地测试解除 `FDE_CASE_EVIDENCE=ABSENT`。

## 必须覆盖的客户旅程

客户证据的 `journeys` 必须恰好覆盖以下四项，且每项 `outcome=passed` 并含独立 evidence SHA-256：

1. `web_authenticated_agent_retry`：认证 Web 端发送、丢失响应后的同幂等键重试，只启动一个 Agent/tool run。
2. `web_attachment_citation_cancel_reconnect`：附件、citation、取消和 SSE 重连不越权、不把未知状态显示为成功。
3. `web_restart_in_doubt_recovery`：重启或断线后，未知执行只显示 `in_doubt`/明确错误，不自动重放可能有副作用的运行。
4. `desktop_authenticated_agent_retry`：桌面端发送与重连遵守同一幂等语义，并能向员工呈现可理解的状态。

每项 evidence SHA 必须指向客户受控留存物，例如执行日志、浏览器/桌面端录屏、代理日志、服务端日志、请求/响应摘要及操作者记录。哈希本身不替代原始留存物。

## 客户签名载荷

客户签名的 JSON 必须严格使用 schema version 1，kind 为 `smart-assistant-fde-case-evidence`，并含：

- `case_id`、`customer_id`、`execution_id`；
- 实际待验收 commit 的 `git_commit`；
- release runner 所算完整 checkout 的 `source_fingerprint_sha256`；
- 签名制品 `artifact_sha256` 和已批准环境 `environment_sha256`；
- 可解析的 `executed_at`；
- 上述全部 `journeys`；
- `attestation.key_id` 和对其余字段 canonical JSON 的 Ed25519 `signature`。

trust root 的 kind 为 `smart-assistant-fde-trust-root`，只允许 schema version 1 和唯一的 `key_id -> ed25519_public_key` 映射。客户签名私钥绝不能传入 SmartAssistant checkout、测试代码、日志或本地环境。

## 受保护 runner 操作

1. checkout 待发布 commit，验证签名制品 SHA。
2. 从密钥管理系统加载 trust-root SHA pin；从客户受控存储挂载 trust root 和 evidence。
3. 以只读方式设置三个环境变量，不打印其完整路径或任何私钥。
4. 执行：

       python -m benchmarks.evidence.release_manifest --output <external-artifact>/release-evidence-manifest.json
       python -m benchmarks.evidence.release_manifest --verify --output <external-artifact>/release-evidence-manifest.json

5. 独立 verifier 必须复算 checkout 指纹、Git commit、trust-root pin、客户签名、journey 集合和所有正式报告，然后把 manifest、制品 SHA、日志摘要和客户签署结果上传到受保护存储。

即使 FDE evidence 验证成功，release 仍必须保持失败，直到受保护 CI、branch protection、签名制品、可重现构建、Docker/installer、迁移回滚、72h soak、告警和 Skills/customer gates 全部由真实证据满足。

## 被拒绝的做法

- 把 evidence 或 trust root 提交到同一 checkout。
- 由 SmartAssistant 生成客户私钥、客户签名或客户 PASS。
- 仅凭本地测试、开发机 Dockerfile、同仓 verifier 或 Git commit 宣称客户验收。
- 缺少任一 journey 时填入 `N/A`、`PASS` 或复制旧 SHA。
- 将 FDE evidence 的 VERIFIED 状态解释为生产发布批准或 external verifier attestation。
