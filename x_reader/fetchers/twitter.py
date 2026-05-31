# -*- coding: utf-8 -*-
"""
X/Twitter fetcher — production-style fallback:

1. X oEmbed probe (fast, no auth, good for public tweets)
2. FxTwitter API (structured fallback for public tweets)
3. X Article via Jina (public content; external cookies require explicit opt-in)
4. Jina Reader (profiles and non-tweet X pages)
5. Playwright + saved session (login-required content)

Install browser tier: pip install "x-reader[browser]" && playwright install chromium
Save X session:       x-reader login twitter
"""

import os
import re
import requests
from html import unescape
from pathlib import Path
from loguru import logger
from typing import Dict, Any, Optional
from urllib.parse import urlparse, urlunparse

from x_reader.fetchers.jina import fetch_via_jina


FXTWITTER_API = "https://api.fxtwitter.com"
OEMBED_URL = "https://publish.twitter.com/oembed"
JINA_BASE = "https://r.jina.ai"


def _normalize_x_url(url: str) -> str:
    """Normalize X/Twitter URLs and drop tracking query strings."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    parsed = urlparse(url)
    netloc = parsed.netloc.lower().replace("www.", "")
    if netloc in ("twitter.com", "mobile.twitter.com"):
        netloc = "x.com"
    return urlunparse((parsed.scheme or "https", netloc, parsed.path, "", "", ""))


def _extract_status_id(url: str) -> Optional[str]:
    """Extract tweet/article id from common X URL shapes."""
    patterns = (
        r'x\.com/[^/]+/status/(\d+)',
        r'x\.com/i/web/status/(\d+)',
        r'x\.com/i/article/(\d+)',
        r'x\.com/[^/]+/article/(\d+)',
    )
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _extract_author(url: str) -> str:
    """Extract @username from tweet URL."""
    match = re.search(r'x\.com/([A-Za-z0-9_]+)/(?:status|article)', url)
    return f"@{match.group(1)}" if match else ""


def _is_tweet_url(url: str) -> bool:
    """Check if this is a direct tweet/status URL (vs profile or other X page)."""
    return bool(
        re.search(r'x\.com/[A-Za-z0-9_]+/status/\d+', url)
        or re.search(r'x\.com/i/web/status/\d+', url)
    )


def _is_article_url(url: str) -> bool:
    """Check if this is an X Article URL or status URL known to carry an article."""
    return bool(
        re.search(r'x\.com/i/article/\d+', url)
        or re.search(r'x\.com/[A-Za-z0-9_]+/article/\d+', url)
    )


def _article_url_from_status(url: str) -> Optional[str]:
    """Build the /i/article URL that X uses for long-form articles."""
    status_id = _extract_status_id(url)
    if not status_id:
        return None
    return f"https://x.com/i/article/{status_id}"


def _strip_tweet_html(html: str) -> str:
    """Extract readable text from X oEmbed HTML without the attribution footer."""
    match = re.search(r'<p[^>]*>(.*?)</p>', html, flags=re.IGNORECASE | re.DOTALL)
    fragment = match.group(1) if match else html
    fragment = re.sub(r'<br\s*/?>', '\n', fragment, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', fragment)
    text = unescape(text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _is_thin_oembed_text(text: str) -> bool:
    """Detect oEmbed payloads that are likely cards, articles, or truncated text."""
    normalized = text.strip()
    if len(normalized) <= 20:
        return True
    if re.fullmatch(r'https://t\.co/\w+', normalized):
        return True
    if normalized.count("https://t.co/") >= 1 and len(normalized) < 80:
        return True
    if normalized.endswith("…") or normalized.endswith("..."):
        return True
    return False


def _session_cookie_header(platform: str = "twitter") -> Optional[str]:
    """Return minimal cookies from a saved Playwright session for authenticated fetches."""
    if os.getenv("X_READER_ALLOW_EXTERNAL_SESSION_COOKIES") != "1":
        return None

    try:
        from x_reader.fetchers.browser import get_session_path
        import json

        session_path = Path(get_session_path(platform))
        if not session_path.exists():
            return None
        state = json.loads(session_path.read_text())
        cookies = state.get("cookies", [])
        wanted = {"auth_token", "ct0", "twid"}
        values = []
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            domain = cookie.get("domain", "")
            if name in wanted and value and ("x.com" in domain or "twitter.com" in domain):
                values.append(f"{name}={value}")
        return "; ".join(values) if values else None
    except Exception as exc:
        logger.warning(f"[Twitter] Could not read saved X session cookies ({exc})")
        return None


def _fetch_via_fxtwitter(url: str) -> Dict[str, Any]:
    """
    Fetch full tweet text via FxTwitter API.
    Free, no auth, returns complete text (no truncation).
    """
    match = re.search(r'x\.com/([A-Za-z0-9_]+)/status/(\d+)', url)
    if not match:
        raise ValueError(f"Cannot parse tweet URL: {url}")

    username, status_id = match.group(1), match.group(2)
    api_url = f"{FXTWITTER_API}/{username}/status/{status_id}"

    resp = requests.get(api_url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    tweet = data.get("tweet", {})
    text = tweet.get("text", "")
    author_name = tweet.get("author", {}).get("name", "")
    author_screen = tweet.get("author", {}).get("screen_name", "")

    return {
        "text": text,
        "author": f"@{author_screen}" if author_screen else "",
        "author_name": author_name,
        "title": text[:100] if text else "",
    }


def _fetch_via_oembed(url: str) -> Dict[str, Any]:
    """
    Fetch tweet text via X's oEmbed API.
    Free, reliable, no auth needed. Works for public tweets.
    """
    resp = requests.get(
        OEMBED_URL,
        params={"url": url, "omit_script": "true"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    html = data.get("html", "")
    text = _strip_tweet_html(html)

    return {
        "text": text,
        "author": data.get("author_name", ""),
        "author_url": data.get("author_url", ""),
        "title": text[:100] if text else "",
    }


def _fetch_article_via_jina(url: str) -> Dict[str, Any]:
    """
    Fetch X Article content via Jina Reader.

    By default this does not send local X cookies to Jina. Users can opt in by
    setting X_READER_ALLOW_EXTERNAL_SESSION_COOKIES=1 if they trust the service.
    """
    article_url = url if _is_article_url(url) else _article_url_from_status(url)
    if not article_url:
        raise ValueError(f"Cannot build X Article URL from: {url}")

    headers = {
        "Accept": "text/markdown",
        "User-Agent": "x-reader/0.2",
    }
    cookie_header = _session_cookie_header("twitter")
    if cookie_header:
        logger.info("[Twitter] Forwarding saved X session cookies to Jina (explicit opt-in)")
        headers["X-Set-Cookie"] = cookie_header

    resp = requests.get(f"{JINA_BASE}/{article_url}", headers=headers, timeout=20)
    resp.raise_for_status()
    content = resp.text.strip()
    content = re.sub(r'\n{3,}', '\n\n', content)
    if not content or "This page requires JavaScript" in content:
        raise ValueError("Jina returned empty or JS-only article content")

    title = ""
    title_match = re.search(r'^Title:\s*(.+)$', content, flags=re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()

    return {
        "text": content,
        "author": _extract_author(url),
        "url": article_url,
        "title": title or content[:100],
    }


async def _fetch_via_playwright(url: str) -> Dict[str, Any]:
    """
    Fetch tweet via Playwright with X-specific DOM selectors.
    Uses saved login session if available (~/.x-reader/sessions/twitter.json).
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright not installed. Run:\n"
            '  pip install "x-reader[browser]"\n'
            "  playwright install chromium"
        )

    from x_reader.fetchers.browser import get_session_path
    from pathlib import Path

    session_path = get_session_path("twitter")
    has_session = Path(session_path).exists()
    if has_session:
        logger.info(f"Using saved X session: {session_path}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )

        context_kwargs = {}
        if has_session:
            context_kwargs["storage_state"] = session_path

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
            **context_kwargs,
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            # Wait for tweet text to render (X is a SPA, needs JS execution)
            try:
                await page.wait_for_selector(
                    '[data-testid="tweetText"]', timeout=10_000
                )
            except Exception:
                pass  # May not appear if login required

            # Extract tweet content with X-specific selectors
            tweet_text = await page.evaluate("""() => {
                // Priority 1: tweet text element
                const tweetEl = document.querySelector('[data-testid="tweetText"]');
                if (tweetEl) return tweetEl.innerText;

                // Priority 2: article element (thread view)
                const article = document.querySelector('article');
                if (article) return article.innerText;

                // Priority 3: main content area
                const main = document.querySelector('main');
                if (main) return main.innerText;

                return '';
            }""")

            title = await page.title()

            return {
                "text": (tweet_text or "").strip(),
                "title": (title or "").strip()[:200],
            }
        finally:
            await context.close()
            await browser.close()


async def fetch_twitter(url: str) -> Dict[str, Any]:
    """
    Fetch a tweet or X post with four-tier fallback.

    Args:
        url: Tweet URL (x.com or twitter.com)

    Returns:
        Dict with: text, author, url, title, platform
    """
    url = _normalize_x_url(url)
    author = _extract_author(url)

    # Tier 1: oEmbed probe. This is fastest and does not require auth.
    if _is_tweet_url(url):
        try:
            logger.info(f"[Twitter] Tier 1 — oEmbed probe: {url}")
            data = _fetch_via_oembed(url)
            text = (data.get("text") or "").strip()
            if text and not _is_thin_oembed_text(text):
                return {
                    "text": text,
                    "author": author or data.get("author", ""),
                    "url": url,
                    "title": data.get("title", ""),
                    "platform": "twitter",
                    "fetch_method": "oembed",
                }
            logger.warning("[Twitter] oEmbed returned thin or truncated content")
        except Exception as e:
            logger.warning(f"[Twitter] oEmbed failed ({e})")

    # Tier 2: FxTwitter API (structured public tweet fallback).
    if _is_tweet_url(url):
        try:
            logger.info(f"[Twitter] Tier 2 — FxTwitter: {url}")
            data = _fetch_via_fxtwitter(url)
            text = (data.get("text") or "").strip()
            if text:
                return {
                    "text": text,
                    "author": author or data.get("author", ""),
                    "url": url,
                    "title": data.get("title", ""),
                    "platform": "twitter",
                    "fetch_method": "fxtwitter",
                }
            logger.warning("[Twitter] FxTwitter returned empty text")
        except Exception as e:
            logger.warning(f"[Twitter] FxTwitter failed ({e})")

    # Tier 3: X Article via Jina + saved session cookies.
    # For status URLs this is attempted only after short tweet methods fail.
    if _is_article_url(url) or _is_tweet_url(url):
        try:
            logger.info(f"[Twitter] Tier 3 — X Article via Jina: {url}")
            data = _fetch_article_via_jina(url)
            text = (data.get("text") or "").strip()
            if text and len(text) > 100:
                return {
                    "text": text,
                    "author": author or data.get("author", ""),
                    "url": data.get("url", url),
                    "title": data.get("title", ""),
                    "platform": "twitter",
                    "fetch_method": "x_article_jina",
                }
            logger.warning("[Twitter] X Article Jina returned short content")
        except Exception as e:
            logger.warning(f"[Twitter] X Article Jina failed ({e})")

    # Tier 4: Jina Reader (handles profiles, threads, non-tweet pages)
    try:
        logger.info(f"[Twitter] Tier 4 — Jina: {url}")
        data = fetch_via_jina(url)
        content = data.get("content", "")
        title = data.get("title", "")
        jina_ok = (
            content
            and len(content.strip()) > 100
            and "not yet fully loaded" not in content.lower()
            and title.lower() not in ("x", "title: x", "")
        )
        if jina_ok:
            return {
                "text": content,
                "author": author,
                "url": url,
                "title": title,
                "platform": "twitter",
                "fetch_method": "jina",
            }
        logger.warning("[Twitter] Jina returned unusable content")
    except Exception as e:
        logger.warning(f"[Twitter] Jina failed ({e})")

    # Tier 5: Playwright + session with X-specific extraction
    try:
        logger.info(f"[Twitter] Tier 5 — Playwright: {url}")
        data = await _fetch_via_playwright(url)
        content = data.get("text", "")
        if content and len(content.strip()) > 20:
            return {
                "text": content,
                "author": author,
                "url": url,
                "title": data.get("title", ""),
                "platform": "twitter",
                "fetch_method": "playwright",
            }
        logger.warning("[Twitter] Playwright returned empty content")
    except RuntimeError:
        raise
    except Exception as e:
        logger.error(f"[Twitter] All methods failed: {e}")

    raise RuntimeError(
        f"❌ All Twitter fetch methods failed for: {url}\n"
        f"   Try: x-reader login twitter (to save session for Article/browser fallback)\n"
        f"   Then retry: x-reader {url}"
    )
