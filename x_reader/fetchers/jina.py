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


_IMPORTANT = re.compile(r"!\s*important\s*$", re.IGNORECASE)
_CLOSERS = {"(": ")", "[": "]", "{": "}"}


def _inline_declarations(style: str) -> list | None:
    """Parse an inline style into (property, value, important) declarations.

    A comment ends a token (it reads as a space), and quoted strings and (), [],
    {} blocks stay whole, so a ";" or "display:none" inside them is not a
    declaration. None when the style cannot be parsed with confidence (escapes,
    an unterminated comment or string, unbalanced brackets).
    """
    if "\\" in style or len(style) > 4000:
        return None  # CSS escapes can spell any property ("dis\\play"); very long styles are not parsed
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
    r"\+?(\d{1,6}(?:\.\d{1,6})?|\.\d{1,6})(px|em|rem|%|vw|vh|vmin|vmax|vi|vb|pt|pc|cm|mm|in|q|ex|ch|lh|rlh"
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
    match = re.match(r"\s*(\d{1,9})(\.\d*)?(%?)", value or "")
    return bool(match) and not match.group(3) and int(match.group(1)) <= 1


_NUMBER = r"(?:\d{1,6}(?:\.\d{1,6})?|\.\d{1,6})"
_PLAIN_NUMBER = re.compile(rf"\+?({_NUMBER})(%?)")
_PLAIN_LENGTH = re.compile(rf"([+-]?)({_NUMBER})(px|pt|pc|em|rem|ex|ch|%|vw|vh|vmin|vmax|cm|mm|in|q)")
_VISIBLE_DISPLAYS = {
    "block", "inline", "inline-block", "flex", "inline-flex", "grid", "inline-grid", "flow-root", "table",
    "inline-table", "list-item", "table-row", "table-cell", "table-row-group", "table-header-group",
    "table-footer-group", "table-caption", "contents", "ruby", "-webkit-box", "-webkit-inline-box",
    "-ms-flexbox", "-ms-inline-flexbox",
}
_FONT_KEYWORDS = {"xx-small": 9, "x-small": 10, "small": 13, "medium": 16, "large": 18, "x-large": 24,
                  "xx-large": 32, "xxx-large": 48}
_ABSOLUTE_PX = {"px": 1, "pt": 4 / 3, "pc": 16, "cm": 96 / 2.54, "mm": 96 / 25.4, "q": 96 / 101.6, "in": 96}
_MIN_TEXT_PX = 6  # smaller than this does not read as text
_MIN_OPACITY = 0.1
_NAMED_COLOR = re.compile(r"[a-z]{3,30}")
_HEX_COLOR = re.compile(r"#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})")
_FUNC_COLOR = re.compile(r"(rgba?|hsla?)\(([^()]{1,80})\)")
# Properties whose value cannot hide, clip, shrink, move or recolor text away.
_INERT_PREFIXES = (
    "--", "background", "border", "outline", "padding", "list-style", "grid", "flex", "align-", "justify-",
    "place-", "column-", "row-", "gap", "font-variant", "font-feature", "font-kerning", "font-optical",
    "font-stretch", "font-synthesis", "text-decoration", "text-emphasis", "text-underline", "scroll-", "break-",
    "page-break", "counter-", "caret", "accent-color", "transition", "box-decoration",
)
_INERT_PROPERTIES = {
    "font-weight", "font-style", "font-family", "text-align", "text-align-last", "text-transform",
    "text-rendering", "text-shadow", "text-justify", "vertical-align", "float", "clear", "cursor", "box-sizing",
    "box-shadow", "direction", "unicode-bidi", "writing-mode", "text-orientation", "word-break",
    "overflow-wrap", "word-wrap", "hyphens", "tab-size", "quotes", "table-layout", "border-collapse",
    "border-spacing", "caption-side", "empty-cells", "orphans", "widows", "pointer-events", "user-select",
    "-webkit-user-select", "-moz-user-select", "-ms-user-select", "will-change", "object-fit",
    "object-position", "aspect-ratio", "resize", "min-width", "min-height", "min-block-size",
    "min-inline-size", "isolation", "z-index", "order", "white-space", "text-overflow", "line-height",
    "content", "-webkit-font-smoothing", "-moz-osx-font-smoothing", "-webkit-tap-highlight-color",
    "-webkit-text-size-adjust", "text-size-adjust", "touch-action", "speak", "image-rendering",
    "-webkit-appearance", "appearance", "-moz-appearance", "color-scheme", "forced-color-adjust",
    "-webkit-touch-callout", "-webkit-overflow-scrolling", "scrollbar-width", "scrollbar-color",
    "scrollbar-gutter", "-webkit-box-orient", "-webkit-box-pack", "-webkit-box-align", "-ms-flex-align",
    "-ms-flex-pack", "-ms-flex-direction", "-ms-flex-wrap", "-webkit-flex-direction", "-webkit-flex-wrap",
    "-webkit-align-items", "-webkit-justify-content", "text-wrap", "text-wrap-mode", "text-wrap-style",
    "font-display", "line-break", "word-spacing-adjust", "hanging-punctuation", "ruby-position",
    "print-color-adjust", "-webkit-print-color-adjust", "shape-outside", "shape-margin", "inset-area",
    "anchor-name", "container-name", "container-type", "counter-reset", "counter-increment",
    "width", "height", "max-width", "max-height", "block-size", "inline-size", "max-block-size",
    "max-inline-size", "top", "left", "right", "bottom", "inset", "inset-block", "inset-inline",
    "inset-block-start", "inset-block-end", "inset-inline-start", "inset-inline-end", "overflow",
    "overflow-x", "overflow-y", "overflow-block", "overflow-inline", "overflow-anchor", "overscroll-behavior",
}
_SIZES = {"height", "max-height", "width", "max-width", "block-size", "inline-size", "max-block-size", "max-inline-size"}
_OVERFLOWS = {"overflow", "overflow-x", "overflow-y", "overflow-block", "overflow-inline"}
_OFFSETS = {"top", "left", "right", "bottom", "inset", "inset-block", "inset-inline", "inset-block-start",
            "inset-block-end", "inset-inline-start", "inset-inline-end"}
_MARGINS = {"margin", "margin-left", "margin-right", "margin-top", "margin-bottom", "margin-inline",
            "margin-inline-start", "margin-inline-end", "margin-block", "margin-block-start", "margin-block-end"}
_NONE_ONLY = {
    "transform", "translate", "scale", "rotate", "filter", "backdrop-filter", "clip", "clip-path", "mask",
    "mask-image", "mask-size", "mask-position", "mask-repeat", "-webkit-mask", "-webkit-mask-image",
    "-webkit-line-clamp", "line-clamp", "contain", "animation", "animation-name", "mix-blend-mode",
    "offset", "offset-path", "perspective",
}


def _opaque(alpha: str) -> bool:
    match = _PLAIN_NUMBER.fullmatch(alpha.strip())
    return bool(match) and float(match.group(1)) / (100 if match.group(2) else 1) >= _MIN_OPACITY


def _shown_color(value: str) -> bool:
    """A color x-reader can tell is not transparent."""
    if value == "transparent":
        return False
    if _NAMED_COLOR.fullmatch(value):
        return True
    if value.startswith("var("):
        # A design-token color, judged like external CSS, unless its fallback is transparent.
        return "transparent" not in value and not re.search(r"\b(?:rgba|hsla|rgb|hsl)\(", value)
    hexcolor = _HEX_COLOR.fullmatch(value)
    if hexcolor:
        digits = hexcolor.group(1)
        if len(digits) in (3, 6):
            return True
        alpha = digits[-1] * 2 if len(digits) == 4 else digits[-2:]
        return int(alpha, 16) >= 26
    func = _FUNC_COLOR.fullmatch(value)
    if not func:
        return False
    body = func.group(2)
    if "/" in body:
        channels, _, alpha = body.partition("/")
        return len(channels.split()) == 3 and _opaque(alpha)
    parts = [part.strip() for part in body.split(",")] if "," in body else body.split()
    return len(parts) == 3 or (len(parts) == 4 and _opaque(parts[3]))


def _length_px(value: str, font_px: float):
    """A plain length in CSS pixels (None for anything else)."""
    if value == "0":
        return 0.0
    match = _PLAIN_LENGTH.fullmatch(value)
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    number, unit = float(match.group(2)), match.group(3)
    if unit in _ABSOLUTE_PX:
        return sign * number * _ABSOLUTE_PX[unit]
    if unit in ("em",):
        return sign * number * font_px
    if unit == "rem":
        return sign * number * 16
    if unit in ("ex", "ch"):
        return sign * number * font_px / 2
    if unit in ("vw", "vh", "vmin", "vmax"):
        return sign * number * 10  # a 1000px viewport
    return None  # percentages depend on the box


def _font_px(value: str, parent_px: float):
    """The computed font size for a font-size value, or None when not understood."""
    if value.startswith("var(") and value.endswith(")"):
        _token, comma, fallback = value[4:-1].partition(",")
        # A design token is judged like external CSS; a fallback must itself be readable.
        return parent_px if not comma else _font_px(fallback.strip(), parent_px)
    if value in _FONT_KEYWORDS:
        return float(_FONT_KEYWORDS[value])
    if value in ("smaller", "larger"):
        return parent_px * (0.83 if value == "smaller" else 1.2)
    if value in ("inherit", "unset"):
        return parent_px
    if value in ("initial", "revert", "revert-layer"):
        return 16.0
    match = _PLAIN_LENGTH.fullmatch(value)
    if not match or match.group(1) == "-":
        return None
    if match.group(3) == "%":
        return parent_px * float(match.group(2)) / 100
    return _length_px(value, parent_px)


def _winners(declarations: list) -> dict:
    """The value each property ends up with: the last !important one, else the last one."""
    won: dict = {}
    for name, value, important in declarations:
        if name not in won or important or not won[name][1]:
            won[name] = (value, important)
    return {name: value for name, (value, _important) in won.items()}


_NAMED_RGB = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0), "green": (0, 128, 0), "blue": (0, 0, 255),
    "gray": (128, 128, 128), "grey": (128, 128, 128), "silver": (192, 192, 192), "maroon": (128, 0, 0),
    "navy": (0, 0, 128), "purple": (128, 0, 128), "teal": (0, 128, 128), "olive": (128, 128, 0),
    "orange": (255, 165, 0), "yellow": (255, 255, 0), "darkgray": (169, 169, 169), "darkgrey": (169, 169, 169),
    "dimgray": (105, 105, 105), "dimgrey": (105, 105, 105), "lightgray": (211, 211, 211),
    "lightgrey": (211, 211, 211), "whitesmoke": (245, 245, 245), "gainsboro": (220, 220, 220),
    "darkred": (139, 0, 0), "darkblue": (0, 0, 139), "darkgreen": (0, 100, 0), "brown": (165, 42, 42),
    "crimson": (220, 20, 60), "firebrick": (178, 34, 34), "indigo": (75, 0, 130), "slategray": (112, 128, 144),
}
_RGB_FUNC = re.compile(r"rgba?\(\s*(\d{1,3})\s*,?\s*(\d{1,3})\s*,?\s*(\d{1,3})\s*(?:[,/]\s*(1|1\.0+|100%))?\s*\)")


def _rgb(value: str):
    """An opaque color as (r, g, b), or None for anything else (alpha, var(), hsl, unknown names)."""
    if value in _NAMED_RGB:
        return _NAMED_RGB[value]
    hexcolor = _HEX_COLOR.fullmatch(value)
    if hexcolor:
        digits = hexcolor.group(1)
        if len(digits) in (4, 8):
            if digits[-1 if len(digits) == 4 else -2:] not in ("f", "ff"):
                return None
            digits = digits[:-1] if len(digits) == 4 else digits[:-2]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
    func = _RGB_FUNC.fullmatch(value)
    if func and all(int(func.group(i)) <= 255 for i in (1, 2, 3)):
        return tuple(int(func.group(i)) for i in (1, 2, 3))
    return None


def _contrast(first, second) -> float:
    def luminance(rgb):
        channels = [c / 255 for c in rgb]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


_INHERIT_COLOR = {"inherit", "currentcolor", "initial", "unset", "revert", "revert-layer"}
_VENDOR_PREFIXES = ("-webkit-", "-moz-", "-ms-", "-o-", "-apple-", "-khtml-", "-epub-")
# Properties allowed only with these exact values; every declaration of them is
# checked, not just the one that wins, so an invalid later value cannot hide an
# earlier hiding one.
_EXACT_VALUES = {
    "visibility": {"visible"},
    "opacity": {"1", "1.0", "100%"},
    "zoom": {"1", "1.0", "100%", "normal"},
    "content-visibility": {"visible", "auto"},
    "overflow": {"visible"}, "overflow-x": {"visible"}, "overflow-y": {"visible"},
    "overflow-block": {"visible"}, "overflow-inline": {"visible"},
    "position": {"static", "relative", "sticky"},
    "-webkit-text-fill-color": set(),
}


def _style_context(attrs: dict, parent: dict) -> dict:
    """How an element's own markup affects whether its text is shown, given its
    parent's: {"visibility": "visible" | "hidden" | "unknown", "font_px",
    "custom"} (font size accumulates down the tree; custom notes inline custom
    properties above, which var() could resolve to).

    hidden: the hidden attribute or a popover with no inline display, or
    display:none. unknown: a style that cannot be parsed; any property outside
    an allowlist of ones that cannot hide text; or any declaration of a risky
    property with a value outside the plain ones x-reader understands: opacity,
    zoom, visibility and overflow only at their defaults, colors only opaque and
    readable against an inline background, font sizes that stay readable down
    the tree, small indents and spacings, margins that keep text on screen,
    no absolute/fixed positioning or offsets, transforms, filters, clipping,
    masks, containment or animation. It decides only what counts as page text:
    wall notices and frames count wherever they are.
    """
    context = {"visibility": "visible", "font_px": parent["font_px"], "custom": parent["custom"]}
    declarations = _inline_declarations(attrs.get("style") or "")
    if declarations is None:
        return dict(context, visibility="unknown")
    names = [name for name, _value, _important in declarations]
    if any(name.startswith("--") for name in names):
        context["custom"] = True
    displays = [value for name, value, _important in declarations if name == "display"]
    if displays == ["none"] and "all" not in names:
        return dict(context, visibility="hidden")  # not rendered, whatever else the style says

    def unknown() -> dict:
        return dict(context, visibility="unknown")

    if "popover" in attrs and displays:
        return unknown()  # a popover shown by inline CSS: not the stated hiding, not plain text either
    won = _winners(declarations)
    for name, value, _important in declarations:
        uses_var = "var(" in value or "env(" in value or "attr(" in value
        if name.startswith("--") or (name.startswith("-") and not name.startswith(_VENDOR_PREFIXES)):
            continue  # custom properties do nothing alone; other dash names are not CSS and are ignored
        if name in _EXACT_VALUES:
            if value not in _EXACT_VALUES[name]:
                return unknown()
        elif name.startswith(_INERT_PREFIXES) or name in _INERT_PROPERTIES:
            continue
        elif name in _NONE_ONLY or name in ("all", "font"):
            if value != "none" or name in ("all", "font"):
                return unknown()
        elif name == "display":
            if value not in _VISIBLE_DISPLAYS:
                return unknown()
        elif name == "font-size":
            if uses_var and context["custom"]:
                return unknown()
            size = _font_px(value, parent["font_px"])
            if size is None:
                return unknown()
            context["font_px"] = size
        elif name == "color":
            # inherit and friends keep the parent's color, already judged above; a
            # design token with no inline custom property in scope comes from the
            # stylesheet (judged like external CSS), and a fallback must be opaque
            token = re.fullmatch(r"var\(\s*--[\w-]+\s*(?:,\s*(.+))?\)", value)
            if token:
                if context["custom"] or (token.group(1) is not None and _rgb(token.group(1).strip()) is None):
                    return unknown()
            elif value not in _INHERIT_COLOR and _rgb(value) is None:
                return unknown()
        elif name in ("text-indent", "letter-spacing", "word-spacing"):
            if value == "normal" and name != "text-indent":
                continue
            amount = _length_px(value, context["font_px"])
            low, high = {"text-indent": (-3 * context["font_px"], 1000),
                         "letter-spacing": (-0.1 * context["font_px"], context["font_px"]),
                         "word-spacing": (-0.25 * context["font_px"], 2 * context["font_px"])}[name]
            if amount is None or not low <= amount <= high:
                return unknown()
        elif name in _MARGINS:
            for token in value.split():
                if token == "auto":
                    continue
                if token.startswith("var("):
                    if context["custom"] or "," in token:
                        return unknown()
                    continue
                amount = _length_px(token, context["font_px"])
                if amount is None or not -200 <= amount <= 2000:
                    return unknown()
        else:
            return unknown()  # outside the allowlist
    if won.get("position") in ("relative", "sticky") and set(won) & _OFFSETS:
        return unknown()  # offsets can move it off-screen
    text_color = _rgb(won["color"]) if "color" in won else None
    for name in ("background-color", "background"):
        if name in won and text_color is not None:
            background = _rgb(won[name])
            if background is None or _contrast(text_color, background) < 1.5:
                return unknown()  # text painted in (nearly) its own background color
    if context["font_px"] < _MIN_TEXT_PX:
        return unknown()
    if displays:
        return context  # an inline display overrides the hidden attribute
    if "hidden" in attrs or "popover" in attrs:
        return dict(context, visibility="hidden")
    return context


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


# Parse budgets, enforced inside the parser: tokens read, attributes per tag
# (checked before tokenizing, whose duplicate-attribute check is quadratic),
# elements and comments created (clones included), attribute entries,
# open depth, scope-check steps and text copying.
MAX_PARSE_TOKENS = 400_000
MAX_TAG_ATTRIBUTES = 400
MAX_PARSE_NODES = 60_000
MAX_PARSE_ATTRIBUTES = 200_000
MAX_PARSE_DEPTH = 1_500
MAX_PARSE_WORK = 20_000_000
MAX_TEXT_COPY = 64_000_000
MAX_CONTENT_CHARS = 1_000_000
# Raw-text elements whose text x-reader never reads.
_UNREAD_TEXT = {"script", "style", "noscript", "iframe", "noembed", "noframes", "xmp"}


class ParseBudgetExceeded(ValueError):
    """The page has more markup than x-reader parses."""


def _attribute_heavy(html: str) -> bool:
    """True when some tag carries more than MAX_TAG_ATTRIBUTES attributes.

    One linear pass following the tokenizer's tag states: a quote opens a value
    only right after "=", unquoted values end at whitespace or ">". It may also
    count tag-like text inside scripts or comments, which only errs toward
    refusing."""
    position, size = 0, len(html)
    while True:
        start = html.find("<", position)
        if start == -1 or start + 1 >= size:
            return False
        position = start + 1
        if not html[position].isalpha():
            continue
        while position < size and not html[position].isspace() and html[position] not in "/>":
            position += 1  # tag name
        count = 0
        while position < size and html[position] != ">":
            char = html[position]
            if char.isspace() or char == "/":
                position += 1
                continue
            count += 1  # an attribute name starts here
            if count > MAX_TAG_ATTRIBUTES:
                return True
            while position < size and not html[position].isspace() and html[position] not in "/>=":
                position += 1
            while position < size and html[position].isspace():
                position += 1
            if position < size and html[position] == "=":
                position += 1
                while position < size and html[position].isspace():
                    position += 1
                if position < size and html[position] in "\"'":
                    end = html.find(html[position], position + 1)
                    position = size if end == -1 else end + 1
                else:
                    while position < size and not html[position].isspace() and html[position] != ">":
                        position += 1


_INVALID_CODEPOINT = re.compile("[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f\ufdd0-\ufdef\ufffe\uffff\ud800-\udfff]")
MAX_INVALID_CODEPOINTS = 1_000


def _error_heavy(html: str) -> bool:
    """More control characters, noncharacters or lone surrogates than
    MAX_INVALID_CODEPOINTS: html5lib reports each as a parse error and drains
    that queue in quadratic time."""
    for count, _match in enumerate(_INVALID_CODEPOINT.finditer(html), 1):
        if count > MAX_INVALID_CODEPOINTS:
            return True
    return False


def _budgeted_parser():
    """An html5lib parser whose tokens, tree and work are counted as it runs.

    It also notes every <template> start tag the tokenizer produces, before an
    insertion mode can drop it."""
    import html5lib

    base = html5lib.getTreeBuilder("etree")

    class Builder(base):
        def __init__(self, *args, **kwargs):
            self.nodes = self.attributes = self.work = self.copied = 0
            builder = self

            def counted(cls):
                class Counted(cls):
                    def __init__(self, *a, **k):
                        builder.nodes += 1
                        if builder.nodes > MAX_PARSE_NODES:
                            raise ParseBudgetExceeded("too many elements")
                        super().__init__(*a, **k)

                    def cloneNode(self):
                        builder.attributes += len(self._element.attrib)
                        if builder.attributes > MAX_PARSE_ATTRIBUTES:
                            raise ParseBudgetExceeded("too many attributes")
                        return super().cloneNode()

                    def insertText(self, data, insertBefore=None):
                        if self.name in _UNREAD_TEXT:
                            return  # script/style bodies are never read; keeping them costs quadratic copies
                        children = self._element
                        if insertBefore is None or not len(children):
                            current = (children[-1].tail if len(children) else children.text) or ""
                        else:
                            # html5lib copies the child list and searches it, then appends to the
                            # tail before the table (foster parenting)
                            index = list(children).index(insertBefore._element)
                            builder.copied += len(children)
                            current = (children[index - 1].tail if index else children.text) or ""
                        builder.copied += len(current) + len(data)
                        if builder.copied > MAX_TEXT_COPY:
                            raise ParseBudgetExceeded("too much text work")
                        super().insertText(data, insertBefore)

                return Counted

            self.elementClass = counted(base.elementClass)
            self.commentClass = counted(base.commentClass)
            super().__init__(*args, **kwargs)

        def _charge(self, amount: int) -> None:
            self.work += amount
            if self.work > MAX_PARSE_WORK:
                raise ParseBudgetExceeded("too much parse work")

        def elementInScope(self, target, variant=None):
            self._charge(len(self.openElements))
            return super().elementInScope(target, variant)

        def reconstructActiveFormattingElements(self):
            self._charge(len(self.activeFormattingElements) + len(self.openElements))
            return super().reconstructActiveFormattingElements()

        def _check_insert(self, token) -> None:
            if len(self.openElements) >= MAX_PARSE_DEPTH:
                raise ParseBudgetExceeded("nesting too deep")
            self.attributes += len(token.get("data") or ())
            if self.attributes > MAX_PARSE_ATTRIBUTES:
                raise ParseBudgetExceeded("too many attributes")

        def insertElementNormal(self, token):
            self._check_insert(token)
            return super().insertElementNormal(token)

        def insertElementTable(self, token):
            self._check_insert(token)
            return super().insertElementTable(token)

    class TokenGate:
        """Wraps the tokenizer: counts tokens and notes template start tags."""

        def __init__(self, tokenizer, parser):
            object.__setattr__(self, "_tokenizer", tokenizer)
            object.__setattr__(self, "_parser", parser)

        def __iter__(self):
            start_tag = 3  # html5lib.constants.tokenTypes["StartTag"]
            for count, token in enumerate(self._tokenizer, 1):
                if count > MAX_PARSE_TOKENS:
                    raise ParseBudgetExceeded("too many tokens")
                if token.get("type") == start_tag and token.get("name") == "template":
                    self._parser.saw_template = True
                yield token

        def __getattr__(self, name):
            return getattr(self._tokenizer, name)

        def __setattr__(self, name, value):
            setattr(self._tokenizer, name, value)

    class Parser(html5lib.HTMLParser):
        saw_template = False

        def mainLoop(self):
            self.tokenizer = TokenGate(self.tokenizer, self)
            return super().mainLoop()

    return Parser(tree=Builder, namespaceHTMLElements=False)


def _parse_refresh(content: str) -> str:
    """The URL a <meta http-equiv=refresh> content value sends visitors to, or ""
    when it only reloads the page or is not valid (the HTML declarative-refresh
    steps: a delay, an optional ";" or ",", an optional "url=", a target that
    may be quoted)."""
    match = re.match(r"\s*(\d+)[0-9.]*\s*[;,]?\s*", content)
    if not match:
        return ""
    rest = content[match.end():]
    rest = re.sub(r"^url\s*=\s*", "", rest, flags=re.IGNORECASE)
    if rest[:1] in ("'", '"'):
        rest = rest[1:].split(rest[0], 1)[0]
    return rest.strip()


def _extract_html_page(html: str) -> dict:
    """{title, content, embeds, page_text, primary_used, visibility_unknown,
    templates, too_complex, client_redirect}.

    The page is parsed with a standard HTML5 tree builder (scripting on, so
    <noscript> is raw text), so misnested or unclosed markup ends where a
    browser ends it; parsing stops at the token, attribute, element, depth,
    work and text budgets (too_complex). content is the page text: navigation,
    footers, asides, hidden elements (the hidden attribute, display:none,
    popovers, a closed <details> beyond its first summary, a closed <dialog>)
    and foreign (SVG/MathML) text are left out; when the page marks its primary
    content (every <main>, else every <article>) and that region carries
    substantial prose, content is that region, so banners around it stay out.
    page_text is every rendered text block, hidden ones included, so wall
    notices count wherever they sit; menus, text boxes, button labels and
    placeholders count only there. embeds counts players, frames and plugins
    page-wide, except frames that are themselves hidden or 0/1-pixel.
    visibility_unknown: content includes text whose visibility x-reader cannot
    settle (see _style_context), or is too long to judge. templates: the page
    has a <template>, whose contents and shadow roots the parser does not
    model. client_redirect: a <meta http-equiv=refresh> names a target URL.
    """
    from x_reader.evidence import MIN_GENERIC_BODY_CHARS, prose_chars

    refused = {"title": "", "content": "", "embeds": 0, "page_text": "", "primary_used": False,
               "visibility_unknown": True, "templates": False, "too_complex": True, "client_redirect": False}
    if _attribute_heavy(html) or _error_heavy(html):
        return refused
    parser = _budgeted_parser()
    try:
        root = parser.parse(html, scripting=True)
    except ParseBudgetExceeded:
        return refused
    templates = parser.saw_template or any(True for _ in root.iter("template"))
    title_element = next(iter(root.iter("title")), None)
    title = ""
    if title_element is not None:
        pieces, kept = [], 0
        for piece in title_element.itertext():
            pieces.append(piece[:1000 - kept])
            kept += len(pieces[-1])
            if kept >= 1000:
                break
        title = " ".join("".join(pieces).split())

    parts: dict = {"gate": [], "body": [], "main": [], "article": []}
    unknown_in: set = set()
    open_groups: set = set()
    embeds = 0
    client_redirect = any(
        (meta.get("http-equiv") or "").strip().lower() == "refresh" and _parse_refresh(meta.get("content") or "")
        for meta in root.iter("meta")
    )
    base = {"skip": False, "foreign": False, "boiler": False, "main": False, "article": False,
            "hidden": False, "unknown": False, "block": False, "control": False, "collapsed": None,
            "font_px": 16.0, "custom": False}

    def emit(text, state) -> None:
        if not text or state["skip"]:
            return
        chunk = text if text == "\n" else text.strip()
        if not chunk:
            return
        parts["gate"].append(chunk)
        if state["foreign"] or state["boiler"] or state["hidden"] or state["control"]:
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
        if name in _EMBED_TAGS and not parent["skip"]:
            embeds += 1  # however small or hidden: ancestors and author CSS can show or enlarge it
        shadow_root = name == "template" and (
            (attrs.get("shadowrootmode") or attrs.get("shadowroot") or "").strip().lower() in ("open", "closed"))
        style = {"visibility": "visible", "font_px": parent["font_px"], "custom": parent["custom"]} \
            if foreign else _style_context(attrs, parent)
        hidden = parent["hidden"]
        if parent["collapsed"] is not None:
            # A closed <details> shows only its first summary.
            first_summary = name == "summary" and not parent["collapsed"]["summary_seen"]
            if name == "summary":
                parent["collapsed"]["summary_seen"] = True
            hidden = parent["collapsed"]["outer_hidden"] if first_summary else True
        group = (attrs.get("name") or "").strip() if name == "details" else ""
        closed = not foreign and name in ("details", "dialog") and (
            "open" not in attrs or (group and group in open_groups))
        if not foreign and name == "details" and group and "open" in attrs:
            open_groups.add(group)  # browsers keep one <details name=...> of a group open
        own_hidden = hidden or style["visibility"] == "hidden"
        return {
            "skip": parent["skip"] or (not foreign and name in _SKIP_TAGS and not shadow_root),
            "foreign": foreign,
            "boiler": parent["boiler"] or (not foreign and name in _BOILERPLATE_TAGS),
            "main": parent["main"] or name == "main",
            "article": parent["article"] or name == "article",
            "hidden": own_hidden or closed,
            "unknown": parent["unknown"] or style["visibility"] == "unknown",
            "block": not foreign and name in _BLOCK_TAGS,
            "control": parent["control"] or (not foreign and name in _CONTROL_TAGS),
            "collapsed": {"summary_seen": False, "outer_hidden": own_hidden} if closed and name == "details" else None,
            "font_px": style["font_px"], "custom": style["custom"],
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
    if len(content) > MAX_CONTENT_CHARS:
        return refused  # too much text to judge, or to hand on, within the service's limits
    return {
        "title": title[:200], "content": content, "embeds": embeds,
        "page_text": _join_text(parts["gate"]), "primary_used": primary_used,
        "visibility_unknown": chosen in unknown_in,
        "templates": templates, "too_complex": False, "client_redirect": client_redirect,
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


def _next_hop(current: str, location: str) -> str:
    """A redirect target, in the canonical form every hop is checked and connected with."""
    from x_reader.utils.url_validator import canonical_url

    try:
        return canonical_url(urljoin(current, location))
    except ValueError as exc:
        raise BlockedDestination(str(exc)) from exc


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

    from x_reader.utils.url_validator import canonical_url

    # DNS checks, SNI, the certificate check and Host all use one name: the
    # UTS46 form routing and policy saw ("faß.de" is xn--fa-hia.de, never fass.de).
    try:
        if canonical_url(url) != url:
            raise BlockedDestination(f"URL hostname is not in canonical form: {url}")
    except ValueError as exc:
        raise BlockedDestination(str(exc)) from exc
    hostname = ascii_hostname = parsed.hostname
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
                current = _next_hop(current, location)
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
        raise ParseBudgetExceeded("The page has more markup than x-reader parses")
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
    if page["templates"]:
        signals.add("template_markup")
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
                current = _next_hop(current, location)
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
