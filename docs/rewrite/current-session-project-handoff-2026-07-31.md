# SmartAssistant + SAS 代码级重写：当前会话上下文、目标与工程交接

生成时间：2026-07-31 11:19:42 +08:00  
工作目录：`C:\Users\UnlimitedPower\Documents\桌面智能助手\SmartAssistant`  
SAS 只读参考：`D:\SuperAgentSystem`  
历史版本：`docs/rewrite/current-session-project-handoff-2026-07-30.md`

## 1. 执行结论

项目尚未完成，不能宣称已经达到投资人技术尽调或目标客户最终验收标准。

当前源码已经形成 SmartAssistant Harness、受治理记忆(Governed Memory)、受治理知识
(Governed Knowledge)、Citation v3、技能治理和客户验收框架的代码基础。Knowledge 证据
协议也已从旧三轮进程内方案推进到八轮独立子进程查询和十六个 ABBA/BAAB 建库区组，加入
原始延迟、调度环境、实现指纹复算、统计量复算和派生失败零容忍门禁。

但是，2026-07-31 11:19 的最新红队复核确认当前持续集成(CI)门禁不通过，旧 Knowledge
正式报告已经与当前比较器失配，不能继续作为当前性能证据。当前准确状态如下：

1. `python -m pytest -q` 结果为
   `17 failed, 759 passed, 40 skipped, 1 warning, 12 subtests passed in 76.63s`；17 个失败全部
   位于 `tests/test_knowledge_benchmark.py`，因此不能宣称完整 Python 回归通过。
2. Knowledge 比较器当前指纹为
   `620d17e2c23fe84d9cbe7a7fef27113f84590d084f00792ad3d85dd4e59ff521`，而现有正式报告记录
   `81613d33725615c9503789446a7ae9bb825d7f579f6e005228ed18c59d92f55f`；报告状态必须视为
   `superseded/unbound`。
3. Legacy 和 Governed 建库子进程均可退出 0，并返回 848 文档、SQLite 完整、纳秒/毫秒一致
   的严格 JSON；但 Legacy 的三个查询探针返回 `query_probes_passed=false`，父门禁抛出
   `ValueError: 建库计时后的索引完整性验证失败`，正式十六区组运行在开始阶段就会失败。
4. 当前比较器指纹只覆盖 13 个路径，漏掉直接决定查询选择、指标和百分位数的
   `benchmarks/retrieval/evaluate.py`、`benchmarks/retrieval/metrics.py`，也没有完整绑定日志等
   子进程运行依赖。
5. `query_selection_sha256` 目前只检查格式和轮次一致性，父进程没有从固定官方数据重新计算
   预期查询 ID 顺序哈希，数据选择绑定仍未闭合。
6. Retrieval 的历史完整数据结果、Memory 的 14/14 历史结果仍可作为后续复验基线，但在
   干净提交、当前指纹报告和远程 CI 完成前都不是发布级证据。
7. Web/Desktop Citation 闭环、Memory 原版同协议对照、Skills 真实金标、客户验收和 Git/CI
   发布绑定仍未完成。

因此，当前工程状态应定义为：**核心重写代码基础已形成，Knowledge 新证据协议主体已实现，
但其真实子进程门禁和 CI 测试尚未闭合；所有旧 Knowledge 性能主张暂停，项目仍处于工程
验证阶段。**

## 2. 当前会话上下文

### 2.1 业务背景

- 原 `D:\SuperAgentSystem` 在开发稳定性、Memory、Knowledge Platform(KP) 和证据可信度上
  存在较大问题，暂时无法承担面向潜在投资人的稳定演示。
- 用户提出以 SmartAssistant 的 Harness 和 Agent Loop 为底座，把 SAS 中确有价值的模块按对应
  边界做代码级重写和融合。
- 用户明确指出 SAS 的 Memory、KP 以及 Claude 生成的历史“证据清单”不可信。因此本项目
  不继承该清单中的完成结论，只接受当前源码、命令输出、真实数据、报告哈希和实现指纹。
- 用户允许并要求将可并行的审计任务拆给多个子代理。并行代理只负责只读检查，最终结论
  由主任务重新执行命令和读取源码后收敛。
- 用户提供了 AutoSkill、EvoSkill、MemSkill、CoEvoSkills、SE-Agent 等“外部工作手册”
  方向，并要求在当前节点后评估是否添加。安全子集已经评估并部分实现，完整自动演化闭环
  尚未获准进入生产。

### 2.2 会话决策轨迹

1. 拉取并阅读 SmartAssistant，定位 `agent/protocol/agent_stream.py`、工具管理、提示词、Memory、
   Knowledge、Skills、Web 和 Desktop。
2. 从“解释 SmartAssistant”转为“以 SmartAssistant 为底座，对 SmartAssistant + SAS 做代码级重写”。
3. 因 SAS/Claude 证据不可信，建立真实数据、实现 SHA-256、报告 SHA-256 和失败关闭
   (Fail Closed)的证据规则。
4. 分阶段加入受治理 Memory、Knowledge、共享检索、Citation v3、Skills 候选治理、影子
   检索、受控生产注入路径和客户验收框架。
5. 在 CMRC 2018 完整开发集上重跑 Retrieval、Knowledge 和 Memory 报告，并重新计算当前
   实现指纹。
6. 红队审计发现：报告“通过”不等于主张成立。Knowledge 建库显著性不足，Retrieval 缺
   原始延迟协议，Memory 缺原版对照，现有 Evidence Manifest 也已过期。
7. 当前节点转为完成边界收敛、证据协议修正、可复现 Git/CI、Citation 客户端闭环和真实
   客户验收。

### 2.3 合规与产品边界

用户曾要求移除许可证和来源信息，并用技术手段伪装成完全原创的非开源项目。该要求不能
执行，也不属于本项目目标。MIT 许可证允许修改、商业使用和再发布，但要求在软件副本或
重要部分中保留版权和许可文本；对投资人或技术团队虚假陈述来源还会制造尽调风险。

允许的路线是：

- 重构产品名称、界面、业务流程、部署方式和内部架构；
- 对 SAS 能力进行独立重写，不逐行复制不可信实现；
- 在交付物中保留 `LICENSE`、`NOTICE` 和依赖归属；
- 对外准确表述为“基于 MIT 上游并完成实质性重写和自有增量”，列出自有代码和指标；
- 用软件物料清单(SBOM)、提交历史、实现指纹和评测数据证明自有增量，而不是隐藏来源。

当前合规证据：

| 对象 | SHA-256 / 状态 |
|---|---|
| SmartAssistant 原始 ZIP | `f121b65e04e0cf7b008e6404f7a9e3ce11cee8457ed58252b9a57b20366bcf58` |
| `LICENSE` | `d72f9e8cfa805e7dd9edc73dda0cb150a715f4eb5b439ed597664639ffbf5dfc` |
| `NOTICE` | `b967fca85187c71da42500f97fc9654dc938a25dbbd9828ef3d9db684f4569f7` |
| Desktop 打包配置 | `desktop/package.json` 已把 `LICENSE`、`NOTICE` 加入 `extraResources` |

## 3. 目标信息与完成定义

### 3.1 总目标

> 以 SmartAssistant 的可运行 Harness 和 Agent Loop 为底座，对 SAS 中有价值、能由源码复核的
> 能力进行代码级重写；保持 SmartAssistant 核心能力不退化；所有“改进”主张由真实数据和当前
> 提交证明；最终通过目标客户控制的验收。

投资演示是阶段目标，不降低工程门禁。演示可以只选择已经通过的功能，但不得把待完成模块
或局部基准包装成完整产品能力。

### 3.2 项目级完成门禁

| 编号 | 完成条件 | 当前状态 |
|---|---|---|
| G1 | SmartAssistant 原版核心能力矩阵和同场景回归通过 | 部分完成；缺完整能力矩阵和多通道端到端对照 |
| G2 | 重写模块至少与原版持平 | Retrieval 历史质量报告可作基线但当前失配；Knowledge/Memory 的完整比较证据不足 |
| G3 | 声明提升的指标形成预注册、统计稳健的严格超越证据 | 未完成 |
| G4 | Memory/Knowledge/Skills 的权限、撤销、并发、崩溃恢复通过 | 本地局部通过；硬崩溃和跨进程边界仍有缺口 |
| G5 | Web/API/Desktop 按当前身份完成 Citation 点击和失效闭环 | 未完成 |
| G6 | Skills 真实金标、影子指标、生产灰度和回滚门禁通过 | 未完成；生产开关保持关闭 |
| G7 | 干净 Git 提交、远程 CI、制品哈希和许可证清单可复现 | 未完成 |
| G8 | 客户任务包、固定执行器、独立 Judge、签名结果通过 | 未完成；当前 `passed=false` |

只有 G1-G8 全部通过，并与同一个受跟踪提交绑定，项目才能标记为完成。

## 4. 工作区、源码和数据基线

### 4.1 SmartAssistant 工作区

本轮命令 `git status --short --branch` 输出：

```text
## No commits yet on master
?? .research/
?? SmartAssistant-master.zip
?? SmartAssistant/
?? run.log
```

这表示父仓库尚无任何提交，SmartAssistant 也没有独立 Git 历史。当前只能用文件字节哈希和实现
指纹临时绑定，不能提供发布级复现。

### 4.2 SAS 只读参考状态

以下是 2026-07-30 已记录的只读快照，不应当作 2026-07-31 实时状态：

- 远端：`https://github.com/Gof-Boy-By-NUAA/SuperAgentSystem.git`
- HEAD：`ad49f51abc8653a8b5b7a80be3ca911f0d3bef9e`
- 相对 `origin/main`：ahead 0、behind 2；
- 当时工作树包含大量已修改和未跟踪项。

因此 SAS 只可作为需求和源码线索，其历史文档、测试结论和“证据清单”不能直接继承。

### 4.3 真实评测数据

| 字段 | 值 |
|---|---|
| 数据集 | CMRC 2018 官方开发集 `cmrc2018_dev` |
| 上游仓库 | `https://github.com/ymcui/cmrc2018.git` |
| 固定提交 | `c0eb1b6ba219847457e6af3180da722bbeb656af` |
| 数据 SHA-256 | `5cfe4414c28a8ecbb51670f78c0dc7d1049f286c2d5769b52f1f94bcc0752cf1` |
| 规模 | 848 个文档、3219 个问题 |
| 当前环境 | Python 3.11.8、SQLite 3.43.1、Windows |

## 5. 已完成工作与证据边界

### 5.1 SmartAssistant 架构调研

已经定位并阅读以下主调用链：

```text
入口 / Channel
  -> AgentStreamExecutor
  -> 模型流式响应
  -> Tool Call 解析、筛选和执行
  -> Tool Result 回填上下文
  -> 下一轮模型调用或结束
```

调研覆盖 Agent Loop、工具、提示词、Memory、Knowledge、Skills、Web SSE 和 Desktop。
“已调研”不等于“已完成原版能力等价验证”；后者仍需要能力矩阵和真实端到端回归。

### 5.2 受治理 Memory

已经实现或形成测试基础：

- 租户、用户、会话、作用域和敏感级别；
- 幂等写、不可变版本、撤销、回滚和审计；
- 事实与派生任务原子提交的事务发件箱(Transactional Outbox)；
- 启动恢复、投影完整性、索引完整性和撤销零污染；
- 统一锁顺序、多运行时排空和部分跨进程行为测试。

Memory 报告当前仍与 Memory 文件指纹一致并记录 14/14 通过，证明单租户顺序协议下的
完整性和阈值性能；它没有 commit/CI 绑定，也未证明原版对照、多进程硬崩溃或线性一致性。

### 5.3 受治理 Knowledge 和 Citation v3

已经实现或形成测试基础：

- 上传、解析、UTF-8 字节范围、来源引用和不可变版本；
- 租户/用户/会话/集合/敏感级别过滤；
- 撤销、回滚、事实源回查、派生恢复和索引清理；
- Citation v3 绑定 document、version、section、evidence、正文哈希、引文哈希和
  `source_ref_hash`；
- `resolve_verified_citation()` 按当前身份复核 active 状态、权限和内容完整性；
- v1/v2、缺字段、截断、乱序、重复/未知参数和内容篡改失败关闭；
- Agent Loop 工具结果和 Web SSE 能完整传输 v3 URI。

后端协议已经具备，但 Web/Desktop 消息 UI 没有 `knowledge://` 点击解析闭环。

### 5.4 Retrieval 和共享词法索引

已经实现租户化、作用域和敏感度过滤的 FTS5 trigram 检索，并被 Knowledge、Memory 和
Skills 共同使用。历史 Retrieval 报告记录 9/9 门禁通过，但当前比较器指纹已变为
`f9526dd6f001ce460353c610049bd525b3304998d460edee9ea901cea7720321`，与报告记录值不一致，
必须重跑后才能恢复当前证据效力。

共享实现意味着任何对 `agent/retrieval/lexical.py` 的修改都必须同时重跑 Retrieval、
Knowledge、Memory 和 Skills 影响集，不能只验证直接调用方。

### 5.5 Skills 和“外部工作手册”安全子集

已经实现：

- 结构化 Skill 候选、版本、来源、角色分离和审计；
- active-only 租户化词法 Top-K 检索；
- 模型兼容、权限和内容哈希二次复核；
- 影子遥测和基于已完成证据的 inactive 候选提案；
- 候选、拒绝和 superseded 版本不进入生产候选集；
- 生产注入代码路径和独立开关；缺键与默认配置都关闭；
- 客户控制的固定配对套件和签名 Oracle 骨架。

当前实现对应 AutoSkill 的读取侧、EvoSkill 的候选侧和 CoEvoSkills 的验收思想，但仍不是
自动演化闭环。自动 Add/Merge/Discard、训练式 MemSkill Controller 和 SE-Agent 多轨迹
融合尚未实现，也不应在没有真实金标时直接添加。

### 5.6 客户验收框架

`benchmarks/customer/` 已具备客户包、执行器、独立 Judge、事件链、签名和独立验证器的
协议基础。合成测试证明协议能运行和拒绝篡改，不证明真实客户任务效果。

当前正式报告为 `pending_customer_inputs`、`passed=false`，因此客户验收未完成。

### 5.7 本轮可复现测试

2026-07-31 00:45 的早期快照曾记录 `774 passed`，但随后 Knowledge 比较器被重写；该旧结果
不再代表当前源码。2026-07-31 11:19 对当前工作树重新执行：

```powershell
python -m pytest -q tests/test_knowledge_benchmark.py
```

结果：

```text
17 failed, 4 passed in 2.41s
```

失败集中在两类协议漂移：测试仍 mock 已从 `compare.py` 移除的
`LegacyKnowledgeEngine`，且仍断言旧“三轮”协议；当前实现要求八轮独立子进程。随后执行
完整 Python 回归：

```powershell
python -m pytest -q
```

结果：

```text
17 failed, 759 passed, 40 skipped, 1 warning, 12 subtests passed in 76.63s
```

唯一警告来自 `channel/chat_channel.py:40` 的 `setDaemon()` 弃用。由于总退出码为 1，当前
质量门禁明确失败；不能用 759 个通过项抵消 17 个 Knowledge 协议失败。

真实子进程最小复核：

```powershell
python -m benchmarks.knowledge.trial `
  --dataset benchmarks\.cache\cmrc2018-source\data\cmrc2018_dev.json `
  --engine legacy --mode index
python -m benchmarks.knowledge.trial `
  --dataset benchmarks\.cache\cmrc2018-source\data\cmrc2018_dev.json `
  --engine governed --mode index
```

本轮结果：Legacy 为 `latency_ns=366222000`、`latency_ms=366.222`，Governed 为
`latency_ns=345179400`、`latency_ms=345.1794`；两者退出码均为 0，848 文档、文档 ID、
SQLite 和索引数量均通过。Legacy 的 `query_probes_passed=false`，Governed 为 `true`。
父进程调用 `_assert_index_trial()` 后明确拒绝 Legacy：

```text
ValueError: 建库计时后的索引完整性验证失败
```

另一个负例使用哈希错误的 `benchmarks/.cache/cmrc2018_dev.json` 时，数据哈希门禁正确拒绝，
但引擎已提前创建且没有正常关闭，退出阶段出现临时 SQLite 文件 `PermissionError`。这暴露了
失败路径资源释放缺口，也应纳入测试。

## 6. 报告状态与证据效力

目前不存在一组可以整体称为“当前权威”的报告。Knowledge 和 Retrieval 源码已晚于正式
报告；Memory 报告仍与当前 Memory 指纹一致，但只覆盖绝对阈值；客户报告明确未通过。
历史、临时、profile、preflight 和 archive 文件均不得覆盖以下状态。

| 报告 | 文件内结果 | 当前绑定状态 | 当前允许用途 |
|---|---|---|---|
| `benchmarks/results/cmrc2018-comparison.json` | 9/9，`passed=true` | `superseded/unbound`；当前 Retrieval 比较器指纹已变化 | 仅作历史基线，重跑前不得作为当前性能证据 |
| `benchmarks/results/cmrc2018-knowledge-comparison.json` | 53/53，`passed=true` | `superseded/unbound`；旧指纹 `81613d...f55f`，当前为 `620d17...f521` | 仅作历史基线，不能支撑当前 Knowledge 主张 |
| `benchmarks/results/cmrc2018-memory-outbox.json` | 14/14，`passed=true` | 当前 Memory 文件指纹闭合，但无 commit/CI 绑定 | 只证明报告协议覆盖的单机绝对阈值和一致性 |
| `benchmarks/results/customer-skill-acceptance.json` | `pending_customer_inputs`、`passed=false` | 当前失败状态 | 只证明客户输入和验收尚未完成 |

### 6.1 当前实现指纹复算

本轮从当前源码重新计算：

```text
retrieval_engine     = fe6c1d8adfe8b8aa2d38b596ab20b9ec34b6468004eb4502a30c5ae1aeb68dba
retrieval_comparator = f9526dd6f001ce460353c610049bd525b3304998d460edee9ea901cea7720321
knowledge_engine     = feccbaf17067851ecf4faa1288af4ba5225e2e7736a2a976953500f32d69466f
knowledge_comparator = 620d17e2c23fe84d9cbe7a7fef27113f84590d084f00792ad3d85dd4e59ff521
memory               = 317475504225513aacd42c23214862d91a30b9c057f7d5627917c8bf396a767f
```

其中 Knowledge 比较器已经与正式报告不一致；子代理复核还确认 Retrieval 当前比较器也与
正式报告记录值不一致。Memory 指纹与报告一致。所有指纹在源码继续变化后都必须重新复算，
且字节绑定本身不证明评测协议足以支撑性能或产品主张。

### 6.2 Retrieval 指标和限制

本节数值来自已经失配的历史正式报告，只用于定义重跑基线，不代表当前实现结果。

| 指标 | 原版基线 | 当前 Retrieval |
|---|---:|---:|
| Recall@1 | 0.1121 | 0.8984 |
| Recall@5 | 0.1351 | 0.9444 |
| Recall@10 | 0.1417 | 0.9487 |
| MRR@10 | 0.1210 | 0.9184 |
| 空结果率 | 0.7897 | 0.0146 |
| 查询 Mean | 2.4145 ms | 1.2729 ms |
| 查询 P50 | 1.9031 ms | 0.9478 ms |
| 查询 P95 | 4.8161 ms | 3.0854 ms |
| 建库中位数 | 327.1806 ms | 231.3917 ms |

质量差异明确；性能报告每轮只保存聚合 Mean/P50/P95 和建库时间，没有保存所有查询原始
延迟，独立验证器无法重新计算 P95。报告也没有完整锁定新进程、冷/热状态、执行顺序和
环境负载，故性能结论仍是本机观测，不是发布级承诺。

### 6.3 Knowledge 指标和限制

本节数值来自旧 schema 3 报告，只用于说明协议演进原因；当前 schema 4 报告尚未生成。

| 指标 | Legacy | Governed Knowledge |
|---|---:|---:|
| Recall@1 | 0.1121 | 0.8984 |
| Recall@5 | 0.1351 | 0.9444 |
| Recall@10 | 0.1417 | 0.9487 |
| MRR@10 | 0.1210 | 0.9184 |
| 空结果率 | 0.7897 | 0.0146 |
| 查询 Mean | 3.6478 ms | 2.4133 ms |
| 查询 P50 | 2.4533 ms | 1.6318 ms |
| 查询 P95 | 10.2230 ms | 7.4117 ms |
| 建库总体中位数 | 1006.1167 ms | 824.0378 ms |
| Citation 解析准确率 | 0 | 1.0 |
| 来源绑定准确率 | 0 | 1.0 |

旧报告的建库六个配对样本中 Governed 只胜 4 对，三轮只胜 2 轮；单侧精确符号检验
`p=0.34375`。新版比较器已增加八轮查询、十六个 ABBA/BAAB 区组、原始比率统计复算、
Bootstrap 种子记录和 `derivative_failure_observed` 零容忍门禁，但尚未通过测试和真实
Legacy 建库父门禁，因此不能使用“稳定严格超越”措辞。

### 6.4 Memory 指标和限制

Memory 报告覆盖 848 个文档，14/14 通过：

```text
fact write P95                         = 1.691165 ms    (门禁 <= 100 ms)
initial recovery throughput            = 38.407953 docs/s (门禁 >= 20)
revoke recovery throughput             = 90.585946 docs/s (门禁 >= 20)
revoked index/projection pollution     = 0 / 0
pending jobs after recovery            = 0
```

这些门禁是绝对阈值和一致性检查，不含 SmartAssistant 原版 Memory 的同协议对照；报告还是单租户、
顺序场景，不能外推到多进程硬崩溃和并发撤销线性语义。

## 7. 未完成模块、实施方法与验收门禁

### P0-1：建立可复现 Git 与远程 CI 基线

**缺口**

- 父仓库无提交，SmartAssistant 整体未跟踪；
- 报告无法绑定 Git commit；
- `.research`、缓存、数据库、日志、密钥和构建产物尚未形成明确提交边界；
- 有 `.github/workflows/` 配置，但没有当前源码对应的远程运行 ID 和制品。

**实施步骤**

1. 决定 SmartAssistant 使用独立仓库还是父仓库子目录，并冻结上游 ZIP、`LICENSE`、`NOTICE`。
2. 增加精确忽略规则，排除虚拟环境、`node_modules`、`dist`、缓存、数据库、日志和密钥。
3. 创建“上游基线”和“重写增量”可审计提交，保留上游来源。
4. 在远程 CI 执行 Python 全量回归、Desktop build、UTF-8/乱码扫描、许可证打包检查和报告
   独立验证。
5. 报告同时绑定 commit、数据哈希、引擎指纹、比较器指纹和 CI run ID。

**完成门禁**

- 从干净提交可安装、构建、测试并重算相同指纹；
- CI 有不可变运行链接和制品 SHA-256；
- 工作树无无法解释的本地产物。

### P0-2：修正 Knowledge 统计和安全证据协议

**已经完成的协议代码**

- 八轮查询质量试验，每个试验使用独立进程和新数据库；
- 十六个等量 ABBA/BAAB 建库区组，每个区组四个独立子进程；
- Windows CPU 亲和性、进程优先级、电源计划、后台负载和唯一
  `process_instance_id` 记录；
- 每条查询原始延迟、`latency_ns`/`latency_ms` 映射、逐轮 Mean/P50/P95 复算；
- 引擎实现路径和 SHA-256 由父进程基于当前源码重算；
- 查询和建库统计量从原始 ratios 复算，平局计失败，Bootstrap 使用实际种子；
- 二十四个不重复真实文档、八个身份上下文和
  `all_derivative_failure_observed=true` 零容忍门禁；
- 建库后核验 848 文档、SQLite、索引、派生队列和查询探针。

**当前阻塞项**

1. Legacy `validate_index()` 用首、中、尾文档的 `text[:24]` 查询，并要求目标文档进入
   Top-10。这把检索质量能力混入了结构完整性探针；真实子进程能完成建库，但
   `query_probes_passed=false`，导致父门禁必失败。
2. `comparison_paths()` 只有 13 项，至少漏绑
   `benchmarks/retrieval/evaluate.py`、`benchmarks/retrieval/metrics.py`、`common/log.py`；还需
   对 `agent/memory/governance/contracts.py` 等实际运行依赖做闭包审计。
3. 父进程没有从固定 CMRC 2018 官方数据重新计算完整 query ID 顺序和
   `query_selection_sha256` 期望值，只检查 64 位格式和轮次一致性。
4. `tests/test_knowledge_benchmark.py` 仍是旧三轮、进程内 mock 协议，当前 17 个测试失败；
   新 schema 4 协议没有有效 CI 保护。
5. 数据文件在引擎构造后才校验；哈希错误的失败路径会留下未正常关闭的 SQLite 连接，并在
   临时目录清理时出现 `PermissionError`。
6. 现有正式报告是 schema 3、旧实现指纹，必须标记为 `superseded`；schema 4 正式报告尚未
   生成。

**实施步骤**

1. 把索引“结构完整”与“检索质量”分离。Legacy 探针使用预先从该文档选择、且在全库中
   可确定命中的稀有 trigram 或唯一标题 token，只验证索引能查询；质量仍由 3219 个官方问题
   的 Recall/MRR 门禁负责。探针必须对 Legacy 和 Governed 使用同一生成规则。
2. 在创建引擎前验证数据文件；所有构造后异常都通过 `try/finally` 关闭引擎和临时目录。
3. 扩大比较器指纹到直接和必要间接依赖，增加负例：任一依赖字节改变都必须使旧报告失效。
4. 建立数据集契约(Dataset Contract)：从固定文件加载 3219 个 query ID，父进程独立计算
   顺序哈希；换序、缺失、重复、子集替换或伪造 hash 都失败关闭。
5. 重写 `tests/test_knowledge_benchmark.py`，覆盖八轮、十六区组、严格 JSON 子进程、唯一
   process ID、实现 SHA、原始延迟复算、统计字段篡改、选择哈希篡改、Legacy/Governed
   查询探针和失败路径资源释放。
6. 先运行目标测试和完整 Python 回归；全部通过后才执行昂贵的八轮/十六区组正式报告。
7. 对 schema 4 报告运行独立验证器，从原始延迟、区组计时和 ratios 重算所有统计量；最后
   再更新 Evidence Manifest 和本交接文档。

**完成门禁**

- `python -m pytest -q tests/test_knowledge_benchmark.py` 全部通过；
- `python -m pytest -q` 退出码为 0；
- Legacy 和 Governed 的 `index`、`full` 子进程均为单行严格 JSON、stderr 无未解释异常；
- 十六个建库区组和八轮查询全部完成，所有实现/数据/统计/安全/完整性门禁通过；
- 独立验证器可仅凭固定数据和报告原始样本复算结果；
- 新报告绑定当前 commit、完整比较器指纹和 CI run，旧报告明确 `superseded`。

### P0-2B：重建 Retrieval 原始性能证据

**缺口**

- 当前 Retrieval 比较器指纹为
  `f9526dd6f001ce460353c610049bd525b3304998d460edee9ea901cea7720321`，正式报告记录
  `eb7fa53dc95cbe322f794a11b1cbd4603f7b92e92c83490555512c06700d3ada`；报告已经失配；
- 旧报告只保存聚合 Mean/P50/P95，独立验证器不能从逐查询样本复算；
- 新进程、预热、执行顺序、CPU 调度和异常轮处理没有形成与 Knowledge 同等级的协议。

**实施步骤**

1. 每轮保存 `query_id`、`latency_ns`、`latency_ms`、轮次、引擎顺序、
   `process_instance_id` 和调度环境。
2. 父进程从原始样本重算 Mean/P50/P95，绑定固定完整 query ID 顺序和数据契约。
3. 使用独立进程、平衡顺序、等量预热和预注册统计规则；无效轮不得静默丢弃或选择性重跑。
4. 增加丢样本、重复 ID、延迟映射篡改、统计字段篡改、重复进程实例和实现漂移负例。

**完成门禁**

- Retrieval 目标测试和完整回归通过；
- 新正式报告与当前比较器指纹、数据哈希、commit 和 CI run 一致；
- 独立验证器可从逐查询样本复算全部质量和性能门禁。

### P0-3：补齐硬崩溃、跨进程和共享索引边界

**缺口**

- Knowledge 快批路径允许索引提交先于事实事务最终提交，现有测试只模拟普通异常；
- 没有在索引 commit 后、事实 commit 前执行真实 `os._exit` 的恢复测试；
- 共享词法索引缺跨进程 replace、触发器重建中断和跨租户损坏隔离测试；
- Memory 14/14 没有多进程硬崩溃、search/close/revoke 固定竞态协议。

**实施步骤**

1. 用子进程阶段屏障在关键提交点前后执行硬退出并重启恢复。
2. 两进程并发更新同一 document_id，核验冲突、版本和旧 Citation 失效语义。
3. A/B 两租户交错 replace，在触发器重建中硬退出，并注入 B 租户 posting/docsize 损坏。
4. 对 Memory 固定 close、search、revoke 的交错点，明确 `runtime_closed` 或成功的契约。

**完成门禁**

- 重启后 active facts、投影和索引一致；孤立 posting、撤销内容和旧 Citation 不可返回；
- outbox/batch 收敛，SQLite/FTS integrity 通过；
- A/B 租户无串扰，修复 B 不修改 A；
- 无底层 SQLite 异常、死锁或无法解释的丢写。

### P0-4：实现 Evidence Manifest 首版

现有根目录 `audit_cmrc_evidence_manifest.json` 已过期：它只覆盖旧 Retrieval/Knowledge，
仍记录 Knowledge 42 门禁，没有 Memory、客户验收、当前实现绑定和 Git 状态；代码和测试也
没有消费它。

**建议文件**

```text
benchmarks/results/evidence-manifest.json
benchmarks/evidence/manifest.py
tests/test_evidence_manifest.py
```

**首版字段和规则**

- `schema_version`、`generated_at`、`authority_policy=only_listed_reports`；
- 四个权威报告 ID、路径、报告版本、生成时间和原始字节 SHA-256；
- `artifact_status=current|superseded`；
- `binding_status=verified|mismatch|unbound`；
- 报告结果、门禁数量、报告值与当前值双侧实现指纹；
- 数据路径、SHA-256、来源仓库和固定提交；
- Git HEAD、分支、工作树、项目是否受跟踪和 porcelain 哈希；
- `release_binding` 和稳定 `reason_codes`；
- 严格 JSON、重复键拒绝、路径逃逸拒绝、小写 SHA-256 校验；
- 清单自身由外部原始字节 SHA-256 绑定，不做递归自哈希。

**关键语义**

首版必须声明 `semantics=artifact_binding_only`，或者增加独立 `claim_status`。文件和实现指纹
匹配只证明“报告绑定当前代码”，不能证明基准协议足以支持“性能超越”。应先修 P0-2 的
统计/安全门禁，再让 Manifest 把新报告标记为可发布主张，避免制造新的伪证据。

**完成门禁**

- 四份报告全部能被独立重新哈希和绑定；
- 任一缺失、篡改、重复键、实现漂移、Git 未跟踪或报告过期都产生稳定失败原因；
- Manifest 不会把 `binding_status=verified` 等同于 `claim_status=passed`。

### P0-5：完成 Web/Desktop Citation 消费闭环

**当前证据**

- Web `channel/web/static/js/console.js` 的 `bindChatKnowledgeLinks()` 只处理相对 `.md`；
- Desktop `components/Markdown.tsx` 只拦截相对 `.md`；聊天消息没有传入身份化处理器；
- Desktop API 只有 list/read/graph/action/import；
- Web 注册路由同样只有 list/read/graph/action/import；
- Electron `desktop/src/main/index.ts` 对新窗口直接执行 `shell.openExternal(url)`。

**实施步骤**

1. 增加 `POST /api/knowledge/citations/resolve`，URI 来自请求体，tenant/user/session 必须来自
   已认证服务端会话，禁止客户端自报身份。
2. 后端调用 `resolve_verified_citation()`，只返回当前身份可见的引文、来源和显示元数据；
   撤销、篡改、过期和越权统一失败关闭。
3. Web 和 Desktop Markdown 显式识别 `knowledge://`，点击后调用 resolve API，并提供过期后
   重新检索动作。
4. Electron 主进程明确拒绝把 `knowledge://`、本地文件和非白名单 scheme 交给
   `shell.openExternal()`。
5. 增加 API 身份负例、Web 点击测试、Desktop 组件测试和 Electron 外部打开拦截测试。

**完成门禁**

- 合法当前 Citation 可点击显示，撤销/篡改/越权 Citation 不泄露任何正文；
- Web/Desktop 行为一致；
- 历史 v1/v2 明确显示失效并要求重新检索；
- 浏览器与 Electron 端到端测试通过。

### P0-6：建立 SmartAssistant 原版能力矩阵和 Agent Loop 端到端等价证据

**缺口**

Agent Loop 已有工具循环、取消、Steering、Citation 传输和单元回归，但没有覆盖原版所有
模型、通道、工具和异常语义的能力矩阵，也没有真实多模型/多通道端到端对照。

**实施步骤**

1. 冻结 SmartAssistant 原始 ZIP 的入口、配置、模型 Provider、Channel、Tools 和消息语义。
2. 建立 capability ID、原版场景、当前场景、预期事件序列和证据测试的机器可读矩阵。
3. 覆盖普通回答、多轮工具、并行工具、截断参数、取消、Steering、上下文恢复、通道回执、
   大工具结果和 Citation。
4. 至少选择一个真实 Provider 和 Web/Desktop/一个消息通道执行端到端回归；密钥场景在
   受控 CI 中运行并脱敏保存事件。

**完成门禁**

- 原版核心 capability 全部有同协议通过证据；
- 事件顺序、工具副作用、消息历史和失败语义无未解释回归；
- 所有结果绑定同一 commit 和 CI run。

### P1-1：补 Memory 原版同协议对照

**实施步骤**

1. 冻结原版 Memory Storage 适配器和源码指纹。
2. 对原版与 Governed Memory 使用相同文档、进程模型、SQLite 配置、持久化和查询协议。
3. 同时比较正确性、写入、恢复、撤销、查询和资源占用；不能通过关闭事务、`fsync`、权限
   复核或事实源回查换取速度。
4. 保存原始样本并执行预注册配对统计。

**完成门禁**

- 安全和一致性为零容忍；
- 若只达到非劣，表述为“持平”；只有统计和效应门禁同时通过才可表述“严格超越”。

### P1-2：重建 Skills 金标并保持生产开关关闭

**缺口**

当前 GitHub Issue 数据存在来源证明不完整、时间矛盾和标题标签泄漏，只能视为银标候选，
数据门禁必须保持 `blocked_invalid_dataset`、`metrics=null`。

**实施步骤**

1. 重新抓取并固定原始正文、评论、HTTP 状态、响应头、时间和来源 SHA-256。
2. 冻结 Skill 版本和候选集，由至少两名标注者独立盲标，处理分歧并保存一致性指标。
3. 建立无 Skill、全量 active Skill、Top-K Skill 的同模型、同提示词、同工具配对评测。
4. 先运行影子检索，观察权限错误、哈希漂移、候选覆盖、延迟和任务回归。
5. 通过客户验收后才申请小流量灰度；保持
   `skill_shadow_retrieval_enabled=false` 和
   `skill_retrieval_injection_enabled=false` 作为默认值。

**完成门禁**

- 数据来源可复核、无标签泄漏、标注一致性达标；
- 任务成功率提升的置信区间下界大于零，关键任务零回归；
- 生产灰度可观测、可立即关闭、可回滚。

### P1-3：外部“工作手册”能力的后续添加

附件方向可以添加，但必须分阶段：

1. **Knowledge -> inactive Skill 窄桥接**：只接受 `SHARED` 且敏感度为 `PUBLIC` 或
   `INTERNAL` 的 active Citation；调用身份同时具备 Knowledge 读取和 `skill:propose`；
   提案保留 URI、字节范围、正文哈希和引文哈希。
2. **发布时二次来源复核**：解决 Knowledge 与 Skill 两个 SQLite 事实库之间无法原子提交的
   撤销竞态；来源失效时禁止发布并撤销相关候选。
3. **Add/Merge/Discard**：在真实金标上验证去重、冲突和失效策略后，才实现自动建议；
   第一阶段仍需人工批准。
4. **CoEvoSkills**：为 Skill 和测试共同版本化，先用隔离验证器，再用真实 Agent Oracle。
5. **MemSkill/SE-Agent**：只有固定长对话场景、奖励定义、多轨迹副作用隔离和消融实验通过后
   才进入实验分支，不直接进入生产。

### P1-4：执行真实客户验收

当前缺少 11 项机器可读输入：

```text
executor_id
executor_json
executor_version
manifest_sha256
package_root
run_root
skill_content_sha256
skill_id
skill_version
skills_db
tenant_id
```

**实施步骤**

1. 与客户冻结任务、预期结果、禁止行为、延迟、成本和安全判据。
2. 生成只读客户包和逐文件 SHA-256，固定模型、工具、执行器、Judge、Skill 和环境。
3. 在隔离 `run_root` 执行无 Skill、固定 Skill、候选能力的盲化配对测试。
4. 由独立 Judge 或确定性 Oracle 评分，被测 Agent 不得自评。
5. 保存完整事件、签名、环境、commit、CI run 和客户签署结论。

**完成门禁**

- 11 项输入完整、哈希可复算；
- 真实客户任务而非合成夹具；
- 独立验证器通过，最终报告 `passed=true`，客户签署结论存在。

### P2：待产品决策的 SAS 能力

以下能力尚未完成迁移，且不应为了“模块数量”直接加入：

| 能力 | 当前状态 | 启动条件 |
|---|---|---|
| 企业身份提供方(IdP)和组织角色同步 | 未接入 | 明确目标客户身份系统和越权场景 |
| Knowledge 审查、批量审批和晋升 | 未形成产品闭环 | 明确角色、工作流和审计要求 |
| 实体去重、合并和知识图谱治理 | 未迁移 | 有人工标注数据和消融指标 |
| 规则抽取、推理和重验证 | 未迁移 | 有非合成数据、错误传播和撤销测试 |
| 程序性记忆 | 未迁移 | 明确调用场景、生命周期和客户指标 |
| 静态加密和密钥管理 | 未完成设计 | 完成密钥生命周期和恢复威胁模型 |

这些项目当前属于产品决策池，不属于已承诺交付范围。若决定启动，统一执行：先冻结客户需求
和威胁模型，再设计数据迁移与回滚，使用真实授权数据建立基线，完成权限/撤销/跨租户/失败
恢复负例，最后才接入 Agent Loop 和客户端。没有需求签署、真实数据和独立验收指标时，状态
保持“未启动”，不把代码数量作为完成标准。

## 8. 修复影响分析(Fix Impact Analysis)

核心共享调用链：

```text
TenantAwareLexicalIndex
  -> Retrieval Benchmark
  -> Knowledge Runtime -> knowledge_search / knowledge_get / Citation v3
  -> Memory Manager -> 投影 / 恢复 / 检索
  -> Skills Retrieval -> 影子 Top-K / 生产候选注入
```

后续任何修改至少执行以下影响检查：

1. 调用链：列出直接调用方、间接调用方、配置开关和失败路径。
2. 数据结构：检查 SQLite schema、报告 schema、Citation URI 和工具结果兼容性。
3. 并发：检查锁顺序、事务边界、跨进程竞态和硬崩溃恢复。
4. 安全：检查租户、用户、会话、scope、sensitivity、撤销和来源篡改。
5. 性能：在功能/安全门禁通过后运行真实数据基准，保存原始样本，不以微基准替代产品协议。
6. 消费方：Knowledge 变更必须覆盖 Agent Loop、Web SSE、Web UI 和 Desktop UI；Skills
   变更必须覆盖影子、生产、演化和客户验收路径。

## 9. 推荐执行顺序

```text
1. 修复 Legacy 结构查询探针和数据校验失败路径资源释放
2. 补全 Knowledge 比较器依赖指纹和父进程查询选择契约
3. 重写 Knowledge benchmark 测试到八轮/十六区组/子进程协议
4. 运行 Knowledge 目标测试和完整 Python 回归，必须全部通过
5. 建立受跟踪 Git 上游基线、自有增量和远程 CI
6. 执行正式 Knowledge schema 4 报告并由独立验证器复算
7. 重建 Retrieval 原始延迟协议，补 Memory 原版对照，重跑报告
8. 生成 semantics=artifact_binding_only 的 Evidence Manifest
9. 完成 Web/Desktop Citation resolve 和点击闭环
10. 建立 SmartAssistant 原版能力矩阵及多通道 Agent Loop 端到端回归
11. 重建真实 Skills 金标，保持生产 Top-K 默认关闭
12. 收齐客户输入并执行签名双臂验收
13. 仅在独立验证和客户签署后宣布项目完成
```

步骤 1-4 是当前节点的串行关键路径；Git/CI 边界、Citation 客户端设计和能力矩阵盘点可以
并行。为了避免 Manifest 固化错误主张，P0-2 应先于“可发布 claim”版本的 Manifest；
Manifest 的代码骨架可以并行开发，但在新统计和安全报告完成前只能表达制品绑定状态。

## 10. 当前禁止使用的对外表述

在上述门禁完成前，不得使用：

- “项目已完成”或“已通过最终验收”；
- “Knowledge 建库稳定严格快于原版”；
- “Memory 已与原版持平/超越”；
- “整个 Harness 已保持 SmartAssistant 全部能力”；
- “Skills 已提升真实任务成功率”或“已实现自动自进化”；
- “Citation 已完成 Web/Desktop 产品闭环”；
- “完全原创、无上游来源”或任何刻意隐瞒 MIT 上游的表述。

当前可使用的准确表述是：

> 当前重写版在完整 CMRC 2018 数据上取得明确的检索质量提升，并已实现本地受治理
> Memory、Knowledge、Citation v3 和 Skills 治理基础。当前 Knowledge 证据协议正在升级，
> 完整 Python 回归尚有 17 个相关测试失败；性能统计、客户端 Citation 闭环、提交级复现、
> 真实技能金标和客户验收仍在完成中。

## 11. 关键文件索引

| 主题 | 路径 |
|---|---|
| Agent Loop | `agent/protocol/agent_stream.py` |
| 共享检索 | `agent/retrieval/lexical.py` |
| Memory | `agent/memory/governance/`、`agent/memory/manager.py` |
| Knowledge | `agent/knowledge/`、`agent/tools/knowledge/knowledge_tools.py` |
| Skills | `agent/skills/`、`agent/evolution/` |
| Retrieval 基准 | `benchmarks/retrieval/` |
| Knowledge 基准 | `benchmarks/knowledge/` |
| Memory 基准 | `benchmarks/memory/outbox.py` |
| 客户验收 | `benchmarks/customer/` |
| Web | `channel/web/web_channel.py`、`channel/web/static/js/console.js` |
| Desktop | `desktop/src/main/`、`desktop/src/renderer/src/` |
| SAS 能力追踪 | `docs/rewrite/sas-capability-traceability.md` |
| Skills 功能评估 | `docs/rewrite/external-skill-evolution-assessment.md` |
| Skills 数据审计 | `docs/rewrite/skill-selection-dataset-audit.md` |
| 客户验收说明 | `docs/rewrite/customer-controlled-skill-acceptance.md` |

## 12. 文档自身的证据边界

本文是工程交接和完成边界说明，不是性能报告、客户签署或许可证法律意见。所有数值应以
其注明来源为准：历史指标只能从第 6 节旧报告复算，当前测试和子进程状态以第 5.7 节命令
输出为准。由于当前尚无 Git HEAD，本文不能声称“当前提交”绑定；只能临时绑定文件字节和
执行时点。当源码、数据、环境、比较器或报告发生变化时，必须重新计算指纹、重跑门禁并
更新本文。任何无法由日志、固定数据、当前源码或独立验证器复现的结论，按项目规则视为
未完成。
