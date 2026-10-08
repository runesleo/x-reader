# -*- coding: utf-8 -*-
"""
Xiaohongshu (RED) note fetcher — three-tier fallback:

1. Jina Reader (fast, no deps)
2. Playwright + saved session (handles 451/403)
3. Error with login instructions

Install browser tier: pip install "x-reader[browser]" && playwright install chromium
"""

from loguru import logger
from typing import Dict, Any
from pathlib import Path
from urllib.parse import urlparse

from x_reader.fetchers.jina import fetch_via_jina


def _valid_note(data: dict, final_url: str) -> bool:
    """Fail closed on deleted/login/security pages and empty note content."""
    path = urlparse(final_url or "").path.lower().rstrip("/")
    if path in ("/404", "/login", "/loginwithredirect") or path.startswith("/404/"):
        return False

    title = str(data.get("title") or "").strip()
    content = str(data.get("content") or "").strip()
    if not content:
        return False
    probe = (title + " " + content[:1000]).lower()
    blocked = (
        "安全限制", "账号异常", "account abnormal", "switch account and retry",
        "你访问的页面不见了", "this page isn't available",
        "登录后推荐更懂你的笔记", "小红书 - 你的生活兴趣社区",
        "请打开小红书app扫码查看",
    )
    return not any(marker in probe for marker in blocked)


async def fetch_xhs(url: str) -> Dict[str, Any]:
    """
    Fetch a Xiaohongshu note with three-tier fallback.

    Args:
        url: xiaohongshu.com or xhslink.com URL

    Returns:
        Dict with: title, content, author, url, platform
    """
    # Tier 1: Jina Reader
    try:
        logger.info(f"[XHS] Tier 1 — Jina: {url}")
        data = fetch_via_jina(url)
        content = data.get("content", "")
        title = data.get("title", "")
        if _valid_note(data, data.get("url") or url):
            return {
                "title": title,
                "content": content,
                "author": data.get("author", ""),
                "url": url,
                "platform": "xhs",
            }
        logger.warning("[XHS] Jina returned login-wall stub, falling back to browser")
    except Exception as e:
        logger.warning(f"[XHS] Jina failed ({e}), falling back to browser")

    # Tier 2: Playwright with session
    if "xsec_token" not in url and "xiaohongshu.com/explore/" in url:
        logger.warning("[XHS] URL missing xsec_token, likely to get 404")

    from x_reader.fetchers.browser import get_session_path, SESSION_DIR

    session_path = get_session_path("xhs")
    if not Path(session_path).exists():
        # Tier 3: No session — guide user
        raise RuntimeError(
            f"❌ XHS blocked Jina and no saved session found.\n"
            f"   Run: x-reader login xhs\n"
            f"   Then retry this URL."
        )

    try:
        logger.info(f"[XHS] Tier 2 — Playwright with session: {url}")
        from x_reader.fetchers.browser import fetch_via_browser

        data = await fetch_via_browser(url, storage_state=session_path)

        # Validate final browser URL's *path*, not the query string:
        # XHS uses /404?redirectPath=... and can return an empty SPA shell.
        final_url = data.get("url") or url
        path = urlparse(final_url).path.lower().rstrip("/")
        if path in ("/explore", "/login") or not _valid_note(data, final_url):
            raise RuntimeError(
                "XHS blocked or empty: note content was not retrieved. "
                "Check link availability, xsec_token and local session freshness."
            )

        return {
            "title": data["title"],
            "content": data["content"],
            "author": data.get("author", ""),
            "url": url,
            "platform": "xhs",
        }
    except RuntimeError:
        # Playwright not installed
        raise
    except Exception as e:
        logger.error(f"[XHS] Browser fetch also failed: {e}")
        raise RuntimeError(
            f"❌ All XHS fetch methods failed.\n"
            f"   Last error: {e}\n"
            f"   Try: x-reader login xhs (to refresh session)"
        )
