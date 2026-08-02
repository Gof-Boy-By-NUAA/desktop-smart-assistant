# 技能影子检索与治理证据节点

## 结论

附件提出的“外部可检索、可验证、可更新工作手册”可以分阶段加入。
本节点已经加入不会改变用户回答的技能影子检索(Shadow Retrieval)、脱敏证据链，以及
独立开关控制的受治理 Top-K 提示词注入路径。两个功能均默认关闭。当前没有真实
“任务 -> 正确技能”客户域标注集，因此生产开关仍未批准启用；自动发布、训练式
控制器(Controller)和多轨迹进化也不在本节点批准范围内。

## 已加入能力

- 只索引技能治理事实库中状态为 `active` 的不可变版本；
- 使用租户化词法检索返回 Top-K 候选，并在写入遥测前回查治理状态和内容哈希；
- 仅开启影子检索时，候选不进入系统提示词、消息、工具集合或最终回答；
- 遥测只保存任务、身份、会话、工具参数和结果的类型、长度、域分离
  HMAC 摘要，不保存原始任务、参数值或工具结果正文；
- 完成后的运行立即封存，拒绝迟到工具事件和二次结束；
- 进化提案只能引用已完成、可解析、白名单化的影子证据批次；没有合格证据时不暴露
  `skill_propose`，执行失败关闭(Fail Closed)；
- 索引是可重建派生面。治理 generation 未变化但索引被篡改时，运行时仍会比对完整
  文档集合并自动重建；
- `skill_shadow_retrieval_enabled` 在默认配置、模板以及配置键缺失时均为关闭。
- `skill_retrieval_injection_enabled` 独立控制受治理 Top-K 注入，默认和缺键均为关闭；
  详细边界见 `docs/rewrite/skill-production-injection.md`。

## 运行时调用链

```text
Agent.run_stream / ChatService.run
  -> AgentStreamExecutor._start_skill_shadow
  -> SkillManager.get_shadow_runtime
  -> ActiveSkillShadowRuntime.start_run
  -> GovernedSkillRepository.list_active_versions
  -> TenantAwareLexicalIndex.replace_tenant / search / matches_tenant
  -> GovernedSkillRepository.read_version（二次事实回查）
  -> ShadowTelemetryRepository.create_run

仅显式开启生产注入时
  -> SkillManager.verify_production_candidates
  -> PromptBuilder.build（严格技能过滤）
  -> ShadowTelemetryRepository.record_injection

工具调用与结果
  -> record_tool_use / record_tool_result（结构和 HMAC）

运行结束
  -> finish_run（封存）
  -> export_evidence（规范 JSON）
  -> Evolution schema v1/v2 白名单归一化
  -> skill-shadow://evidence/sha256/<hash>
```

## 修复影响分析(Fix Impact Analysis)

### 调用方与行为

- `Agent.run_stream()` 和 `ChatService.run()` 共用同一个执行器钩子；没有新增第二条 Agent
  Loop；
- 两个开关均关闭时不会创建索引或遥测连接；
- 开关开启后，影子运行异常被捕获并记录异常类型，用户回答路径保持不变；
- `SkillManager` 按需创建并缓存运行时，`close()` 同时释放检索与遥测 SQLite 连接；
- `AgentInitializer` 从记忆运行时的可信 `IdentityContext` 传递租户，不接受模型提交租户。

### 数据结构与并发

- 新增 `skill-retrieval.db`、`skill-shadow.db` 和本机随机
  `skill-shadow-hmac.key`，都位于技能目录的 `.system` 下；
- 现有技能 YAML/Markdown 和治理事实表结构不变；索引可从治理事实完整重建；
- 遥测库 schema v2 增加注入请求、状态、数量和候选投影复核标记；演化证据归一化器
  同时接受历史 v1 和当前 v2，并只输出固定白名单字段；
- 进程内使用可重入锁，跨运行时实例使用技能根目录共享锁；SQLite 设置 WAL、忙等待和
  外键；事务异常路径显式回滚；
- 身份绑定、显式技能复核、生产候选复核、提示词构建和运行快照构建都会自行取得同一把
  技能根锁；锁顺序统一为“治理根锁 -> 运行时实例锁”；
- 真实双线程测试并发执行首次 `SkillManager.get_shadow_runtime()` 与
  `Agent.get_full_system_prompt()`，通过 `Barrier` 同步启动并以 `Future.result(timeout=10)`
  将死锁转换为确定性失败；
- 完整提示词重建失败时，无论是否使用严格过滤，缓存回退都会移除整个技能区段，避免继续
  使用已经撤销或替换的技能指令；
- 当前 HMAC 只能证明同一安装内结构摘要的一致性，不能证明技能提升任务成功率。

## 可复核证据

执行环境：Windows，Python 3.11.8，SQLite 3.43.1，日期 2026-07-29。

当前父仓库没有 SmartAssistant 提交历史，节点暂以固定文件清单的字节级 SHA-256 绑定实现：

```text
skill_shadow_implementation_sha256=7bc7507d7aa4b4fbb91cd9c4baf24ba89f8c4edd0f853eeafe3740aa9ecdad26
benchmark_selector_sha256=73df23356ea935de7ffca5b12842a3d2b5bb14c81b52aa0193df51b9745fa011
skill_dataset_sha256=ed5587e918854a32e8ee143550f2ef88e841bf311a3efbc3eb43c756edf889d8
retrieval_report_sha256=d38227b3e4bc7f39f98af43bcc12a37095a368472da9ba5e9ff683316db1142a
knowledge_report_sha256=4299e311f331616bc3254fd3834aad9ecb85ebfe1d8f03cbdb823f59e6e4f3e3
```

实现指纹按排序后的相对 POSIX 路径名、换行、文件原始字节和零分隔符依次聚合，共覆盖
21 个文件：`agent/skills/governance/` 的 5 个 Python 文件、
`agent/skills/retrieval/` 的 4 个 Python 文件、`agent/skills/manager.py`、
`agent/skills/locks.py`、`agent/protocol/agent.py`、`agent/protocol/agent_stream.py`、
`agent/prompt/builder.py`、`agent/evolution/executor.py`、`bridge/agent_initializer.py`、
`benchmarks/skills/dataset.py`、`benchmarks/skills/metrics.py`、
`benchmarks/skills/runner.py`、`config.py` 和 `config-template.json`。这不是 Git 提交的
替代品，正式交付仍需提交级绑定。

```text
治理技能定向影响集：108 passed in 14.58s
全量回归：724 passed, 38 skipped, 1 warning in 114.78s
依赖检查：No broken requirements found.
自有源码编译检查：exit code 0
严格 UTF-8 扫描：strict_utf8_ok files=898
乱码特征扫描：mojibake_scan_ok files=613
JSON / GitHub Actions YAML 解析：2 / 12 files passed
桌面生产构建：Vite 2100 modules transformed，渲染器和主进程 TypeScript 构建通过
桌面归属资源检查：LICENSE / NOTICE 均存在且进入 extraResources
技能选择数据门禁：exit=2，status=blocked_invalid_dataset，metrics=null
```

首次全量回归与其他代理的毫秒级性能测试并行时，出现一次候选 P95 相对延迟门禁失败；
该单测随后在隔离条件下连续 5 次通过，停止并行负载后的完整回归也通过。该门禁仍可能受
Windows 线程调度抖动影响，后续持续集成应避免同时运行同一性能夹具。

真实 CMRC 2018 检索门禁使用 848 个文档、3219 个问题、三轮交错运行，报告为
`benchmarks/results/cmrc2018-comparison.json`：

| 指标 | SmartAssistant 原版基线 | 当前词法检索 | 结果 |
|---|---:|---:|---|
| Recall@1 | 0.1121 | 0.8984 | 通过 |
| Recall@5 | 0.1351 | 0.9444 | 通过 |
| Recall@10 | 0.1417 | 0.9487 | 通过 |
| MRR@10 | 0.1210 | 0.9184 | 通过 |
| 空结果率 | 0.7897 | 0.0146 | 通过 |
| 平均延迟 | 5.9593 ms | 4.3326 ms | 通过 |

结果为 9/9 门禁通过，`passed: true`。该数据证明共用词法引擎优于 SmartAssistant 原版，
但 CMRC 没有技能相关性标签，不能据此声称技能 Top-K 召回率已经提升。

当前知识实现还通过了 `benchmarks/results/cmrc2018-knowledge-comparison.json` 的
42/42 门禁：引用覆盖率为 1.0，权限泄漏率和撤销污染率均为 0.0；比较实现指纹为
`a92e1805992e19fba3719200c51e5fc5b4710275208e3134df75ce3897158289`。

## 未批准能力与下一门禁

以下能力在获得真实客户域标注集前保持未实现或不启用：

1. 在生产环境打开 Top-K 技能注入开关；
2. 以向量加 BM25 的混合检索(Hybrid Retrieval)替代当前词法影子检索；
3. 根据一次成功或失败自动执行 Add / Merge / Discard；
4. 训练记忆控制器或自动选择记忆操作技能；
5. 用多轨迹融合结果自动修改技能；
6. 宣称技能提高了客户任务成功率。

下一阶段至少需要客户提供或确认：任务文本、期望技能 ID/版本、禁止技能、目标模型、
环境版本和成功判据。报告必须绑定数据集 SHA-256、实现指纹、完整运行日志，并分别比较
无技能、影子检索和候选注入三组结果。
