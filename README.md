<p align="center"><img src="https://github.com/user-attachments/assets/eca9a9ec-8534-4615-9e0f-96c5ac1d10a3" alt="SmartAssistant" width="420" /></p>

<p align="center">
  <a href="https://github.com/zhayujie/SmartAssistant/releases/latest"><img src="https://img.shields.io/github/v/release/zhayujie/SmartAssistant?cacheSeconds=3600" alt="Latest release"></a>
  <a href="https://github.com/zhayujie/SmartAssistant/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"></a>
  <a href="https://github.com/zhayujie/SmartAssistant"><img src="https://img.shields.io/github/stars/zhayujie/SmartAssistant?style=flat-square&cacheSeconds=3600" alt="Stars"></a>
  <a href="https://docs.smart_assistant.ai/"><img src="https://img.shields.io/badge/Docs-smart_assistant.ai-blue?style=flat&logo=readthedocs&logoColor=white" alt="Docs"></a>
</p>

<p align="center">
  <a href="https://trendshift.io/repositories/25763" target="_blank"><img src="https://trendshift.io/api/badge/repositories/25763" alt="zhayujie%2FSmartAssistant | Trendshift" style="width: 250px; height: 55px;" width="250" height="55"/></a>
</p>

<p align="center">
  [English] | [<a href="docs/zh/README.md">中文</a>] | [<a href="docs/zh/README-Hant.md">繁體中文</a>] | [<a href="docs/ja/README.md">日本語</a>]
</p>

**SmartAssistant** is an open-source super AI assistant that proactively plans tasks, controls your computer and external services, creates and runs Skills, builds a personal knowledge base and long-term memory, and grows alongside you through self-evolution — a reference implementation of Agent Harness engineering.

SmartAssistant is lightweight, easy to deploy, and built to extend. Plug in any major LLM provider and run it 24/7 on a personal computer or server, across the web and all major IM platforms.

<p align="center">
  <a href="https://smart_assistant.ai/">🌐 Website</a> &nbsp;·&nbsp;
  <a href="https://docs.smart_assistant.ai/intro/index">📖 Docs</a> &nbsp;·&nbsp;
  <a href="https://docs.smart_assistant.ai/guide/quick-start">🚀 Quick Start</a> &nbsp;·&nbsp;
  <a href="https://skills.smart_assistant.ai/">🧩 Skill Hub</a> &nbsp;·&nbsp;
  <a href="https://smart_assistant.ai/download/">💻 Download</a> &nbsp;·&nbsp;
  <a href="https://link-ai.tech/smart_assistant/create">☁️ Try Online</a>
</p>

<br/>

## 🌟 Highlights

| Capability | Description |
| :--- | :--- |
| [Planning](https://docs.smart_assistant.ai/intro/architecture) | Decomposes complex tasks and executes them step by step, looping over tools until the goal is reached |
| [Memory](https://docs.smart_assistant.ai/memory/index) | Three-tier architecture (context → daily → core), automatic Deep Dream distillation, hybrid keyword + vector retrieval |
| [Knowledge](https://docs.smart_assistant.ai/knowledge/index) | Auto-curates structured knowledge into a Markdown wiki, builds an evolving knowledge graph with visual browsing |
| [Evolution](https://docs.smart_assistant.ai/memory/self-evolution) | Self-Evolution reviews conversations automatically to improve skills, follow up on unfinished tasks, and consolidate memory and knowledge, growing through everyday use |
| [Skills](https://docs.smart_assistant.ai/skills/index) | One-click install from [Skill Hub](https://skills.smart_assistant.ai/), GitHub, ClawHub; or create custom skills via natural-language conversation |
| [Tools](https://docs.smart_assistant.ai/tools/index) | Built-in file I/O, terminal, browser, scheduler, memory retrieval, web search, and 10+ more tools — with native MCP integration |
| [Channels](https://docs.smart_assistant.ai/channels/index) | Integrates with Web, WeChat, Feishu, DingTalk, WeCom, QQ, Official Accounts, Telegram, and Slack |
| Multimodal | First-class support for text, images, voice, and files — recognition, generation, and delivery |
| [Models](https://docs.smart_assistant.ai/models/index) | Claude, GPT, Gemini, DeepSeek, Qwen, GLM, Kimi, MiniMax, Doubao, and more — swap providers from the Web console with one click |
| [Deploy](https://docs.smart_assistant.ai/guide/quick-start) | One-line installer, unified Web console, multiple deployment modes (local, Docker, server) |

<br/>

## 🏗️ Architecture

<img src="https://cdn.jsdelivr.net/gh/zhayujie/smart-assistant-assets@main/architecture/en/architecture.png" alt="SmartAssistant Architecture" width="750"/>

SmartAssistant is a complete **Agent Harness**: messages flow in through **Channels**; the **Agent Core** plans and reasons over memory, knowledge, and the available tools and skills; **Models** generate the response, which is sent back through the originating channel. Every layer is decoupled and independently extensible.

Read more in [Architecture](https://docs.smart_assistant.ai/intro/architecture).

<br/>

## 🚀 Quick Start

A one-line installer takes care of dependencies, configuration, and startup:

**Linux / macOS:**

```bash
bash <(curl -fsSL https://cdn.link-ai.tech/code/cow/run.sh)
```

**Windows (PowerShell):**

```powershell
irm https://cdn.link-ai.tech/code/cow/run.ps1 | iex
```

**Docker:**

```bash
curl -O https://cdn.link-ai.tech/code/cow/docker-compose.yml
docker compose up -d
```

Once started, open `http://localhost:9899` to access the **Web console** — your one-stop hub to chat with the Agent, configure models, connect channels, and install skills.

> Deploying on a server? The embedded console is forced to `127.0.0.1`. Expose it only through a TLS reverse proxy that forwards to `127.0.0.1:9899`, and set `web_password` as defense in depth; do not publish the embedded HTTP port directly.

> 📖 Detailed guides: [Quick Start](https://docs.smart_assistant.ai/guide/quick-start) · [Install from Source](https://docs.smart_assistant.ai/guide/manual-install) · [Upgrade](https://docs.smart_assistant.ai/guide/upgrade)

After installation, manage the service with the [cow CLI](https://docs.smart_assistant.ai/cli/index):

```bash
cow start | stop | restart        # service control
cow status | logs                  # status and logs
cow update                         # pull latest code and restart
cow skill install <name>           # install a skill
cow install-browser                # install browser automation
```

> 💻 Desktop client: download the **[SmartAssistant Desktop client](https://smart_assistant.ai/download/)** (macOS / Windows) — the backend is bundled, ready to use out of the box.

<br/>

## 🤖 Models

SmartAssistant supports all mainstream LLM providers. **Chat, vision, image generation, ASR/TTS, and embeddings** can each be routed to a different vendor. Providers are configured directly in the Web console — no manual file editing required.

| Provider | Featured Models | Chat | Vision | Image Gen | ASR | TTS | Embedding |
| --- | --- | :-: | :-: | :-: | :-: | :-: | :-: |
| [Claude](https://docs.smart_assistant.ai/models/claude) | claude-opus-5 / sonnet-5 | ✅ | ✅ | | | | |
| [OpenAI](https://docs.smart_assistant.ai/models/openai) | gpt-5.6 series | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| [Gemini](https://docs.smart_assistant.ai/models/gemini) | gemini-3.5-flash | ✅ | ✅ | ✅ | | | |
| [DeepSeek](https://docs.smart_assistant.ai/models/deepseek) | deepseek-v4-flash / pro | ✅ | | | | | |
| [Qwen](https://docs.smart_assistant.ai/models/qwen) | qwen3.7-plus | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| [GLM](https://docs.smart_assistant.ai/models/glm) | glm-5.2, glm-5v-turbo | ✅ | ✅ | | ✅ | | ✅ |
| [Doubao](https://docs.smart_assistant.ai/models/doubao) | doubao-seed-2.1 series | ✅ | ✅ | ✅ | | | ✅ |
| [Kimi](https://docs.smart_assistant.ai/models/kimi) | kimi-k3 | ✅ | ✅ | | | | |
| [MiniMax](https://docs.smart_assistant.ai/models/minimax) | MiniMax-M3 | ✅ | ✅ | ✅ | | ✅ | |
| [ERNIE](https://docs.smart_assistant.ai/models/qianfan) | ernie-5.1 | ✅ | ✅ | | | | |
| [MiMo](https://docs.smart_assistant.ai/models/mimo) | mimo-v2.5 / pro | ✅ | ✅ | | | ✅ | |
| [LinkAI](https://docs.smart_assistant.ai/models/linkai) | One key for 100+ models | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| [Custom](https://docs.smart_assistant.ai/models/custom) | Local models / third-party proxy | ✅ | | | | | |

> For details on each provider, see the [Models overview](https://docs.smart_assistant.ai/models/index).

<br/>

## 💬 Channels

A single Agent instance can serve multiple channels in parallel. Most channels can be onboarded right from the Web console.

| Channel | Text | Image | File | Voice | Group |
| --- | :-: | :-: | :-: | :-: | :-: |
| [Web Console](https://docs.smart_assistant.ai/channels/web) (default) | ✅ | ✅ | ✅ | ✅ | |
| [Telegram](https://docs.smart_assistant.ai/channels/telegram) | ✅ | ✅ | ✅ | ✅ | ✅ |
| [Slack](https://docs.smart_assistant.ai/channels/slack) | ✅ | ✅ | ✅ | | ✅ |
| [Discord](https://docs.smart_assistant.ai/channels/discord) | ✅ | ✅ | ✅ | | ✅ |
| [WeChat](https://docs.smart_assistant.ai/channels/weixin) | ✅ | ✅ | ✅ | ✅ | |
| [Feishu / Lark](https://docs.smart_assistant.ai/channels/feishu) | ✅ | ✅ | ✅ | ✅ | ✅ |
| [DingTalk](https://docs.smart_assistant.ai/channels/dingtalk) | ✅ | ✅ | ✅ | ✅ | ✅ |
| [WeCom Bot](https://docs.smart_assistant.ai/channels/wecom-bot) | ✅ | ✅ | ✅ | ✅ | ✅ |
| [QQ](https://docs.smart_assistant.ai/channels/qq) | ✅ | ✅ | ✅ | | ✅ |
| [WeCom App](https://docs.smart_assistant.ai/channels/wecom) | ✅ | ✅ | ✅ | ✅ | |
| [WeChat Customer Service](https://docs.smart_assistant.ai/channels/wechat-kf) | ✅ | ✅ | ✅ | ✅ | |
| [WeChat Official Account](https://docs.smart_assistant.ai/channels/wechatmp) | ✅ | ✅ | | ✅ | |

> See the [Channels overview](https://docs.smart_assistant.ai/channels/index) for setup details.

<img src="https://cdn.jsdelivr.net/gh/zhayujie/smart-assistant-assets@main/screenshots/en/web-console-chat.png" alt="SmartAssistant Web Console" width="800"/>

*The Web console is the default channel and the unified entry point to configure models, channels, skills, memory, and more.*

<br/>

## 🧠 Memory & Knowledge Base

**Long-term memory** uses a three-tier architecture: conversation context (short-term) → daily memory (mid-term) → MEMORY.md (long-term). A nightly **Deep Dream** pass distills scattered memories into refined long-term entries and a narrative journal. See [Long-term Memory](https://docs.smart_assistant.ai/memory/index) · [Deep Dream](https://docs.smart_assistant.ai/memory/deep-dream).

**Personal knowledge base** complements the time-ordered memory by organizing structured knowledge **by topic**. The Agent automatically curates valuable information from conversations, maintains cross-references and indexes, and the Web console offers an interactive knowledge-graph view. See [Personal Knowledge Base](https://docs.smart_assistant.ai/knowledge/index).

<table>
  <tr>
    <td width="50%">
      <img src="https://cdn.jsdelivr.net/gh/zhayujie/smart-assistant-assets@main/screenshots/en/web-console-memory.png" alt="Long-term Memory" />
      <p align="center"><em>Long-term Memory · Three-tier architecture + Deep Dream</em></p>
    </td>
    <td width="50%">
      <img src="https://cdn.jsdelivr.net/gh/zhayujie/smart-assistant-assets@main/screenshots/en/web-console-knowledge.png" alt="Personal Knowledge Base" />
      <p align="center"><em>Knowledge Base · Auto-curated Markdown wiki</em></p>
    </td>
  </tr>
</table>

<br/>

## 🔧 Tools & Skills

**Tools** are atomic capabilities the Agent uses to interact with system resources. **Skills** are higher-level workflows defined by a manifest file that compose multiple tools to accomplish complex tasks.

### Tool System

**Built-in tools** cover file I/O (`read` / `write` / `edit` / `ls`), terminal (`bash`), file sending (`send`), memory retrieval (`memory`), environment variables (`env_config`), web fetching (`web_fetch`), scheduling (`scheduler`), web search (`web_search`), vision (`vision`), and browser automation (`browser`).

**MCP protocol** integrates the open ecosystem of [Model Context Protocol](https://modelcontextprotocol.io) servers. A single `mcp.json` is enough — supports stdio / SSE transports, hot reload, and zero-code integration.

Learn more: [Tools overview](https://docs.smart_assistant.ai/tools/index) · [MCP integration](https://docs.smart_assistant.ai/tools/mcp).

### Skills System

- **[Skill Hub](https://skills.smart_assistant.ai/)** — open skill marketplace: browse, search, install in one click
- **GitHub / ClawHub / URL and more** — install skills from any source
- **Conversational authoring** — generate custom skills through dialogue with `skill-creator`; turn any workflow or third-party API into a reusable skill

```bash
/skill list                   # list installed skills
/skill search <keyword>        # search the marketplace
/skill install <name>          # one-click install
```

Learn more: [Skills overview](https://docs.smart_assistant.ai/skills/index) · [Creating Skills](https://docs.smart_assistant.ai/skills/create).

<br/>

## 🏷 Changelog

> **2026.07.20:** [v2.1.4](https://github.com/zhayujie/SmartAssistant/releases/tag/2.1.4) — Desktop experience improvements, MCP OAuth authorization, Lark channel enhancements, scheduler improvements and data backup, new models.

> **2026.07.08:** [v2.1.3](https://github.com/zhayujie/SmartAssistant/releases/tag/2.1.3) — [Desktop client](https://smart_assistant.ai/download/) for macOS / Windows, knowledge base document management, on-demand MCP tool retrieval, Traditional Chinese support, new models.

> **2026.06.18:** [v2.1.2](https://github.com/zhayujie/SmartAssistant/releases/tag/2.1.2) — Web console upgrades (scheduled task management, knowledge base categories, multiple custom model providers), Self-Evolution improvements, new models (kimi-k2.7-code, glm-5.2), security hardening and refinements.

> **2026.06.09:** [v2.1.1](https://github.com/zhayujie/SmartAssistant/releases/tag/2.1.1) — Self-Evolution, Web console upgrades (message management, parallel sessions), cross-platform MCP enhancements with concurrent calls, new models (MiniMax-M3, qwen3.7-plus), Python 3.13 support.

> **2026.06.01:** [v2.1.0](https://github.com/zhayujie/SmartAssistant/releases/tag/2.1.0) — Internationalization, new channels (Telegram, Discord, Slack, WeChat Customer Service), CLI interaction upgrades, streamlined one-line install, MCP Streamable HTTP support, new models (claude-opus-4-8, MiMo).

> **2026.05.22:** [v2.0.9](https://github.com/zhayujie/SmartAssistant/releases/tag/2.0.9) — Model management, MCP protocol support, persistent browser sessions, new models (gpt-5.5, gemini-3.5-flash, qwen3.7-max), deployment hardening.

> **2026.05.06:** [v2.0.8](https://github.com/zhayujie/SmartAssistant/releases/tag/2.0.8) — Feishu channel overhaul (voice, streaming, QR onboarding), DeepSeek V4 and Baidu Qianfan support, scheduler tool upgrades.

> **2026.04.22:** [v2.0.7](https://github.com/zhayujie/SmartAssistant/releases/tag/2.0.7) — Built-in image generation (GPT Image 2, Nano Banana), new models (Kimi K2.6, Claude Opus 4.7, GLM 5.1), memory and knowledge enhancements.

> **2026.04.14:** [v2.0.6](https://github.com/zhayujie/SmartAssistant/releases/tag/2.0.6) — Knowledge base, Deep Dream memory distillation, smart context compression, multi-session Web console.

> **2026.04.01:** [v2.0.5](https://github.com/zhayujie/SmartAssistant/releases/tag/2.0.5) — Cow CLI, Skill Hub open source, browser tool, WeCom Bot QR onboarding.

> **2026.02.03:** [v2.0.0](https://github.com/zhayujie/SmartAssistant/releases/tag/2.0.0) — Major upgrade to a super Agent assistant with multi-step task planning, long-term memory, and the Skills framework.

Full history: [Release Notes](https://docs.smart_assistant.ai/releases/overview)

<br/>

## 🤝 Community & Support

[File an issue](https://github.com/zhayujie/SmartAssistant/issues) on GitHub, or scan the QR code below to join our WeChat community:

<img width="130" src="https://img-1317903499.cos.ap-guangzhou.myqcloud.com/docs/open-community.png">

<br/>

## 🔗 Related Projects

- **[Cow Skill Hub](https://github.com/zhayujie/cow-skill-hub)** — open skill marketplace for AI Agents; works with SmartAssistant, OpenClaw, Claude Code, and more
- **[bot-on-anything](https://github.com/zhayujie/bot-on-anything)** — lightweight LLM application framework with integrations for Slack, Telegram, Discord, Gmail, and more
- **[AgentMesh](https://github.com/MinimalFuture/AgentMesh)** — open-source multi-agent framework for solving complex problems through team collaboration

<br/>

## 🏢 Enterprise Services

[**LinkAI**](https://link-ai.tech/) is an all-in-one AI Agent platform for enterprises and developers, offering managed hosting and enterprise-grade support for SmartAssistant:

- **🚀 Zero-deployment hosted runtime** — spin up a [SmartAssistant online assistant](https://link-ai.tech/smart_assistant/create) in under a minute, no server required
- **🧠 Agent infrastructure** — unified access to LLMs, knowledge bases, databases, skills, and workflows; plug-and-play building blocks that extend what SmartAssistant can do
- **🏢 Team & enterprise features** — workspaces, role-based access, audit logs, and private deployment for production use cases

For enterprise inquiries: sales@simple-future.tech or [scan the QR code](https://cdn.link-ai.tech/consultant.jpg) to reach our team on WeChat.

<br/>

## 🛠️ Development & Contributing

All kinds of contributions are welcome — new features, bug fixes, performance improvements, docs, or sharing your own skills on the [Skill Hub](https://skills.smart_assistant.ai/submit). See [CONTRIBUTING.md](/CONTRIBUTING.md) to get started, then open an Issue to discuss or send a PR directly.

⭐ Star the project to show your support, and Watch → Custom → Releases to get notified of new versions. PRs and Issues are always welcome.

## 🌟 Contributors

![cow contributors](https://contrib.rocks/image?repo=zhayujie/SmartAssistant&max=1000)

<br/>

## ⚠️ Disclaimer

1. This project is licensed under the [MIT License](/LICENSE) and is intended for technical research and learning. You are responsible for complying with applicable laws and regulations in your jurisdiction; the maintainers assume no liability for any consequences arising from use of this project.
2. **Cost & safety:** Agent mode consumes substantially more tokens than regular chat — pick models that balance quality and cost. The Agent has access to your local operating system, so only deploy it in trusted environments.
3. SmartAssistant is a pure open-source project and does not participate in, authorize, or issue any cryptocurrency.

<br/>

## 📌 Project Renaming Notice

This project was previously named `chatgpt-on-wechat` and is now officially **SmartAssistant**. The old GitHub URL redirects automatically; existing users may optionally run `git remote set-url origin https://github.com/zhayujie/SmartAssistant.git` to update the local remote.
