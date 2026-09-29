# 录屏沉淀技能方案封存包

## 封存状态

```text
FEATURE_ID=SCREEN_RECORDING_SKILL_DISTILLATION
FEATURE_STATUS=FROZEN_NOT_IMPLEMENTED
IMPLEMENTATION_AUTHORIZED=NO
RELEASE_ELIGIBLE=NO
CUSTOMER_ACCEPTANCE=NOT_RUN
ARCHIVED_AT=2026-08-28
```

本包用于保存“录屏沉淀为 Skill”功能的冻结技术方案、未来实施路径、三轮严格审查、风险与验收合同以及封存决定。当前项目继续推进其他工作，但本功能暂时不实现。

## 文档顺序

1. [`01-技术方案冻结版.md`](./01-技术方案冻结版.md)：未来启封时唯一推荐的架构和关键决策。
2. [`02-未来实施路径.md`](./02-未来实施路径.md)：启封后应按顺序执行的工程计划；当前不得执行。
3. [`03-三轮严格审查记录.md`](./03-三轮严格审查记录.md)：五角色独立发现、交叉反驳和最终 VETO。
4. [`04-风险与验收合同.md`](./04-风险与验收合同.md)：P0/P1 风险、硬否认字段和解除条件。
5. [`05-封存决定.md`](./05-封存决定.md)：封存范围、允许/禁止行为和未来启封流程。
6. `SHA256SUMS.txt`：包内文档 SHA-256，用于发现归档后篡改。

## 原始材料

- `docs/录屏沉淀Skill技术方案-2026-08-20.md`：原方案及其未提交修订，保留为历史输入，不再作为无歧义实施合同。
- `docs/未完成项与路线图-2026-08-20.md`：记录本功能原先处于“完全不存在”状态。
- `feat/screen-skill-m1-distiller@e475a6b`：仅包含 `agent/skills/distiller.py` 和 `tests/test_skill_distiller.py` 的原型切片，不是完整实现，不要求未来发布提交必须以它为祖先。
- `benchmarks/results/release-evidence-manifest.json`：当前整体 `passed=false`，只能说明现有证据状态。
- `benchmarks/results/customer-skill-acceptance.json`：当前 `pending_customer_inputs`。

## 使用规则

- 仅以本目录文档作为“封存方案”的索引，不得把它们解释为运行证据。
- 未来若代码、依赖、模型供应商、隐私规则或发布基础设施已经变化，必须重新执行五角色三轮审查，不能直接沿用本次 VETO 解除条件之外的旧结论。
- 解压后的文件若与 `SHA256SUMS.txt` 不一致，视为已被修改，不得用于启封决策。

