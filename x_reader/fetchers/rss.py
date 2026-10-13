# -*- coding: utf-8 -*-
"""RSS/Atom feed fetcher.

The feed is downloaded with the pinned, per-hop validated public fetcher and
only then parsed by feedparser, so feedparser never opens network connections
or follows redirects itself.
"""

import asyncio
import re
from collections import Counter
from typing import Any, Dict, List

import feedparser
from loguru import logger

from x_reader.fetchers.jina import fetch_public_bytes

FEED_TYPES = {
    "application/rss+xml", "application/atom+xml", "application/rdf+xml",
    "application/xml", "text/xml", "application/x-rss+xml",
    "application/feed+json", "application/json",
}


def looks_like_feed(body: bytes) -> bool:
    """True for RSS/Atom/RDF XML or a JSON Feed, whatever Content-Type it came with."""
    head = body[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"{"):
        return b"jsonfeed.org" in head
    return head.startswith(b"<?xml") or any(tag in head for tag in (b"<rss", b"<feed", b"<rdf:rdf"))


def _is_full_body(html_body: str) -> bool:
    """A feed item may carry the article only if its visible text is substantial and not a teaser."""
    from x_reader.evidence import (
        MIN_GENERIC_BODY_CHARS, _PAYWALL_PHRASES, _ends_with_read_more, prose_chars, visible_text,
    )

    text = visible_text(html_body)
    lowered = text.lower()
    return (
        prose_chars(text) >= MIN_GENERIC_BODY_CHARS
        and not _ends_with_read_more(text)
        and not any(phrase in lowered for phrase in _PAYWALL_PHRASES)
    )


# A feed body counts as full when it covers this share of the article page's
# text, counted in overlapping word runs (with repeats), not just length...
FULL_BODY_COVERAGE = 0.8
# ...and when this share of the feed body actually appears on the page.
FEED_ON_PAGE_SHARE = 0.7
_SHINGLE = 6
_MAX_TOKENS = 50_000
# CJK, kana and hangul count per character; every other script per word.
_TOKEN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]|[^\W_]+")


def _shingles(text: str) -> Counter:
    """Runs of six words (or CJK characters), with how often each occurs."""
    tokens = _TOKEN.findall(text.casefold())[:_MAX_TOKENS]
    return Counter(tuple(tokens[i:i + _SHINGLE]) for i in range(max(0, len(tokens) - _SHINGLE + 1)))


async def verify_full_body(article: Dict[str, Any]) -> str:
    """Check a "full" feed body against the article page it links to.

    An unmarked excerpt looks like a full body until it is set against the
    article. The page itself must pass as a readable article (no login,
    paywall or thin shell); then the feed body must share most of its text.
    Returns "full", "summary" (an excerpt of the page) or "unverified".
    """
    if article.get("content_kind") != "full":
        return article.get("content_kind", "summary")
    from x_reader.evidence import PASS, classify_payload, visible_text
    from x_reader.fetchers.jina import CrossPlatformRedirect, fetch_html_page
    from x_reader.routing import detect_platform

    def article_pages_only(hop: str) -> None:
        if detect_platform(hop) != "generic":
            raise CrossPlatformRedirect(hop, detect_platform(hop))

    link = str(article.get("url") or "")
    if not link.startswith(("http://", "https://")) or detect_platform(link) != "generic":
        return "unverified"  # no article page to compare with (a video, a post, ...)
    try:
        page = await asyncio.to_thread(fetch_html_page, link, hop_guard=article_pages_only)
    except Exception as exc:
        logger.warning(f"RSS article page not readable ({type(exc).__name__}); body unverified")
        return "unverified"
    page_receipt = classify_payload({
        "source_type": "manual", "title": page["title"], "content": page["content"], "url": page["url"],
        "extra": {"redirects_verified": True, "embedded_media": page["embedded_media"]},
    })
    if page_receipt["status"] != PASS:
        return "unverified"  # gated, thin or media-carrying article: nothing solid to compare
    feed = _shingles(visible_text(article.get("summary") or ""))
    on_page = _shingles(visible_text(page["content"]))
    if not feed or not on_page:
        return "unverified"
    shared = sum((feed & on_page).values())
    if shared < FEED_ON_PAGE_SHARE * sum(feed.values()):
        return "unverified"  # the feed body is not this page's text
    return "full" if shared >= FULL_BODY_COVERAGE * sum(on_page.values()) else "summary"


async def fetch_rss(url: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Fetch and parse an RSS/Atom feed.

    Returns a list of article dicts with: title, summary, url, source, published,
    content_kind ("full" when the feed carries the article body, else "summary").
    """
    logger.info(f"Fetching RSS: {url}")
    body, content_type, final_url = await asyncio.to_thread(
        fetch_public_bytes, url, FEED_TYPES, sniff=looks_like_feed,
    )
    feed = feedparser.parse(
        body,
        resolve_relative_uris=False,
        response_headers={"content-type": content_type, "content-location": final_url},
    )

    if feed.bozo and not feed.entries:
        raise ValueError(f"Failed to parse RSS feed: {type(feed.bozo_exception).__name__}")

    source_name = feed.feed.get("title", final_url)
    articles = []
    for entry in feed.entries[:limit]:
        full = ""
        if getattr(entry, "content", None):
            full = entry.content[0].get("value", "") or ""
        summary = getattr(entry, "summary", "") or ""
        articles.append({
            "title": entry.get("title", ""),
            "summary": full or summary,
            "content_kind": "full" if full and _is_full_body(full) else "summary",
            "url": entry.get("link", ""),
            "source": source_name,
            "published": entry.get("published", ""),
            "platform": "rss",
        })

    logger.info(f"RSS: {len(articles)} articles from {source_name}")
    return articles
