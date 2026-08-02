# 受治理记忆重写：第一阶段

## 结论

本阶段新增了受治理记忆核心(Governed Memory Core)，但没有接管 SmartAssistant 当前运行时。
它解决的是身份、租户、权限、版本、幂等、审计、撤销和回滚，不包含检索质量优化，也不包含大模型抽取。

因此，当前可以证明“治理核心的 13 个行为测试通过”，不能宣称“新版记忆系统已经接入产品”或“检索效果已经提升”。

## 代码来源边界

从 SmartAssistant 保留的工程思路：

- Python 标准库优先；
- SQLite 写前日志(Write-Ahead Logging, WAL)；
- 进程内可重入锁(Reentrant Lock, RLock)；
- 新旧模块并行，避免直接破坏智能体循环(Agent Loop)。

从 SAS 只借鉴的需求：

- 租户和作用域边界；
- 幂等(Idempotency)；
- 审计(Audit)；
- 不可变版本和回滚(Rollback)。

明确没有沿用的 SAS 实现：

- 请求体声明 `actor_role` 或 `user_role`；
- 默认管理员角色；
- 确定性夹具(Deterministic Fixture)生成的智能指标；
- 固定置信度和伪语义抽取；
- 由实现提交同时生成的自证报告。

## 当前结构

```text
IdentityContext
    ↓
GovernedMemoryService
    ↓
GovernedMemoryRepository
    ↓
SQLite: versions + idempotency + audit
```

可信身份上下文(Identity Context)与业务命令分离。`MemoryWriteCommand` 中不存在租户、调用用户、角色和认证来源字段，
并且 `metadata` 显式拒绝这些保留字段，避免复制 SAS 的“请求体自报身份”问题。

## 数据结构

新增表均使用 `governed_memory_` 前缀：

- `governed_memory_versions`：保存不可变版本，同一记忆最多一个 `active` 版本；
- `governed_memory_idempotency`：按租户、调用用户、操作和幂等键唯一；
- `governed_memory_audit`：与业务变更在同一事务提交。

本阶段没有修改 SmartAssistant 原有 `chunks`、`files`、`sessions` 或 `messages` 表。

## 已验证行为

本地命令：

```powershell
python -m unittest tests.test_governed_memory -v
python -m compileall -q agent\memory\governance tests\test_governed_memory.py
```

结果：13 条行为测试全部通过，编译检查返回码为 0。

覆盖范围：

- 租户隔离；
- 身份字段注入拦截；
- 用户所有权；
- 共享记忆授权；
- 会话边界；
- 受限数据读写权限；
- 并发幂等写入；
- 幂等键冲突；
- 不可变版本；
- 撤销；
- 回滚；
- 审计事务。

现有记忆配置测试仍有原始红灯：9 条中 5 条通过、2 条失败、2 条因本地缺少 `regex` 依赖报错。
这些问题在新增模块之前已经复现，本阶段没有修改相关旧代码。

## 修复影响分析

### 直接影响

- 未修改任何现有函数或方法；
- 未修改参数签名；
- 未修改现有返回值；
- 未修改现有数据库表；
- 当前没有旧模块调用新增模块。

### 间接影响

- 现有 `AgentInitializer → MemoryManager → MemoryStorage` 调用链保持不变；
- 现有提示词、工具注册、事件回调和智能体循环(Agent Loop)保持不变；
- 新模块只有被显式导入和实例化后才创建数据库连接；
- 新持续集成(CI)工作流只监听新增目录和测试文件。

### 数据兼容性

- 新表没有读取或迁移旧数据，不存在旧字段缺省问题；
- 记忆版本为追加写入，不原地改写正文；
- 撤销会生成 `revoked` 版本，读取接口不再返回该记忆；
- 回滚会生成新的 `active` 版本，不修改目标历史版本；
- 元数据必须可序列化为 JSON，并拒绝身份和权限保留字段。

## 尚未完成

- 未连接 SmartAssistant 通道的真实认证身份；
- 未把受治理记录投影到旧向量索引；
- 未迁移旧 `MEMORY.md` 和 `memory/*.md`；
- 未实现文档引用和证据定位；
- 未实现静态加密和密钥管理；
- 未使用真实数据集评测检索指标；
- GitHub 持续集成(CI)尚未实际执行，因此不能宣称持续集成(CI)绿灯。

下一阶段必须先建立带人工相关性标注的真实检索数据集，再决定如何把现有关键词和向量检索接入受治理边界。
