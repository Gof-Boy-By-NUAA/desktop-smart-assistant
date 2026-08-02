# 中文检索基线

本目录使用 CMRC 2018 官方开发集测试文档检索。原始数据包含 848 个真实上下文、3219 个真实问题和 9657 个人工答案。

来源、提交、文件哈希和许可证见 `data_sources.json`。下载器会校验 Git 提交和 SHA-256，任一不一致都会终止评测。

运行完整 SmartAssistant 原始关键词检索基线：

```powershell
python -m benchmarks.retrieval.evaluate --output benchmarks/results/cmrc2018-smart-assistant-baseline.json
```

快速验证固定问题子集：

```powershell
python -m benchmarks.retrieval.evaluate --max-queries 100
```

运行新的租户化词法检索：

```powershell
python -m benchmarks.retrieval.evaluate --engine improved --output benchmarks/results/cmrc2018-improved-lexical.json
```

运行同数据、同问题集合的自动性能门禁：

```powershell
python -m benchmarks.retrieval.compare --output benchmarks/results/cmrc2018-comparison.json
```

门禁默认交错运行三个重复轮次并取中位数，要求 Recall@1/5/10、MRR@10 全部严格提升，同时空结果率、平均/P50/P95 延迟和索引时间全部严格下降。

核心指标：

- 召回率(Recall)@1、@5、@10；
- 平均倒数排名(Mean Reciprocal Rank, MRR)@10；
- 空结果率；
- 平均、P50 和 P95 查询延迟；
- 索引构建时间。

该基线只调用 SmartAssistant 当前 `MemoryStorage.search_keyword()`，不使用新治理模块，也不修改原始排序算法。
