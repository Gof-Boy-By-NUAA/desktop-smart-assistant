# RC3 目标 VM 安装与卸载验证

## 制品绑定

- 源码提交：`46a2ed179e65de70d10b113453d2dcf827a274fd`。
- 安装包：`SmartAssistant-Setup-2.1.3-rc.3-x64.exe`，191471072 字节。
- SHA256：`6DDC1DAED6455FF19E093CC493468A3F9C651551419179406B90069594320283`。
- 签名状态：未签名。本记录不发布安装包，也不改变其源码绑定。
- 目标：`SmartAssistant-A05-Clean`，Windows 11 专业版 `10.0.26200`，客体用户 SAS。
- 实际 PowerShell 管理员令牌检查为 False；这不能证明账户不属于管理员组。

## 实际结果

| 检查 | 结果及范围 |
| --- | --- |
| 安装包一致性 | 用户在客体执行 SHA256 校验，与上列制品一致。 |
| 已安装前端 | app.asar SHA256 为 `52FCCA1DF5EAA33F18330B69FCA5E87062D645227CE5888BA9FA7AA295207B83`，与该安装包解包内容一致。 |
| 已安装后端 | backend exe SHA256 为 `CF4F5224EB1BA07DB5ACDE147A53C0759D53D933C4475993E4AFFDDA054C1E1F`，与该安装包解包内容一致。 |
| 图片交互 | 上传公开 S 图标，在真实桌面会话收到 Vision 成功卡片及正确描述；无模型替身。 |
| 结构化结果展示 | 实际展开 Vision 输入/输出 JSON 后页面正常显示。 |
| 正常退出 | 托盘 Quit 后用户查询两种进程名，均返回 NoProcessFoundForGivenName。未记录进程退出码及精确退出耗时。 |
| 系统重启与恢复 | 通过客体 Windows 正常重启；用户手动登录后，同一测试图片、消息及回复恢复显示，无额外模型请求。 |
| 标准卸载 | 从核实的安装目录启动标准卸载程序；用户最终检查安装目录 Test-Path=False。 |
| 卸载进程与注册项 | 用户检查前后端进程均不存在；HKCU、HKLM 及 HKLM WOW6432Node 三处卸载注册表查询无匹配项。 |
| 数据保留 | `.cow` 目录及 `web_sse_journal.sqlite3` 的 Test-Path 均为 True；未声称逐字节完整性验证。 |

## 证据边界

本轮无生产代码修改、无重新构建、无测试替身。GUI 观察与用户返回的客体只读命令共同形成证据。WindowsApps 的 Python 别名不能证明真实解释器存在或不存在；Node、npm、cl 未在 PATH 查到，也不能替代完整环境审计。

目标 VM 已有安装历史和用户数据，本轮不证明全新干净机安装。模型请求的持续观察存在间隔，不能排除间隔内瞬时白屏，也不报告独立测得的首字延迟。Vision 卡片的 9.91 秒仅为 UI 所示工具耗时。GUI 安装/卸载未取得进程退出码，不冒称 exit 0。

本轮已关闭的项目：制品/已安装文件匹配、实际图片结果及结构化卡片显示、正常退出后的进程清理、系统重启后的会话恢复、标准卸载及上述数据存在性检查。

整体交付状态仍为 **PARTIAL / INTEGRATION_VERIFIED**：未签名、全新干净机验收尚未建立。Mimosa 原生逐项审批接入状态见 [审批记录](2026-10-11-mimosa-individual-approvals.md)，不将人工审批称作原生门禁通过。

## 本地原始证据入口

`tmp/rc3-new-installer-vm-acceptance-20261011-214212-6826fe8e/` 保留制品绑定、GUI 截图、重启记录及用户返回的安装/卸载检查；不把安装器、运行日志、用户会话数据库或凭据加入 Git。

关键记录：`image-ui-result.json`、`guest-post-quit-and-payload-user.json`、`restart-persistence.json`、`uninstall-ui.json`、`guest-uninstall-final-user.json`。这些本地原件未随本报告上传，远端读者不能仅凭文件引用独立复验本地原件。
