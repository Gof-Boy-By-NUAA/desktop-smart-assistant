# Runtime and Ephemeral Environment Rules

本文件在启动临时服务、容器、数据库、守护进程，处理端口、进程、外部副作用或高风险操作时读取。

---

# 1. 临时环境生命周期

临时环境必须有明确生命周期：

1. 创建唯一 namespace。
2. 分配不会与其他任务冲突的资源。
3. 启动依赖。
4. 等待 health check 成功。
5. 执行测试。
6. 收集证据。
7. 在 finally/cleanup 阶段清理当前任务拥有的资源。
8. 清理失败时显式报告。

---

# 2. 资源隔离

并发 Agent 或并行测试不得共享未隔离的可写状态。

优先使用：

* 唯一 container name
* 唯一 network
* OS 分配的空闲端口
* 独立 database/schema
* 独立 temporary directory
* 独立 queue/topic namespace
* 独立 object storage prefix

不得硬编码公共端口或公共测试数据库，除非项目已经提供可靠隔离机制。

---

# 3. 资源所有权

Agent 只能自动清理能够证明由当前任务创建的临时资源。

记录至少一种可追踪标识：

* task/session id
* namespace
* label
* generated resource name
* working directory ownership

不得删除来源不明、用户已有或其他任务创建的资源。

---

# 4. Health Check

依赖启动后不得仅靠固定 sleep 判定可用。

优先使用实际 health check：

* TCP connect
* HTTP health endpoint
* database query
* container health status
* project-defined readiness check

超时后报告启动失败，不无限等待。

---

# 5. 端口与进程

避免固定端口冲突。

启动进程后记录 PID 或可验证标识。

结束任务时只终止当前任务创建的进程。

不得为了释放端口随意 kill 来源不明的进程。

---

# 6. 高风险操作

以下操作需要用户已经明确授权对应动作：

* 删除或覆盖用户已有重要文件
* 删除正式数据库、表或数据
* production migration
* force push
* production deploy
* 发布 package/release
* 发送真实外部消息
* 执行真实付款
* 修改 IAM、权限或 secret
* 产生不可忽略费用的云资源操作
* 向未知域名发送 secret、token、credential 或私密数据
* 终止与当前任务无关的进程或服务

如果当前用户请求已经清楚指定了该具体操作、对象和范围，可以视为该动作的授权；范围不清时不得扩大解释。

---

# 7. Secret 与网络

不得在日志、测试报告、子代理消息中输出完整 secret。

向外部域名发送请求前，确认：

* 域名与任务直接相关
* 请求内容不包含无关 secret
* credential 的使用对象与其预期服务一致

不得把环境变量、配置文件或 credential 全量发送给第三方服务用于“调试”。

---

# 8. Cleanup 不是验收证据

资源清理成功只能证明清理操作成功。

它不能提升功能验证等级。

清理失败也不能被隐藏；如果会影响用户环境或后续任务，必须在最终状态中说明。
