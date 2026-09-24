# SmartAssistant

SmartAssistant 是一个可在桌面、Web 和即时通信渠道中运行的个人 AI Agent。它提供任务规划、工具调用、长期记忆、知识库、Skill、MCP 和多模型接入能力。

当前项目由两部分组成：

- Python 后端：负责 Agent、渠道、模型、工具、记忆、知识库和持久化。
- Electron 桌面端：负责桌面 UI、Python 后端生命周期、受限 IPC 和本地文件交互。

## 主要能力

- 多步骤 Agent 任务规划和工具执行
- 文件、终端、浏览器、搜索、调度器、记忆和知识库工具
- Markdown 知识库与长期记忆
- Skill 和 MCP 扩展
- Web 控制台和 Electron 桌面客户端
- 文本、图片、文件、语音输入与输出
- 多模型供应商和 OpenAI 兼容自定义模型
- Web、微信、企业微信、飞书、钉钉、Telegram、Slack、Discord、QQ 等渠道

模型推荐列表由后端集中维护。部分供应商支持通过模型列表 API 动态发现；用户填写的自定义模型 ID 不受推荐列表限制。模型目录只说明供应商返回的 ID，不能单独证明该模型支持当前 Agent 的工具、视觉或推理参数。

## 架构

```text
Electron Renderer / Web Console / IM Channel
                    │
                    ▼
              WebChannel API
                    │
                    ▼
        AgentBridge + ConversationStore
          │          │           │
          ▼          ▼           ▼
       Models      Tools      Memory / Knowledge
```

桌面端由 Electron 主进程启动 Python 后端。Renderer 通过 preload 暴露的受限 API 请求本地后端；生产桌面端使用绑定启动信息和证书校验保护这条本地控制通道。打包后的用户数据默认位于 Windows 的 `%USERPROFILE%\.cow`，源码运行时默认使用项目运行目录，也可以通过 `COW_DATA_DIR` 指定数据目录。

## 从源码运行后端

建议使用 Python 3.10 或更高版本和虚拟环境。项目的部分依赖会随 Python 版本和平台变化，请以 `requirements.txt` 为准。

Windows PowerShell：

```powershell
py -m venv .venv
.\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt
Copy-Item config-template.json config.json
# 编辑 config.json，至少填写一个模型供应商的 API key
.\\.venv\\Scripts\\python.exe app.py
```

Linux 或 macOS：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp config-template.json config.json
# 编辑 config.json，至少填写一个模型供应商的 API key
python app.py
```

默认 Web 控制台地址为：

```text
http://127.0.0.1:9899/chat
```

`config.json` 中常用设置包括：

- `channel_type`：启动的渠道，可填写单个字符串、逗号分隔字符串或数组。
- `web_host`、`web_port`：Web 控制台监听地址和端口。
- `model`：当前模型 ID。
- 各供应商的 `*_api_key` 和 `*_api_base`：供应商凭据与 API 地址。
- `agent`：是否启用 Agent 模式。
- `agent_workspace`：Agent 工作空间目录。
- `web_password`：Web 控制台密码。

不要把 `config.json`、API key、运行日志或用户数据提交到 Git。对外提供 Web 访问时，应使用 TLS 反向代理和认证，不要直接暴露内置 HTTP 服务。

## 桌面端开发

桌面端要求 Node.js 18 或更高版本。

```powershell
cd desktop
npm install
npm run dev
```

`npm run dev` 会构建 Renderer 和 Electron 主进程，并启动桌面应用。开发模式下 Electron 会启动当前源码中的 Python 后端。

单独运行前端或主进程：

```powershell
npm run dev:renderer
npm run dev:main
```

常用构建命令：

```powershell
npm run build       # 构建 Renderer 和 Electron 主进程
npm run dist        # 按当前平台生成制品
npm run dist:win    # 生成 Windows x64 NSIS 制品
npm run dist:mac    # 生成 macOS 制品
```

Windows 构建使用 `electron-builder.win.js`。代码签名由构建环境凭据控制，仓库中不保存证书或密钥。

## 目录结构

```text
agent/       Agent 协议、工具、记忆和知识库
bridge/      Agent 与渠道之间的桥接层
channel/     Web、微信、飞书、钉钉等渠道
common/      常量、日志、通用工具和国际化
models/      各供应商模型实现与模型目录发现
plugins/     插件实现
skills/      内置 Skill 和 Skill 说明
desktop/     Electron + React + TypeScript 桌面客户端
tests/       Python 测试
docs/        项目文档与审计报告
```

模型目录发现入口是 [`models/catalog.py`](models/catalog.py)，供应商推荐表和模型配置 API 位于 [`channel/web/web_channel.py`](channel/web/web_channel.py)。桌面端模型设置位于 [`desktop/src/renderer/src/pages/settings`](desktop/src/renderer/src/pages/settings)。

## 测试与检查

Python 测试：

```powershell
.\\.venv\\Scripts\\python.exe -m pytest
```

桌面端类型检查和构建：

```powershell
cd desktop
node node_modules/typescript/bin/tsc --noEmit -p tsconfig.json
npm run build
```

涉及真实 Electron、Python 后端或正式制品的测试，应使用独立的 profile、数据目录和临时资源。Mock 或本地故障注入只能证明错误处理和本地状态转换，不能证明真实供应商模型调用成功。

## 渠道与会话边界

每个渠道保留自己的 session 和身份边界。微信消息会写入 ConversationStore，但桌面端是否显示某个微信用户的记录，需要明确的微信身份与桌面用户绑定及授权规则。当前项目没有通过删除 channel 或 owner 过滤来解决跨渠道可见性问题。

后续如果启用跨渠道任务交付，建议以任务、交付物和任务事件为关联对象，通过微信发送完成通知、文档预览和修改入口；这与全量合并聊天历史是不同的功能。

## 文档

- [`docs/README.md`](docs/README.md)：本地文档站说明
- [`desktop/README.md`](desktop/README.md)：桌面端开发说明
- [`channel/web/README.md`](channel/web/README.md)：Web 渠道说明
- [`CONTRIBUTING.md`](CONTRIBUTING.md)：贡献指南
- [`docs/audits/`](docs/audits/)：当前项目的审计与正式制品验证记录

## 许可证

本项目使用 MIT License，详见 [`LICENSE`](LICENSE)。使用 Agent 工具访问本地文件、终端、浏览器或外部服务时，请确认运行环境、模型凭据和数据权限符合你的安全要求。
