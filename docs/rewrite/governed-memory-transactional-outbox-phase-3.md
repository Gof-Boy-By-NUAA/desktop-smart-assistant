# 治理记忆事务发件箱第三阶段

## 结论

本阶段为治理记忆增加持久事务发件箱(Transactional Outbox)和租户级跨进程发布锁。事实、幂等结果、审计事件与最新派生目标在同一 SQLite 事务登记；Markdown 投影和 FTS5 索引在事实事务外生成并核验，失败任务由重试或启动恢复继续收敛。

这不是分布式事务，也不是租约(Lease)工作队列。当前正确性依赖同一工作区的所有派生发布者遵守相同租户文件锁。

## 调用链与事务边界

```text
remember / revoke / rollback
  -> GovernedMemoryService：事实、审计、幂等结果、outbox 同事务提交
  -> MemoryManager：取得租户跨进程文件锁
  -> 短事务读取任务目标和最新事实后立即提交
  -> 事务外写入并核验投影与 FTS5 索引
  -> 短事务重读目标和最新事实
  -> 版本未变化才按 tenant_id + memory_id + target_version 确认
  -> 版本已前进则继续处理最新目标
```

代码证据：

- `agent/memory/governance/service.py` 在事实写入事务内登记派生任务；旧幂等请求也会重新登记最新事实版本。
- `agent/memory/governance/repository.py` 使用单调 `UPSERT` 保存 `target_version`，条件确认同时匹配租户、记忆和版本。
- `agent/memory/manager.py` 将事实读取、派生 I/O 和条件确认拆成两个短事务；投影使用 `flush + fsync + os.replace`。
- `agent/memory/governance/locks.py` 使用租户 SHA-256 锁文件，Windows 采用 `msvcrt`，POSIX 采用 `fcntl`，并检查符号链接和 Windows 重解析点。

## 故障收敛与隔离

- 投影写入失败、索引静默 no-op、撤销删除失败或确认失败时，事实已提交且任务保留。
- 启动恢复重新扫描最新事实；恢复期间出现更高版本时不会删除新任务。
- 搜索返回治理候选前回查事实状态和版本，因此撤销残留或陈旧索引按失败关闭(Fail Closed)处理。
- 投影路径为 `memory/.governed/<tenant_sha256>/<memory_id>.md`，正文包含 `tenant_id`；任务、索引和锁也按租户隔离。
- 无法可靠归属租户的旧版根目录平铺 `*.md` 和 `.*.tmp` 被视为不可信派生缓存并清除，不作为事实迁移来源。
- `MemoryManager` 拒绝身份租户与配置租户错配；初始化器在创建数据库或目录前绑定可信非默认租户。

## 真实数据门禁

执行日期：2026-07-29。报告：`benchmarks/results/cmrc2018-memory-outbox.json`。

```text
数据集：CMRC 2018 官方开发集
数据 SHA-256：5cfe4414c28a8ecbb51670f78c0dc7d1049f286c2d5769b52f1f94bcc0752cf1
真实文档：848/848
撤销样本：50
门禁：14/14 passed
事实写入 P95：1.531795 ms，阈值 <= 100 ms
初始恢复：33.9253 docs/s，阈值 >= 20 docs/s
撤销恢复：27.4495 docs/s，阈值 >= 20 docs/s
初始有效/投影：848/848
最终有效：798
恢复后待处理任务：0
撤销索引污染：0
撤销投影污染：0
实现 SHA-256：831295817ee303781ad82f1329c68b36b5c85457c7ca580dee9fd65e3a978924
```

实现指纹绑定事实仓库、服务、跨进程锁、运行时管理器、词法检索、共用路径安全、门禁、数据加载、P95 指标实现和数据源清单。此前未包含锁、路径安全与指标实现的旧指纹均已废弃。

CMRC 门禁证明本地事实与派生数据的一致性、撤销污染防护和恢复性能，不证明客户任务成功率、语义记忆质量或生产负载容量。

本地持续集成(CI)等价证据：治理记忆工作流精确命令 `46 passed`；全量回归 `688 passed, 38 skipped`；完整编译、工作流 YAML 解析和依赖检查均通过。远程 GitHub Actions 尚无运行记录。

## 修复影响分析(Fix Impact Analysis)

- 公共记忆命令和 `MemoryRecord` 结构未改变；新增内部发件箱表和状态字段 `governed_memory_pending_derivatives`。
- 投影路径从平铺目录改为租户哈希目录。投影是可重建机器缓存，事实库格式与 `governed://<memory_id>` 标识不变。
- 普通 `memory/*.md`、旧 `index.db`、向量检索和非治理文件同步链不变。
- 派生失败发生在事实提交后。调用方可使用相同幂等键重试，启动恢复也会继续排空任务。
- 阻塞派生 I/O 不再持有 SQLite 写事务；同租户派生仍被文件锁串行化，不同租户使用不同锁。
- 自演化读取当前 `MemoryManager` 的租户配置，不再依赖可能被其他运行时覆盖的全局配置。

## 剩余限制

- 当前治理数据库没有 `PRAGMA user_version` 和严格结构迁移版本门禁。
- 当前没有领取(Claim)、租约和分布式锁，不能外推到共享网络文件系统或多主机部署。
- 本次初始恢复耗时约 `25.00 s`，期间持有租户派生锁；大租户仍需评估分片或更细锁粒度。
- 写入延迟指标测量的是事实与任务提交，不是同步 `MemoryManager.remember` 的端到端延迟。
- 当前仅有本地持续集成(CI)等价验证；没有 GitHub Actions 运行 ID 或远程通过证据。
