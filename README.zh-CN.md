# x-reader

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[English](./README.md)

通用内容阅读器：给定 URL（文章、视频、播客、推文），返回结构化内容。可作为 **CLI**、**Python 库**、**MCP 服务** 或 **Claude Code Skills** 使用。

## 能力概览

```
任意 URL → 平台识别 → 抓取内容 → 统一输出
                ↓              ↓
           自动识别        文本：Jina Reader
          7+ 平台         视频：yt-dlp 字幕
                          音频：Whisper 转写
                          API：Bilibili / RSS / Telegram
```

Python 层负责文本抓取与 YouTube 字幕；可选的 **Claude Code skills** 为视频/播客提供完整 Whisper 转写与 AI 分析。

## 三层架构

| 层级 | 作用 | 安装 |
|------|------|------|
| **Python CLI/库** | 基础抓取 + 统一 schema | 必需，见 [安装](#安装) |
| **Claude Code Skills** | 视频转写 + 内容分析 | 可选，复制 `skills/` |
| **MCP Server** | 将阅读能力暴露为 MCP 工具 | 可选，`python mcp_server.py` |

### CLI 示例

```bash
x-reader https://mp.weixin.qq.com/s/abc123
x-reader https://x.com/elonmusk/status/123456
x-reader login xhs
x-reader list
```

### Skills 目录

```
skills/
├── video/       # YouTube/Bilibili/播客 → Whisper 全文转写
└── analyzer/    # 任意内容 → 结构化分析报告
```

### MCP

```bash
git clone https://github.com/runesleo/x-reader.git
cd x-reader
pip install -e ".[mcp]"
python mcp_server.py
```

工具包括：`read_url`、`read_batch`、`list_inbox`、`detect_platform`。Claude Desktop 配置示例见英文 [README](./README.md) 中 JSON 片段。

## 支持平台（节选）

| 平台 | 文本 | 视频/音频稿 |
|------|------|-------------|
| YouTube | ✅ | ✅ 字幕 / Groq Whisper |
| Bilibili | ✅ API | ✅（经 Skill） |
| X / Twitter | ✅ | — |
| 微信公众号 | ✅ | — |
| 小红书 | ✅（需登录） | — |
| Telegram | ✅ Telethon | — |
| RSS | ✅ | — |

> YouTube Whisper 需 `GROQ_API_KEY`（[Groq](https://console.groq.com/keys) 免费申请）。

## 安装

```bash
pip install git+https://github.com/runesleo/x-reader.git
pip install "x-reader[telegram] @ git+https://github.com/runesleo/x-reader.git"
pip install "x-reader[browser] @ git+https://github.com/runesleo/x-reader.git"
playwright install chromium
pip install "x-reader[all] @ git+https://github.com/runesleo/x-reader.git"
```

可选：本地克隆后 `pip install -e ".[all]"`。视频/音频依赖需 `yt-dlp`、`ffmpeg`（见英文 README）。

## 库用法

```python
import asyncio
from x_reader.reader import UniversalReader

async def main():
    reader = UniversalReader()
    content = await reader.read("https://mp.weixin.qq.com/s/abc123")
    print(content.title)
    print(content.content[:200])

asyncio.run(main())
```

## 配置

复制 `.env.example` → `.env`。主要变量：`TG_API_ID` / `TG_API_HASH`（Telegram）、`GROQ_API_KEY`（Whisper）、`INBOX_FILE`、`OUTPUT_DIR`、`OBSIDIAN_VAULT`。详见英文 README 表格。

## 仓库结构

`x_reader/`（CLI、`UniversalReader`、各平台 fetcher）、`skills/`、`mcp_server.py`、`pyproject.toml`。各层如何协同的流程图见英文 README。

## Star History / Author / License

与英文 [README](./README.md) 相同：Star 图、作者 Leo ([@runes_leo](https://x.com/runes_leo))、**MIT**。
