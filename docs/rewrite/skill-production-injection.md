# 受治理 Top-K 技能生产注入

## 状态

代码路径已实现，生产启用未批准。`skill_retrieval_injection_enabled=false` 和
`skill_retrieval_injection_top_k=5` 是默认配置；配置键缺失时同样关闭。当前 GitHub Issue
技能选择数据因来源缺失、时间矛盾和标签泄漏被严格门禁拒绝，输出必须保持
`status=blocked_invalid_dataset`、`metrics=null`。

## 调用链

```text
Agent.run_stream / ChatService.run
  -> AgentStreamExecutor._start_skill_shadow
  -> ActiveSkillShadowRuntime.start_run
  -> SkillManager.verify_production_candidates
  -> active 状态、内容哈希、模型兼容、租户和 skill:read 复核
  -> 治理投影路径、内存内容和磁盘 UTF-8 字节复核
  -> SkillManager.production_skill_filter
  -> Agent.get_full_system_prompt(strict_skill_filter=True)
  -> ShadowTelemetryRepository.record_injection
  -> 首次 LLM 调用
```

只替换治理技能子集：内置技能和非治理自定义技能保持原有行为。显式 `skill_filter` 不触发
自动 Top-K 选择，但其中的治理技能仍在提示词构建前执行相同的身份、模型和投影复核。
身份绑定、技能刷新、显式复核和提示词构建持有同一技能根治理锁，发布不能插入复核与
构建之间。

## 失败关闭

- 缺可信身份、租户不匹配或缺少 `skill:read` 时不检索、不记录未授权候选；
- 候选撤销、版本变化、哈希变化、模型不兼容、投影损坏或技能禁用时不注入；
- 检索、提示词严格重建或注入遥测失败时移除整个技能提示词区段；
- 关闭开关后的下一轮会清理上一轮 `_last_skill_injection`，避免陈旧状态被误读；
- 遥测只保存任务、身份、参数和结果的类型、长度及域分离 HMAC，不保存正文。

## 证据边界

单元和集成测试证明权限、投影一致性、失败关闭、统一入口与证据脱敏按代码契约工作。
它们不证明技能选择正确，也不证明回答质量、客户任务成功率、延迟或成本改善。启用生产前
必须使用客户授权金标，在同一模型、提示词、工具和参数下完成全量技能基线与 Top-K 的
配对验收，并由独立验证器复算签名证据。

## 修复影响分析(Fix Impact Analysis)

- `Agent.run_stream` 和 `ChatService.run` 继续共用一个 `AgentStreamExecutor`，没有第二条
  智能体循环(Agent Loop)；
- `PromptBuilder` 的严格过滤参数只在治理注入重建时传播异常，普通路径保留兼容行为；
- `SkillManager` 的可信身份会在每次提示词重建时重新绑定，包括显式清空，防止跨会话残留；
- 遥测 schema 从 v1 幂等迁移到 v2；旧列保留，新证据消费者兼容 v1/v2；
- 默认开关关闭，因此未获批准的部署不会自动改变模型输入。
