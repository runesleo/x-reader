# -*- coding: utf-8 -*-
"""
Universal Reader — routes any URL to the right fetcher.

The core dispatcher: give it a URL, get back structured content.
"""

import asyncio
from urllib.parse import urlparse
from loguru import logger
from typing import Dict, Any, Optional

from x_reader.schema import (
    UnifiedContent, UnifiedInbox, SourceType,
    from_bilibili, from_twitter, from_wechat,
    from_xiaohongshu, from_youtube, from_rss, from_telegram, from_podcast,
)
from x_reader.fetchers.jina import fetch_via_jina
from x_reader.utils.url_validator import validate_url


class UniversalReader:
    """
    Routes URLs to platform-specific fetchers.
    Falls back to Jina Reader for unknown platforms.
    """

    def __init__(self, inbox: Optional[UnifiedInbox] = None, media_preview_seconds: int = 0,
                 full_short_audio: bool = False, full_long_audio: bool = False):
        if type(media_preview_seconds) is not int or not 0 <= media_preview_seconds <= 30:
            raise ValueError("media_preview_seconds must be 0 through 30")
        if type(full_short_audio) is not bool or type(full_long_audio) is not bool:
            raise ValueError("full media flags must be boolean")
        if sum((bool(media_preview_seconds), full_short_audio, full_long_audio)) > 1:
            raise ValueError("Preview, full-short and full-long modes cannot be enabled together")
        self.inbox = inbox
        self.media_preview_seconds = media_preview_seconds
        self.full_short_audio = full_short_audio
        self.full_long_audio = full_long_audio

    def _detect_platform(self, url: str) -> str:
        """Detect platform from URL."""
        domain = urlparse(url).netloc.lower()

        if "mp.weixin.qq.com" in domain:
            return "wechat"
        if "x.com" in domain or "twitter.com" in domain:
            return "twitter"
        if "youtube.com" in domain or "youtu.be" in domain:
            return "youtube"
        if "xiaohongshu.com" in domain or "xhslink.com" in domain:
            return "xhs"
        if "bilibili.com" in domain or "b23.tv" in domain:
            return "bilibili"
        if "xiaoyuzhoufm.com" in domain:
            return "podcast"
        if "podcasts.apple.com" in domain:
            return "podcast"
        if "t.me" in domain or "telegram.org" in domain:
            return "telegram"
        if url.endswith(".xml") or "/rss" in url or "/feed" in url or "/atom" in url:
            return "rss"
        return "generic"

    async def read(self, url: str) -> UnifiedContent:
        """
        Fetch content from any URL and return as UnifiedContent.

        The main entry point — give it a URL, get back structured content.
        """
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        # DNS validation is blocking; keep it off the event loop.
        await asyncio.to_thread(validate_url, url)

        platform = self._detect_platform(url)
        if (self.full_short_audio or self.full_long_audio) and platform != "podcast":
            raise ValueError("Full-audio mode supports podcast episodes only")
        logger.info(f"[{platform}] {url[:60]}...")

        try:
            content = await self._fetch(platform, url)

            if self.inbox:
                if self.inbox.add(content):
                    self.inbox.save()
                    logger.info(f"Saved to inbox: {content.title[:50]}")

            from x_reader.utils.storage import save_to_markdown
            save_to_markdown(content)

            return content

        except Exception as e:
            logger.error(f"[{platform}] Failed: {e}")
            raise

    async def _fetch(self, platform: str, url: str) -> UnifiedContent:
        """Dispatch to platform-specific fetcher."""

        if platform == "bilibili":
            from x_reader.fetchers.bilibili import fetch_bilibili
            data = await fetch_bilibili(url)
            return from_bilibili(data)

        if platform == "twitter":
            from x_reader.fetchers.twitter import fetch_twitter
            data = await fetch_twitter(url)
            return from_twitter(data)

        if platform == "wechat":
            from x_reader.fetchers.wechat import fetch_wechat
            data = await fetch_wechat(url)
            return from_wechat(data)

        if platform == "xhs":
            from x_reader.fetchers.xhs import fetch_xhs
            data = await fetch_xhs(url)
            return from_xiaohongshu(data)

        if platform == "youtube":
            from x_reader.fetchers.youtube import fetch_youtube
            data = await fetch_youtube(url)
            return from_youtube(data)

        if platform == "podcast":
            from x_reader.fetchers.podcast import fetch_podcast
            if self.full_long_audio:
                data = await fetch_podcast(url, full_long_audio=True)
            elif self.full_short_audio:
                data = await fetch_podcast(url, full_short_audio=True)
            else:
                data = await fetch_podcast(url, preview_seconds=self.media_preview_seconds)
            return from_podcast(data)

        if platform == "rss":
            from x_reader.fetchers.rss import fetch_rss
            articles = await fetch_rss(url, limit=1)
            if articles:
                return from_rss(articles[0])
            raise ValueError(f"No articles found in RSS feed: {url}")

        if platform == "telegram":
            from x_reader.fetchers.telegram import fetch_telegram
            path = urlparse(url).path.strip("/").split("/")[0]
            channel = path if path else url
            messages = await fetch_telegram(channel, limit=1)
            if messages:
                return from_telegram(messages[0], channel, channel)
            raise ValueError(f"No messages from Telegram channel: {url}")

        logger.info(f"Using Jina fallback for: {url}")
        try:
            data = await asyncio.to_thread(fetch_via_jina, url)
        except Exception as exc:
            logger.warning(
                f"Jina generic fallback failed ({exc}); trying direct HTML"
            )
            from x_reader.fetchers.jina import fetch_direct_html
            data = await asyncio.to_thread(fetch_direct_html, url)

        return UnifiedContent(
            source_type=SourceType.MANUAL,
            source_name=urlparse(url).netloc,
            title=data["title"],
            content=data["content"],
            url=data.get("url", url),
            extra={"fetch_method": data.get("fetch_method", "jina")},
        )

    async def read_batch(self, urls: list[str]) -> list[UnifiedContent]:
        """Fetch multiple URLs concurrently, except costly full-short audio."""
        if (self.full_short_audio or self.full_long_audio) and len(urls) != 1:
            raise ValueError("Full-audio mode supports exactly one URL")
        tasks = [self.read(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        contents = []
        for url, result in zip(urls, results):
            if isinstance(result, Exception):
                logger.error(f"Batch failed for {url}: {result}")
            else:
                contents.append(result)

        return contents
