# Testing and Verification Rules

本文件只在涉及实现验证、测试替身、真实依赖、数据库、网络、外部 API、LLM、RAG、Memory、安全、并发、性能或正式制品测试时读取。

---

# 1. 先定义验证目标

执行测试前必须明确：

```text
verification_target:
要证明的具体行为

verification_boundary:
必须真实经过的组件、接口或系统边界

modified_path:
本次修改实际影响的代码路径

required_level:
DEV_VERIFIED | INTEGRATION_VERIFIED | ACCEPTANCE_VERIFIED

allowed_test_doubles:
允许被替代的非目标依赖

real_verification_required:
是否需要真实组件、sandbox、正式制品或目标环境
```

不能先选测试，再根据测试能力降低验证目标。

---

# 2. 测试替身定义

测试替身包括：

* Mock
* Stub
* Fake
* in-memory implementation
* 固定返回实现
* 手工模拟外部服务
* 替代 client
* 替代 repository
* 替代 storage
* monkey patch 后的替代行为

以下内容本身不等于测试替身：

* dependency injection
* deterministic fixture
* 固定时间
* 固定随机种子
* 固定 UUID
* 测试数据
* 当前机器真实文件系统
* 当前机器真实网络 socket
* 启动真实 PostgreSQL、Redis、Kafka、MinIO 等受控服务

官方 sandbox、test tenant、simulator 单独标记，不能自动等同 production。

---

# 3. 允许使用测试替身的条件

只有同时满足以下条件，测试替身才可以作为当前测试的一部分：

1. 被替代对象不属于当前验证目标。
2. 当前测试不验证被替代对象自身行为。
3. 当前测试不验证当前代码与该对象之间的真实交互。
4. 替身不会绕过本次新增、修改或修复的代码路径。
5. 替身不会改变当前关键行为。
6. 结果不会用于证明被替代对象实际可用。
7. 结果不会用于证明真实生产路径已经通过。
8. 如果被替代对象属于 production path，真实连接会由更高等级验证覆盖。
9. 验证记录公开替身存在和证据边界。

合理用途包括：

* 单元内部业务逻辑
* 稳定制造 timeout、429、500、connection error 等错误
* 大量低成本测试中替代付费或限流服务
* 上游尚未提供时验证独立组件内部行为
* 固定 clock、random、UUID 等非确定因素

---

# 4. 严禁的测试替身用法

以下情况不得用替身作为对应结论的证据：

* 替代当前验证目标
* 替代正在验证的交互边界任一关键参与方
* 绕过本次修改路径
* 用 Mock Repository 证明真实数据库持久化正确
* 用 Mock HTTP client 证明真实协议兼容
* 用 Mock Auth 证明真实权限隔离
* 用 Fake lock 证明真实并发行为
* 用 Mock latency 推断真实性能
* 用 Mock LLM 证明真实模型质量、真实 tool calling 或真实 streaming
* 用预设检索结果证明真实 RAG retrieval
* 因真实依赖失败而改用 Mock 获取 PASS
* 让 Mock 直接返回最终断言值，且没有执行需要验证的中间逻辑

核心规则：

> 正在验证的对象必须真正参与验证；正在验证的边界必须真正经过。

---

# 5. 验证等级

## DEV_VERIFIED

可以使用合规测试替身。

适合证明：

* algorithm
* state transition
* validation logic
* local transformation
* error handling
* 单组件内部行为

不能单独证明真实数据库、真实 API、真实浏览器、真实操作系统、真实网络、真实外部服务或生产环境。

## INTEGRATION_VERIFIED

当前任务涉及的真实边界必须真实参与。

可以使用：

* Testcontainers
* 本地真实服务
* 官方 sandbox
* test tenant
* 临时数据库
* 实际 client/server

必须明确实际环境。

## ACCEPTANCE_VERIFIED

满足用户最终验收条件。

如果验收要求正式制品、目标机器、真实客户环境或真实外部系统，必须使用对应对象。

---

# 6. 不同边界的最低要求

## 数据库

涉及 Repository、ORM、SQL、schema、migration、transaction、constraint、index、isolation、connection pool、persistence 时：

* Mock DB 只允许证明调用前后的本地逻辑。
* 修改涉及真实数据库行为时，至少需要真实目标数据库或具有相关兼容性的受控真实数据库验证。
* SQLite、in-memory DB、Fake Repository 不自动证明 PostgreSQL/MySQL 等目标数据库行为。

## 文件系统和桌面能力

涉及文件创建、读取、修改、权限、锁、path、桌面应用、computer use 时：

* Mock filesystem 只能证明本地调用逻辑。
* 需要证明真实文件行为时必须实际读写目标环境文件。
* OS 特有行为应在对应 OS 验证。

## HTTP、SSE、WebSocket、RPC、streaming

涉及协议格式、header、encoding、chunk framing、timeout、reconnect、keep-alive、connection close 时：

* 不得完全由固定返回值替代。
* 修改协议实现时执行实际 client/server 交互。

## 安全

涉及 authentication、authorization、token、session、ACL、owner isolation、tenant isolation、resource access 时：

* 当前安全路径必须真实经过。
* `auth_mock.return_value = False` 不能证明真实隔离有效。

## 并发和事务

涉及 race condition、lost update、locking、deadlock、transaction isolation、process/thread synchronization 时：

* 当前并发参与方和关键同步机制必须真实执行。
* 单线程 Fake 不能证明真实并发正确。

## 性能

进入正式性能结论的数据必须来自与目标指标匹配的实际执行路径。

报告至少记录：

* artifact
* environment
* configuration
* dataset
* concurrency
* sample count
* measurement method
* relevant dependencies
* test doubles
* warm-up behavior
* key percentiles

影响目标指标的依赖被 Mock 时，不得推断真实系统性能。

## LLM 和 Agent

Mock LLM 可以验证：

* prompt assembly
* local parsing
* timeout/retry handling
* state transition
* tool dispatch logic

Mock LLM 不能证明：

* model name 实际有效
* authentication 有效
* streaming 兼容
* 真实 tool calling 行为
* 模型质量达标

如果任务声称 Agent 能实际执行某个 tool，最终对应验证必须经过真实 tool implementation。

## RAG

修改涉及 chunking、embedding、indexing、retrieval、reranking、permission filtering 的哪个阶段，就必须实际经过哪个阶段。

预先注入最终文档不能证明真实 retrieval path。

## Memory

修改涉及 write、update、retrieval、isolation、persistence、expiration 时，根据修改范围验证真实 storage path。

in-memory Fake 只证明本地逻辑。

---

# 7. 故障注入

允许通过 Mock、Stub、fault injection、受控服务或网络控制稳定制造异常。

记录：

```text
injected_failure:
人为制造的异常

target_behavior:
验证的错误处理

proves:
能够证明什么

does_not_prove:
不能证明什么
```

不得把故障注入结果描述成真实 production incident。

---

# 8. 正式制品

如果修改涉及：

* build
* packaging
* installer
* executable
* wheel
* container image
* desktop build
* production web bundle

source tree 测试不能自动证明正式制品可用。

要求正式制品验收时，测试对象必须是当前生成的对应 artifact。

---

# 9. 独立验证

实施者可以执行开发测试，但最终批准不能只依赖实施者自己的结论。

独立执行只提高“执行结果可信度”，不会提升“测试证据等级”。

例如 CI 跑了一组全 Mock 单元测试，它仍然只属于相应开发级证据。

---

# 10. 替身披露

使用测试替身时，验证记录至少包含：

```text
test_type:
verification_target:
verification_boundary:
test_doubles:
replaced_dependencies:
reason:
modified_path_covered:
proves:
does_not_prove:
real_verification_required:
real_verification_result:
```

不得隐藏替身。

---

# 11. 回归修复

修复 bug 时至少验证：

1. 覆盖原问题的测试或复现步骤。
2. 修改后的目标行为。
3. 与修改直接相关的重要既有行为。

如果无法复现原问题，明确记录限制，不得伪造“修复前失败”。

---

# 12. 测试失败

测试失败后先判断：

* production code
* test
* fixture
* environment
* dependency
* assumption

哪一项错误。

只有存在证据证明测试本身错误时才修改测试。

修改测试后重新确认：

* 验收目标没有降低
* 原问题仍能被检测
* production path 没有被绕过

---

# 13. 证据记录

适用时保留：

* 实际命令
* exit status
* passed/failed/skipped count
* 关键输出
* artifact identity
* environment
* dependency version
* test doubles
* 当前代码状态
* 修改路径与验证的对应关系

所有最终证据必须来自当前修改完成后的状态。

---

# 14. 完成判定

仅有替身测试时，下列修改通常最多达到 `DEV_VERIFIED`：

* component interaction
* external API
* database behavior
* migration
* file system
* network protocol
* streaming
* authentication/authorization
* security isolation
* concurrency/locking
* performance critical path
* build/package/installer
* OS integration
* browser/desktop automation
* real LLM interaction
* Agent tool execution
* RAG retrieval
* persistent Memory

如果任务最终验收要求更高等级：

* 已经完成实现且开发级证据成立，但真实环境暂缺：`PARTIAL + DEV_VERIFIED`
* 缺少条件导致无法继续任何必要工作：`BLOCKED`
* 达到要求等级：`COMPLETED`

不得通过增加更多 Mock 测试把验证等级从 `DEV_VERIFIED` 提升为 `INTEGRATION_VERIFIED` 或 `ACCEPTANCE_VERIFIED`。
