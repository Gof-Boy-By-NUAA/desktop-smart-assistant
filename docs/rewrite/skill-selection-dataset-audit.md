# GitHub Issue 技能选择数据集审计

## 结论

`benchmarks/skills/github_issue_skill_selection.json` 当前不能作为技能选择的金标数据集，
也不能作为生产启用门禁。当前文件只能被视为一个尚未完成来源固定的银标候选清单，且在
进入任何指标计算前必须被严格加载器拒绝。

拒绝原因不是单一缺陷，而是四个相互独立的证据问题：

1. 标签由标题关键词直接生成，同一规则可 30/30 精确复现预期标签；
2. 数据没有保存 Issue 正文、评论、REST `Date`、`ETag` 或原始响应，无法独立复核来源；
3. 声明快照时间早于 #2994 的 `updated_at`，违反时间单调性；
4. 数据混入 Pull Request、旧版架构任务、产品开发诉求和多意图任务，不能等同于
   “用户任务 -> 当前正确技能”。

本审计不修改核心代码，也不修改该数据文件。

## 审计对象与指纹

当前数据文件：

```text
path=benchmarks/skills/github_issue_skill_selection.json
size=13528 bytes
sha256=ed5587e918854a32e8ee143550f2ef88e841bf311a3efbc3eb43c756edf889d8
file_created_utc=2026-07-29T15:34:42.2244269Z
file_last_write_utc=2026-07-29T15:34:42.2244269Z
declared_snapshot=2026-07-29T00:00:00Z
```

本地父仓库没有提交历史，因此本地实现不能用 Git 提交绑定：

```text
## No commits yet on master
```

原始 SmartAssistant ZIP 可复核指纹为：

```text
f121b65e04e0cf7b008e6404f7a9e3ce11cee8457ed58252b9a57b20366bcf58
```

只读 GitHub REST 查询得到的候选远端冻结点为：

```text
zhayujie/SmartAssistant master
commit=39d412cd2b56cbe97610c2804eec338609b4966a
tree=77f8df9adf664fc2d1d5d0297c31da9794b67616
committed_at=2026-07-29T07:12:52Z

zhayujie/cow-skill-hub main
commit=0c214c3a61f66f8c122111c23270bd146241001b
tree=e17c1e1686bca81f2967eb8dafe695e211ebc592
committed_at=2026-07-06T03:31:40Z
```

这些远端提交只能绑定代码和技能目录，不能绑定可变的 GitHub Issue。Issue 必须另行保存
原始 REST 响应、响应头和内容哈希。

当前本地技能目录只有 3 个 `SKILL.md`，`workspace/skills` 不存在：

```text
image-generation  d55b49362a7448a5b20c7e410d4b781c8a4426014735fdf1b88919456fcb19c1
knowledge-wiki    0af1907ca109f1d0913e120a0adabf15375a8265f48fe6e32cc8bf80c6b2aa85
skill-creator     cda9c62adf48a072a572d4907bf9ff9db14f40118cbb6cb7deed9ed4d8db0176
```

其中 `knowledge-wiki` 与原始 ZIP 中的文件不一致；原始 ZIP 版本为
`d9c15c6b0f04b08e54141392b4e2db33f9084cf2176f2eeb3054bca61f86a7e5`。
因此评测必须绑定当前技能内容 SHA-256，不能只写技能名称。

## 可复核问题

### 标签泄漏

数据文件声明使用以下标题标记生成确定性银标：

```text
knowledge-wiki: 知识库, rag, 语料库, 嵌入式搜索, 引用, knowledge
image-generation: 图生图, 绘图, 画图, 图片模型, dall-e, midjourney,
                  p图, 绘画, 图片生成, 文生图, image
```

按文件声明的 NFKC 和大小写归一化规则重放后：

```text
cases=30
positive_label_assignments=21
positive_assignments_with_title_marker=21
marker_rule_exact_cases=30
marker_rule_exact_rate=1.0
```

这说明该数据集衡量的是“实现是否复现数据生成规则”，而不是语义技能选择。任何基于它得到
的 100% 准确率都不能证明检索器理解了任务。

### 时间不一致

当前文件中的 #2994 实际字段为：

```text
created_at=2026-07-28T20:08:29Z
updated_at=2026-07-29T01:50:59Z
snapshot_generated_at=2026-07-29T00:00:00Z
```

`created_at` 没有晚于文件时间，但 `updated_at` 晚于声明快照 1 小时 50 分 59 秒。
对全部 30 条记录执行相同检查，#2994 是唯一直接违反声明快照单调性的条目：

```text
case_count=30
updated_or_created_after_declared_snapshot=1
violating_issue=2994
```

不能把 `snapshot_generated_at` 猜测性改成文件写入时间。必须重新抓取来源并保存服务端响应
证据。由于当前文件没有保存 REST `Date`、`ETag`、`Last-Modified` 或原始响应，30/30 条
记录都缺少独立来源可验证性；#2994 另外还有文件内部的时间矛盾。

### 来源获取边界

只读获取过程观察到：

```text
gh auth status -> exit 1，已配置 GitHub token 无效
匿名 REST /rate_limit -> 初始 core remaining=60
选定 Issue/评论请求 -> 返回可解析 JSON，命令 exit 0
匿名额度耗尽后 GET /issues/2729 -> HTTP 403
```

本轮没有落盘任何 REST 响应头，因此不能事后补造 `Date` 或 `ETag`。正式抓取任务必须在
同一次响应中保存状态码、响应头和原始正文；HTTP 非 200 或条件请求链不完整时失败关闭。

### 浏览器独立抽样复核

浏览器对公开 GitHub HTML 页面进行了独立只读抽样。远端提交页面返回并显示：

```text
commit=39d412cd2b56cbe97610c2804eec338609b4966a
displayed_short_commit=39d412c
title=feat: supplement interface call parameters
```

这与 REST 查询得到的 SmartAssistant 远端提交 SHA 一致，只能印证该远端提交页面当时存在及其
显示标题，不能证明当前本地无提交工作树等同于该提交。

Issue HTML 抽样结果为：

| Issue | HTTP | Date | GitHub request id | HTML 字节数 | 临时计算的 HTML SHA-256 |
|---|---:|---|---|---:|---|
| #2433 | 200 | `Wed, 29 Jul 2026 15:47:12 GMT` | `3F7E:2B0DE2:2BF799:34986E:6A6A207B` | 276030 | `b456334e0494942f64b88e44156398e3eabad27e495601fded0c1dec3dd8f7b7` |
| #2729 | 200 | `Wed, 29 Jul 2026 15:48:01 GMT` | `D22E:19DD90:2B0F5C:33B58C:6A6A20B1` | 263749 | `c8838c04db494b7fdf955275a1942ad7b2518a345d4b0ac76f3133e4e28dfb0e` |
| #2994 | 200 | `Wed, 29 Jul 2026 15:48:15 GMT` | `D22E:19DD90:2B1948:33C1AC:6A6A20B2` | 265675 | `4c38016577bdcd2afa20743909a63e583011d65cf6663f20d401a14be7f278ea` |

页面标题独立印证 #2433、#2729 属于图片生成方向的正候选，#2994 属于 QQ channel
方向的 `none` 候选。这仍不是金标判定：标题不能替代正文、评论、当前技能契约和双人盲标。

本轮没有保存上述原始 HTML 字节，因此这些 SHA-256 无法从审计制品重新计算。它们只能
证明抽样页面在对应服务端 `Date` 时返回 HTTP 200，以及页面内容方向与候选清单一致；
不能修复以下缺口：

- 其余 27 条没有等价 HTML 抽样；
- 30 条 REST 响应仍然没有原始正文、`Date`、`ETag` 和响应哈希；
- #2994 的 HTML 响应发生在 `15:48:15Z`，不能倒推它存在于文件声明的 `00:00:00Z`
  快照，更不能修复其 `updated_at` 晚于声明快照的问题；
- GitHub HTML 会变化，request id 和未保存的内容哈希不能替代可重放的原始快照。

因此浏览器抽样不会改变本审计的失败关闭结论，也不会使当前数据集获得质量门禁或生产
启用资格。

### 数据组成

```text
total=30
design=6
evaluation=24
pull_requests=1
created_before_2026=16
none=10
knowledge-wiki=10
image-generation=11
multi_label=1
```

16/30 样本创建于 SmartAssistant 2.0 重写之前。旧插件、旧 LinkAI 和旧模型适配问题不能在没有
版本迁移标注的情况下用于评价当前技能目录。

## 逐项处置

以下处置针对当前文件中的预期标签。由于所有来源响应均未固定，当前文件整体仍应拒绝；
“仅作银标”表示完成重新抓取后最多可以进入非生产烟雾测试。

### 从当前相关性评测排除

```text
2812  UI 文档管理开发诉求；不能按“知识库”关键词标 knowledge-wiki
1177  LangChain 本地知识库集成开发；旧版架构
2881  自定义厂商图生图配置通道；产品配置和技能使用混合意图
1848  旧 Azure DALL-E 适配；当前技能契约未绑定该后端
2383  按群切换 API key、模型和知识库；不是 knowledge-wiki 操作
2179  LinkAI 智能体、插件和工作流集成；不是单一知识技能任务
2256  RAG、文生图、语音三意图；当前标签遗漏语音能力
830   开源中文语料库收集建议；不是个人知识 Wiki 操作
1233  用嵌入式搜索增强机器人；属于架构开发
1138  消息“引用回复”；“引用”标记与知识引用发生词义碰撞
2786  Pull Request，不是 Issue 或用户任务，且标题直接包含 RAG/knowledge
1276  旧插件中画图和 `$tool` 同时报错；复合任务且版本不匹配
```

#2812 和 #2881 可在保存正文后作为“包含目标关键词但不应选择该技能”的困难负例候选，
但必须由独立标注者重新判定，不能直接把当前正标签改成 `none`。

### 重新抓取后最多保留为银标

```text
1181, 2729,
2621, 2626, 1496, 1766, 2433, 899,
2994, 2993, 2907, 2839, 2847, 2858, 2890, 2938, 2916, 2976
```

其中：

- #2729 的正文询问绘图模型支持情况，维护者评论明确指向内置
  `image-generation`；
- #2433 的正文包含真实图片生成失败和 HTTP 401 日志，评论确认 API key 问题；当前
  `image-generation` 也有配置和错误处理规则；
- 上述评论只能作为标注证据，不能进入模型输入，因为评论直接出现技能名或解决方案；
- #2729 和 #2433 在重新固定正文/评论、绑定当前技能版本并完成双人盲标后，可以申请晋升
  为金标；当前文件中的标题记录本身仍不是金标；
- #2621、#2626 等条目尚未逐条固定正文和评论，不能仅凭标题晋升；
- 10 个 `none` 条目只能证明对这些显式不相关标题的拒绝能力，不能代表真实线上无技能
  分布。

本轮没有发现可由公开 SmartAssistant Issues 独立支持的当前 `knowledge-wiki` 正金标。
知识类正样本必须来自经授权的真实客户任务或用户会话，不能通过改写 #2812 生成。

## 正式快照规范

每个来源记录至少保存：

```text
repository_node_id
issue_number
issue_node_id
api_version
request_url
http_status
response_date
etag
last_modified（如存在）
fetched_at
title
body
comments（按 comment id 排序）
created_at
updated_at
state / state_reason
raw_response_sha256
canonical_response_sha256
normalizer_version
normalized_task_sha256
```

同时保存技能目录：

```text
repository full 40-character commit
tree SHA
skill path
Git blob id
SKILL.md SHA-256
skill id / version / governed content hash
```

快照门禁：

1. `http_status == 200`；若使用 304，必须提供已固定的前序 200 响应和相同 ETag；
2. `response_date <= fetched_at`，并记录允许的时钟偏差；
3. `created_at <= updated_at <= response_date`；
4. 数据集 `snapshot_generated_at` 不早于任一来源 `response_date`；
5. 原始响应、规范 JSON、任务输入和最终数据集分别计算 SHA-256；
6. 任一响应头、正文、评论、技能内容或标注变化后，整体数据集 SHA-256 和分割 SHA-256
   必须重算，旧报告失效；
7. Issue 正文和评论的再分发许可必须经法务或数据治理确认；“public GitHub issue metadata”
   不是许可证名称。

模型输入建议仅使用经过确定性模板清理和隐私脱敏的“标题 + 原始正文”。评论、GitHub
labels、关闭原因、Issue 编号、URL、关联 PR 和变更路径只供标注者使用，禁止进入检索输入。
原始正文和清理后正文必须分别保留哈希和转换器版本。

## 独立标注规范

金标不得由检索器、关键词规则、同一嵌入模型或候选系统输出生成。建议流程：

1. 先冻结任务文本和技能目录；
2. 两名不了解候选系统输出的标注者独立阅读任务和固定技能手册；
3. 标注 `required_skill_ids`、`acceptable_skill_ids`、`forbidden_skill_ids`、`none`、
   `critical` 和判定依据；
4. 不一致样本交由第三人裁决；
5. 记录标注者版本、手册版本、时间、签名和一致性指标；
6. 以来源线程和时间分组切分，避免同一 Issue、评论或关联 PR 跨训练集和测试集；
7. 明确区分直接出现技能词的显式意图和没有技能词的隐式意图；最终结论必须分别报告。

## 持续集成门禁

银标 Issue 集只能承担非生产回归，建议持续集成(CI)按以下顺序失败关闭：

### 数据完整性

- 严格 UTF-8、严格 JSON、无重复键、无重复 Issue/node id；
- 来源响应、数据集、分割和技能目录哈希 100% 匹配；
- 全部时间满足正式快照规范；
- Issue 集禁止 `pull_request=true`；
- design/evaluation 无线程、文本近重复或关联 PR 泄漏；
- 金标分区拒绝 `label_tier=deterministic_silver`；
- 标注签名和双人独立性检查通过。

### 功能回归

- 固定同一技能目录、模型、归一化器、Top-K 和硬件；
- 与冻结基线交错运行，报告 Top-1、Recall@K、MRR、包含 `none` 的宏平均 F1 和无技能
  误选率；
- 候选在任一主要指标上不得低于冻结基线；
- active 状态、租户、技能内容哈希和模型兼容性错误必须为 0；
- 检索异常、空索引错误和证据导出错误必须为 0；
- 报告冷启动与热运行 P50/P95/P99，热运行 P95 建议不超过基线的 1.05 倍。

这些 CI 指标只能发现实现回归。即使全部通过，也不能将银标结果解释为语义质量提升。

## 生产启用门禁

把影子候选注入提示词或自动选择技能前，还需要独立的生产门禁：

1. 使用客户授权、当前分布的真实任务金标，覆盖每个目标技能、无技能、禁止技能、隐式
   意图、多语言和多轮任务；样本量由预注册的最小可检测效果和统计功效确定；
2. 同一模型、提示词、工具、端点和参数下比较原版全量技能注入、影子检索和候选注入；
3. 主要质量指标和端到端任务成功率的配对差值，其 95% 置信区间下界必须大于 0；
4. `none` 误选率和关键回归不得高于基线，关键禁止技能、跨租户、撤销技能和哈希错配
   事件必须为 0；
5. 端到端 P95 延迟、总令牌和成本必须满足预注册的非劣界；建议延迟比 95% 置信区间
   上界不超过 1.05；
6. 影子期安全事件按“零事件规则”计算置信上界，观察量由可接受风险反推，不能任意指定
   一个小样本数；
7. 通过客户控制的执行器、Judge、签名证据和完整日志验收后，才允许小流量灰度；任一门禁
   失败自动回退到 SmartAssistant 原有全量技能提示行为。

## 该数据集不能证明的指标

当前 GitHub Issue 标题银标不能证明：

- `knowledge-wiki` 的真实召回率或任务成功率；
- 隐式意图、改写、口语、多轮上下文或客户行业任务的泛化能力；
- 技能注入后的回答正确性、工具执行成功率或端到端成功率；
- 生产提示词令牌、模型延迟、工具延迟、成本或吞吐；
- 禁止技能、撤销技能、跨租户、模型不兼容和提示词注入的安全性；
- 自动 Add/Merge/Discard、训练控制器或技能进化的有效性；
- 相对 SmartAssistant 原版全量技能注入的整体性能提升；
- 目标客户验收通过。

CMRC 2018 的真实问题和文档标签也不能补足上述缺口，因为它标注的是文档相关性，不是
“任务 -> 正确技能”。

## 只读复核命令

```powershell
git status --short --branch
Get-FileHash benchmarks\skills\github_issue_skill_selection.json -Algorithm SHA256
Get-FileHash ..\SmartAssistant-master.zip -Algorithm SHA256

$d = Get-Content benchmarks\skills\github_issue_skill_selection.json -Raw -Encoding UTF8 |
  ConvertFrom-Json
$all = @($d.design_cases) + @($d.evaluation_cases)
$snapshot = [datetimeoffset]::Parse($d.snapshot_generated_at)
$all | Where-Object {
  [datetimeoffset]::Parse($_.created_at) -gt $snapshot -or
  [datetimeoffset]::Parse($_.updated_at) -gt $snapshot
} | Select-Object number, created_at, updated_at

gh auth status
curl.exe -sS -o NUL -w "%{http_code}" `
  -H "Accept: application/vnd.github+json" `
  -H "X-GitHub-Api-Version: 2022-11-28" `
  "https://api.github.com/repos/zhayujie/SmartAssistant/issues/2729"
```

最后一条命令在匿名额度耗尽后的实测状态为 `403`。正式快照任务不得把该响应当作数据，
也不得在缺少成功响应头时沿用旧标题。
