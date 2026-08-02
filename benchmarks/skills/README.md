# 技能选择基准

该基准对比“全部技能元数据”与受治理的 Top-K 选择链。Top-K 路径直接调用 SmartAssistant 的检索、有效状态复核、内容哈希复核、模型兼容复核和投影字节复核。

第二版数据加载器会拒绝非 UTF-8、重复 JSON 键、未知字段、非标准数值、标签与 `annotation_policy` 复算不一致、SHA-256 不一致及快照时间早于样本时间的数据。每条样本还必须提供 `HTTP 200`、`Date`、非空 `ETag`、`fetched_at`、本地归档原始响应及其 SHA-256、规范字段 SHA-256。加载器会重新读取原始响应并逐字段比对。

当前 `github_issue_skill_selection.json` 是第一版隔离快照，会首先因缺少 `provenance_complete` 和逐条抓取证明被拒绝。它同时存在冻结时间早于最新 `updated_at` 的时序错误。在可验证快照重建前，不得修改时间、替换命令行哈希或绕过门禁产生“通过”指标。命令行会输出 `status=blocked_invalid_dataset`、`metrics=null` 的 JSON 并以非零状态退出。

公开 GitHub issue 标题只能产生确定性银标（deterministic silver label）。这些数据没有 issue body、评论、客户任务结果或人工金标，因此不能用于声称客户成功率。

数据修复后的执行命令：

```powershell
python -m benchmarks.skills.runner --top-k 2
```
