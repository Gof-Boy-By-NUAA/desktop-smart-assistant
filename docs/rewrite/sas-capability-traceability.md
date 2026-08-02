# SAS 能力重写追踪矩阵

## 证据边界

本矩阵只记录可由当前源码、测试或命令输出复核的映射，不采信 SAS 历史“证据清单”、
自证报告、夹具指标或未绑定当前实现的性能报告。

审计基线（2026-07-29）：

- SmartAssistant 原始 ZIP SHA-256：`F121B65E04E0CF7B008E6404F7A9E3CE11CEE8457ED58252B9A57B20366BCF58`；
- 当前 SmartAssistant 位于无提交历史的父 Git 仓库，尚不能用本地提交绑定实现；
- SAS 仓库：`https://github.com/Gof-Boy-By-NUAA/SuperAgentSystem.git`；
- SAS 当前 HEAD：`ad49f51abc8653a8b5b7a80be3ca911f0d3bef9e`；
- SAS 工作树有 28 项未提交修改，并落后 `origin/main` 2 个提交，因此当前 SAS 工作树不是可复现基线。

这里的“重写”表示依据需求重新实现，不表示逐行复制 SAS 源码。

## 已证实映射

| 能力 | SAS 当前源码入口 | SmartAssistant 重写入口 | 可复核测试或指标 | 状态与限制 |
|---|---|---|---|---|
| 可信身份、租户、作用域和敏感级别 | `services/memory-service/src/requestContext.ts`、`services/memory-service/src/accessControl.ts`、`db/migrations/0010_permission_sensitivity_governance.sql` | `agent/memory/governance/contracts.py`、`bridge/agent_initializer.py` | `tests/test_governed_memory.py`、`tests/test_governed_memory_runtime.py` | 已重写本地身份边界；`session_id` 不是企业身份提供方(IdP)认证 |
| 幂等、不可变版本、撤销、回滚和审计 | `libs/db/src/repositories/memoryGovernance.ts`、`libs/db/src/repositories/memoryAudit.ts` | `agent/memory/governance/repository.py`、`agent/memory/governance/service.py` | `tests/test_governed_memory.py`、`tests/test_governed_memory_runtime.py` | 已由行为测试覆盖；仍需真实持续集成(CI)运行记录 |
| 受权限约束的记忆检索和事实源回查 | `services/memory-service/src/retrievalGate.ts`、`services/memory-service/src/indexProjection.ts` | `agent/retrieval/lexical.py`、`agent/memory/manager.py` | `tests/test_memory_governed_retrieval_integration.py`、`benchmarks/retrieval/compare.py` | 真实 CMRC 2018 检索指标已有本地报告；原版基线仍需提交或源文件指纹冻结 |
| 知识上传、解析、来源和证据定位 | `services/memory-service/src/knowledgeUpload.ts`、`services/memory-service/src/knowledgeIngestion.ts`、`db/migrations/0014_knowledge_markdown_ingestion.sql` | `agent/knowledge/parser.py`、`agent/knowledge/contracts.py`、`agent/knowledge/repository.py`、`agent/knowledge/runtime.py` | `tests/test_governed_knowledge_runtime.py`、`tests/test_knowledge_service.py` | 已实现 UTF-8 字节位置、正文与引文哈希；需在当前实现指纹上重跑正式门禁 |
| 知识权限检索、引用回查和撤销污染防护 | `services/memory-service/src/knowledgeRetrieval.ts`、`services/memory-service/src/knowledgeDeleteCleanup.ts` | `agent/knowledge/runtime.py`、`agent/tools/knowledge/knowledge_tools.py` | `tests/test_governed_knowledge_runtime.py`、`benchmarks/knowledge/compare.py` | 2026-07-29 已按当前比较指纹 `a92e1805992e19fba3719200c51e5fc5b4710275208e3134df75ce3897158289` 重跑：CMRC 2018 全量三轮，42/42 门禁通过；不替代客户域验收 |

## SmartAssistant 增量能力

| 能力 | 当前实现 | 可复核证据 | 状态与限制 |
|---|---|---|---|
| 外部技能工作手册的影子检索、脱敏证据与受治理 Top-K 注入 | `agent/skills/retrieval/`、`agent/protocol/agent_stream.py`、`agent/evolution/executor.py` | `tests/test_skill_shadow_retrieval.py`、`docs/rewrite/skill-shadow-retrieval.md`、`docs/rewrite/skill-production-injection.md` | 影子和注入开关均默认关闭；生产路径只选择 active、模型兼容且投影字节一致的技能。共用检索引擎 9/9 真实数据门禁通过，但技能选择数据失败关闭，不能启用生产或宣称任务成功率提升 |
| 治理记忆持久事务发件箱与跨进程收敛 | `agent/memory/governance/repository.py`、`agent/memory/governance/locks.py`、`agent/memory/manager.py` | `tests/test_governed_memory_runtime.py`、`tests/test_memory_outbox_benchmark.py`、`benchmarks/results/cmrc2018-memory-outbox.json` | CMRC 2018 全量 848 文档 14/14 门禁通过；证明本地派生一致性和恢复性能，不证明客户任务效果或分布式协调 |
| 评测专用受控技能注入与签名证据复算 | `benchmarks/customer/` | `tests/test_customer_acceptance.py`、`docs/rewrite/customer-controlled-skill-acceptance.md` | 17 个签名合成协议测试通过；该客户验收框架未与默认关闭的生产 Top-K 路径自动联动。缺真实客户包、私钥隔离的执行适配器、客户 Judge 和签署结果，状态必须保持 `pending_customer_inputs` |

## 尚未证实或未迁移

| SAS 能力 | SAS 当前源码入口 | SmartAssistant 当前状态 | 完成所需证据 |
|---|---|---|---|
| 企业身份提供方、组织角色同步 | `services/memory-service/src/accessControl.ts` | 未接入企业 IdP | 目标身份系统集成测试、越权负例和审计日志 |
| 知识审查、批量审批和晋升 | `services/memory-service/src/knowledgeReview.ts`、`services/memory-service/src/knowledgePromotion.ts` | 未发现等价产品闭环 | 选型决定、产品接口、权限测试和真实审查数据指标 |
| 实体去重、合并与知识图谱治理 | `services/memory-service/src/knowledgeEntityDedup.ts`、`services/memory-service/src/knowledgeEntityMerge.ts`、`services/memory-service/src/knowledgeGraph.ts` | 未迁移 | 独立实现、消融实验和人工标注准确率 |
| 规则抽取、推理和重验证 | `services/memory-service/src/knowledgeRule.ts`、`services/memory-service/src/knowledgeReasoning.ts`、`services/memory-service/src/knowledgeReasoningRevalidation.ts` | 未迁移 | 非合成数据集、精确率/召回率、错误传播和撤销测试 |
| 程序性记忆 | `services/memory-service/src/proceduralMemory.ts` | 未迁移 | 产品需求确认、调用链测试和客户场景指标 |
| 静态加密和密钥管理 | 尚未完成 SAS 源码入口审计 | 未实现 | 先定位可复核源码，再执行密钥生命周期、迁移、恢复和威胁模型验证 |
| 目标客户验收 | 不适用 | 未执行 | 客户数据集哈希、场景清单、环境版本、完整日志和验收结论 |

## 交付门禁

每次声明某项 SAS 能力完成前，必须同时满足：

1. 本矩阵绑定 SAS 基线提交或归档补丁哈希，以及 SmartAssistant 实现提交；
2. 列出真实调用链、数据结构兼容性和修复影响分析(Fix Impact Analysis)；
3. 单元、集成、全量回归和持续集成(CI)均有可复现运行记录；
4. 性能报告绑定当前实现指纹、真实数据集哈希和门禁代码指纹；
5. 涉及目标客户的能力必须通过客户数据与场景验收，不得以 CMRC 或合成夹具替代。
