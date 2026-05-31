# x-reader

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Universal content reader — fetch, transcribe, and digest content from any platform.

Give it a URL (article, video, podcast, tweet), get back structured content. Works as CLI, Python library, MCP server, or Claude Code skills.

**简体中文：** [README.zh.md](./README.zh.md) / [README.zh-CN.md](./README.zh-CN.md)

## What It Does

```
Any URL → Platform Detection → Fetch Content → Unified Output
              ↓                      ↓
         auto-detect           text: Jina Reader
         7+ platforms          video: yt-dlp subtitles
                               audio: Whisper transcription
                               API: Bilibili / RSS / Telegram
```

The Python layer handles text fetching and YouTube subtitle extraction. The **Claude Code skills** (optional) add full Whisper transcription for video/podcast and AI-powered content analysis.

## Three Layers

x-reader is composable. Use the layers you need:

| Layer | What | Format | Install |
|-------|------|--------|---------|
| **Python CLI/Library** | Basic content fetching + unified schema | See [Install](#install) | Required |
| **Claude Code Skills** | Video transcription + AI analysis | Copy `skills/` to your Claude Code skills directory | Optional |
| **MCP Server** | Expose reading as MCP tools | `python mcp_server.py` | Optional |

### Layer 1: Python CLI

```bash
# Fetch any URL
x-reader https://mp.weixin.qq.com/s/abc123

# Fetch a tweet
x-reader https://x.com/elonmusk/status/123456

# Fetch multiple URLs
x-reader https://url1.com https://url2.com

# Login to a platform (one-time, for browser fallback)
x-reader login xhs

# View inbox
x-reader list
```

### Layer 2: Claude Code Skills

> Requires cloning the repo (not included in pip install).

For video/podcast transcription and content analysis:

```
skills/
├── video/       # YouTube/Bilibili/podcast → full transcript via Whisper
└── analyzer/    # Any content → structured analysis report
```

Install:
```bash
export CLAUDE_SKILLS_DIR="/path/to/claude-code-skills"
mkdir -p "$CLAUDE_SKILLS_DIR"
cp -r skills/video "$CLAUDE_SKILLS_DIR/video"
cp -r skills/analyzer "$CLAUDE_SKILLS_DIR/analyzer"
```

Then in Claude Code, just send a YouTube/Bilibili/podcast link — the video skill auto-triggers and produces a full transcript + summary.

### Layer 3: MCP Server

> Requires cloning the repo (mcp_server.py is not included in pip install).

```bash
git clone https://github.com/runesleo/x-reader.git
cd x-reader
pip install -e ".[mcp]"
python mcp_server.py
```

Tools exposed:
- `read_url(url)` — fetch any URL
- `read_batch(urls)` — fetch multiple URLs concurrently
- `list_inbox()` — view previously fetched content
- `detect_platform(url)` — identify platform from URL

Claude Code config (`~/.claude/claude_desktop_config.json`):
```json
{
    "mcpServers": {
        "x-reader": {
            "command": "python",
            "args": ["/path/to/x-reader/mcp_server.py"]
        }
    }
}
```

## Supported Platforms

| Platform | Text Fetch | Video/Audio Transcript |
|----------|-----------|----------------------|
| YouTube | ✅ Jina | ✅ yt-dlp subtitles → Groq Whisper fallback |
| Bilibili (B站) | ✅ API | ✅ via Claude Code skill |
| X / Twitter | ✅ oEmbed → FxTwitter → Article/Jina → Playwright | — |
| WeChat (微信公众号) | ✅ Jina → Playwright | — |
| Xiaohongshu (小红书) | ✅ Jina → Playwright* | — |
| Telegram | ✅ Telethon | — |
| RSS | ✅ feedparser | — |
| 小宇宙 (Xiaoyuzhou) | — | ✅ via Claude Code skill |
| Apple Podcasts | — | ✅ via Claude Code skill |
| Any web page | ✅ Jina fallback | — |

> \*XHS requires a one-time login: `x-reader login xhs` (saves session for Playwright fallback)
>
> X Articles and login-required X pages can use a saved local browser session: `x-reader login twitter`
>
> YouTube Whisper transcription requires `GROQ_API_KEY` — get a free key from [Groq](https://console.groq.com/keys)

### X / Twitter Reading Path

`x-reader` uses a lightweight public-first chain for X:

1. X oEmbed for fast public tweet text.
2. FxTwitter for structured public tweet fallback.
3. Jina Reader for public Articles and long-form pages.
4. Generic Jina Reader for profiles and non-status X pages.
5. Playwright with saved session for login-required content.

For Articles or gated pages, run:

```bash
x-reader login twitter
x-reader "https://x.com/user/status/123"
```

By default, local X cookies stay local. If you explicitly want to let Jina use
your saved X session for gated Articles, set:

```bash
export X_READER_ALLOW_EXTERNAL_SESSION_COOKIES=1
```

## Install

```bash
# From GitHub (recommended)
pip install git+https://github.com/runesleo/x-reader.git

# With Telegram support
pip install "x-reader[telegram] @ git+https://github.com/runesleo/x-reader.git"

# With browser fallback (Playwright — for XHS/WeChat anti-scraping)
pip install "x-reader[browser] @ git+https://github.com/runesleo/x-reader.git"
playwright install chromium

# With all optional dependencies
pip install "x-reader[all] @ git+https://github.com/runesleo/x-reader.git"
playwright install chromium
```

Or clone and install locally:
```bash
git clone https://github.com/runesleo/x-reader.git
cd x-reader
pip install -e ".[all]"
playwright install chromium
```

### Dependencies for video/audio (optional)

```bash
# macOS
brew install yt-dlp ffmpeg

# Linux
pip install yt-dlp
apt install ffmpeg
```

For Whisper transcription, get a free API key from [Groq](https://console.groq.com/keys) and set:
```bash
export GROQ_API_KEY=your_key_here
```

## Use as Library

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

## Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|----------|----------|-------------|
| `TG_API_ID` | Telegram only | From https://my.telegram.org |
| `TG_API_HASH` | Telegram only | From https://my.telegram.org |
| `GROQ_API_KEY` | Whisper only | From https://console.groq.com/keys (free) |
| `INBOX_FILE` | No | Path to inbox JSON (default: `./unified_inbox.json`) |
| `OUTPUT_DIR` | No | Directory for Markdown output (default: disabled) |
| `OBSIDIAN_VAULT` | No | Path to Obsidian vault (writes to `01-收集箱/x-reader-inbox.md`) |

## Architecture

```
x-reader/
├── x_reader/              # Python package
│   ├── cli.py             # CLI entry point
│   ├── reader.py          # URL dispatcher (UniversalReader)
│   ├── schema.py          # Unified data model (UnifiedContent + Inbox)
│   ├── login.py           # Browser login manager (saves sessions)
│   ├── fetchers/
│   │   ├── jina.py        # Jina Reader (universal fallback)
│   │   ├── browser.py     # Playwright headless (anti-scraping fallback)
│   │   ├── bilibili.py    # Bilibili API
│   │   ├── youtube.py     # yt-dlp subtitle extraction
│   │   ├── rss.py         # feedparser
│   │   ├── telegram.py    # Telethon
│   │   ├── twitter.py     # oEmbed → FxTwitter → Article/Jina → Playwright
│   │   ├── wechat.py      # Jina → Playwright fallback
│   │   └── xhs.py         # Jina → Playwright + session fallback
│   └── utils/
│       └── storage.py     # JSON + Markdown dual output
├── skills/                # Claude Code skills
│   ├── video/             # Video/podcast → transcript + summary
│   └── analyzer/          # Content → structured analysis
├── mcp_server.py          # MCP server entry point
└── pyproject.toml
```

## How the Layers Work Together

```
User sends URL
    │
    ├─ Text content (article, tweet, WeChat)
    │   └─ Python fetcher → UnifiedContent → inbox
    │
    ├─ Video (YouTube, Bilibili, X video)
    │   ├─ Python fetcher → metadata (title, description)
    │   └─ Video skill → full transcript via subtitles/Whisper
    │
    ├─ Podcast (小宇宙, Apple Podcasts)
    │   └─ Video skill → full transcript via Whisper
    │
    └─ Analysis requested
        └─ Analyzer skill → structured report + action items
```

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=runesleo/x-reader&type=Date)](https://star-history.com/#runesleo/x-reader&Date)

## Author

*Leo ([@runes_leo](https://x.com/runes_leo)) — AI × Crypto independent builder. Trading on [Polymarket](https://polymarket.com/?r=githuball&via=runes-leo&utm_source=github&utm_content=x-reader), building data and trading systems with Claude Code and Codex.*

[leolabs.me](https://leolabs.me) — writing · community · open-source tools · indie projects · all platforms.

[X Subscription](https://x.com/runes_leo/creator-subscriptions/subscribe) — paid content weekly, or just buy me a coffee 😁

*Learn in public, Build in public.*

## License

MIT
