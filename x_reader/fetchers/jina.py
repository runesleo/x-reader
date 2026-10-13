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


# Never rendered as page text: head, scripts and styles, <noscript> (raw text
# while scripts run), inert templates (one declaring a shadow root renders), the
# fallback inside players, frames and canvases, and legacy no-frame blocks.
_SKIP_TAGS = {
    "head", "title", "script", "style", "noscript", "template", "iframe", "object", "video", "audio",
    "canvas", "noembed", "noframes", "datalist",
}
# Form controls show their text, but in a box or a menu: walls count, page text does not.
_CONTROL_TAGS = {"select", "textarea"}
_BUTTON_INPUTS = {"submit", "button", "reset"}
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
_OPTIONAL_END_TAGS = {
    "p", "li", "dt", "dd", "option", "optgroup", "tr", "td", "th", "thead", "tbody", "tfoot",
    "colgroup", "caption", "rb", "rt", "rtc", "rp", "html", "head", "body",
}
_RAW_TEXT_TAGS = (
    "script", "style", "textarea", "title", "xmp", "iframe", "noembed", "noframes", "noscript", "plaintext",
)


_IMPORTANT = re.compile(r"!\s*important\s*$", re.IGNORECASE)
_CLOSERS = {"(": ")", "[": "]", "{": "}"}


def _inline_declarations(style: str) -> list | None:
    """Parse an inline style into (property, value, important) declarations.

    A comment ends a token (it reads as a space), and quoted strings and (), [],
    {} blocks stay whole, so a ";" or "display:none" inside them is not a
    declaration. None when the style cannot be parsed with confidence (escapes,
    an unterminated comment or string, unbalanced brackets).
    """
    if "\\" in style:
        return None  # CSS escapes can spell any property ("dis\\play")
    declarations, current, quote, open_blocks, i = [], [], "", [], 0
    while i < len(style):
        char = style[i]
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            elif char == "\n":
                return None
        elif style.startswith("/*", i):
            end = style.find("*/", i + 2)
            if end == -1:
                return None
            current.append(" ")
            i = end + 2
            continue
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char in _CLOSERS:
            open_blocks.append(_CLOSERS[char])
            current.append(char)
        elif char in ")]}":
            if not open_blocks or open_blocks.pop() != char:
                return None
            current.append(char)
        elif char == ";" and not open_blocks:
            declarations.append("".join(current))
            current = []
        else:
            current.append(char)
        i += 1
    if quote or open_blocks:
        return None
    declarations.append("".join(current))
    parsed = []
    for declaration in declarations:
        name, colon, value = declaration.partition(":")
        if not colon:
            continue
        value = value.strip()
        important = bool(_IMPORTANT.search(value))
        parsed.append((name.strip().lower(), " ".join(_IMPORTANT.sub("", value).split()).lower(), important))
    return parsed


_UNRESOLVED = "unresolved"  # a value only known at render time (var(), env(), attr())


def _effective(declarations: list, prop: str, valid) -> str | None:
    """The value a property ends up with: the last valid !important one, else
    the last valid one. Invalid declarations are dropped, as browsers do;
    values that need custom properties resolve to _UNRESOLVED."""
    winner, winner_important = None, False
    for name, value, important in declarations:
        if name != prop:
            continue
        if re.search(r"\b(?:var|env|attr)\(", value):
            value = _UNRESOLVED
        elif not valid(value):
            continue
        if important or not winner_important:
            winner, winner_important = value, important
    return winner


_DISPLAY_KEYWORDS = {
    "none", "block", "inline", "inline-block", "flex", "inline-flex", "grid", "inline-grid",
    "flow-root", "contents", "table", "inline-table", "table-row", "table-cell", "table-row-group",
    "table-header-group", "table-footer-group", "table-column", "table-column-group",
    "table-caption", "list-item", "run-in", "ruby", "ruby-base", "ruby-text", "ruby-base-container",
    "ruby-text-container", "math", "inherit", "initial", "unset", "revert", "revert-layer",
    "-webkit-box", "-webkit-inline-box", "-webkit-flex", "-webkit-inline-flex", "-moz-box",
    "-moz-inline-box", "-ms-flexbox", "-ms-inline-flexbox", "-ms-grid", "-ms-inline-grid",
}
_DISPLAY_PARTS = {"block", "inline", "run-in", "flow", "flow-root", "table", "flex", "grid", "ruby", "list-item", "math"}


def _valid_display(value: str) -> bool:
    if value in _DISPLAY_KEYWORDS:
        return True
    parts = value.split()
    return 1 < len(parts) <= 3 and len(set(parts)) == len(parts) and all(p in _DISPLAY_PARTS for p in parts)


_CSS_LENGTH = re.compile(
    r"([+]?(?:\d+\.?\d*|\.\d+))(px|em|rem|%|vw|vh|vmin|vmax|vi|vb|pt|pc|cm|mm|in|q|ex|ch|lh|rlh"
    r"|cqw|cqh|cqi|cqb|cqmin|cqmax|svh|lvh|dvh|svw|lvw|dvw)"
)
_CSS_SIZE_WORDS = {
    "auto", "min-content", "max-content", "fit-content", "stretch", "inherit", "initial", "unset",
    "revert", "revert-layer",
}


def _valid_css_size(value: str) -> bool:
    return (value == "0" or bool(_CSS_LENGTH.fullmatch(value)) or value in _CSS_SIZE_WORDS
            or value.startswith(("calc(", "min(", "max(", "clamp(", "fit-content(")))


def _tiny_css_size(value: str) -> bool:
    """Zero in any unit, or at most one pixel."""
    if value == "0":
        return True
    match = _CSS_LENGTH.fullmatch(value)
    return bool(match) and (float(match.group(1)) == 0 or (match.group(2) == "px" and float(match.group(1)) <= 1))


def _tiny_html_size(value: str) -> bool:
    """An HTML width/height attribute of 0 or 1 pixel (leading digits, as browsers parse them)."""
    match = re.match(r"\s*(\d+)(\.\d*)?(%?)", value or "")
    return bool(match) and not match.group(3) and int(match.group(1)) <= 1


def _small_number(value: str, limit: float) -> bool:
    match = re.fullmatch(r"([+-]?(?:\d+\.?\d*|\.\d+))(%?)", value)
    return bool(match) and float(match.group(1)) / (100 if match.group(2) else 1) < limit


_ALWAYS_HIDING = {
    "all", "clip", "clip-path", "mask", "mask-image", "-webkit-mask", "-webkit-mask-image", "transform",
    "translate", "scale", "rotate", "filter", "-webkit-line-clamp", "line-clamp",
}
_SIZES = {"height", "max-height", "width", "max-width", "block-size", "inline-size", "max-block-size", "max-inline-size"}
_OVERFLOWS = {"overflow", "overflow-x", "overflow-y", "overflow-block", "overflow-inline"}
# A custom property in these can switch the text off; in colors, sizes and fonts
# it is a design token, judged like external CSS.
_UNRESOLVED_HIDING = {"visibility", "content-visibility", "opacity", "display"}


def _hides_text(name: str, value: str) -> bool:
    """An inline declaration that can hide, clip or shrink an element's text away."""
    if name in _ALWAYS_HIDING:
        return value not in ("none", "auto", "initial", "unset", "normal", "revert", "revert-layer")
    if re.search(r"\b(?:var|env|attr)\(", value):
        return name in _UNRESOLVED_HIDING
    if name == "visibility":
        return value in ("hidden", "collapse")
    if name == "content-visibility":
        return value == "hidden"
    if name == "opacity":
        return _small_number(value, 0.1)
    if name in ("font-size", "zoom"):
        return value == "0" or _tiny_css_size(value) or _small_number(value, 0.1)
    if name in ("color", "-webkit-text-fill-color"):
        return value == "transparent" or bool(re.search(r"[,/]\s*0*\.?0*%?\s*\)$", value))
    if name == "text-indent":
        return value.startswith("-")
    if name in _SIZES:
        return _tiny_css_size(value)
    if name == "position":
        return value in ("absolute", "fixed")  # with offsets it may sit off-screen
    return False


def _content_visibility(attrs: dict) -> str:
    """Whether an element's own markup shows its text: "hidden" (the hidden
    attribute, or display:none as its only display declaration), "unknown" (a
    style that cannot be parsed, or any other declaration that can hide, clip
    or cut off text) or "visible". It decides only what counts as page text:
    wall notices and frames count wherever they are."""
    declarations = _inline_declarations(attrs.get("style") or "")
    if declarations is None:
        return "unknown"
    names = {name for name, _value, _important in declarations}
    if any(_hides_text(name, value) for name, value, _important in declarations):
        return "unknown"
    if names & _SIZES and any(
            name in _OVERFLOWS and value in ("hidden", "clip", "scroll", "auto") for name, value, _i in declarations):
        return "unknown"  # a box that clips or scrolls its text
    displays = [value for name, value, _important in declarations if name == "display"]
    if displays:
        if not all(_valid_display(value) for value in displays):
            return "unknown"
        if "none" in displays:
            return "hidden" if len(displays) == 1 else "unknown"
        return "visible"  # an inline display overrides the hidden attribute
    return "hidden" if "hidden" in attrs else "visible"


# Inline properties that cannot make a 0/1-pixel frame show its content.
_SIZE_NEUTRAL = {
    "display", "visibility", "border", "position", "top", "left", "right", "bottom", "margin",
    "padding", "opacity", "overflow", "clip", "clip-path", "z-index", "pointer-events", "float",
    "width", "height",
}


def _is_tracking_frame(attrs: dict) -> bool:
    """A frame with nothing to show: the hidden attribute or display:none on the
    frame itself, or 0/1 pixel each way (a valid inline width/height overrides
    the attribute, an invalid one is ignored, without either the frame keeps its
    visible default size). Any other inline property that could show or enlarge
    it (all, transform, zoom, min-height, ...) or a style that cannot be parsed
    makes it count. A hidden container does not hide a frame."""
    declarations = _inline_declarations(attrs.get("style") or "")
    if declarations is None:
        return False
    for name, _value, _important in declarations:
        if name not in _SIZE_NEUTRAL and not name.startswith(("border-", "margin-", "padding-")):
            return False
    displays = [value for name, value, _important in declarations if name == "display"]
    if displays == ["none"] or (not displays and "hidden" in attrs):
        return True

    def tiny(prop: str) -> bool:
        value = _effective(declarations, prop, _valid_css_size)
        if value is None:
            return _tiny_html_size(attrs.get(prop) or "")
        return value != _UNRESOLVED and _tiny_css_size(value)

    return tiny("width") and tiny("height")


def _join_text(parts: list) -> str:
    """Inline chunks joined by spaces, blocks separated by a blank line."""
    blocks, current = [], []
    for chunk in parts:
        if chunk == "\n":
            if current:
                blocks.append(" ".join(current))
                current = []
        else:
            current.append(chunk)
    if current:
        blocks.append(" ".join(current))
    content = "\n".join(line.strip() for line in "\n\n".join(blocks).splitlines())
    return re.sub(r"\n{3,}", "\n\n", content).strip()


# Parse budgets: more tags or deeper nesting than this is refused before parsing.
MAX_PARSE_TAGS = 150_000
MAX_PARSE_DEPTH = 1_500
MAX_PARSE_WORK = 20_000_000
_TAG_TOKEN = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9:-]*)")


def _prescan_markup(html: str) -> tuple:
    """(too_complex, templates_unsure), in one linear pass over the tags.

    too_complex: over the tag, nesting or parse-work budget. Open elements are
    tracked the way a parser keeps them (an end tag closes its element and
    whatever is still open inside it; stray end tags change nothing; optional
    end tags, void elements and raw-text contents such as scripts do not
    nest), and the work estimate adds the open depth at every tag, which is
    what a tree builder's scope checks cost. templates_unsure: a <template>
    whose content closes an element it did not open or leaves one open; the
    parser does not model template contents, so such markup could reshape the
    page around it.
    """
    if html.count("<") > MAX_PARSE_TAGS:
        return True, False
    lowered = html.lower()
    stack: list = []  # open element names
    open_count: dict = {}
    templates: list = []  # per open template: {tag: open count inside it}
    work = foreign = 0
    unsure = False
    position = 0
    while True:
        match = _TAG_TOKEN.search(html, position)
        if not match:
            break
        closing, tag = bool(match.group(1)), match.group(2).lower()
        position = match.end()
        work += len(stack)
        if work > MAX_PARSE_WORK:
            return True, unsure
        tag_end = html.find(">", position)
        # "/>" closes an element only inside SVG/MathML; in HTML it is ignored.
        self_closed = foreign > 0 and tag_end != -1 and html[tag_end - 1] == "/"
        if tag in ("svg", "math") and not closing and not self_closed:
            foreign += 1
        elif tag in ("svg", "math") and closing:
            foreign = max(0, foreign - 1)
        if self_closed:
            continue
        if closing:
            if templates:
                if templates[-1].get(tag):
                    templates[-1][tag] -= 1
                elif tag == "template":
                    unsure |= any(count for t, count in templates.pop().items() if t not in _OPTIONAL_END_TAGS)
                elif tag not in _OPTIONAL_END_TAGS:
                    unsure = True
            if open_count.get(tag):
                while stack:
                    closed = stack.pop()
                    open_count[closed] -= 1
                    if closed == tag:
                        break
            continue
        if tag == "template":
            templates.append({})
        elif templates and tag not in _VOID_TAGS:
            templates[-1][tag] = templates[-1].get(tag, 0) + 1
        if tag not in _VOID_TAGS and tag not in _OPTIONAL_END_TAGS:
            stack.append(tag)
            open_count[tag] = open_count.get(tag, 0) + 1
            if len(stack) > MAX_PARSE_DEPTH:
                return True, unsure
        if tag in _RAW_TEXT_TAGS:
            end = lowered.find("</" + tag, position)
            if end == -1:
                break
            position = end
    return False, unsure or bool(templates)


def _extract_html_page(html: str) -> dict:
    """{title, content, embeds, page_text, primary_used, visibility_unknown, too_complex}.

    The page is parsed with a standard HTML5 tree builder (scripting on, so
    <noscript> is raw text), so misnested or unclosed markup ends where a
    browser ends it. content is the page text: navigation, footers, asides,
    hidden elements and foreign (SVG/MathML) text are left out; when the page
    marks its primary content (every <main>, else every <article>) and that
    region carries substantial prose, content is that region, so banners around
    it stay out. page_text is every rendered text block, hidden ones included,
    so wall notices count wherever they sit. embeds counts players, frames and
    plugins page-wide, except frames that are themselves hidden or 0/1-pixel.
    visibility_unknown: content includes text under markup whose visibility
    x-reader cannot settle, or template markup that could reshape the page.
    client_redirect: a <meta http-equiv="refresh"> sends visitors to another URL.
    Closed <details> (beyond the summary) and <dialog> hide their text; menus
    and text boxes, button labels and placeholders count only as wall wording.
    """
    import html5lib
    from x_reader.evidence import MIN_GENERIC_BODY_CHARS, prose_chars

    too_complex, templates_unsure = _prescan_markup(html)
    if too_complex:
        return {"title": "", "content": "", "embeds": 0, "page_text": "", "primary_used": False,
                "visibility_unknown": True, "too_complex": True, "client_redirect": False}
    root = html5lib.parse(html, treebuilder="etree", namespaceHTMLElements=False, scripting=True)
    title_element = next(iter(root.iter("title")), None)
    title = " ".join("".join(title_element.itertext()).split()) if title_element is not None else ""

    parts: dict = {"gate": [], "body": [], "main": [], "article": []}
    unknown_in: set = set()
    embeds = 0
    client_redirect = any(
        (meta.get("http-equiv") or "").strip().lower() == "refresh" and "url" in (meta.get("content") or "").lower()
        for meta in root.iter("meta")
    )
    base = {"skip": False, "foreign": False, "boiler": False, "main": False, "article": False,
            "hidden": False, "unknown": False, "block": False, "collapsed": False, "summary_hidden": False}

    def emit(text, state) -> None:
        if not text or state["skip"]:
            return
        chunk = text if text == "\n" else text.strip()
        if not chunk:
            return
        parts["gate"].append(chunk)
        if state["foreign"] or state["boiler"] or state["hidden"] or state.get("control"):
            return
        regions = ["body"] + [region for region in ("main", "article") if state[region]]
        if state["unknown"] and chunk != "\n":
            unknown_in.update(regions)
        for region in regions:
            parts[region].append(chunk)

    def child_state(element, parent) -> dict:
        nonlocal embeds
        tag = element.tag
        foreign = parent["foreign"] or tag.startswith("{")
        name = tag.rsplit("}", 1)[-1].lower()
        attrs = {str(key).rsplit("}", 1)[-1]: value for key, value in element.attrib.items()}
        if name in _EMBED_TAGS and not parent["skip"] and not _is_tracking_frame(attrs):
            embeds += 1
        shadow_root = name == "template" and ("shadowrootmode" in attrs or "shadowroot" in attrs)
        visibility = "visible" if foreign else _content_visibility(attrs)
        hidden = parent["hidden"]
        if parent["collapsed"]:
            # A closed <details> shows only its summary.
            hidden = parent["summary_hidden"] if name == "summary" else True
        closed = not foreign and "open" not in attrs and name in ("details", "dialog")
        return {
            "skip": parent["skip"] or (not foreign and name in _SKIP_TAGS and not shadow_root),
            "foreign": foreign,
            "boiler": parent["boiler"] or (not foreign and name in _BOILERPLATE_TAGS),
            "main": parent["main"] or name == "main",
            "article": parent["article"] or name == "article",
            "hidden": hidden or visibility == "hidden" or (closed and name == "dialog") or (closed and name == "details"),
            "unknown": parent["unknown"] or visibility == "unknown",
            "block": not foreign and name in _BLOCK_TAGS,
            "control": parent.get("control") or (not foreign and name in _CONTROL_TAGS),
            "collapsed": closed and name == "details",
            "summary_hidden": hidden or visibility == "hidden",
            "button_text": name == "input" and (attrs.get("type") or "").strip().lower() in _BUTTON_INPUTS
            and (attrs.get("value") or ""),
            "placeholder": attrs.get("placeholder") or "" if name in ("input", "textarea") else "",
        }

    stack: list = []

    def enter(element, parent) -> None:
        state = child_state(element, parent)
        if state["block"]:
            emit("\n", state)
        for label in (state["button_text"], state["placeholder"]):
            if label:  # visible wording that is not a text node: a wall's button or prompt
                emit(label, dict(state, control=True))
        emit(element.text, state)
        stack.append((element, state, iter(list(element)), parent))

    enter(root, base)
    while stack:
        element, state, children, parent = stack[-1]
        child = next(children, None)
        if child is None:
            stack.pop()
            if state["block"]:
                emit("\n", state)
            emit(element.tail, parent)
        elif not isinstance(child.tag, str):  # comment or processing instruction
            emit(child.tail, state)
        else:
            enter(child, state)

    content, primary_used, chosen = _join_text(parts["body"]), False, "body"
    for name in ("main", "article"):
        primary = _join_text(parts[name])
        if primary and prose_chars(primary) >= MIN_GENERIC_BODY_CHARS:
            content, primary_used, chosen = primary, True, name
            break
    return {
        "title": title[:200], "content": content, "embeds": embeds,
        "page_text": _join_text(parts["gate"]), "primary_used": primary_used,
        "visibility_unknown": chosen in unknown_in or templates_unsure, "too_complex": False,
        "client_redirect": client_redirect,
    }


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
    from x_reader.evidence import consent_signals, gate_signals

    html, final_url = _download_html(url, max_redirects, hop_guard)
    page = _extract_html_page(html)
    if page["too_complex"]:
        raise ValueError("The page has more markup than x-reader parses")
    title, content = page["title"], page["content"]
    # Walls announced anywhere on the page count, not only in the extracted text;
    # a standalone "Read more" call counts only inside the text itself. A cookie
    # banner or consent overlay beside substantial marked main content hides
    # nothing from this read (the article is in the HTML), so consent wording
    # counts only inside the text. The signals cover the text shown, so the
    # receipt reuses them instead of scanning it again.
    signals = set(gate_signals(title, page["page_text"], cta_text=content))
    if page["primary_used"]:
        signals -= {"consent", "consent_title"}
        signals |= set(consent_signals(title, content))
    if page["visibility_unknown"]:
        signals.add("visibility_unknown")
    if page["client_redirect"]:
        signals.add("client_redirect")
    return {
        "title": title or final_url,
        "content": content,
        "url": final_url,
        "author": "",
        "fetch_method": "direct_html_pinned",
        "embedded_media": page["embeds"],
        "page_signals": sorted(signals),
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
