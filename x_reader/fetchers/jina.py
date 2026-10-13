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


_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
    "source", "track", "wbr",
}


_IMPORTANT = re.compile(r"!\s*important\s*$", re.IGNORECASE)


def _inline_declarations(style: str) -> list | None:
    """Parse an inline style into (property, value, important) declarations.

    Comments, quoted strings and parentheses are honoured, so a ";" or
    "display:none" inside them is not a declaration. None when the style cannot
    be parsed with confidence (escapes, unterminated comment or string).
    """
    if "\\" in style:
        return None  # CSS escapes can spell any property ("dis\\play")
    declarations, current, quote, depth, i = [], [], "", 0, 0
    while i < len(style):
        char = style[i]
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
        elif style.startswith("/*", i):
            end = style.find("*/", i + 2)
            if end == -1:
                return None
            i = end + 2
            continue
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == ";" and not depth:
            declarations.append("".join(current))
            current = []
        else:
            current.append(char)
        i += 1
    if quote:
        return None
    declarations.append("".join(current))
    parsed = []
    for declaration in declarations:
        name, colon, value = declaration.partition(":")
        if not colon:
            continue
        value = value.strip()
        important = bool(_IMPORTANT.search(value))
        parsed.append((name.strip().lower(), _IMPORTANT.sub("", value).strip().lower(), important))
    return parsed


def _effective(declarations: list, prop: str) -> str | None:
    """The value a property ends up with: the last !important one, else the last one."""
    winner, winner_important = None, False
    for name, value, important in declarations:
        if name == prop and (important or not winner_important):
            winner, winner_important = value, important
    return winner


def _is_hidden_element(attrs: dict) -> bool:
    """Provably not rendered: an effective inline display:none, or the hidden
    attribute with no inline display overriding it. Anything else counts as
    visible: a style that cannot be parsed, visibility (children can undo it),
    external CSS and aria-hidden (assistive technology only)."""
    declarations = _inline_declarations(attrs.get("style") or "")
    if declarations is None:
        return False
    display = _effective(declarations, "display")
    if display is not None:
        return display == "none"
    return "hidden" in attrs


_TINY = {"0", "1", "0px", "1px"}
# Inline properties that cannot make a 0/1-pixel frame show its content.
_SIZE_NEUTRAL = {
    "display", "visibility", "border", "position", "top", "left", "right", "bottom", "margin",
    "padding", "opacity", "overflow", "clip", "clip-path", "z-index", "pointer-events", "float",
    "width", "height",
}


def _is_hidden_embed(attrs: dict) -> bool:
    """Hidden frames, or frames whose effective size is 0/1 pixel each way
    (tracking pixels). Inline width/height override the attributes; any other
    inline property that could enlarge the frame (transform, zoom, min-height,
    ...) makes it count as visible."""
    if _is_hidden_element(attrs):
        return True
    declarations = _inline_declarations(attrs.get("style") or "")
    if declarations is None:
        return False
    for name, _value, _important in declarations:
        if name not in _SIZE_NEUTRAL and not name.startswith(("border-", "margin-", "padding-")):
            return False
    width = _effective(declarations, "width")
    height = _effective(declarations, "height")
    width = attrs.get("width") or "" if width is None else width
    height = attrs.get("height") or "" if height is None else height
    return width.strip().lower() in _TINY and height.strip().lower() in _TINY


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


def _extract_html_page(html: str) -> tuple[str, str, int, str, bool]:
    """Title, page text, embedded players/frames/plugins, page-wide text, and
    whether the page text is the page's marked primary content.

    Page text: navigation, footers, asides and explicitly hidden elements are
    dropped; when the page marks its primary content (every <main>, else every
    <article>) and that region carries substantial prose, the text is that
    region, so banners around it stay out. The page-wide text is every visible
    block, navigation and footers included, so wall notices anywhere are seen.
    Embeds are counted over the whole page, except hidden ones.
    """
    from html.parser import HTMLParser
    from x_reader.evidence import MIN_GENERIC_BODY_CHARS, prose_chars

    class _PageParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.skip_depth = self.boiler_depth = 0
            self.main_depth = self.article_depth = 0
            self.hidden_tag, self.hidden_nest = None, 0
            self.in_head = self.in_title = False
            self.title_parts, self.body_parts, self.gate_parts = [], [], []
            self.main_parts, self.article_parts = [], []
            self.embeds = 0

        def _append(self, chunk):
            if self.hidden_tag:
                return
            self.gate_parts.append(chunk)
            if self.boiler_depth:
                return
            self.body_parts.append(chunk)
            if self.main_depth:
                self.main_parts.append(chunk)
            if self.article_depth:
                self.article_parts.append(chunk)

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            values = {}
            for name, value in attrs:
                values.setdefault(name, value)  # browsers keep the first of repeated attributes
            if tag == "head":
                self.in_head = True
                return
            if tag == "title":
                self.in_title = True
                return
            if tag in _SKIP_TAGS:
                self.skip_depth += 1
                return
            if self.hidden_tag is None:
                if tag not in _VOID_TAGS and _is_hidden_element(values):
                    self.hidden_tag, self.hidden_nest = tag, 1
            elif tag == self.hidden_tag:
                self.hidden_nest += 1
            if tag in _BOILERPLATE_TAGS:
                self.boiler_depth += 1
            if tag == "main":
                self.main_depth += 1
            elif tag == "article":
                self.article_depth += 1
            if (tag in _EMBED_TAGS and not self.skip_depth and not self.hidden_tag
                    and not _is_hidden_embed(values)):
                self.embeds += 1
            if not self.in_head and tag in _BLOCK_TAGS:
                self._append("\n")

        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag == "title":
                self.in_title = False
                return
            if tag == "head":
                self.in_head = False
                return
            if tag in _SKIP_TAGS:
                self.skip_depth = max(0, self.skip_depth - 1)
                return
            if not self.in_head and tag in _BLOCK_TAGS:
                self._append("\n")
            if tag in _BOILERPLATE_TAGS and self.boiler_depth:
                self.boiler_depth -= 1
            if tag == "main" and self.main_depth:
                self.main_depth -= 1
            elif tag == "article" and self.article_depth:
                self.article_depth -= 1
            if self.hidden_tag is not None and tag == self.hidden_tag:
                self.hidden_nest -= 1
                if self.hidden_nest == 0:
                    self.hidden_tag = None

        def handle_data(self, data):
            text = data.strip()
            if self.skip_depth or not text:
                return
            if self.in_title:
                self.title_parts.append(text)
            elif not self.in_head:
                self._append(text)

    parser = _PageParser()
    parser.feed(html)
    title = " ".join(parser.title_parts).strip()
    content, primary_used = _join_text(parser.body_parts), False
    for region in (parser.main_parts, parser.article_parts):
        primary = _join_text(region)
        if primary and prose_chars(primary) >= MIN_GENERIC_BODY_CHARS:
            content, primary_used = primary, True
            break
    return title[:200], content, parser.embeds, _join_text(parser.gate_parts), primary_used


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
    from x_reader.evidence import gate_signals

    html, final_url = _download_html(url, max_redirects, hop_guard)
    title, content, media, page_text, primary_used = _extract_html_page(html)
    # Walls announced anywhere on the page count, not only in the extracted text.
    # A cookie banner or consent overlay beside substantial marked main content
    # hides nothing from this read (the article is in the HTML), so it is the
    # one signal that only counts inside the text itself.
    signals = gate_signals(title, page_text)
    if primary_used:
        signals = [s for s in signals if s not in ("consent", "consent_title")]
    return {
        "title": title or final_url,
        "content": content,
        "url": final_url,
        "author": "",
        "fetch_method": "direct_html_pinned",
        "embedded_media": media,
        "page_signals": signals,
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


_JINA_HEADER = re.compile(r"^(Title|URL Source|Published Time|Warning|Description|Number of Pages):[ \t]?(.*)$")


def _parse_jina_text(text: str) -> tuple:
    """Split a Jina Reader response into (title, content, header fields).

    Responses open with "Title: ...", "URL Source: ..." (the page Jina read) and
    optional "Published Time:" / "Warning:" lines, then "Markdown Content:".
    Anything else is a first-line title followed by the content.
    """
    lines = text.strip().split("\n")
    if not lines or not lines[0].startswith("Title:"):
        title, content_lines = "", []
        for line in lines:
            if not title and line.strip():
                title = line.lstrip("#").strip()
            else:
                content_lines.append(line)
        return title, "\n".join(content_lines).strip(), {}
    headers: dict = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip() == "Markdown Content:":
            index += 1
            break
        match = _JINA_HEADER.match(line)
        if match:
            headers.setdefault(match.group(1).lower(), []).append(match.group(2).strip())
        elif line.strip():
            break
        index += 1
    return (headers.get("title") or [""])[0], "\n".join(lines[index:]).strip(), headers


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

        title, content, headers = _parse_jina_text(text)
        if len(content) < 20:
            raise ValueError("Jina returned too little readable content")

        return {
            "title": title[:200],
            "content": content,
            "url": url,
            "source_url": (headers.get("url source") or [""])[0],
            "author": "",
            "fetch_method": "jina_pinned",
        }
    finally:
        if response is not None:
            response.close()
        if pool is not None:
            pool.close()
