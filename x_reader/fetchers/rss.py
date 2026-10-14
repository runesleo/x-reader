# -*- coding: utf-8 -*-
"""RSS/Atom feed fetcher.

The feed is downloaded with the pinned, per-hop validated public fetcher and
only then parsed by feedparser, so feedparser never opens network connections
or follows redirects itself.
"""

import asyncio
import re
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
    from x_reader.utils.textmatch import overlap
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
        "extra": {"fetch_method": "direct_html_pinned", "redirects_verified": True, "body_verified": True,
                  "embedded_media": page["embedded_media"], "page_signals": page.get("page_signals", [])},
    })
    if page_receipt["status"] != PASS:
        return "unverified"  # gated, thin or media-carrying article: nothing solid to compare
    # (the page read directly is the words it shows: nothing in it is markup)
    shares = overlap(visible_text(article.get("summary") or ""), visible_text(page["content"], markup=False))
    if shares is None:
        return "unverified"
    feed_on_page, page_in_feed = shares
    if feed_on_page < FEED_ON_PAGE_SHARE:
        return "unverified"  # the feed body is not this page's text
    return "full" if page_in_feed >= FULL_BODY_COVERAGE else "summary"


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
        # players, frames and audio or video enclosures the item carries (not read)
        media = len(re.findall(r"<(?:audio|video|iframe|embed|object)\b", full or summary, re.IGNORECASE)) + sum(
            1 for enclosure in entry.get("enclosures") or []
            if str(enclosure.get("type") or "").lower().startswith(("audio/", "video/")))
        articles.append({
            "title": entry.get("title", ""),
            "summary": full or summary,
            "content_kind": "full" if full and _is_full_body(full) else "summary",
            "url": entry.get("link", ""),
            "source": source_name,
            "published": entry.get("published", ""),
            "platform": "rss",
            "embedded_media": media,
        })

    logger.info(f"RSS: {len(articles)} articles from {source_name}")
    return articles
