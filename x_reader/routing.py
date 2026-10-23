# -*- coding: utf-8 -*-
"""Single source of truth for URL → platform routing.

Both the CLI reader and the hosted web surface call `detect_platform`, so a URL
can never be classified one way by the hosted policy check and another way by
the reader that actually fetches it.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

# Exact host or subdomain match only: "x.com" must not match "dropbox.com".
_HOSTS = (
    ("wechat", ("mp.weixin.qq.com",)),
    ("twitter", ("x.com", "twitter.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com", "fixvx.com")),
    ("youtube", ("youtube.com", "youtu.be")),
    ("xhs", ("xiaohongshu.com", "xhslink.com")),
    ("bilibili", ("bilibili.com", "b23.tv")),
    ("podcast", ("xiaoyuzhoufm.com", "podcasts.apple.com")),
    ("telegram", ("t.me", "telegram.me", "telegram.org")),
)
_FEED_SEGMENTS = {"rss", "feed", "feeds", "atom"}
_FEED_SUFFIXES = (".xml", ".rss", ".atom")

HOSTED_ENV = "X_READER_HOSTED"
ALLOWED_PLATFORMS_ENV = "X_READER_ALLOWED_PLATFORMS"


def host_matches(host: str, domain: str) -> bool:
    host = host.lower().rstrip(".")
    domain = domain.lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


def _routing_host(hostname: str) -> str:
    """The hostname as x-reader will connect to it (IDNA/UTS46), so look-alike
    spellings such as "x。com" route like x.com."""
    from x_reader.utils.url_validator import _canonical_hostname

    try:
        return _canonical_hostname(hostname)
    except ValueError:
        return hostname.lower()


def detect_platform(url: str) -> str:
    """Return the platform the reader will use for this URL."""
    parsed = urlparse(url)
    host = _routing_host(parsed.hostname or "") if parsed.hostname else ""
    for platform, domains in _HOSTS:
        if any(host_matches(host, domain) for domain in domains):
            return platform
    path = parsed.path.lower()
    if path.endswith(_FEED_SUFFIXES) or any(seg in _FEED_SEGMENTS for seg in path.split("/")):
        return "rss"
    return "generic"


def hosted_mode() -> bool:
    """True inside the hosted web surface's per-request reader process."""
    return os.environ.get(HOSTED_ENV) == "1"


def allowed_platforms() -> set[str] | None:
    """Platforms the hosted surface permits, or None when unrestricted (local CLI)."""
    raw = os.environ.get(ALLOWED_PLATFORMS_ENV)
    if raw is None:
        return None
    return {item.strip() for item in raw.split(",") if item.strip()}
