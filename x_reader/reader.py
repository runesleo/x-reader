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
from x_reader.routing import allowed_platforms, detect_platform, hosted_mode
from x_reader.utils.url_validator import canonical_url, validate_url


# Jina's text stands in for the page only when it carries it: most of what it
# shows is on the page, and it shows nearly all of the page's main text
# (counted in overlapping word runs, both ways).
SHOWN_ON_PAGE_SHARE = 0.7
PAGE_IN_SHOWN_SHARE = 0.8


def _same_text(shown: str, read_directly: str) -> bool:
    """The shown text and the directly read page carry the same text."""
    from x_reader.evidence import visible_text
    from x_reader.utils.textmatch import overlap

    # (the page read directly is the words it shows: nothing in it is markup)
    shares = overlap(visible_text(shown), visible_text(read_directly, markup=False))
    return (
        shares is not None
        and shares[0] >= SHOWN_ON_PAGE_SHARE
        and shares[1] >= PAGE_IN_SHOWN_SHARE
    )


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
        """Detect platform from URL (shared with the hosted surface)."""
        return detect_platform(url)

    async def read(self, url: str) -> UnifiedContent:
        """
        Fetch content from any URL and return as UnifiedContent.

        The main entry point — give it a URL, get back structured content.
        """
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        # Route, check and fetch the hostname exactly as it will be connected to.
        url = canonical_url(url)

        # DNS validation is blocking; keep it off the event loop.
        await asyncio.to_thread(validate_url, url)

        platform = self._detect_platform(url)
        allowed = allowed_platforms()
        if allowed is not None and platform not in allowed:
            raise ValueError(f"Platform '{platform}' is not allowed in this environment")
        if (self.full_short_audio or self.full_long_audio) and platform != "podcast":
            raise ValueError("Full-audio mode supports podcast episodes only")
        logger.info(f"[{platform}] {url[:60]}...")

        try:
            content = await self._fetch(platform, url)

            if hosted_mode():
                return content  # hosted requests never persist anything

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

    async def _fetch(self, platform: str, url: str, redirected: bool = False) -> UnifiedContent:
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
            from x_reader.fetchers.rss import fetch_rss, verify_full_body
            articles = await fetch_rss(url, limit=1)
            if articles:
                article = articles[0]
                article["content_kind"] = await verify_full_body(article)
                return from_rss(article)
            raise ValueError(f"No articles found in RSS feed: {url}")

        if platform == "telegram":
            from x_reader.fetchers.telegram import fetch_telegram
            parts = [p for p in urlparse(url).path.split("/") if p]
            if parts and parts[0] == "s":  # t.me/s/<channel>/<id> web preview
                parts = parts[1:]
            if not parts:
                raise ValueError(f"Telegram URL has no channel: {url}")
            channel = parts[0]
            message_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            messages = await fetch_telegram(channel, limit=1, message_id=message_id)
            if messages:
                return from_telegram(messages[0], channel, channel)
            raise ValueError(f"No messages from Telegram channel: {url}")

        return await self._fetch_generic(url, redirected)

    async def _fetch_generic(self, url: str, redirected: bool) -> UnifiedContent:
        """Read any other page; a redirect into another platform goes to that reader.

        x-reader first loads the page itself over pinned, validated hops: that proves
        where the URL leads and shows embedded players and frames. Jina follows
        redirects on its own side and may be sent elsewhere, so the hosted surface
        never uses it: there the reader returns only text this process fetched over
        checked hops. Locally Jina's cleaner text is shown when it matches the
        direct read both ways; otherwise the direct read itself is shown.
        """
        from x_reader.fetchers.jina import (
            BlockedDestination, CrossPlatformRedirect, ParseBudgetExceeded, fetch_html_page,
        )

        def generic_only(hop: str) -> None:
            hop_platform = detect_platform(hop)
            if hop_platform != "generic":
                raise CrossPlatformRedirect(hop, hop_platform)

        hosted = hosted_mode()
        page = None
        try:
            page = await asyncio.to_thread(fetch_html_page, url, hop_guard=generic_only)
        except CrossPlatformRedirect as hop:
            return await self._redispatch(hop.platform, hop.url, redirected)
        except BlockedDestination:
            raise
        except ParseBudgetExceeded:
            # Too much markup to check: no text can be vouched for, so none is shown.
            return UnifiedContent(
                source_type=SourceType.MANUAL, source_name=urlparse(url).netloc, title="", content="", url=url,
                extra={"fetch_method": "direct_html_pinned", "redirects_verified": True, "body_verified": False,
                       "embedded_media": None, "page_signals": ["scan_budget_exceeded"]},
            )
        except Exception as exc:
            if hosted:
                raise
            logger.warning(f"Direct page read failed ({type(exc).__name__}); trying Jina unverified")

        target_url = page["url"] if page else url
        page_usable = bool(page) and len(page["content"]) >= 40

        if hosted:
            if not page_usable:
                raise ValueError("The page returned too little readable text")
            data, body_verified = page, True  # the text shown is the text that was checked
        else:
            jina = None
            try:
                jina = await asyncio.to_thread(fetch_via_jina, target_url)
            except Exception as exc:
                if not page_usable:
                    raise
                logger.warning(f"Jina read failed ({type(exc).__name__}); using the direct read")
            if jina is not None and page_usable and _same_text(jina["content"], page["content"]):
                data, body_verified = jina, True
            elif page_usable:
                if jina is not None:
                    logger.warning("Jina's text does not match the page; using the direct read")
                data, body_verified = page, True
            else:
                data, body_verified = jina, False  # nothing x-reader read itself to check it against

        return UnifiedContent(
            source_type=SourceType.MANUAL,
            source_name=urlparse(target_url).netloc,
            title=data["title"],
            content=data["content"],
            url=target_url,
            extra={
                "fetch_method": data.get("fetch_method", "jina"),
                "redirects_verified": bool(page),
                "body_verified": body_verified,
                "embedded_media": page["embedded_media"] if page else None,
                "page_signals": page.get("page_signals") if page else None,
            },
        )

    async def _redispatch(self, platform: str, url: str, redirected: bool) -> UnifiedContent:
        """Hand a redirect target to its own reader, under the same policy as a direct URL."""
        if redirected:
            raise ValueError("Redirect chain crosses platforms more than once")
        url = canonical_url(url)
        await asyncio.to_thread(validate_url, url)
        allowed = allowed_platforms()
        if allowed is not None and platform not in allowed:
            raise ValueError(f"URL redirects to a {platform} page, which is not allowed in this environment")
        if (self.full_short_audio or self.full_long_audio) and platform != "podcast":
            raise ValueError("Full-audio mode supports podcast episodes only")
        logger.info(f"Redirect leads to {platform}: {url[:60]}")
        return await self._fetch(platform, url, redirected=True)

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
