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
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host in ("twitter.com", "mobile.twitter.com", "mobile.x.com", "fxtwitter.com", "vxtwitter.com",
                "fixupx.com", "fixvx.com", "x.com") and parsed.port in (None, 443, 80):
        netloc = "x.com"
    return urlunparse((parsed.scheme or "https", netloc, parsed.path, "", "", ""))


# The whole path must be a status or article path (optionally a media,
# analytics or engagement view of it); "/status/123garbage" is not post 123.
# Post ids are ASCII numbers with no leading zero ("/status/01" names no post).
_STATUS_PATH = re.compile(
    r"/(?:(?P<user>[A-Za-z0-9_]{1,15})/status(?:es)?|i/web/status|i/status)/(?P<id>[1-9][0-9]{0,19})"
    r"(?:/(?:photo|video)/[0-9]{1,2}|/analytics|/quotes|/retweets(?:/with_comments)?|/likes|/history)?/?"
)
_ARTICLE_PATH = re.compile(r"/(?:i|[A-Za-z0-9_]{1,15})/article/(?P<id>[1-9][0-9]{0,19})/?")


def _x_path(url: str, pattern: re.Pattern):
    parsed = urlparse(_normalize_x_url(url))
    if (parsed.hostname or "") != "x.com":
        return None
    return pattern.fullmatch(parsed.path)


def _extract_status_id(url: str) -> Optional[str]:
    """The post/article id of an X status or article URL, or None."""
    match = _x_path(url, _STATUS_PATH) or _x_path(url, _ARTICLE_PATH)
    return match.group("id") if match else None


def _extract_author(url: str) -> str:
    """Extract @username from tweet URL."""
    match = re.search(r'x\.com/([A-Za-z0-9_]+)/(?:status|article)', url)
    return f"@{match.group(1)}" if match and match.group(1).lower() != "i" else ""  # /i/status/<id> names nobody


def _is_tweet_url(url: str) -> bool:
    """Check if this is a direct tweet/status URL (vs profile or other X page)."""
    return _x_path(url, _STATUS_PATH) is not None


def _is_article_url(url: str) -> bool:
    """Check if this is an X Article URL."""
    return _x_path(url, _ARTICLE_PATH) is not None


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


def _media_status_from_fxtwitter(tweet: dict) -> str:
    """Return present/none/unknown from FxTwitter's structured media field.

    FxTwitter omits "media" for posts without media, so a missing key on a
    real tweet object (it has an id and text) means none; a key that is there
    but null or empty is a damaged layer, not proof of no media.
    """
    if "media" not in tweet:
        return "none" if tweet.get("id") and "text" in tweet else "unknown"

    media = tweet.get("media")
    if isinstance(media, dict):
        return "present" if any(bool(value) for value in media.values()) else "unknown"
    if isinstance(media, (list, tuple)):
        return "present" if media else "unknown"
    return "present" if media else "unknown"


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


def _fetch_via_fxtwitter(url: str, timeout: float = 10) -> Dict[str, Any]:
    """
    Fetch full tweet text via FxTwitter API.
    Free, no auth, returns complete text (no truncation).
    """
    match = _x_path(url, _STATUS_PATH)
    if not match:
        raise ValueError(f"Cannot parse tweet URL: {url}")

    username, status_id = match.group("user") or "i", match.group("id")
    api_url = f"{FXTWITTER_API}/{username}/status/{status_id}"

    resp = requests.get(api_url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    tweet = data.get("tweet") if isinstance(data, dict) else None
    if not isinstance(tweet, dict) or not _post_id(tweet.get("id")) or str(tweet.get("id")) != status_id:
        raise ValueError("FxTwitter answered with a different post")
    text = tweet.get("text", "")
    if not isinstance(text, str):
        raise ValueError("FxTwitter returned post text that is not a string")
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    author_name = author.get("name") if isinstance(author.get("name"), str) else ""
    author_screen = author.get("screen_name") if isinstance(author.get("screen_name"), str) else ""

    return {
        "text": text,
        "author": f"@{author_screen}" if author_screen else "",
        "author_name": author_name,
        "title": text[:100] if text else "",
        "media_status": _media_status_from_fxtwitter(tweet),
        "post_id": status_id,
        **_structure_from_fxtwitter(tweet),
    }


def _structure_from_fxtwitter(tweet: dict) -> Dict[str, Any]:
    """Quoted post, poll and X Article layers, which the post text alone does not carry."""
    if not (tweet.get("id") and "text" in tweet) or _reply_state(tweet) == "unknown" or not _handle(tweet):
        return {"structure_status": "unknown"}  # (no readable author, or a reply relation that cannot be read)
    result: Dict[str, Any] = {"structure_status": "verified", "quote_status": "none",
                              "poll_status": "none", "article_status": "none",
                              "self_reply": _is_self_reply(tweet)}
    # FxTwitter omits a layer the post does not have: a key that is present
    # counts, whatever its value (null, empty or damaged layers are not "none").
    quote = tweet.get("quote")
    if isinstance(quote, dict):
        quote_text = quote.get("text").strip() if isinstance(quote.get("text"), str) else ""
        quote_author = _handle(quote)
        if not quote_author or not _post_id(quote.get("id")) or _reply_state(quote) == "unknown":
            quote_text = ""  # no readable author handle, or an unreadable reply relation: a damaged layer
        quote_media = _media_status_from_fxtwitter(quote) if quote.get("id") else "unknown"
        if not quote_text or quote_media == "unknown":
            result["quote_status"] = "unknown"
        else:
            result["quote_text"] = f"[Quoted post by @{quote_author}]\n{quote_text}"
            if quote_media != "none":
                result["quote_status"] = "media_unread"
            elif (any(key in quote for key in ("quote", "poll", "article"))
                    or _looks_like_thread(quote_text) or _is_self_reply(quote)):
                # The quoted post carries its own layers; only its text was read.
                result["quote_status"] = "incomplete"
            else:
                result["quote_status"] = "read"
    elif "quote" in tweet:
        result["quote_status"] = "unknown"
    poll = tweet.get("poll")
    choices = poll.get("choices") if isinstance(poll, dict) else None
    labels = [str(choice.get("label")).strip() if isinstance(choice, dict) and isinstance(choice.get("label"), str)
              else "" for choice in choices] if isinstance(choices, list) else []
    if 2 <= len(labels) <= 4 and all(labels):
        # Read only when every option (an X poll has two to four) came back with its wording.
        lines = []
        for choice, label in zip(choices, labels):
            pct = choice.get("percentage")
            lines.append(f"- {label} ({pct}%)" if pct is not None else f"- {label}")
        result["poll_text"] = "[Poll]\n" + "\n".join(lines)
        result["poll_status"] = "read"
    elif "poll" in tweet:
        result["poll_status"] = "unknown"
    if "article" in tweet:
        article = tweet.get("article")
        result["article_status"] = "present" if isinstance(article, dict) and article else "unknown"
    return result


def _reply_state(tweet: dict) -> str:
    """"none", "self" (a reply to the author's own post: a thread) or "other";
    "unknown" for a reply whose author or replied-to account cannot be read."""
    status, replied = tweet.get("replying_to_status"), tweet.get("replying_to")
    if status is None:  # FxTwitter sends null (or nothing) for a post that is not a reply
        return "none" if replied is None else "unknown"
    if not _post_id(status):
        return "unknown"  # a reply names the post it answers by its numeric id
    screen = _handle(tweet)
    replied = replied.strip().removeprefix("@") if isinstance(replied, str) else ""  # (one "@" at most)
    if not (screen and _HANDLE.fullmatch(replied)):
        return "unknown"
    return "self" if screen.lower() == replied.lower() else "other"


_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
_POST_ID = re.compile(r"[1-9][0-9]{0,19}")


def _post_id(value) -> bool:
    """An X post id: a positive decimal number of at most 20 ASCII digits."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 < value < 10 ** 20
    return isinstance(value, str) and bool(_POST_ID.fullmatch(value))


def _handle(tweet: dict) -> str:
    """The post author's handle, or "" when the author is missing or not an X handle."""
    author = tweet.get("author")
    screen = author.get("screen_name") if isinstance(author, dict) else None
    return screen.strip() if isinstance(screen, str) and _HANDLE.fullmatch(screen.strip()) else ""


def _is_self_reply(tweet: dict) -> bool:
    """A reply to the author's own post continues a thread."""
    return _reply_state(tweet) == "self"


_STRUCTURE_KEYS = ("structure_status", "quote_status", "poll_status", "article_status")
_THREAD_START = re.compile(r"^\s*[\(（]?1\s*/", re.IGNORECASE)
_THREAD_COUNTER_END = re.compile(r"[\(（]\d{1,2}\s*/\s*(?:\d{1,2}|n)[\)）]\s*$", re.IGNORECASE)


def _looks_like_thread(text: str) -> bool:
    """A post that opens or continues a thread: the other posts are not in this read."""
    stripped = text.strip()
    if "🧵" in stripped or _THREAD_START.search(stripped) or _THREAD_COUNTER_END.search(stripped):
        return True
    return "thread" in stripped.lower() and any(mark in stripped for mark in ("👇", "⬇", "↓"))


def _with_structure(result: Dict[str, Any], structure: Dict[str, Any]) -> Dict[str, Any]:
    """Attach structure fields and append quoted-post / poll text that was actually read."""
    result["thread_marker"] = _looks_like_thread(result["text"]) or bool(structure.get("self_reply"))
    for key in _STRUCTURE_KEYS:
        if key in structure:
            result[key] = structure[key]
    result.setdefault("structure_status", "unknown")
    extras = [structure[key] for key in ("quote_text", "poll_text") if structure.get(key)]
    if extras:
        result["text"] = "\n\n".join([result["text"], *extras])
    return result


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
        # The embedded post's own permalink: the reply may describe another post.
        "post_id": _extract_status_id(_normalize_x_url(str(data.get("url") or ""))) or "",
        "title": text[:100] if text else "",
        # oEmbed exposes media links inconsistently. Presence is useful evidence,
        # absence is not proof that the tweet has no media.
        "media_status": "present" if "pic.twitter.com/" in html.lower() else "unknown",
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

            # A status URL must render the post itself: the text of the article
            # whose timestamp permalink is the requested status (a reply page
            # shows ancestor posts first). A login or "this post was deleted"
            # shell has no such article and is not a post read.
            extracted = await page.evaluate("""(statusId) => {
                const own = new RegExp('/status/' + statusId + '(?:$|[/?#])');
                for (const article of document.querySelectorAll('article')) {
                    const permalink = Array.from(article.querySelectorAll('a[href]'))
                        .some(a => a.querySelector('time') && own.test(a.getAttribute('href') || ''));
                    const tweetEl = permalink && article.querySelector('[data-testid="tweetText"]');
                    if (tweetEl) return {text: tweetEl.innerText, kind: 'tweet', bound: true};
                }
                const article = document.querySelector('article');
                if (article) return {text: article.innerText, kind: 'page'};
                const main = document.querySelector('main');
                if (main) return {text: main.innerText, kind: 'page'};
                return {text: '', kind: 'page'};
            }""", _extract_status_id(url) or "-")

            title = await page.title()

            return {
                "text": (extracted.get("text") or "").strip(),
                "kind": extracted.get("kind") or "page",
                "title": (title or "").strip()[:200],
                "final_url": page.url,
                "bound": extracted.get("bound") is True,
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
    requested_id = _extract_status_id(url)

    # Tier 1: oEmbed probe. This is fastest and does not require auth.
    if _is_tweet_url(url):
        try:
            logger.info(f"[Twitter] Tier 1 — oEmbed probe: {url}")
            data = _fetch_via_oembed(url)
            text = (data.get("text") or "").strip()
            if data.get("post_id") != requested_id:
                logger.warning("[Twitter] oEmbed answered for a different post; trying FxTwitter")
            elif text and not _is_thin_oembed_text(text):
                media_status = data.get("media_status", "unknown")
                media_probe_method = "oembed_html"
                # oEmbed carries no structured media, quote, poll or article data.
                structure: Dict[str, Any] = {}
                oembed_text, fetch_method = text, "oembed"
                try:
                    structure = _fetch_via_fxtwitter(url, timeout=3)
                    probe_media = structure.get("media_status", "unknown")
                    if probe_media == "present" or media_status == "unknown":
                        media_status, media_probe_method = (
                            ("present", media_probe_method) if media_status == "present" else (probe_media, "fxtwitter"))
                    probe_text = (structure.get("text") or "").strip()
                    if probe_text and probe_text != text:
                        # the structured read carries the whole post; oEmbed's text may be cut short
                        text, fetch_method = probe_text, "fxtwitter"
                except Exception as probe_error:
                    logger.warning(
                        f"[Twitter] Structure probe unavailable ({type(probe_error).__name__}); "
                        "media and structure stay unknown"
                    )

                result = _with_structure({
                    "text": text,
                    "author": structure.get("author") or author or data.get("author", ""),
                    "url": url,
                    "title": data.get("title", ""),
                    "platform": "twitter",
                    "fetch_method": fetch_method,
                    "post_id_verified": True,
                    "media_status": media_status,
                    "media_probe_method": media_probe_method,
                }, structure)
                result["thread_marker"] = result["thread_marker"] or _looks_like_thread(oembed_text)
                return result
            else:
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
                return _with_structure({
                    "text": text,
                    "author": data.get("author") or author,
                    "url": url,
                    "title": data.get("title", ""),
                    "platform": "twitter",
                    "fetch_method": "fxtwitter",
                    "post_id_verified": True,
                    "media_status": data.get("media_status", "unknown"),
                    "media_probe_method": "fxtwitter",
                }, data)
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
                    "media_status": "unknown",
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
                "media_status": "unknown",
            }
        logger.warning("[Twitter] Jina returned unusable content")
    except Exception as e:
        logger.warning(f"[Twitter] Jina failed ({e})")

    # Tier 5: Playwright + session with X-specific extraction.
    # Not on the hosted surface: it has no sessions and must not start browsers.
    from x_reader.routing import hosted_mode
    if hosted_mode():
        raise RuntimeError(f"All public X fetch methods failed for: {url}")
    try:
        logger.info(f"[Twitter] Tier 5 — Playwright: {url}")
        data = await _fetch_via_playwright(url)
        content = data.get("text", "")
        is_post = data.get("kind") == "tweet"
        if _is_tweet_url(url) and not is_post:
            logger.warning("[Twitter] Playwright page has no post text (login or deleted shell)")
        elif content and len(content.strip()) > 20:
            return {
                "text": content,
                "author": author,
                "url": url,
                "title": data.get("title", ""),
                "platform": "twitter",
                # Only a rendered tweetText element counts as the post itself.
                "fetch_method": "playwright" if is_post else "playwright_page",
                # A rendered page cannot prove which post's text it read (quoted cards
                # carry their own permalinks), so rendered reads stay unverified.
                "post_id_verified": False,
                "media_status": "unknown",
                "structure_status": "unknown",
                "thread_marker": _looks_like_thread(content),
            }
        else:
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
