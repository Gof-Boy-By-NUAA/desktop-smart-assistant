# SmartAssistant 恢复说明（2026-09-30 校正）

开发收尾基线是 `master` @ `2cf708e334a600d59e42831f83efe63a6a215016`，已通过 PR #5 rebase 合并到 `https://github.com/Gof-Boy-By-NUAA/desktop-smart-assistant.git`。本说明不依赖已删除的工作树；文档与 CI 整改后的版本以对应 PR 合并提交为准。

## 恢复当前 master

在现有 Git 仓库内创建专用恢复目录，避免覆盖当前主树。以下命令用于需要恢复时执行：

```powershell
$repo = (git rev-parse --show-toplevel).Trim()
$recovery = Join-Path $repo ('tmp\restore-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
git clone --branch master https://github.com/Gof-Boy-By-NUAA/desktop-smart-assistant.git $recovery
if ($LASTEXITCODE -ne 0) { throw 'Clone failed' }
git -C $recovery rev-parse origin/master
git -C $recovery status --short
# 如需核对开发收尾基线，读取提交，不覆盖当前工作树：
git -C $recovery show --stat 2cf708e334a600d59e42831f83efe63a6a215016
```

## 离线 bundle

既有宿主机归档 `C:\Users\UnlimitedPower\Documents\桌面智能助手\archive\final-state-20260929\` 中：

- `smart-assistant-all-refs.bundle` 是 2026-09-29 17:06 的历史快照，master 当时为 `2fc8065`，HEAD 为 `116a503`。它不包含开发收尾基线 `2cf708e`。
- `qoder-wrapper-repo-snapshot.bundle` 保留旧 wrapper 仓库历史；它不能替代项目最终 master。
- `rc2-uncommitted-changes.patch`、状态快照和 zip 保留当时的历史增量。不要将它们直接套用到当前 master 或重复提交已经合并的修改。

需要当前 master 的离线恢复时，先在现有 Git 仓库内创建包含该提交的独立 bundle，再验证。下面的完整历史 bundle 不修改分支或安全门禁：

```powershell
$repo = (git rev-parse --show-toplevel).Trim()
$snapshot = Join-Path $repo ('tmp\master-bundle-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $snapshot -ErrorAction Stop | Out-Null
$bundle = Join-Path $snapshot 'smart-assistant-master.bundle'
git bundle create $bundle refs/heads/master
if ($LASTEXITCODE -ne 0) { throw 'Bundle creation failed' }
git bundle verify $bundle
if ($LASTEXITCODE -ne 0) { throw 'Bundle verification failed' }
git bundle list-heads $bundle
$recovery = Join-Path $snapshot 'restored'
git clone --branch master $bundle $recovery
if ($LASTEXITCODE -ne 0) { throw 'Bundle clone failed' }
git -C $recovery rev-parse HEAD
git -C $recovery status --short
```

只有 `bundle list-heads` 与目标提交匹配，且独立恢复核验成功，才能称为对应版本的恢复备份。Git bundle 不包含未跟踪的用户数据、日志、安装器或 Git LFS 对象；这些对象须按已有交付记录单独核对。

## 制品与证据边界

当前保留的 RC2 安装器为 `C:\A05-Acceptance\Transfer\SmartAssistant-Setup-2.1.3-rc.2-x64.exe`，SHA256 `6D82F4E6CA43A0B354D2065F8362F4B170AC7A9B987C2FA69581E3DE34C7BDF3`。解包检查证明其中已包含白屏修复；它来自当时含未提交修复的构建工作树，不能据此宣称由后续 master SHA 构建。

恢复或合并后，使用仓库现有 manifest 生成器重新绑定当前干净 HEAD，并独立验证。manifest 是忽略的生成制品，不能提交到其证明干净的同一源码树。外部发布门禁未通过时保留真实失败状态。

保留全部安全门禁；发生提交或验证拒绝时记录真实原因，不关闭、跳过或绕过门禁。不使用 `git add -A`、强制推送或历史补丁覆盖当前主树作为恢复捷径。
