# -*- coding: utf-8 -*-
"""
WeChat article fetcher — two-tier fallback:

1. Jina Reader (fast, no deps)
2. Playwright headless (no login needed for public articles)
"""

import re
from loguru import logger
from typing import Dict, Any


def _proxy_wechat_images(content: str) -> str:
    """Replace WeChat image URLs with a proxy to bypass anti-hotlinking."""
    if not content:
        return content
    return re.sub(
        r'(https?://mmbiz\.qpic\.cn/[^\s\)]+)',
        r'https://wsrv.nl/?url=\1',
        content
    )


async def fetch_wechat(url: str) -> Dict[str, Any]:
    """
    Fetch a WeChat public account article with fallback.

    Args:
        url: mp.weixin.qq.com article URL

    Returns:
        Dict with: title, content, author, url, platform
    """
    # Tier 1: Jina Reader
    try:
        logger.info(f"[WeChat] Tier 1 — Jina: {url}")
        from x_reader.fetchers.jina import fetch_via_jina

        data = fetch_via_jina(url)
        content = data.get("content", "")
        is_captcha = "[去验证]" in content or data.get("title") == "Weixin Official Accounts Platform"
        if content and not is_captcha:
            return {
                "title": data["title"],
                "content": _proxy_wechat_images(content),
                "author": data.get("author", ""),
                "url": url,
                "platform": "wechat",
                "extraction": "reader",
            }
        logger.warning("[WeChat] Jina returned CAPTCHA/anti-scrape stub, falling back to browser")
    except Exception as e:
        logger.warning(f"[WeChat] Jina failed ({e}), falling back to browser")

    # Tier 2: Playwright headless (no session needed)
    try:
        logger.info(f"[WeChat] Tier 2 — Playwright headless: {url}")
        from x_reader.fetchers.browser import fetch_via_browser

        data = await fetch_via_browser(url)
        article = {
            "title": data["title"],
            "content": _proxy_wechat_images(data["content"]),
            "author": data.get("author", ""),
            "url": url,
            "platform": "wechat",
            "extraction": data.get("extraction", "page_body"),
        }
        if isinstance(data.get("embedded_media"), int):
            article["embedded_media"] = data["embedded_media"]  # players and frames the walk counted
        article["final_url"] = str(data.get("url") or "")  # where the browser ended up
        if isinstance(data.get("visual_risks"), list):
            article["visual_risks"] = data["visual_risks"]  # what the page's text read cannot vouch for
        if "page_text" in data:
            # walls drawn anywhere on the page (overlays, prompts outside the article); a page text
            # cut short could hold one past the cut
            from x_reader.evidence import page_wall_signals
            signals = set(page_wall_signals(data["title"], str(data["page_text"] or "")))
            if data.get("page_text_complete") is not True:
                signals.add("scan_budget_exceeded")
            article["page_signals"] = sorted(signals)
        return article
    except RuntimeError:
        # Playwright not installed — re-raise with original Jina error context
        raise
    except Exception as e:
        logger.error(f"[WeChat] Browser fetch also failed: {e}")
        raise RuntimeError(
            f"❌ All WeChat fetch methods failed.\n"
            f"   Last error: {e}"
        )
