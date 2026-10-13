# -*- coding: utf-8 -*-
"""
Jina Reader + safe direct-web fallback.

Both network paths use pinned-IP connections so the hostname is resolved and
validated once, then the TCP connection is made to that exact address.
"""

import re
import time
import requests
from loguru import logger
from urllib.parse import urljoin, urlsplit
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool, Timeout
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError


JINA_BASE = "https://r.jina.ai"

HEADERS = {
    "Accept": "text/markdown",
    "User-Agent": "x-reader/0.2",
}
DIRECT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
    "User-Agent": "Mozilla/5.0 (compatible; x-reader/0.2; +https://github.com/runesleo/x-reader)",
}

DIRECT_TOTAL_TIMEOUT = 20
JINA_TOTAL_TIMEOUT = 30
IO_TIMEOUT = 5
MAX_CONNECT_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_DIRECT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
_JINA_TYPES = {"text/plain", "text/markdown"}
# Labels servers put on bodies of any kind; the body itself decides (see sniff).
_SNIFFABLE_TYPES = {"", "text/plain", "application/octet-stream", "binary/octet-stream"}


class CrossPlatformRedirect(ValueError):
    """A redirect leads to a URL another reader handles (X, YouTube, podcasts, ...)."""

    def __init__(self, url: str, platform: str):
        super().__init__(f"Redirects to a {platform} URL")
        self.url = url
        self.platform = platform


class BlockedDestination(ValueError):
    """A hop points at a private/special address, an unresolvable host or a non-http scheme."""


_SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
_BOILERPLATE_TAGS = {"nav", "footer", "aside"}
_BLOCK_TAGS = {
    "p", "div", "article", "section", "main", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "tr",
}
# Anything a page embeds is content x-reader does not read: players, frames, plugins.
_EMBED_TAGS = {"video", "audio", "iframe", "embed", "object"}


def _is_hidden_embed(attrs: dict) -> bool:
    """Zero-size or hidden frames (tracking pixels, tag managers) carry no content."""
    style = (attrs.get("style") or "").replace(" ", "").lower()
    return (
        "hidden" in attrs
        or attrs.get("aria-hidden") == "true"
        or "display:none" in style
        or "visibility:hidden" in style
        or (attrs.get("width") or "").strip() in ("0", "1")
        or (attrs.get("height") or "").strip() in ("0", "1")
    )


def _join_text(parts: list) -> str:
    lines = []
    for chunk in parts:
        if chunk == "\n":
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if lines and lines[-1] not in ("", "\n"):
            lines[-1] = f"{lines[-1]} {chunk}".strip()
        else:
            lines.append(chunk)
    content = "\n".join(line.strip() for line in "\n".join(lines).splitlines())
    while "\n\n\n" in content:
        content = content.replace("\n\n\n", "\n\n")
    return content.strip()


def _extract_html_page(html: str) -> tuple[str, str, int]:
    """Title, page text and the number of embedded players, frames and plugins.

    Navigation, footers and asides are dropped; everything else in the body is
    kept, so no part of the page's own text is silently left out.
    """
    from html.parser import HTMLParser

    class _PageParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.skip_depth = self.boiler_depth = 0
            self.in_head = self.in_title = False
            self.title_parts, self.body_parts = [], []
            self.embeds = 0

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag == "head":
                self.in_head = True
            elif tag == "title":
                self.in_title = True
            elif tag in _SKIP_TAGS:
                self.skip_depth += 1
            else:
                if tag in _BOILERPLATE_TAGS:
                    self.boiler_depth += 1
                if (tag in _EMBED_TAGS and not self.skip_depth and not self.boiler_depth
                        and not _is_hidden_embed(dict(attrs))):
                    self.embeds += 1
                if not self.in_head and tag in _BLOCK_TAGS:
                    self.body_parts.append("\n")

        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag == "title":
                self.in_title = False
            elif tag == "head":
                self.in_head = False
            elif tag in _SKIP_TAGS:
                self.skip_depth = max(0, self.skip_depth - 1)
            else:
                if tag in _BOILERPLATE_TAGS and self.boiler_depth:
                    self.boiler_depth -= 1
                if not self.in_head and tag in _BLOCK_TAGS:
                    self.body_parts.append("\n")

        def handle_data(self, data):
            text = data.strip()
            if self.skip_depth or not text:
                return
            if self.in_title:
                self.title_parts.append(text)
            elif not self.in_head and not self.boiler_depth:
                self.body_parts.append(text)

    parser = _PageParser()
    parser.feed(html)
    title = " ".join(parser.title_parts).strip()
    return title[:200], _join_text(parser.body_parts), parser.embeds


def _extract_html_text(html: str) -> tuple[str, str]:
    """Extract a readable title/body from simple public HTML without extra deps."""
    from html.parser import HTMLParser

    class _Extractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.skip_depth = 0
            self.in_head = False
            self.in_title = False
            self.title_parts = []
            self.body_parts = []

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag == "head":
                self.in_head = True
            elif tag == "title":
                self.in_title = True
            elif tag in {"script", "style", "noscript", "svg"}:
                self.skip_depth += 1
            elif not self.in_head and tag in {
                "p", "div", "article", "section", "main", "li", "br", "h1", "h2", "h3"
            }:
                self.body_parts.append("\n")

        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag == "title":
                self.in_title = False
            elif tag == "head":
                self.in_head = False
            elif tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
                self.skip_depth -= 1
            elif not self.in_head and tag in {
                "p", "div", "article", "section", "main", "li", "h1", "h2", "h3"
            }:
                self.body_parts.append("\n")

        def handle_data(self, data):
            if self.skip_depth:
                return
            text = data.strip()
            if not text:
                return
            if self.in_title:
                self.title_parts.append(text)
                return
            if self.in_head:
                return
            self.body_parts.append(text)

    parser = _Extractor()
    parser.feed(html)

    title = " ".join(parser.title_parts).strip()
    lines = []
    for chunk in parser.body_parts:
        if chunk == "\n":
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if lines and lines[-1] not in ("", "\n"):
            lines[-1] = f"{lines[-1]} {chunk}".strip()
        else:
            lines.append(chunk)

    content = "\n".join(lines)
    content = "\n".join(line.strip() for line in content.splitlines())
    while "\n\n\n" in content:
        content = content.replace("\n\n\n", "\n\n")
    return title[:200], content.strip()


def _format_host_header(hostname: str, port: int, scheme: str) -> str:
    host = hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    return host if port == default_port else f"{host}:{port}"


def _request_pinned(
    url: str,
    timeout_seconds: float,
    headers: dict | None = None,
):
    """
    Resolve+validate once, then connect to that exact IP.

    HTTPS keeps the original hostname for SNI and certificate verification.
    """
    from x_reader.utils.url_validator import resolve_safe_ips

    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BlockedDestination(f"Pinned request requires http(s) URL: {url}")

    hostname = parsed.hostname
    ascii_hostname = hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    remaining = max(0.1, float(timeout_seconds))
    deadline = time.monotonic() + remaining
    try:
        safe_ips = resolve_safe_ips(
            hostname,
            port,
            timeout=min(remaining, IO_TIMEOUT),
        )
    except ValueError as exc:
        raise BlockedDestination(str(exc)) from exc

    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"

    request_headers = dict(headers or DIRECT_HEADERS)
    request_headers["Host"] = _format_host_header(
        ascii_hostname,
        port,
        parsed.scheme,
    )

    # Every candidate was validated above. IPv4 goes first and a failed connect
    # moves on to the next address (many hosts have IPv6 addresses but no IPv6
    # route), within the caller's time budget and at most MAX_CONNECT_ATTEMPTS.
    candidates = sorted(safe_ips, key=lambda address: ":" in address)[:MAX_CONNECT_ATTEMPTS]
    for attempt, ip in enumerate(candidates):
        left = deadline - time.monotonic()
        if left <= 0.1:
            raise TimeoutError(f"Connecting to {hostname} exceeded its time budget")
        io_timeout = min(left, IO_TIMEOUT)
        common = {
            "host": ip,
            "port": port,
            "timeout": Timeout(connect=io_timeout, read=io_timeout),
            "maxsize": 1,
            "block": True,
        }
        if parsed.scheme == "https":
            pool = HTTPSConnectionPool(
                **common,
                cert_reqs="CERT_REQUIRED",
                assert_hostname=ascii_hostname,
                server_hostname=ascii_hostname,
            )
        else:
            pool = HTTPConnectionPool(**common)
        try:
            response = pool.request(
                "GET",
                target,
                headers=request_headers,
                redirect=False,
                preload_content=False,
                retries=False,
            )
        except (NewConnectionError, ConnectTimeoutError):
            pool.close()
            if attempt == len(candidates) - 1:
                raise
            continue
        except BaseException:
            pool.close()
            raise
        return pool, response, ip
    raise BlockedDestination(f"No address to connect to for {hostname}")


def _read_bounded(
    response,
    deadline: float,
    allowed_types: set[str],
    sniff=None,
) -> tuple[bytes, str]:
    """Read a text response with Content-Type, size, and total-deadline checks.

    With sniff, a missing or generic Content-Type is accepted when sniff(body)
    recognises the bounded body (feeds served as text/plain, for example).
    """
    raw_content_type = (response.headers.get("Content-Type") or "").strip().lower()
    content_type = raw_content_type.split(";", 1)[0].strip()
    needs_sniff = content_type not in allowed_types
    if needs_sniff and (sniff is None or content_type not in _SNIFFABLE_TYPES):
        raise ValueError(
            "Unsupported Content-Type: "
            f"{raw_content_type or '<missing>'}"
        )

    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > MAX_RESPONSE_BYTES:
            raise ValueError("Response exceeds 5 MiB limit")

    chunks = []
    total = 0
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("Fetch exceeded total time limit")
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ValueError("Response exceeds 5 MiB limit")
        chunks.append(chunk)

    body = b"".join(chunks)
    if needs_sniff and not sniff(body):
        raise ValueError(
            "Unsupported Content-Type: "
            f"{raw_content_type or '<missing>'}"
        )
    return body, raw_content_type


def fetch_public_bytes(
    url: str,
    allowed_types: set[str],
    max_redirects: int = 5,
    total_timeout: float | None = None,
    headers: dict | None = None,
    sniff=None,
) -> tuple[bytes, str, str]:
    """GET a public URL with the same SSRF defenses as the direct HTML fallback.

    Every hop is re-resolved, validated against private/special addresses and
    pinned to the validated IP; bodies are size- and deadline-bounded.
    Returns (body, raw_content_type, final_url).
    """
    current = url
    deadline = time.monotonic() + (DIRECT_TOTAL_TIMEOUT if total_timeout is None else total_timeout)
    for _ in range(max_redirects + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Fetch exceeded total time limit")
        pool = None
        response = None
        try:
            pool, response, _pinned_ip = _request_pinned(current, remaining, headers=headers or DIRECT_HEADERS)
            status = response.status
            if status in _REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError(f"Redirect status {status} without Location")
                current = urljoin(current, location)
                continue
            if not 200 <= status < 300:
                raise ValueError(f"HTTP status {status}")
            body, raw_content_type = _read_bounded(response, deadline, allowed_types, sniff=sniff)
            return body, raw_content_type, current
        finally:
            if response is not None:
                response.close()
            if pool is not None:
                pool.close()
    raise requests.TooManyRedirects(f"Too many redirects while fetching: {url}")


def fetch_direct_html(url: str, max_redirects: int = 5, hop_guard=None) -> dict:
    """Direct public HTML fallback with pinned-IP SSRF defense and bounded reads."""
    html, final_url = _download_html(url, max_redirects, hop_guard)
    title, content = _extract_html_text(html)
    if len(content) < 40:
        raise ValueError(
            "Direct fallback returned too little readable body text"
        )
    return {
        "title": title or final_url,
        "content": content,
        "url": final_url,
        "author": "",
        "fetch_method": "direct_html_pinned",
    }


def fetch_html_page(url: str, max_redirects: int = 5, hop_guard=None) -> dict:
    """Article-oriented direct read used as evidence for generic pages.

    Same pinned, per-hop validated download as fetch_direct_html, but without the
    navigation/footer text, with the count of embedded audio/video players, and
    returning thin pages too (the receipt decides what thin means).
    """
    html, final_url = _download_html(url, max_redirects, hop_guard)
    title, content, media = _extract_html_page(html)
    return {
        "title": title or final_url,
        "content": content,
        "url": final_url,
        "author": "",
        "fetch_method": "direct_html_pinned",
        "embedded_media": media,
    }


def _download_html(url: str, max_redirects: int = 5, hop_guard=None) -> tuple[str, str]:
    """GET a public HTML page over pinned, validated hops; return (html, final_url).

    hop_guard(url), when given, runs on every redirect target before it is
    followed; it raises to stop the chain (e.g. CrossPlatformRedirect).
    """
    current = url
    deadline = time.monotonic() + DIRECT_TOTAL_TIMEOUT

    for _ in range(max_redirects + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Direct fallback exceeded total time limit")

        pool = None
        response = None
        try:
            logger.info(f"Direct HTML fetch: {current}")
            pool, response, _pinned_ip = _request_pinned(
                current,
                remaining,
                headers=DIRECT_HEADERS,
            )

            status = response.status
            if status in _REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError(
                        f"Direct fallback got redirect status {status} without Location"
                    )
                current = urljoin(current, location)
                if hop_guard is not None:
                    hop_guard(current)
                continue

            if not 200 <= status < 300:
                raise ValueError(f"Direct fallback HTTP status {status}")

            body, raw_content_type = _read_bounded(
                response,
                deadline,
                _DIRECT_TYPES,
            )

            charset_match = re.search(r"charset=([^;\s]+)", raw_content_type)
            encoding = (
                charset_match.group(1).strip('"\'')
                if charset_match
                else "utf-8"
            )
            return body.decode(encoding, errors="replace"), current
        finally:
            if response is not None:
                response.close()
            if pool is not None:
                pool.close()

    raise requests.TooManyRedirects(f"Too many redirects while fetching: {url}")


def fetch_via_jina(url: str) -> dict:
    """
    Fetch a target URL via r.jina.ai using the same pinned-IP network boundary.

    Redirects from Jina itself are not followed automatically. A non-2xx response
    fails closed so the generic reader can try the direct pinned-HTML path.
    """
    jina_url = f"{JINA_BASE}/{url}"
    logger.info(f"Jina fetch: {url}")
    deadline = time.monotonic() + JINA_TOTAL_TIMEOUT

    pool = None
    response = None
    try:
        pool, response, _pinned_ip = _request_pinned(
            jina_url,
            JINA_TOTAL_TIMEOUT,
            headers=HEADERS,
        )

        status = response.status
        if status in _REDIRECT_STATUSES:
            raise ValueError(
                f"Jina returned redirect status {status}; redirects are disabled"
            )
        if not 200 <= status < 300:
            raise ValueError(f"Jina HTTP status {status}")

        body, raw_content_type = _read_bounded(
            response,
            deadline,
            _JINA_TYPES,
        )

        charset_match = re.search(r"charset=([^;\s]+)", raw_content_type)
        encoding = (
            charset_match.group(1).strip('"\'')
            if charset_match
            else "utf-8"
        )
        text = body.decode(encoding, errors="replace")

        lines = text.strip().split("\n")
        title = ""
        content_lines = []
        for line in lines:
            if not title and line.strip():
                title = line.lstrip("#").strip()
            else:
                content_lines.append(line)

        content = "\n".join(content_lines).strip()
        if len(content) < 20:
            raise ValueError("Jina returned too little readable content")

        return {
            "title": title[:200],
            "content": content,
            "url": url,
            "author": "",
            "fetch_method": "jina_pinned",
        }
    finally:
        if response is not None:
            response.close()
        if pool is not None:
            pool.close()
