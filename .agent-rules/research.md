# Research and External Information Rules

本文件在任务依赖第三方技术信息、版本、协议、API、CLI、SDK、模型能力或外部系统行为时读取。

---

# 1. 不依赖模型记忆给出易变化结论

以下内容影响实现时，应核验当前信息：

* library/framework version
* API behavior
* CLI option
* SDK
* protocol
* database behavior
* model capability
* compatibility
* pricing
* resource limit
* release behavior
* timeout/limit

---

# 2. 证据优先级

优先使用：

1. 当前项目实际版本和配置
2. 当前环境实际运行结果
3. 官方文档
4. 官方 release note
5. 官方 source code
6. 用户提供的权威资料

无法确认时使用 `UNKNOWN`。

---

# 3. 用户提供 URL

用户提供网页作为任务依据时，读取与当前任务直接相关的完整内容。

不得只依赖：

* 搜索摘要
* 标题
* 局部 snippet
* 模型记忆

---

# 4. 不扩大调查

已经取得足够证据回答当前问题时停止搜索。

不得为了表现工作量继续查询无关资料。

---

# 5. 外部信息与本地事实分开

官方文档说明“应该如何工作”不能自动证明当前项目“实际就是这样工作”。

需要确认项目实际行为时，结合当前代码、配置或运行结果。
