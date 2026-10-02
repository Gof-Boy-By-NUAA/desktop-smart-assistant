# Mimosa 拦截验证与待审基线

## 范围与状态

基线源码：`d748b204923834e993d5e0b665c25b3a5cd9fae6`。任务状态 `PARTIAL`：原生 pre-commit 对指定 SSRF 探针的拦截已真实验证；完整扫描执行完毕，但深度覆盖不完整，告警审查与人工豁免未完成。这份记录不构成安全放行或发布批准。

## 实际命令与返回值

在任务专用 `test/mimosa-probe` 分支暂存 `test_probe.py`（仅静态扫描，未执行其代码），执行 `git commit -m "test: probe mimosa pre-commit intercept"`：

1. 恢复原生 Git 门禁之前，返回 `0`。这证明本次直接 Git 路径没有阻断该探针，不能推出 ZCode 的所有交互式钩子均失效。
2. 当前插件登记为 Mimosa 1.0.3，配置启用，缓存存在；此前将插件登记数组当对象查询、得出“未安装”的结论撤回。仓库当时没有活动的 `pre-commit` / `pre-push`，未配置 `core.hooksPath`。
3. 通过该版本官方 CLI 执行 `git-gate install`，返回 `0`。安装 `deny` 模式原生 `pre-commit` / `pre-push`，未覆盖已有活动钩子，未增加忽略规则或降低阈值。
4. 恢复后重复同一探针，提交返回 `1`，实际输出为 `Mimosa Git 门禁：发现 1 个高危：test_probe.py:L1 SSRF 服务端请求伪造`。
5. 两次探针均已清理，切回 master 与删除临时分支返回 `0`。未推送探针提交。master SHA 保持不变，工作区当时干净。

pre-push 当前仅确认安装，尚未在推送时验证。配置和钩子存在不等于所有工具路径的拦截能力已获验证。

完整扫描使用现有 Node 26.4.0 运行插件 CLI（仅扫描工具运行时，不用于桌面发布构建）：

```text
node <Mimosa-1.0.3>/payload/dist/cli.js audit <当前仓库>
  --deep --json --engine native --no-triage --no-ignore-comments
  --min info --fail-on high --ir-mode enforce
```

首次返回 `2`，耗时 128.215 秒。第二次以二进制 stdout/stderr 捕获，并只在子进程增加已有 Python 路径与 UTF-8 设置，返回 `2`，耗时 72.969 秒；报告字节完全一致。没有 GLM 调用、目录通配排除、依赖排除、规则更新、包安装或构建型外部分析器执行。

## 覆盖与告警

- 库存选取 1,966 个文件，未达到 5,000 文件上限；报告 `runStatus=inconclusive`、`coverage.status=partial`。
- 直接告警：824 high / 4 medium / 54 low。这些是静态规则命中，不能直接等同已证实漏洞。
- 直接 high 按路径用途分为：Git 跟踪源码 268、Git 跟踪运行时 skills 17、Git 跟踪测试 49、临时/历史副本 473、未跟踪工作区 skills 17。路径分类不能证明代码作者或上游来源；运行时 skills 未获豁免。
- Python 语义 AST 不可用记录 603 项；调用图失败；库源码扫描达到 800 文件上限，仅覆盖 29 个已解析库中的 21 个。完整业务语义覆盖尚未成立。
- 供应链检查实际使用离线 advisory pack，121 条匹配记录 `gateEligible=false`。不能将其描述为实时 OSV 验证通过。

## Finding Ledger 与豁免纪律

[逐条待审台账](2026-09-30-mimosa-pending-findings.json)保留 1,134 条记录：882 直接告警、6 跨文件告警、15 库告警、231 可达性 advisory。保留重复来源，不把这些类别的和当作独立漏洞数。

每项登记规则 ID、文件、行号、文件 SHA-256、Finding Hash、风险等级、静态调用链的原始报告位置/摘要。静态报告不提供实际运行调用栈。原始代码和完整静态链保存在仓库内忽略的证据目录，未复制到公开台账，以防泄露凭据。

这份冻结基线的全部记录为 `disposition=PENDING_REVIEW`、`Approver=null`、`Expiry Date=null`，登记时批准的豁免为 **0**。本台账不是 Mimosa 豁免配置，不会让门禁放行；未添加任何通配白名单。2026-10-01 用户随后批准了当前源码的一个精确 sidecar finding，另行保存在[正式审批记录](2026-10-01-approved-sidecar-triage.json)，不回写这份历史基线，也不批准其余记录。

上游模板与自研归属需要逐项来源证据，不能仅凭 `skills/` 或 `tests/` 路径签署豁免。后续需补齐受支持的深度分析环境、评估真实风险并由授权人员审批明确的 Finding Hash、理由与有效期。

定向来源核查发现：17 条运行时 skill 的直接 high 均在 `skills/image-generation/scripts/generate.py`；另有 13 条可达性 advisory。该文件可追溯到本地根基线提交 `fa39c286bad0f5045b50f35806981847ce616294`，后续 `6c5baf83279c102ec8c95e42d60dd4f2a7f02503` 仅增加 future import。文件级上游 URL、版本和作者来源未确立，不能沿用“全部来自上游模板”的旧归因。项目 NOTICE 的分发声明也不能代替逐文件来源证据。

## 证据

`tmp/delivery-defense/20260930-115850/` 保存实际命令、两次探针 stdout/stderr/退出码/清理记录、官方门禁安装日志、两次原始扫描报告和计时。该目录属于仓库内临时证据，未进入发布制品。

双层白屏防御可按授权进行开发与验证；安全基线尚未获发布批准，不能因此直接打包交付或宣称全部门禁通过。

## 后续提交的真实阻断

防御分支第一次正常提交返回 `1`，原生门禁命中当前 `desktop/src/main/index.ts:74` 的路径拼接与 `:392` 的后端请求。这两段代码对应基线的第 73/391 行，本次仅新增 import 与 guard 挂载导致行号偏移，未修改它们的执行逻辑。

当前源码可确认的约束：设备身份文件名为常量，生产调用从 `app.getPath('userData')` 传入目录；后端 IPC 校验可信 renderer，代理解析路径并拒绝外部 origin，PythonBackend 请求主机固定为 loopback 且校验后端证书。上述静态约束是正式安全审查的输入，不能自动把告警改成 PASS，也不能代替精确豁免审批。

上述首次提交时批准者与有效期为空，门禁继续阻断。相关真实 stdout/stderr/exit 保存为证据目录中的 `defense-commit-*`；没有通过其他提交方式规避该结果。当前精确审批与原生门禁的支持限制见正式审批记录；审批成立不等于工具已经支持该项放行。
