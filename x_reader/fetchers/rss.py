# -*- coding: utf-8 -*-
"""RSS/Atom feed fetcher.

The feed is downloaded with the pinned, per-hop validated public fetcher and
only then parsed by feedparser, so feedparser never opens network connections
or follows redirects itself.
"""

import asyncio
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
    """A feed item carries the article only if its visible text is substantial and not a teaser."""
    from x_reader.evidence import MIN_GENERIC_BODY_CHARS, _ends_with_read_more, prose_chars, visible_text

    text = visible_text(html_body)
    return prose_chars(text) >= MIN_GENERIC_BODY_CHARS and not _ends_with_read_more(text)


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
