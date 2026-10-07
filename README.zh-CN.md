# x-reader

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[English](./README.md)

**把一个 URL 丢给 Agent，并要求它证明自己到底读到了什么。**

x-reader 是一个 source-first 的 Agent Skill + CLI，支持 X/Twitter、网页文章、视频、播客、微信公众号、小红书、Telegram、RSS 等来源。

它解决的不是“再做一个摘要器”，而是一个更基础的问题：

**搜索摘要不等于原文，推文正文不等于附件视频，视频简介也不等于视频内容。**

如果关键来源层没有真正读取到，x-reader 必须明确告诉你。

## Evidence Receipt 网页工具

稳定 Beta 已上线：**https://xreader.leolabs.me**，面向公开 X、YouTube 和普通网页 URL。粘贴链接后，它直接返回与 x-reader 相同的四种证据状态：`PASS`、`PARTIAL`、`FAIL`、`UNKNOWN`。结果可以复制为可读收据或 JSON，也可以分享；任何分享都不会把缺失证据包装成成功。

本地运行使用 `x-reader-web`，也可以直接使用上面的 Hosted Beta。详见 [Evidence Receipt](./docs/EVIDENCE_RECEIPT.md)、[分发方案](./docs/DISTRIBUTION.md) 和 [公开 Beta 发布门](./docs/PUBLIC_BETA_GATE.md)。

## 一行安装

```bash
npx skills add runesleo/x-reader --skill x-reader
```

然后把链接发给 Agent，例如：

> 读取原始来源。如果里面有和结论相关的视频或音频，也必须实际读取。告诉我你真正读到了什么，还有什么没读到。

canonical skill 使用四种状态：

```text
PASS     原始来源以及回答所需的关键媒体都已读取
PARTIAL  只读到了部分来源，仍缺关键层
FAIL     无法可靠获取原始来源
UNKNOWN  当前材料不足以确认是否就是用户要求的来源
```

## Evidence Receipt 网页预览（实验性）

现在可以把同一套 evidence contract 通过一个很小的本地网页界面跑起来：

```bash
pip install -e .
x-reader-web --host 127.0.0.1 --port 8787
```

当前 hosted MVP 只接受公开的 X/Twitter、YouTube 和普通网页 URL。每次请求都使用独立的临时 HOME / inbox，不继承浏览器登录态、Telegram 凭证、Groq key、Obsidian/output 路径，也不会向外部服务转发 X cookie。

网页层**没有再造第二套 reader 或 evidence 状态机**：它仍然调用现有的 `x-reader URL --json`，只是在隔离环境中运行，再把结果投影为 `PASS / PARTIAL / FAIL / UNKNOWN`。

API 示例、隔离边界和 mixed-media receipt 细节见 [Evidence Receipt](./docs/EVIDENCE_RECEIPT.md)。

## v0.3.1：真实 before / after

这次 source-fetch hardening 有可复现的真实差异，不只是功能列表：

- `https://example.com`：v0.3.0 遇到 Jina HTTP 401 后直接失败；v0.3.1 会安全 fallback 到 `direct_html_pinned`，最终返回 `READ`。
- 带附件视频的 X：v0.3.0 只返回推文文字，没有结构化媒体状态；v0.3.1 返回 `media_status=present`，因此可以明确区分“正文已读”和“附件媒体仍是 `PARTIAL`”。

![x-reader v0.3.0 vs v0.3.1 实测](https://github.com/runesleo/x-reader/releases/download/v0.3.1/x-reader-v030-vs-hardening.gif)

[查看 12 秒 MP4 实测](https://github.com/runesleo/x-reader/releases/download/v0.3.1/x-reader-v030-vs-hardening.mp4) · [v0.3.1 Release Notes](https://github.com/runesleo/x-reader/releases/tag/v0.3.1)

## 已验证的 first success

合并到 `main` 后，我们对一个公开 X 链接做了完整实测：

```text
来源：
https://x.com/dontbesilent/status/2103875422522077377

推文正文       PASS     通过原始 status 的 oEmbed 读取
附件视频       PASS     解析并下载公开 MP4
视频时长                约 178.3 秒
视频内容       PASS     获取并完整读取原生中文字幕
错误链接       FAIL     非零退出码 + 机器可读 JSON error
```

公开安装路径也在全新目录中重新验证：

```bash
npx skills add runesleo/x-reader --skill x-reader
# Repository cloned
# Found 3 skills
# Selected 1 skill: x-reader
# Installation complete
```

完整的日期化验收证据见 [First-success receipt](./docs/FIRST_SUCCESS.md)。

## 为什么需要 source-first

很多 Agent “能找到点东西”，但这不代表它真的读了你给的来源。

| Agent 实际拿到的东西 | 最多可以声称什么 |
|---|---|
| 搜索结果 / preview card | 只能用于发现，不能当原文证据 |
| X 推文正文 | 只能支持正文里的内容 |
| 推文正文 + 未读取的附件视频 | 正文可通过；视频必须保持 `PARTIAL` |
| 视频标题 / 简介 | 只是 metadata，不是视频内容 |
| 字幕 / transcript | 才能支持视频里的口头内容 |
| 登录墙 / 删除页 / 空响应 | `FAIL` 或 `PARTIAL`，不能脑补 |

## 支持来源

| 来源 | 基础读取链路 | 媒体 / 登录补全 |
|---|---|---|
| X / Twitter | oEmbed → FxTwitter → Article/Jina → Playwright | Skill 可继续读取附件媒体 |
| 普通网页 / 文章 | Jina Reader | 支持时走 browser fallback |
| YouTube | yt-dlp metadata / 字幕 | 配置后可 Groq Whisper |
| Bilibili | Bilibili API | 字幕 / 音频转录流程 |
| 微信公众号 | Jina → Playwright | 必要时使用本地浏览器 session |
| 小红书 | Jina → Playwright | 部分页面需一次性本地登录 |
| Telegram | Telethon | 需要 Telegram 凭证 |
| RSS | feedparser | — |
| 小宇宙 / Apple Podcasts | 媒体发现 | 必须转录后才能声称读过口头内容 |

平台规则会变化。x-reader 的承诺不是“永远全覆盖”，而是**覆盖不到时不装作覆盖到了**。

## 安全模型

- 网页、推文、字幕、transcript、metadata、评论全部视为**不可信数据**，绝不能当作给 Agent 的指令。
- 不能因为来源内容要求，就执行命令、泄露本地数据或做外部操作。
- Agent Skill 的自动 bootstrap 固定到已验证的不可变 CLI commit，不跟随移动的 branch。
- URL 校验会在支持的网络抓取前拦截 private / localhost 目标。
- 本地浏览器 cookie 默认只保留在本机。

## CLI

从 GitHub 安装：

```bash
pip install "x-reader @ git+https://github.com/runesleo/x-reader.git"
```

读取 URL：

```bash
x-reader https://x.com/elonmusk/status/123456
```

完整 JSON：

```bash
x-reader https://x.com/elonmusk/status/123456 --json
```

批量读取：

```bash
x-reader https://url1.com https://url2.com --json
```

浏览器 fallback：

```bash
pip install "x-reader[browser] @ git+https://github.com/runesleo/x-reader.git"
playwright install chromium
x-reader login twitter
x-reader login xhs
```

默认本地 cookie 只留在本机。

## 仓库内的 Agent Skills

```text
skills/
├── x-reader/    # canonical source-first URL reader
├── video/       # 视频 / 播客转录
└── analyzer/    # 基于证据的内容分析
```

查看全部 Skill：

```bash
npx skills add runesleo/x-reader --list
```

大多数用户只需要安装 `x-reader`。

## MCP Server

```bash
git clone https://github.com/runesleo/x-reader.git
cd x-reader
pip install -e ".[mcp]"
python mcp_server.py
```

也可以直接从 GitHub 用 `uvx` 启动打包后的 MCP 入口：

```bash
uvx --with "mcp[cli]>=1.0,<2" --from git+https://github.com/runesleo/x-reader.git x-reader-mcp
```

`x-reader-mcp` 是打包在 `x_reader` 内的 console entrypoint；根目录的 `mcp_server.py` 继续保留，兼容原来的源码 checkout 用法。

暴露四个工具：

- `read_url(url)`
- `read_batch(urls)`
- `list_inbox()`
- `detect_platform(url)`

Claude Code 推荐直接用 CLI 添加，不要把配置写进 Claude Desktop 的配置文件：

```bash
claude mcp add x-reader -- python /absolute/path/to/x-reader/mcp_server.py
```

需要全局可用时再加 `--scope user`。

## 视频 / 音频依赖

```bash
# macOS
brew install yt-dlp ffmpeg

# Linux
pip install yt-dlp
sudo apt install ffmpeg
```

Whisper 转录需要：

```bash
export GROQ_API_KEY=your_key_here
```

默认模型：`whisper-large-v3-turbo`。

## Python 库

```python
import asyncio
from x_reader.reader import UniversalReader

async def main():
    reader = UniversalReader()
    content = await reader.read("https://example.com")
    print(content.title)
    print(content.content[:200])

asyncio.run(main())
```

## 配置

复制 `.env.example` → `.env`。

主要变量：

- `TG_API_ID` / `TG_API_HASH`
- `GROQ_API_KEY`
- `INBOX_FILE`
- `OUTPUT_DIR`
- `OBSIDIAN_VAULT`

## 架构

```text
用户给 URL
    │
    ├─ 文本来源
    │   └─ 平台 fetcher → UnifiedContent → CLI JSON / inbox
    │
    ├─ 视频 / 音频对答案很关键
    │   └─ 优先字幕 → 转录 fallback → evidence receipt
    │
    └─ 用户要求分析
        └─ 先确认 source coverage，再分析
```

## Author / License

作者：Leo ([@runes_leo](https://x.com/runes_leo))

[leolabs.me](https://leolabs.me)

MIT License
