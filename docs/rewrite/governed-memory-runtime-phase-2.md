# 治理记忆运行时第二阶段

## 结论

本阶段把第一阶段的治理记忆核心接入了 SmartAssistant 真实运行时。SQLite 事实库现在负责写入、版本、撤销、回滚和权限判断；Markdown 与词法索引均为可重建派生数据，不再作为治理记忆的事实源(Source of Truth)。

本阶段不代表整个 SmartAssistant + SAS 重写完成。知识管线(Knowledge Pipeline)、企业身份接入、客户真实数据集和目标客户验收仍未完成。

## 运行时调用链

```text
AgentInitializer
  -> 从可信会话构造 IdentityContext
  -> 注入 memory_search / memory_get / memory_write / memory_revoke / memory_rollback

memory_write
  -> MemoryManager.remember
  -> GovernedMemoryService.write
  -> governance.db：不可变版本 + 幂等结果 + 审计事件（同一事务）
  -> memory/.governed/<memory_id>.md：原子兼容投影
  -> retrieval-v2.db / governed 集合：权限化词法文档

memory_search
  -> 租户、用户、会话、敏感级别 SQL 过滤
  -> BM25 + 查询覆盖率重排
  -> 对 governed 候选回查 governance.db 的当前有效版本和读取权限

memory_revoke
  -> governance.db 提交撤销版本
  -> 删除投影与索引文档
  -> 即使索引删除失败，搜索回查仍拒绝已撤销记录

MemoryManager 启动
  -> 从 governance.db 枚举有效记录
  -> 重建缺失投影
  -> 原子替换 governed 检索集合
```

## 代码变更

### 事实源与派生数据

- `agent/memory/manager.py`
  - 初始化 `GovernedMemoryRepository` 和 `GovernedMemoryService`。
  - 新增 `remember`、`revoke`、`rollback`、`get_governed_memory`。
  - 启动时从事实库恢复投影和 `governed` 检索集合。
  - 使用同目录临时文件和 `os.replace` 完成原子投影。
  - 对治理搜索候选回查当前版本，过期、撤销或无权记录一律丢弃。
  - `close()` 释放治理数据库连接，`get_status()` 报告有效治理记录数。

### 工具与可信身份

- `agent/tools/memory/memory_lifecycle.py`
  - 新增写入、撤销和回滚工具。
  - 未传幂等键时，对规范化请求计算 SHA-256 稳定键。
  - 工具输出包含 `memory_id`、版本、状态、内容哈希和来源引用。
- `agent/tools/memory/memory_get.py`
  - 支持 `memory_id` 和 `governed://<memory_id>`。
  - 治理正文只能通过身份化服务读取。
  - 修复 Windows 绝对路径被错误加上 `memory/` 前缀的问题。
- `bridge/agent_initializer.py`
  - 在初始化边界构造 `IdentityContext`，业务参数不能提交租户或用户身份。
  - 每个会话注入五个记忆工具。
- `agent/tools/tool_manager.py`、`agent/tools/__init__.py`
  - 生命周期工具改为由初始化器专门构造，避免无身份实例。

### 绕过防护

- `agent/tools/utils/governed_memory.py`
  - 统一识别 `.governed`、`governance.db`、WAL/SHM 和只读旧版 `MEMORY.md`。
- `read`、`write`、`edit`
  - 禁止直接读取治理私有投影或数据库。
  - 禁止直接改写 `MEMORY.md` 和治理机器文件。
- `search_files`
  - 禁止直接搜索治理私有路径。
  - 外部后端在 `no_ignore=true` 时也会对结果再次过滤。
  - 同时修复 Windows `rg` 输出中盘符冒号导致内容结果被全部丢弃的问题。
- `agent/prompt/builder.py`
  - 新记忆统一使用生命周期工具。
  - `MEMORY.md` 明确为只读旧版快照。
  - 明确 `source_ref` 和 `evidence_quote` 只是来源声明，不是独立核验证据。

### 持续集成与跨平台修复

- `.github/workflows/test-governed-memory.yml`
  - Windows Python 3.8、3.11、3.13 均执行核心、运行时和检索集成测试。
  - 工作流监听运行时、工具、提示词和初始化器的相关路径。
- `cli/commands/backup.py`
  - 恢复默认工作区时使用统一 `expand_path()`，Windows 下显式 `HOME` 生效。
- `agent/tools/browser/browser_tool.py`
  - 已存在浏览器服务时不再被引擎安装预检误拦截。
- `models/dashscope/dashscope_bot.py`
  - 缺少可选 SDK 时模块仍可被工具发现，真正实例化供应商时给出明确错误。
- 平台相关测试
  - Windows 未授予符号链接权限时跳过依赖符号链接的用例。
  - SQLite 低于 3.44 时跳过仅在 3.44+ 可复现的 FTS5 完整性断言。
  - 修复测试自身未关闭 SQLite 临时连接造成的 Windows 文件锁。

## 修复影响分析(Fix Impact Analysis)

### 调用方影响

- 原有 `MemoryManager.search()`、文件同步、向量检索和旧版关键词回退接口保持不变。
- `MemorySearchTool` 新增独立 `session_id`，旧调用只传 `user_id` 时保持原行为。
- `MemoryGetTool` 不再强制要求 `path`，调用方可传 `memory_id`；旧文件路径读取继续兼容。
- 根 `MEMORY.md` 对通用 `write/edit` 变为只读。这是有意的行为变更，避免绕过版本和审计。
- 普通 `memory/*.md` 和 `knowledge/*.md` 文件仍由旧同步链处理；隐藏 `.governed` 目录被明确排除。

### 数据结构兼容性

- 旧 `index.db` 未迁移或删除，旧记忆和向量索引继续存在。
- 新事实库位于 `memory/long-term/governance.db`，使用独立表和 SQLite 写前日志(Write-Ahead Logging, WAL)。
- 新词法文档位于 `retrieval-v2.db` 的 `governed` 集合，可从事实库完整重建。
- 搜索结果以 `governed://<memory_id>` 标识治理记录，旧结果仍返回相对文件路径。
- 投影位于 `memory/.governed/<memory_id>.md`，只用于兼容和检查，不参与普通文件同步。

### 一致性与故障影响

- 事实事务先提交，随后刷新投影和索引。派生刷新失败时，使用相同幂等请求重试或重启即可修复。
- 撤销后的索引删除即使失败，搜索仍以事实库状态做失败关闭(Fail Closed)判断，不返回残留正文。
- 更新后的旧索引版本与事实库版本不一致时直接丢弃，不返回过期内容。
- 进程内多个 `MemoryManager` 使用共享可重入锁，避免启动恢复与生命周期写入交错。
- 外部多进程同时操作仍依赖 SQLite 事务；投影与索引不是跨进程分布式事务，后续应增加持久化事务发件箱(Transactional Outbox)。

### 身份与证据限制

- 当前本地运行时将 `session_id` 映射为 `actor_user_id`。这能提供会话间隔离，但不是企业级身份提供方(Identity Provider, IdP)认证。
- 默认本地无会话代理只获得 `memory:write_shared`，普通会话没有共享写入或受限读取角色。
- 模型提交的 `source_ref`、`evidence_quote` 和元数据没有密码学签名，也没有回查原始消息存证；只能称为来源声明。
- 在消息原文存证、来源存在性校验和哈希绑定完成前，禁止输出“已核验证据清单”。

## 验证证据

执行日期：2026-07-28，Windows，Python 3.11.8，SQLite 3.43.1。

### 持续集成(CI)等价本地回归

```powershell
python -m pytest -q
```

结果：

```text
504 passed, 38 skipped, 1 warning in 26.38s
exit code: 0
```

治理工作流精确测试：

```powershell
python -m pytest tests\test_governed_memory.py tests\test_governed_memory_runtime.py tests\test_memory_governed_retrieval_integration.py -q
```

结果：

```text
22 passed in 1.27s
exit code: 0
```

### 真实中文检索门禁

```powershell
python -m benchmarks.retrieval.compare --output benchmarks\results\cmrc2018-comparison.json
```

数据：CMRC 2018 官方开发集，848 个上下文，3219 个问题，三轮交错运行。

| 指标 | SmartAssistant 基线 | 新版 | 门禁 |
|---|---:|---:|---|
| Recall@1 | 0.1121 | 0.8984 | 通过 |
| Recall@5 | 0.1351 | 0.9444 | 通过 |
| Recall@10 | 0.1417 | 0.9487 | 通过 |
| MRR@10 | 0.1210 | 0.9184 | 通过 |
| 空结果率 | 0.7897 | 0.0146 | 通过 |
| 平均延迟 | 2.2357 ms | 1.3829 ms | 通过 |
| P50 延迟 | 1.8024 ms | 1.0601 ms | 通过 |
| P95 延迟 | 4.4356 ms | 3.3797 ms | 通过 |
| 索引时间 | 322.2077 ms | 244.7530 ms | 通过 |

结果：9/9 门禁通过，`passed: true`。

### 编码与编译

```text
UTF8_STRICT_OK files=23
python -m compileall ...
exit code: 0
```

官方 CMRC 缓存中包含 Python 2 的原始评测脚本，直接编译整个 `benchmarks/.cache` 会因 `ur''` 语法失败；项目自有 `benchmarks/retrieval` 已单独编译通过。

## 未完成项

1. 知识管线的解析、切块、引用、权限和真实数据指标尚未重写。
2. SAS 有价值模块尚未逐项完成代码级迁移与独立消融实验(Ablation Study)。
3. 消息原文存证和来源真实性校验尚未实现。
4. 企业身份提供方(IdP)、角色同步和跨进程事务发件箱尚未实现。
5. 目标客户测试尚未执行，因此不能进入最终验收状态。
