# SmartAssistant AgentBridge / Web 执行账本严格多角色审查补充（2026-08-03）

## 范围、阶段与证据边界

- **阶段**：F1-002；五个角色均启用，尚未进入 F1-003。
- **范围**：认证 Web Agent 请求的 durable execution claim、SSE 终态围栏、重连恢复、浏览器/桌面端幂等键，以及相应正式边界攻击。
- **本地证据**：
  - `tests/test_durable_web_execution_ledger.py` 含 22 个账本攻击测试。
  - 本轮定向回归为 `86 passed in 15.48s`。
  - `benchmarks/results/web-boundary-security.json` 与其 verification 已按当前源码生成，均为 `passed=true`；限制条件仍是 local-only、非生产、非客户执行、非受保护 CI。
  - `benchmarks/evidence/release_manifest.py` 当前退出 1、manifest 为 `passed=false`；这是 fail-closed 结果。
  - `benchmarks/evidence/fde_case.py` 现可验证外部签名、外部路径且由外部 SHA pin 的 FDE case；当前未配置任何外部输入，故 `FDE_CASE_EVIDENCE=ABSENT`。
- **不得外推**：以上不能证明外部工具 exactly-once、active-active 可用性、生产部署或客户验收。

## 第一轮独立发现

各节按角色责任边界独立记录；每条包含严重度、引用、问题和可执行修正建议。

### FDE 产品交付官

| ID / 严重度 | 文件或设计引用 | 问题描述 | 建议修改方案 |
| --- | --- | --- | --- |
| FDE-EB-01 / P0 | `channel/web/web_channel.py:76-125,2636-2744`；`console.js:3049-3091`；`chatStore.ts:566-588` | `in_doubt` 不再伪造成功，但员工没有 owner-bound 状态/结果查询和受控处置，重新发送可能产生新业务操作。 | 交付只读 run-status/result、双人确认的人工处置和审计记录；未知状态禁止自动重发。 |
| FDE-EB-02 / P0 | `web_channel.py:2636-2744`；`sse_persistence.py:488-529` | 路由到没有 live queue 的实例会把合法运行请求围栏为 `in_doubt`；安全失败不等于客户可用。 | 发布前验证单活粘性路由，或实现共享 queue、lease、handoff 与结果查询。 |
| FDE-EB-03 / P0 | `benchmarks/results/customer-skill-acceptance.json`；`benchmarks/evidence/fde_case.py`；实际 Desktop/Web 客户环境 | 外部签名 FDE case 的解析/commit/tree binding 已具备，但当前没有客户在签名制品上完成登录、发送/重试、附件、引用、取消、重连、重启和结果核验。 | 客户在受保护 runner 提供外部 case、信任根 pin、环境、期望/实际结果、artifact SHA、日志和签署；UI 只能展示/校验。 |
| FDE-EB-04 / P1 | `web_channel.py:1690-1848`；`console.js:2747-2776` | `error`、`cancelled`、`in_doubt`、网络及鉴权失败没有统一恢复操作，用户可把“新请求”误认为“安全重试”。 | 设计稳定错误码和 UI 操作矩阵：重连、查询、取消、联系管理员、显式新建。 |

### 执行可靠性架构师

| ID / 严重度 | 文件或设计引用 | 问题描述 | 建议修改方案 |
| --- | --- | --- | --- |
| REL-EB-01 / P0 | `sse_persistence.py:284-486`；`agent_bridge.py:644-875` | SQLite claim 只围栏本地请求；工具副作用、ledger 和 conversation persistence 不是一个原子事务。工具后崩溃只能为 `in_doubt`，不能证明 exactly-once。 | 每个可变外部通道使用 outbox、下游幂等键和送达查询；否则合同明确 at-most-once + 人工处置。 |
| REL-EB-02 / P0 | `web_channel.py:2636-2744`；`sse_persistence.py:488-529` | 跨实例没有共享 worker 所有权、租约续期或原子 handoff；旧 worker 迟到完成时控制面不能安全接管。 | 引入共享 worker registry、fenced epoch、心跳和持久化 queue；攻击双恢复者、时钟偏差和迟到完成。 |
| REL-EB-03 / P1 | `agent_bridge.py:671-875`；conversation store 写入路径 | pre-persisted user message、`clear_history` 和 assistant persistence 跨多个存储动作，中断可造成历史与账本不一致。 | 定义阶段状态、和解任务和审计；为 COMMIT、rename、fsync、kill 边界注入故障。 |
| REL-EB-04 / P1 | `sse_persistence.py:65-158,557-680` | additive migration 只证明旧 SQL projection 可读；旧二进制不能理解执行围栏。`in_doubt` 永不 reap，缺少容量与处置控制。 | 禁止新旧执行二进制混跑；真实升级/回滚演练；建立 `in_doubt` 保留、阈值、告警、导出和管理员处理。 |

### 治理与红队审计官

| ID / 严重度 | 文件或设计引用 | 问题描述 | 建议修改方案 |
| --- | --- | --- | --- |
| GOV-EB-01 / P0 | `benchmarks/security/web_boundary.py:32-91,994-1175`；`verify.py` | 被测代码、攻击、验证器和报告在同一工作树；同一 PR 可修改它们后自签 PASS。 | 外部受保护 runner 固定 verifier digest/公钥，验证 checkout、命令、报告 SHA、制品 SHA 与签名。 |
| GOV-EB-02 / P0 | `release_manifest.py`；`git remote -v` 无输出；当前 manifest 无 protected remote evidence | 无 remote、branch protection、required checks、签名制品或外部 attestation；本地 Git 不构成发布门禁。 | 组织控制远程仓库配置保护和 required checks，签名 commit/tag/artifact，由独立身份写入证据。 |
| GOV-EB-03 / P1 | `sse_persistence.py:177-189,284-426`；`web_channel.py:2429-2609` | 幂等键来自客户端；若代理身份映射或日志脱敏回归，键可用于跨请求关联或拒绝服务。 | 在真实代理/多租户环境攻击身份边界；限制键的保留/脱敏，增加 owner-only 冲突速率限制与监控。 |
| GOV-EB-04 / P1 | Sol Advisor `install-agents.sh:149` | 独立 Sol 审查预检失败：`dirname: command not found`、`cd: null directory`；普通 worker 不能替代该独立结论。 | 修复工具链并记录 read-only 独立审查输入、输出、身份和版本；此前保持 `EXTERNAL_VERIFIER_ATTESTATION=ABSENT`。 |

### 测试破坏官

| ID / 严重度 | 文件或设计引用 | 问题描述 | 建议修改方案 |
| --- | --- | --- | --- |
| TST-EB-01 / P0 | `tests/test_durable_web_execution_ledger.py`；`agent_bridge.py:644-875` | 覆盖 Python 层竞争、伪 lease、SystemExit、空回复和旧 `done`，但未在真实 tool、fsync、COMMIT 后网络断开、进程 kill 和代理重试的每个指令边界注入故障。 | 建立进程级 kill/fault harness，记录副作用、ledger、UI、重试和历史一致性。 |
| TST-EB-02 / P1 | `web_boundary.py:994-1175`；`test_durable_web_execution_ledger.py:1-776` | 当前为手写样例，未 property-based 生成 owner/session/key/digest/attachment/is_voice/cancel/reconnect 的乱序组合。 | 用 Hypothesis 或等价生成器验证：跨 owner 零副作用、同语义单 worker、变异拒绝、未知状态无 `done`。 |
| TST-EB-03 / P1 | `web_channel.py:1690-1848,2636-2744` | 本地 `_SSEEventJournal` harness 不覆盖实际 WSGI、反向代理、EventSource、负载均衡、HTTP buffering 与断线回放。 | 在隔离部署中进行浏览器驱动的乱序、网络分区和重连攻击，留存代理日志与用户可见结果。 |
| TST-EB-04 / P1 | `release_manifest.py`；hard-denial 输出 | 未暴力证明硬否认字段不能被同 PR 文档/JSON/生成器覆盖，也未攻击旧报告复制、时钟回拨、路径替换。 | 加入“仅外部签名输入可转正”的策略测试，并生成式攻击时间、指纹、路径和回滚输入。 |

### 运维与部署审计官

| ID / 严重度 | 文件或设计引用 | 问题描述 | 建议修改方案 |
| --- | --- | --- | --- |
| OPS-EB-01 / P0 | Docker 输出 `failed to connect to docker API ... docker_engine`；`Dockerfile` | Docker daemon 不可用；没有 build、运行、健康检查、镜像 digest、SBOM 或签名制品证据。 | 在受控 CI/部署环境执行固定 build、签名、installer smoke 和制品 digest 绑定。 |
| OPS-EB-02 / P0 | `sse_persistence.py:65-158`；SQLite fixture migration | 只做开发机 additive migration，未证实“旧签名包→新版本→真实流量→旧包回滚”。 | 在带数据卷的生产等价环境演练升级/回滚，保存 DB hash、日志和操作记录。 |
| OPS-EB-03 / P0 | `web_channel.py:2636-2744`；监控/告警证据缺失 | 没有 72h soak、WAL/`in_doubt` 增长、内存/FD、跨实例路由和告警真实触发证据。 | 目标拓扑压测/长稳 72h，定义 SLO、阈值、告警路由并至少实触发一次。 |
| OPS-EB-04 / P1 | `config.py`；生产变量/密钥证据缺失 | 开发机配置不证明 TLS、trusted headers、密钥轮换、DB ACL、文件权限和日志脱敏符合合同。 | 提供受控环境只读配置证明、密钥来源/轮换、网络策略与最小权限检查。 |

## 第二轮交叉反驳记录与唯一裁决

| 争议点 | 交叉攻击与反驳证据 | 唯一推荐方案 |
| --- | --- | --- |
| A. 唯一索引是否等于 exactly-once | 可靠性指出 claim 不能覆盖 tool；破坏官以工具后 SystemExit 证明只能 `in_doubt`；FDE 反驳 UI 重试会误导；治理拒绝扩大声明。 | 声明“authenticated request at-most-once + in_doubt”；每个外部通道有 outbox/下游幂等/送达查询后才可提高声明。 |
| B. fail-closed 是否等于产品闭环 | FDE 指出未知状态没有可验证结果；可靠性反驳自动恢复可重复副作用；破坏官确认旧 `done` 被降级但无处置；运维指出多实例未验证。 | 保留无自动重放和无成功终态；交付 owner-bound 查询及受控处置，发布前验证单活或共享恢复协议。 |
| C. 同仓 benchmark/verifier 是否独立 | 治理证明同 PR 可一起改；破坏官确认新增攻击有价值；运维指出不覆盖生产制品。 | 本地报告只作回归；外部受保护 runner 固定身份与 digest，独立验证 checkout、报告、制品和签名。 |
| D. 旧 `done` replay | 可靠性认为 transport done 不能追溯执行成功；破坏官实际攻击 `begin→done→recovery`；代码重写为 `phase + error`；FDE 指出业务结果仍未知。 | 保留改写与错误，增加结果查询/人工处置；拒绝“旧 journal 有 done 即成功”。 |
| E. 迁移兼容是否等于可回滚 | 可靠性指出旧二进制不理解新围栏；运维指出无制品演练；治理拒绝 fixture 替代部署证据。 | 禁止新旧执行二进制混跑；签名制品在生产等价环境演练升级、回滚和数据卷恢复。 |
| F. 本地绿能否解除 VETO | 破坏官承认 76 项和报告通过；FDE、治理、运维分别指出旅程、证据链、部署测试缺失。 | 拒绝以本地单测、同仓 verifier、local Git 或报告替代外部验收；VETO 保持。 |

## 第三轮联合最终否决登记

| 角色 | 结论 | 可验证阻断条件 | 明确拒绝的方案 |
| --- | --- | --- | --- |
| FDE 产品交付官 | **VETO** | 客户在签名制品完成端到端 case，含未知状态查询/处置和签署证据。 | 控制台成功、SSE done、展示页或单测替代客户可用。 |
| 执行可靠性架构师 | **VETO** | 外部工具 outbox/幂等/送达查询或合同化人工处置；崩溃、双恢复、多实例演练通过。 | 唯一索引、SQLite transaction 或 `in_doubt` 等于 exactly-once/高可用。 |
| 治理与红队审计官 | **VETO** | 受保护远程 commit、required CI、固定独立 verifier、签名制品与客户 evidence 链齐全。 | 同 PR 自签 PASS 或人工覆盖 hard denial。 |
| 测试破坏官 | **VETO** | 真实进程、代理、网络、磁盘和外部 tool 的生成式故障攻击证明不该成功时稳定拒绝。 | 正常路径、mock 或一次 local green 即停止攻击。 |
| 运维与部署审计官 | **VETO** | 可重现签名构建、installer/Docker、升级回滚、72h、告警、生产最小权限证据齐全。 | 开发机 CLI、Dockerfile、fixture 或测试告警替代生产验证。 |

**结论**：五个 VETO 全部有效；没有 APPROVE 或 APPROVE_WITH_AMENDMENT，禁止进入 F1-003。

## 当前不可声明清单

    TARGET_CUSTOMER_ACCEPTANCE=NO
    CUSTOMER_ATTESTATION=ABSENT
    CUSTOMER_TEST_EXECUTION=NOT_RUN
    FDE_CASE_EVIDENCE=ABSENT
    BRANCH_PROTECTION=ABSENT
    REMOTE_CI_REQUIRED_CHECKS=NOT_RUN
    SIGNED_RELEASE_ARTIFACT=ABSENT
    EXTERNAL_VERIFIER_ATTESTATION=ABSENT
    REPRODUCIBLE_BUILD=NOT_RUN
    DOCKER_BUILD=NOT_RUN
    INSTALLER_SMOKE_TEST=NOT_RUN
    MIGRATION_ROLLBACK_TEST=NOT_RUN
    72H_SOAK=NOT_RUN
    PRODUCTION_ALERT_FIRE_TEST=NOT_RUN
    SESSION_CITATION_PRODUCTION_VERIFIED=NOT_RUN
    KNOWLEDGE_INDEPENDENT_VERIFICATION=NO
    SKILLS_GOLD_DATASET_VALID=NO
    SKILLS_PRODUCTION_GATE_ELIGIBLE=NO

`SESSION_CITATION_UI_CLOSED_LOOP=YES`、`KNOWLEDGE_LOCAL_RECOMPUTATION_VERIFIED=YES` 和当前 Web boundary 的本地 PASS 都只是受限本地事实，不能抵消上述否认。

本地 Git 提交即使形成 commit-bound 记录，也不等于受保护远程分支、required CI 或独立签名证据；后者仍由 `BRANCH_PROTECTION=ABSENT`、`REMOTE_CI_REQUIRED_CHECKS=NOT_RUN` 和 `EXTERNAL_VERIFIER_ATTESTATION=ABSENT` 明确否认。

## 审查轮次摘要

- **总轮次**：3；五角色均参与三轮。
- **第一轮**：20 条当前发现（P0 11 条、P1 9 条）。
- **第二轮**：6 个争议点均有唯一推荐方案。
- **第三轮**：5 个可验证 VETO，无遗留“可接受风险”。
- **局部修复**：重试复用幂等键；claim/lease/digest 围栏；运行中/未知状态禁止 `done`；旧成功型 SSE 恢复为 `phase + error`；空回复、持久化失败和工具后异常进入 `in_doubt`；Web steering 强制 bool 和 run-scoped idempotency key；新增攻击已写入正式 Web boundary report；新增只接受外部路径、信任根 pin 与签名的 FDE case verifier，但尚无客户输入。
- **遗留阻断**：外部副作用结果、active-active 所有权、历史/账本原子性、人工处置、受保护独立证据、可重现发布、真实运维演练与目标客户验收。
