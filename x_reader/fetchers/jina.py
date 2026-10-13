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
    "canvas", "noembed", "noframes", "datalist", "meter", "progress", "rp",
}
# Not rendered unless an inline display shows them.
_UNRENDERED = {"head", "title", "script", "style", "noscript", "template", "noembed", "noframes", "datalist",
               "meta", "link", "base", "rp", "area", "param", "source", "track"}
# Form controls show their text, but in a box or a menu: walls count, page text does not.
_CONTROL_TAGS = {"select", "textarea", "button"}
_BUTTON_INPUTS = {"submit", "button", "reset"}
# Inputs that show no typed wording (other types, unknown ones included, show their value as text).
_NON_TEXT_INPUTS = _BUTTON_INPUTS | {"hidden", "password", "checkbox", "radio", "file", "image", "color", "range",
                                     "date", "datetime-local", "month", "time", "week"}
_BOILERPLATE_TAGS = {"nav", "footer", "aside"}
# Elements browsers lay out as blocks by default (a block starts a new line).
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "caption", "center", "dd", "details", "dialog", "div",
    "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hgroup", "hr", "html", "legend", "li", "listing", "main", "menu", "nav", "ol", "optgroup", "option", "p",
    "plaintext", "pre", "search", "section", "summary", "table", "tbody", "tfoot", "thead", "tr", "ul", "xmp",
}
_CELL_TAGS = {"td", "th"}
_PRE_TAGS = {"pre", "xmp", "listing", "plaintext", "textarea"}
_BLOCK_DISPLAYS = {
    "block", "flow-root", "list-item", "table", "flex", "grid", "table-row", "table-row-group",
    "table-header-group", "table-footer-group", "table-caption", "-webkit-box", "-ms-flexbox",
}
# Containers whose children sit side by side or wherever the layout puts them:
# their children's text runs on without a line break.
_FLOWING_DISPLAYS = {"flex", "inline-flex", "grid", "inline-grid", "-webkit-box", "-webkit-inline-box",
                     "-ms-flexbox", "-ms-inline-flexbox"}
_LINE_BREAK = "\x0b"  # a line break inside a block (<br>, preformatted newlines)
# An element boundary inside a line: nothing in the text as laid out, but a
# margin, padding or gap there can part the words on either side, so walls are
# also looked for with a space at each one.
_BOUNDARY = "\x1f"
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


MAX_STYLE_CHARS = 20_000


def _inline_declarations(style: str) -> list | None:
    """Parse an inline style into (property, value, important) declarations.

    CSS escapes are decoded as browsers decode them ("dis\\play" is display,
    "\\61 bsolute" is absolute), and an escaped character is never syntax: an
    escaped ";", ":" or "!" stays part of a name or value. A comment ends a
    token (it reads as a space), and quoted strings and (), [], {} blocks stay
    whole, so a ";" or "display:none" inside them is not a declaration. None
    when the style cannot be parsed with confidence (an unterminated comment,
    string or escape, unbalanced brackets) or is longer than MAX_STYLE_CHARS.
    """
    if len(style) > MAX_STYLE_CHARS:
        return None
    if not _PLAIN_STYLE.fullmatch(style):
        return _style_tokens(style)
    parsed = []
    for declaration in style.split(";"):  # no escapes, strings, comments or brackets holding a ";"
        name, colon, value = declaration.partition(":")
        if not colon:
            continue
        value = value.strip()
        important = bool(_IMPORTANT.search(value))
        parsed.append((name.strip().lower(), " ".join(_IMPORTANT.sub("", value).split()).lower(), important))
    return parsed


# Styles needing no tokenizer: no escapes, quotes, comments or [] {} blocks, and
# () only one level deep with no ";" inside.
_PLAIN_STYLE = re.compile(r"[^\\\"'/\[\]{}()]*(?:\([^\\\"'/\[\]{}();]*\)[^\\\"'/\[\]{}()]*)*")


def _declaration(parts: list, colon, bang, escaped: bool = False):
    """(name, value, important) from a declaration's pieces and the positions of
    its unescaped ":" and last unescaped "!"; None without a ":". A value with
    an escape outside a string starts with U+FFFD: escapes only spell names
    (an escaped digit is not a number), so no value parser here may take it."""
    if colon is None:
        return None
    name = "".join(parts[:colon]).strip().lower()
    if bang is not None and bang > colon and "".join(parts[bang + 1:]).strip().lower() == "important":
        value, important = "".join(parts[colon + 1:bang]), True
    else:
        value, important = "".join(parts[colon + 1:]), False
    value = " ".join(value.split()).lower()
    return name, ("\ufffd" + value if escaped else value), important


def _style_tokens(style: str) -> list | None:
    """_inline_declarations for styles that need the full tokenizer."""
    declarations, parts, colon, bang, escaped = [], [], None, None, False
    quote, open_blocks, position, size = "", [], 0, len(style)
    while position < size:
        match = _STYLE_TOKEN.match(style, position)
        token, position = match.group(0), match.end()
        if token[0] == "\\":
            digits, newline, char = match.group(1), match.group(2), match.group(3)
            if digits:
                code = int(digits, 16)
                char = chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "\ufffd"
            elif newline:
                if not quote:
                    return None  # an escaped newline is only valid inside a string, where it continues the line
                continue
            elif char is None:
                return None  # a backslash at the very end
            # Outside a string an escape only ever spells part of an identifier: one
            # that yields a space, bracket, comma or other non-name character leaves
            # a value browsers drop, so it reads as one no CSS parser here accepts.
            parts.append(char if quote or char.isalnum() or char in "-_" or ord(char) >= 0x80 else "\ufffd")
            escaped = escaped or (not quote and colon is not None and bang is None)  # (an escaped !important is fine)
        elif quote:
            if token == "\n":
                return None
            parts.append(token)
            if token == quote:
                quote = ""
        elif token == "/*":
            end = style.find("*/", position)
            if end == -1:
                return None
            parts.append(" ")
            position = end + 2
        elif token in ("\"", "'"):
            quote = token
            parts.append(token)
        elif token in _CLOSERS:
            open_blocks.append(_CLOSERS[token])
            parts.append(token)
        elif token in (")", "]", "}"):
            if not open_blocks or open_blocks.pop() != token:
                return None
            parts.append(token)
        elif token == ";" and not open_blocks:
            declaration = _declaration(parts, colon, bang, escaped)
            if declaration:
                declarations.append(declaration)
            parts, colon, bang, escaped = [], None, None, False
        else:
            if not open_blocks:
                if token == ":" and colon is None:
                    colon = len(parts)
                elif token == "!" and colon is not None:
                    bang = len(parts)
            parts.append(token)
    if quote or open_blocks:
        return None
    declaration = _declaration(parts, colon, bang, escaped)
    if declaration:
        declarations.append(declaration)
    return declarations


# An escape (hex, newline, other character, or a dangling backslash), a comment
# opener, a delimiter, or a run of ordinary characters.
_STYLE_TOKEN = re.compile(
    r"\\(?:([0-9a-fA-F]{1,6})(?:\r\n|[ \t\n\r\f])?|(\r\n|[\n\r\f])|(.)|$)"
    r"|/\*|[\"'();:!\[\]{}\n]|[^\\\"'/;:!()\[\]{}\n]+|/",
    re.S,
)


_NUMBER = r"(?:\d{1,6}(?:\.\d{1,6})?|\.\d{1,6})"
_PLAIN_NUMBER = re.compile(rf"\+?({_NUMBER})")
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
_VIEWPORT_UNITS = {
    "vw", "vh", "vmin", "vmax", "vi", "vb", "svw", "svh", "lvw", "lvh", "dvw", "dvh", "svi", "svb", "lvi", "lvb",
    "dvi", "dvb", "svmin", "svmax", "lvmin", "lvmax", "dvmin", "dvmax", "cqw", "cqh", "cqi", "cqb", "cqmin", "cqmax",
}
_MIN_TEXT_PX = 6  # smaller than this does not read as text
_MIN_CONTRAST = 1.3  # text this close to the color behind it does not read
_MIN_ALPHA = 0.1  # text more transparent than this does not read
_MIN_LINE = 0.75  # lines closer than this (times the font size) overlap
_MAX_LINE = 10.0
_FAR_PX = 5000  # geometry reaching past this pushes or paints text out of reach
_WIDE_PX = 2000  # a text box wider than this runs off the screen
_WIDE_KEYWORDS = {"inherit", "initial", "unset", "revert", "revert-layer"}
# Browser default sizes, relative to the parent's (an <h1> in sectioning content can be 0.67em).
_TAG_FONT_SCALE = {"small": 5 / 6, "sub": 5 / 6, "sup": 5 / 6, "big": 1.2, "h2": 1.5, "h3": 1.17, "h4": 1.0,
                   "h5": 0.83, "h6": 0.67}
_SECTIONING = {"article", "aside", "nav", "section"}
_LEGACY_FONT_PX = {1: 10, 2: 13, 3: 16, 4: 18, 5: 24, 6: 32, 7: 48}
# Laid out inline by default: their backgrounds and borders paint over neighbouring
# lines when padding or borders reach past the line.
_INLINE_TAGS = {
    "a", "abbr", "b", "bdi", "cite", "code", "data", "del", "dfn", "em", "font", "i", "ins", "kbd", "label", "mark",
    "nobr", "q", "s", "samp", "small", "span", "strike", "strong", "sub", "sup", "time", "tt", "u", "var", "big",
}
_LEGACY_BACKGROUND_TAGS = {"body", "table", "thead", "tbody", "tfoot", "tr", "td", "th"}
_LEGACY_SIZE_TAGS = {"table", "td", "th", "col", "colgroup", "hr", "tr", "svg"}
# Tags whose defaults or attributes x-reader models (see _style_context, _presentational, _covers).
_MODELED_TAGS = (set(_TAG_FONT_SCALE) | _LEGACY_BACKGROUND_TAGS | _LEGACY_SIZE_TAGS | {
    "h1", "a", "font", "body", "mark", "img", "object", "applet", "dialog", "bdo", "marquee"})
_SYMBOL_FONTS = re.compile(r"wingdings|webdings|dingbats|(?<![\w-])symbol(?![\w-])|marlett|mt extra|ms reference specialty")
_HEX_COLOR = re.compile(r"#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})")
_RGB_FUNC = re.compile(r"rgba?\(([^()]{1,80})\)")
_HSL_FUNC = re.compile(r"hsla?\(([^()]{1,80})\)")
_CHANNEL = re.compile(rf"\+?({_NUMBER})(%?)")
_HUE = re.compile(rf"([+-]?{_NUMBER})(deg)?")
# Properties whose value cannot hide, clip, shrink, move or recolor an element's own text.
_INERT_PREFIXES = (
    "--", "background", "border", "outline", "grid", "flex", "align-", "justify-",
    "place-", "column-", "row-", "gap", "font-variant", "font-feature", "font-kerning", "font-optical",
    "font-stretch", "font-synthesis", "text-decoration", "text-emphasis", "text-underline", "scroll-", "break-",
    "page-break", "counter-", "caret", "accent-color", "transition", "box-decoration",
)
_INERT_PROPERTIES = {
    "font-weight", "font-style", "text-align", "text-align-last", "text-transform",
    "text-rendering", "text-shadow", "text-justify", "clear", "cursor", "box-sizing",
    "box-shadow", "direction", "writing-mode", "text-orientation", "word-break",
    "overflow-wrap", "word-wrap", "hyphens", "table-layout", "border-collapse",
    "border-spacing", "caption-side", "empty-cells", "orphans", "widows", "pointer-events", "user-select",
    "-webkit-user-select", "-moz-user-select", "-ms-user-select", "will-change", "object-fit",
    "object-position", "aspect-ratio", "resize", "min-height", "min-block-size",
    "isolation", "z-index", "white-space", "text-overflow",
    "-webkit-font-smoothing", "-moz-osx-font-smoothing", "-webkit-tap-highlight-color",
    "-webkit-text-size-adjust", "text-size-adjust", "touch-action", "speak", "image-rendering",
    "-webkit-appearance", "appearance", "-moz-appearance", "forced-color-adjust",
    "-webkit-touch-callout", "-webkit-overflow-scrolling", "scrollbar-width", "scrollbar-color",
    "scrollbar-gutter", "-webkit-box-orient", "-webkit-box-pack", "-webkit-box-align", "-ms-flex-align",
    "-ms-flex-pack", "-ms-flex-direction", "-ms-flex-wrap", "-webkit-flex-direction", "-webkit-flex-wrap",
    "-webkit-align-items", "-webkit-justify-content", "text-wrap", "text-wrap-mode", "text-wrap-style",
    "font-display", "line-break", "word-spacing-adjust", "hanging-punctuation", "ruby-position",
    "print-color-adjust", "-webkit-print-color-adjust", "shape-outside", "shape-margin", "inset-area",
    "anchor-name", "container-name", "container-type", "counter-reset", "counter-increment",
    "max-width", "max-inline-size", "top", "left", "right", "bottom", "inset", "inset-block", "inset-inline",
    "inset-block-start", "inset-block-end", "inset-inline-start", "inset-inline-end", "overflow",
    "overflow-x", "overflow-y", "overflow-block", "overflow-inline", "overflow-anchor", "overscroll-behavior",
}
# Sizes that set how wide a line of the element's text runs.
_WIDTHS = {"width", "min-width", "inline-size", "min-inline-size", "flex", "flex-basis", "-webkit-flex",
           "-webkit-flex-basis", "-ms-flex", "grid-template-columns", "grid-auto-columns"}
_GRID_TRACKS = {"grid-template-rows", "grid-template-columns", "grid-auto-rows", "grid-auto-columns"}
_BOX_HEIGHTS = {"height", "max-height", "block-size", "max-block-size"}
_OFFSETS = {"top", "left", "right", "bottom", "inset", "inset-block", "inset-inline", "inset-block-start",
            "inset-block-end", "inset-inline-start", "inset-inline-end"}
_MARGINS = {"margin", "margin-left", "margin-right", "margin-top", "margin-bottom", "margin-inline",
            "margin-inline-start", "margin-inline-end", "margin-block", "margin-block-start", "margin-block-end"}
_PADDINGS = {"padding", "padding-left", "padding-right", "padding-top", "padding-bottom", "padding-inline",
             "padding-inline-start", "padding-inline-end", "padding-block", "padding-block-start", "padding-block-end"}
_NONE_ONLY = {
    "transform", "translate", "scale", "rotate", "filter", "backdrop-filter", "clip", "clip-path", "mask",
    "mask-image", "mask-size", "mask-position", "mask-repeat", "-webkit-mask", "-webkit-mask-image",
    "-webkit-line-clamp", "line-clamp", "contain", "animation", "animation-name", "mix-blend-mode",
    "offset", "offset-path", "perspective",
}
_VALIGN_KEYWORDS = {"baseline", "sub", "super", "text-top", "text-bottom", "middle", "top", "bottom"} | _WIDE_KEYWORDS
# Painted after the text around them, or able to move over it.
_POSITIONED = {"absolute", "fixed", "sticky"}
_STACKING = {"transform", "translate", "scale", "rotate", "filter", "backdrop-filter", "mix-blend-mode",
             "perspective", "offset", "offset-path", "isolation", "will-change", "contain", "z-index", "opacity",
             "border-image", "border-image-source", "box-reflect", "clip-path", "mask", "mask-image", "mask-border"}
# Sizes that can leave a box shorter than what it holds.
_HEIGHT_LIMITS = {"height", "max-height", "block-size", "max-block-size", "aspect-ratio", "container-type",
                  "contain-intrinsic-size", "flex", "flex-basis", "-webkit-line-clamp", "line-clamp"}
_NO_EFFECT = {"none", "auto", "normal", "1", "1.0", "100%", "size", "inline-size", "style", "scroll-position",
              "contents", "visible"} | _WIDE_KEYWORDS
_GRID_PLACEMENT = {"grid-area", "grid-row", "grid-column", "grid-row-start", "grid-row-end", "grid-column-start",
                   "grid-column-end"}
_NO_PAINT = {"none", "transparent", "0"} | _WIDE_KEYWORDS
_BG_WORDS = {
    "none", "repeat", "repeat-x", "repeat-y", "no-repeat", "space", "round", "scroll", "fixed", "local",
    "border-box", "padding-box", "content-box", "top", "bottom", "left", "right", "center", "auto", "cover",
    "contain",
}
# Properties whose size cannot carry text, or paint, away from where it sits.
_REACH_EXEMPT = (
    "--", "max-", "background-position", "background-size", "mask", "-webkit-mask", "scroll-", "transition",
    "animation", "outline-offset", "text-underline-offset", "perspective", "transform-origin", "object-position",
    "shape-margin", "text-size-adjust", "-webkit-text-size-adjust", "counter-", "font-weight", "z-index", "order",
    "flex-grow", "flex-shrink", "opacity", "aspect-ratio", "font-stretch",
)
_COLOR_OR_URL = re.compile(
    r"(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix|url|image-set|format|local|steps|cubic-bezier)"
    r"\([^()]*(?:\([^()]*\)[^()]*)*\)"
)
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_VALUE_NUMBER = re.compile(r"(?<![\w#.])([+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:e[+-]?\d+)?)([a-z%]*)", re.IGNORECASE)
_BIDI_OVERRIDE = re.compile("[‭‮]")

# CSS named colors.
_NAMED_RGB = {
    name: tuple(int(code[i:i + 2], 16) for i in (0, 2, 4))
    for name, code in (pair.split(":") for pair in (
        "aliceblue:f0f8ff antiquewhite:faebd7 aqua:00ffff aquamarine:7fffd4 azure:f0ffff beige:f5f5dc bisque:ffe4c4 "
        "black:000000 blanchedalmond:ffebcd blue:0000ff blueviolet:8a2be2 brown:a52a2a burlywood:deb887 "
        "cadetblue:5f9ea0 chartreuse:7fff00 chocolate:d2691e coral:ff7f50 cornflowerblue:6495ed cornsilk:fff8dc "
        "crimson:dc143c cyan:00ffff darkblue:00008b darkcyan:008b8b darkgoldenrod:b8860b darkgray:a9a9a9 "
        "darkgreen:006400 darkgrey:a9a9a9 darkkhaki:bdb76b darkmagenta:8b008b darkolivegreen:556b2f "
        "darkorange:ff8c00 darkorchid:9932cc darkred:8b0000 darksalmon:e9967a darkseagreen:8fbc8f "
        "darkslateblue:483d8b darkslategray:2f4f4f darkslategrey:2f4f4f darkturquoise:00ced1 darkviolet:9400d3 "
        "deeppink:ff1493 deepskyblue:00bfff dimgray:696969 dimgrey:696969 dodgerblue:1e90ff firebrick:b22222 "
        "floralwhite:fffaf0 forestgreen:228b22 fuchsia:ff00ff gainsboro:dcdcdc ghostwhite:f8f8ff gold:ffd700 "
        "goldenrod:daa520 gray:808080 green:008000 greenyellow:adff2f grey:808080 honeydew:f0fff0 hotpink:ff69b4 "
        "indianred:cd5c5c indigo:4b0082 ivory:fffff0 khaki:f0e68c lavender:e6e6fa lavenderblush:fff0f5 "
        "lawngreen:7cfc00 lemonchiffon:fffacd lightblue:add8e6 lightcoral:f08080 lightcyan:e0ffff "
        "lightgoldenrodyellow:fafad2 lightgray:d3d3d3 lightgreen:90ee90 lightgrey:d3d3d3 lightpink:ffb6c1 "
        "lightsalmon:ffa07a lightseagreen:20b2aa lightskyblue:87cefa lightslategray:778899 lightslategrey:778899 "
        "lightsteelblue:b0c4de lightyellow:ffffe0 lime:00ff00 limegreen:32cd32 linen:faf0e6 magenta:ff00ff "
        "maroon:800000 mediumaquamarine:66cdaa mediumblue:0000cd mediumorchid:ba55d3 mediumpurple:9370db "
        "mediumseagreen:3cb371 mediumslateblue:7b68ee mediumspringgreen:00fa9a mediumturquoise:48d1cc "
        "mediumvioletred:c71585 midnightblue:191970 mintcream:f5fffa mistyrose:ffe4e1 moccasin:ffe4b5 "
        "navajowhite:ffdead navy:000080 oldlace:fdf5e6 olive:808000 olivedrab:6b8e23 orange:ffa500 "
        "orangered:ff4500 orchid:da70d6 palegoldenrod:eee8aa palegreen:98fb98 paleturquoise:afeeee "
        "palevioletred:db7093 papayawhip:ffefd5 peachpuff:ffdab9 peru:cd853f pink:ffc0cb plum:dda0dd "
        "powderblue:b0e0e6 purple:800080 rebeccapurple:663399 red:ff0000 rosybrown:bc8f8f royalblue:4169e1 "
        "saddlebrown:8b4513 salmon:fa8072 sandybrown:f4a460 seagreen:2e8b57 seashell:fff5ee sienna:a0522d "
        "silver:c0c0c0 skyblue:87ceeb slateblue:6a5acd slategray:708090 slategrey:708090 snow:fffafa "
        "springgreen:00ff7f steelblue:4682b4 tan:d2b48c teal:008080 thistle:d8bfd8 tomato:ff6347 "
        "turquoise:40e0d0 violet:ee82ee wheat:f5deb3 white:ffffff whitesmoke:f5f5f5 yellow:ffff00 "
        "yellowgreen:9acd32"
    ).split())
}
_BLACK = (0, 0, 0, 1.0)
_WHITE = (255, 255, 255)
_LINKS = ((0, 0, 238, 1.0), (85, 26, 139, 1.0))  # the browser's default link and visited-link colors
_LINK_COLOR = "\x00link"  # stands for those in declarations; no style attribute can hold a NUL
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
    "content": {"normal", "none"} | _WIDE_KEYWORDS,
    "color-scheme": {"normal", "light", "only light", "light only"} | _WIDE_KEYWORDS,
}


def _unit_px(unit: str, font_px: float):
    """CSS pixels per unit (a 1000px viewport or box for viewport units and percentages); None for non-lengths."""
    if unit in _ABSOLUTE_PX:
        return _ABSOLUTE_PX[unit]
    if unit in ("em", "cap", "ic"):
        return font_px
    if unit == "lh":
        return font_px * 1.2
    if unit in ("rem", "rcap", "ric"):
        return 16.0
    if unit == "rlh":
        return 19.2
    if unit in ("ex", "ch"):
        return font_px / 2
    if unit in ("rex", "rch"):
        return 8.0
    if unit in _VIEWPORT_UNITS or unit == "%":
        return 10.0
    return None


def _reach(value: str, font_px: float, combined: bool = True) -> float:
    """How far a value's lengths reach, in CSS pixels: their sum (or the largest
    one), times any factor a math function multiplies them by. Exponents count;
    numbers inside colors, URLs and strings do not."""
    if not any(char.isdigit() for char in value):
        return 0.0
    text = " ".join(_COLOR_OR_URL.sub(" ", _QUOTED.sub(" ", value)).split()) if "(" in value or "'" in value \
        or '"' in value else value
    total = largest = 0.0
    factor = 1.0
    for match in _VALUE_NUMBER.finditer(text):
        number = abs(float(match.group(1)))
        unit = match.group(2).lower()
        if not unit:
            before = text[max(0, match.start() - 8):match.start()].rstrip()
            if before.endswith("/"):
                if number < 1:
                    factor = float("inf") if number == 0 else factor / number
            elif before.endswith(("*", "repeat(")) or text[match.end():match.end() + 2].lstrip().startswith("*"):
                factor *= max(number, 1.0)
            continue
        scale = _unit_px(unit, font_px)
        if scale is None:
            continue
        total += number * scale
        largest = max(largest, number * scale)
    return (total if combined else largest) * factor


def _length_px(value: str, font_px: float):
    """A plain length in CSS pixels (None for anything else)."""
    if value == "0":
        return 0.0
    match = _PLAIN_LENGTH.fullmatch(value)
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    number, unit = float(match.group(2)), match.group(3)
    if unit == "%":
        return None  # percentages depend on the box
    return sign * number * _unit_px(unit, font_px)


def _box_px(token: str, font_px: float):
    """A margin or padding token as (pixels, is_percentage); None when not a plain value."""
    match = _PLAIN_LENGTH.fullmatch(token)
    if match and match.group(3) == "%":
        return (-1 if match.group(1) == "-" else 1) * float(match.group(2)), True
    amount = _length_px(token, font_px)
    return None if amount is None else (amount, False)


def _font_px(value: str, parent_px: float, depth: int = 0):
    """The computed font size for a font-size value, or None when not understood."""
    if value.startswith("var(") and value.endswith(")"):
        _token, comma, fallback = value[4:-1].partition(",")
        if depth >= 8:
            return None  # fallbacks nested this deep are not followed
        # A design token is judged like external CSS; a fallback must itself be readable.
        return parent_px if not comma else _font_px(fallback.strip(), parent_px, depth + 1)
    if value in _FONT_KEYWORDS:
        return float(_FONT_KEYWORDS[value])
    if value in ("smaller", "larger"):
        return parent_px * (5 / 6 if value == "smaller" else 1.2)
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


def _alpha(text: str):
    match = _CHANNEL.fullmatch(text.strip())
    if not match:
        return None
    return min(float(match.group(1)) / (100 if match.group(2) else 1), 1.0)


def _rgba(value: str):
    """A plain color as (r, g, b, alpha); None for anything else (var(), system colors, other color spaces)."""
    if value == "transparent":
        return (0, 0, 0, 0.0)
    if value in _NAMED_RGB:
        return _NAMED_RGB[value] + (1.0,)
    hexcolor = _HEX_COLOR.fullmatch(value)
    if hexcolor:
        digits = hexcolor.group(1)
        if len(digits) in (3, 4):
            digits = "".join(ch * 2 for ch in digits)
        channels = [int(digits[i:i + 2], 16) for i in range(0, len(digits), 2)]
        return (channels[0], channels[1], channels[2], channels[3] / 255 if len(channels) == 4 else 1.0)
    for pattern in (_RGB_FUNC, _HSL_FUNC):
        func = pattern.fullmatch(value)
        if not func:
            continue
        body = func.group(1).strip()
        legacy = "," in body  # comma syntax: no "/", and rgb channels all numbers or all percentages
        if legacy:
            if "/" in body:
                return None
            parts = [part.strip() for part in body.split(",")]
            alpha = _alpha(parts.pop()) if len(parts) == 4 else 1.0
        elif "/" in body:
            channels_text, _slash, alpha_text = body.partition("/")
            parts, alpha = channels_text.split(), _alpha(alpha_text)
        else:
            parts, alpha = body.split(), 1.0
        if len(parts) != 3 or alpha is None or any(not part for part in parts):
            return None
        if pattern is _RGB_FUNC:
            matches = [_CHANNEL.fullmatch(part) for part in parts]
            if not all(matches) or (legacy and len({bool(match.group(2)) for match in matches}) > 1):
                return None
            channels = [round(min(float(match.group(1)) * (2.55 if match.group(2) else 1), 255)) for match in matches]
            return (channels[0], channels[1], channels[2], alpha)
        hue = _HUE.fullmatch(parts[0])
        levels = [_CHANNEL.fullmatch(part) for part in parts[1:]]
        if not hue or not all(levels) or (legacy and not all(level.group(2) for level in levels)):
            return None
        import colorsys

        saturation, lightness = (min(float(level.group(1)), 100) / 100 for level in levels)
        red, green, blue = colorsys.hls_to_rgb((float(hue.group(1)) % 360) / 360, lightness, saturation)
        return (round(red * 255), round(green * 255), round(blue * 255), alpha)
    return None


def _over(top, bottom):
    """The opaque color of top (r, g, b, alpha) painted over the opaque color bottom."""
    alpha = top[3]
    return tuple(round(alpha * upper + (1 - alpha) * lower) for upper, lower in zip(top[:3], bottom))


def _contrast(first, second) -> float:
    def luminance(rgb):
        channels = [c / 255 for c in rgb]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _readable(color, background) -> bool:
    """Text in color over background reads: a design token on either side is
    judged like external CSS; an image or unknown background is not readable."""
    if isinstance(color, tuple) and color[3] < _MIN_ALPHA:
        return False  # (nearly) transparent text, whatever is behind it
    if color == "token" or background == "token":
        return True
    if background is None:
        return False
    return _contrast(_over(color, background), background) >= _MIN_CONTRAST


def _text_color(value: str, custom: bool):
    """A color declaration as (r, g, b, alpha), "inherit", "token" (a design
    token from the stylesheet), or None when not understood."""
    if value in ("inherit", "currentcolor", "unset"):
        return "inherit"
    if value in ("initial", "revert", "revert-layer"):
        return _BLACK
    token = re.fullmatch(r"var\(\s*--[\w-]+\s*(?:,\s*(.+))?\)", value)
    if token:
        if custom:
            return None  # an inline custom property could resolve it to anything
        return "token" if token.group(1) is None else _rgba(token.group(1).strip())
    return _rgba(value)


def _split_layers(value: str) -> list:
    """Background tokens split on spaces, commas and "/" outside parentheses."""
    tokens, current, depth = [], [], 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth == 0 and (char.isspace() or char in ",/"):
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def _background_color(value: str, shorthand: bool, custom: bool):
    """What a background or background-color declaration paints: (r, g, b,
    alpha), "keep" (nothing new), "currentcolor", "token", "image", or None
    when not understood."""
    if value in _NO_PAINT:
        return "keep"
    if re.search(r"\b(?:var|env|attr)\(", value):
        return None if custom else "token"
    if re.search(r"(?:url|gradient|image|image-set|element|cross-fade|paint)\(", value):
        return "image"
    if value == "currentcolor":
        return "currentcolor"
    if not shorthand:
        return _rgba(value)
    color, colors = "keep", 0
    for token in _split_layers(value):
        if token in _BG_WORDS or token == "0" or _PLAIN_LENGTH.fullmatch(token):
            continue
        parsed = "currentcolor" if token == "currentcolor" else _rgba(token)
        if parsed is None:
            return None
        color, colors = parsed, colors + 1
    return color if colors <= 1 else None  # a background has one color: two make the declaration invalid


def _legacy_color(value: str) -> str:
    """An HTML color attribute as a CSS color value ("dddddd" is #dddddd)."""
    value = value.strip().lower()
    return "#" + value if re.fullmatch(r"[0-9a-f]{3}|[0-9a-f]{6}", value) else value


def _legacy_number(value: str):
    match = re.match(r"\s*(\d{1,9})", value or "")
    return int(match.group(1)) if match else None


def _presentational(name: str, attrs: dict, parent: dict) -> list:
    """Presentational attributes as the declarations browsers map them to (they
    come before, and lose to, the inline style), plus the default link color."""
    declarations = []
    if name == "a" and "href" in attrs:
        declarations.append(("color", _LINK_COLOR, False))
    if name == "mark":
        declarations += [("background-color", "#ffff00", False), ("color", "black", False)]
    if name == "font":
        if (attrs.get("color") or "").strip():
            declarations.append(("color", _legacy_color(attrs["color"]), False))
        if (attrs.get("face") or "").strip():
            declarations.append(("font-family", attrs["face"].strip().lower(), False))
        size = (attrs.get("size") or "").strip()
        match = re.fullmatch(r"([+-]?)(\d{1,3})", size)
        if match:
            level = int(match.group(2))
            level = 3 + level if match.group(1) == "+" else 3 - level if match.group(1) == "-" else level
            declarations.append(("font-size", f"{_LEGACY_FONT_PX[max(1, min(7, level))]}px", False))
    if name == "body":
        if (attrs.get("text") or "").strip():
            declarations.append(("color", _legacy_color(attrs["text"]), False))
        for attr, sides in (("marginwidth", ("left", "right")), ("leftmargin", ("left",)),
                            ("rightmargin", ("right",)), ("marginheight", ("top", "bottom")),
                            ("topmargin", ("top",)), ("bottommargin", ("bottom",))):
            amount = _legacy_number(attrs.get(attr))
            if amount is not None:
                declarations += [(f"margin-{side}", f"{amount}px", False) for side in sides]
    if name in _LEGACY_BACKGROUND_TAGS:
        if (attrs.get("bgcolor") or "").strip():
            declarations.append(("background-color", _legacy_color(attrs["bgcolor"]), False))
        if (attrs.get("background") or "").strip():
            declarations.append(("background-image", "url(attribute)", False))
    if name in ("img", "object", "applet"):
        for attr, sides in (("hspace", ("left", "right")), ("vspace", ("top", "bottom"))):
            amount = _legacy_number(attrs.get(attr))
            if amount is not None:
                declarations += [(f"margin-{side}", f"{amount}px", False) for side in sides]
    if name in _LEGACY_SIZE_TAGS:
        for attr in ("width", "height"):
            raw = (attrs.get(attr) or "").strip()
            match = re.match(r"(\d{1,9}(?:\.\d+)?)\s*(%|px)?", raw)
            if match:
                declarations.append((attr, match.group(1) + (match.group(2) or "px"), False))
    if name == "table":
        for attr in ("cellpadding", "cellspacing", "border"):
            amount = _legacy_number(attrs.get(attr))
            if amount is not None:
                declarations.append(("border-spacing" if attr != "border" else "border-width", f"{amount}px", False))
    return declarations


def _winner(declarations: list, prop: str, parse):
    """The parsed value a property ends up with, from declarations that all
    parse: the last !important one, else the last one. ("unknown",) when one
    does not parse; None when the property is not declared."""
    won, won_important = None, False
    for name, value, important in declarations:
        if name != prop:
            continue
        parsed = parse(value)
        if parsed is None:
            return ("unknown",)
        if important or not won_important:
            won, won_important = parsed, important
    return won


def _line(value: str, font_px: float, custom: bool):
    """A line-height as ("mult", factor) or ("px", pixels), "inherit", or None when not understood."""
    if value in ("normal", "initial", "revert", "revert-layer"):
        return ("mult", 1.2)
    if value in ("inherit", "unset"):
        return "inherit"
    if re.search(r"\b(?:var|env|attr)\(", value):
        return None if custom else ("mult", 1.2)  # a stylesheet token, judged like external CSS
    number = _PLAIN_NUMBER.fullmatch(value)
    if number:
        return ("mult", float(number.group(1)))
    match = _PLAIN_LENGTH.fullmatch(value)
    if not match or match.group(1) == "-":
        return None
    if match.group(3) == "%":
        return ("px", font_px * float(match.group(2)) / 100)
    return ("px", _length_px(value, font_px))


def _vertical_box(prop: str, value: str, font_px: float) -> float:
    """The largest top/bottom padding or border width a declaration sets, in pixels."""
    tokens = value.split()
    if prop in ("padding", "border-width"):
        tokens = tokens[0:1] + tokens[2:3]
    elif not (prop in ("padding-top", "padding-bottom", "border-top-width", "border-bottom-width")
              or prop.startswith(("padding-block", "border-block")) or prop in ("border", "border-top", "border-bottom")):
        return 0.0
    widths = [{"thin": 1.0, "medium": 3.0, "thick": 5.0}.get(token) or abs(_length_px(token, font_px) or 0.0)
              for token in tokens]
    return max(widths, default=0.0)


_CLIP_VALUES = {"hidden", "clip", "auto", "scroll"}
_OVERFLOW_AXES = {"overflow": ("x", "y"), "overflow-x": ("x",), "overflow-inline": ("x",), "overflow-y": ("y",),
                  "overflow-block": ("y",)}


def _clips_both(declarations: list) -> bool:
    """The overflow that wins on each axis (physical and logical properties, in
    horizontal writing) is hidden, clip, auto or scroll; any overflow value
    browsers would drop, or another writing mode, makes it no."""
    axes, important_axes = {"x": None, "y": None}, {"x": False, "y": False}
    for prop, value, important in declarations:
        if prop == "writing-mode" and value not in {"horizontal-tb"} | _WIDE_KEYWORDS:
            return False
        if prop not in _OVERFLOW_AXES:
            continue
        parts = value.split()
        if not 1 <= len(parts) <= len(_OVERFLOW_AXES[prop]) or any(part not in _CLIP_VALUES | {"visible"} for part in parts):
            return False
        for index, axis in enumerate(_OVERFLOW_AXES[prop]):
            if important or not important_axes[axis]:
                axes[axis], important_axes[axis] = parts[min(index, len(parts) - 1)], important
    return all(axes[axis] in _CLIP_VALUES for axis in ("x", "y"))


# Boxes that clip what overflows them (overflow does not apply to inline boxes,
# nor to display:contents, which generates no box).
_CLIPPING_DISPLAYS = {"block", "flow-root", "flex", "grid", "inline-block", "inline-flex", "inline-grid",
                      "list-item", "-webkit-box", "-ms-flexbox"}
_BLOCK_TAGS_BY_DEFAULT = {
    "address", "article", "aside", "blockquote", "body", "center", "dd", "details", "dialog", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup",
    "html", "li", "main", "menu", "nav", "ol", "p", "pre", "search", "section", "summary", "ul",
}


def _covers(name: str, attrs: dict, declarations: list, font_px: float, custom: bool) -> bool:
    """Whether an element could paint over other text or carry it out of reach:
    positioning (absolute, fixed, sticky; relative moved over half a line, or
    a relative block with a height limit that does not clip its overflow, since
    a positioned box paints after the text around it and what spills out of it
    can cover that text; vendor-prefixed forms count), a transform, clip or mask
    or other stacking context, outlines over 3px, shadows over 16px (text
    shadows over 1em), explicit grid placement, negative margins past 1em,
    geometry reaching past _FAR_PX (or set through an inline custom property),
    an inline box whose background or border reaches past its line, a collapsed
    table column, or an open <dialog>, which browsers position over the page;
    also anything that shows text in another order than written (flex/grid
    order, reversed or dense flow, right floats, right-to-left flex and grid)
    or makes text from CSS (string list markers, quotes)."""
    if name == "dialog" and "open" in attrs:
        return True
    relative = paints = moved = limited = False
    vertical = 0.0
    display, display_important = None, False
    direction, direction_important = None, False
    for prop, value, important in declarations:
        if prop.startswith("--") or (prop.startswith("-") and not prop.startswith(_VENDOR_PREFIXES)):
            continue
        for prefix in _VENDOR_PREFIXES:
            if prop.startswith(prefix):
                prop = prop[len(prefix):]  # -webkit-transform moves things like transform
                break
        exempt = prop.startswith(_REACH_EXEMPT) or "radius" in prop
        if not exempt:
            if custom and re.search(r"\b(?:var|env)\(", value):
                return True
            if _reach(value, font_px) > _FAR_PX:
                return True
        if prop == "position":
            if value in _POSITIONED or value not in {"static", "relative"} | _WIDE_KEYWORDS:
                return True
            relative = relative or value == "relative"
        elif prop in _STACKING:
            if value not in _NO_EFFECT:
                return True
        elif prop in _GRID_PLACEMENT:
            if value not in {"auto"} | _WIDE_KEYWORDS:
                return True
        elif prop in ("outline", "outline-width"):
            if "thick" in value.split() or _reach(value, font_px, combined=False) > 3:
                return True
        elif prop == "box-shadow":
            if value not in _NO_PAINT and _reach(value, font_px, combined=False) > 16:
                return True
            paints = paints or value not in _NO_PAINT
        elif prop == "text-shadow":
            if value not in _NO_PAINT and _reach(value, font_px, combined=False) > font_px:
                return True
        elif prop in _MARGINS:
            for token in value.split():
                amount = _box_px(token, font_px)
                if amount is not None and amount[0] * (10 if amount[1] else 1) < -font_px:
                    return True
        elif prop == "line-height":
            parsed = _line(value, font_px, custom)
            if isinstance(parsed, tuple) and (parsed[1] * font_px if parsed[0] == "mult" else parsed[1]) > _FAR_PX:
                return True
        elif prop in _OFFSETS:
            if value not in {"auto"} | _WIDE_KEYWORDS and any(
                    (amount := _box_px(token, font_px)) is None or abs(amount[0]) * (10 if amount[1] else 1) > font_px / 2
                    for token in value.split()):
                moved = True  # further than half a line
        elif prop == "visibility" and name in ("col", "colgroup"):
            if value != "visible":
                return True
        elif prop == "display" and (important or not display_important):
            display, display_important = value, important
        # Text shown in another order than written, or made by CSS, can spell a
        # notice nowhere in the page's text (or hide one that is).
        elif prop == "order":
            if value not in {"0"} | _WIDE_KEYWORDS:
                return True
        elif prop in ("flex-direction", "flex-flow", "grid-auto-flow"):
            if "reverse" in value or "dense" in value:
                return True
        elif prop == "float":
            if value in ("right", "inline-end"):
                return True  # right floats line up from the right: later ones show first
        elif prop.startswith("list-style") or prop == "quotes":
            if (prop == "quotes" and value not in {"auto", "none"} | _WIDE_KEYWORDS) or (
                    prop != "quotes" and re.search(r"[\"']|\b(?:symbols|counters?|url|image-set|attr)\(", value)):
                return True
        elif prop == "direction" and (important or not direction_important):
            direction, direction_important = value, important
        if prop in _HEIGHT_LIMITS and value not in {"auto", "none", "normal"} | _WIDE_KEYWORDS:
            limited = True
        if prop.startswith(("background", "border")) and value not in _NO_PAINT:
            paints = True
        if prop.startswith(("padding", "border")):
            vertical = max(vertical, _vertical_box(prop, value, font_px))
    if display in _FLOWING_DISPLAYS and "rtl" in (direction, (attrs.get("dir") or "").strip().lower()):
        return True  # flex and grid items line up from the right: shown in another order than written
    inline = display == "inline" or (display is None and name in _INLINE_TAGS)
    if relative and (moved or (name not in ("html", "body") and not inline and limited
                               and not _clips_both(declarations))):
        # A positioned box paints after the text around it: moved, or holding
        # more than its height allows without clipping it, it can cover that text.
        return True
    if inline and paints and vertical > font_px / 4:
        return True
    if name == "table":
        for attr in ("cellpadding", "cellspacing"):
            amount = _legacy_number(attrs.get(attr))
            if amount is not None and amount > 200:
                return True
    return False


def _style_context(name: str, attrs: dict, parent: dict) -> dict:
    """How an element's own markup affects whether its text is shown, given its
    parent's: {"visibility": "visible" | "hidden" | "unknown", "font_px", "custom",
    "color", "bg", "line", "text_ok", "sectioned", "cover", "box_shown"}.

    Font size, text color, the background behind the text and line height
    accumulate down the tree from browser defaults (black on white, smaller
    <small>/<sub>/<sup>/<h5>/<h6>, blue links), presentational attributes
    (<font color/size/face>, bgcolor, <body text/margins>) and inline styles;
    text_ok says text with them reads: a font size of at least 6px, contrast of
    at least 1.3 against an opaque background x-reader can read (blended through
    alpha), and lines between 0.75 and 10 times the font size apart.

    hidden: the hidden attribute (until-found whatever the display) or a popover
    with no inline display, or display:none. unknown: a style that cannot be
    parsed; any property outside an allowlist of ones that cannot hide text; or
    any declaration of a risky property with a value outside the plain ones
    x-reader understands: opacity, zoom, visibility and overflow only at their
    defaults, colors x-reader can read, symbol fonts, small indents and
    spacings, margins and padding that keep text on screen, lines no wider than
    2000px, small vertical-align shifts and decoration lines, no bidi override
    (or <bdo>, or <marquee>), no generated content or dark color scheme, no
    absolute/fixed positioning or offsets, transforms, filters, clipping, masks,
    containment or animation. cover (see _covers) is about other text: an
    element that could paint over it or carry it out of reach. It decides only
    what counts as page text: wall notices and frames count wherever they are.
    """
    if not attrs.get("style") and name not in _MODELED_TAGS and "hidden" not in attrs and "popover" not in attrs:
        return {
            "visibility": "visible", "font_px": parent["font_px"], "custom": parent["custom"],
            "color": parent["color"], "bg": parent["bg"], "line": parent["line"], "text_ok": parent["text_ok"],
            "sectioned": parent["sectioned"] or name in _SECTIONING, "cover": False, "box_shown": False,
            "links": parent["links"], "clip_box": False, "display": None, "floated": False,
            "pre": parent["pre"] or name in _PRE_TAGS, "definite_h": False,
        }
    context = {
        "visibility": "visible", "font_px": parent["font_px"], "custom": parent["custom"],
        "color": parent["color"], "bg": parent["bg"], "line": parent["line"], "text_ok": parent["text_ok"],
        "sectioned": parent["sectioned"] or name in _SECTIONING, "cover": False, "box_shown": False,
        "links": parent["links"], "clip_box": False, "display": None, "floated": False,
        "pre": parent["pre"] or name in _PRE_TAGS, "definite_h": False,
    }
    if name == "body":
        # <body link/vlink> color every link; a link may show either.
        colors = [_rgba(_legacy_color(attrs[attr])) for attr in ("link", "vlink") if (attrs.get(attr) or "").strip()]
        context["links"] = tuple(colors) or parent["links"]
    if name == "h1":
        context["font_px"] = parent["font_px"] * (0.67 if parent["sectioned"] else 2.0)
    elif name in _TAG_FONT_SCALE:
        context["font_px"] = parent["font_px"] * _TAG_FONT_SCALE[name]
    inline = _inline_declarations(attrs.get("style") or "")
    if inline is None:
        return dict(context, visibility="unknown", cover=True, text_ok=False)
    declarations = _presentational(name, attrs, parent) + inline
    context["definite_h"] = _definite_height(name, declarations, parent["definite_h"])
    names = {prop for prop, _value, _important in declarations}
    if any(prop.startswith("--") for prop in names):
        context["custom"] = True
    displays = [value for prop, value, _important in declarations if prop == "display"]
    if displays == ["none"] and "all" not in names:
        return dict(context, visibility="hidden")  # not rendered, whatever else the style says
    until_found = (attrs.get("hidden") or "").strip().lower() == "until-found"
    custom = context["custom"]
    size = _winner(declarations, "font-size", lambda value: _font_px(value, parent["font_px"]))
    own_px = size if isinstance(size, float) else context["font_px"]  # em and % below are of this size
    context["cover"] = _covers(name, attrs, declarations, own_px, custom)
    # A relative box that clips both axes and passed _covers (so it stays put):
    # whatever is positioned inside it cannot paint outside it.
    position = _winner(declarations, "position", lambda value: value)
    display = _winner(declarations, "display", lambda value: value)
    context["display"] = display
    context["floated"] = _winner(declarations, "float", lambda value: value) in ("left", "right", "inline-start",
                                                                                "inline-end")
    spacing = (_winner(declarations, "white-space", lambda value: value) or "").split()
    if spacing and spacing[0] not in _WIDE_KEYWORDS:
        context["pre"] = spacing[0] in ("pre", "pre-wrap", "pre-line", "break-spaces")
    # A clip box is a relative box that generates a box overflow applies to and
    # clips both axes with values browsers keep; it may still hide some of its
    # own text (so that text stays unsettled), but nothing inside it paints outside.
    context["clip_box"] = (not context["cover"] and position == "relative" and _clips_both(declarations)
                           and (display in _CLIPPING_DISPLAYS or (display is None and name in _BLOCK_TAGS_BY_DEFAULT)))
    context["box_shown"] = until_found  # content-visibility:hidden still paints the element's own box

    # Font size, text color, background and line height for this element's text.
    link = name == "a" and "href" in attrs
    color = _winner(declarations, "color",
                    lambda value: context["links"][0] if value == _LINK_COLOR else _text_color(value, custom))
    paint = _winner(declarations, "background-color", lambda value: _background_color(value, False, custom))
    shorthand = _winner(declarations, "background", lambda value: _background_color(value, True, custom))
    line = _winner(declarations, "line-height", lambda value: _line(value, own_px, custom))
    images = [value for prop, value, _important in declarations if prop == "background-image"]

    def unknown() -> dict:
        return dict(context, visibility="unknown", text_ok=False)

    if ("unknown",) in (size, color, paint, shorthand, line) or ("var(" in " ".join(
            value for prop, value, _important in declarations if prop == "font-size") and custom):
        return unknown()
    if size is not None:
        context["font_px"] = size
    if color == "inherit":
        context["color"] = parent["color"]
    elif color is not None:
        context["color"] = color
    background = _merge_background(declarations, paint, shorthand)
    if display == "contents":
        background, images = "keep", []  # no box, so no background: its text sits on what is behind it
    if background == "currentcolor":
        background = context["color"]
    if any(value not in _NO_PAINT for value in images) or background == "image":
        # The page's own backdrop is judged like external CSS; an image behind
        # text anywhere else is not something x-reader can read the colors of.
        context["bg"] = "token" if name in ("html", "body") else None
    elif background == "token":
        context["bg"] = "token"
    elif isinstance(background, tuple) and background[3] > 0:
        under = context["bg"]
        if background[3] >= 1:
            context["bg"] = background[:3]
        elif isinstance(under, tuple):
            context["bg"] = _over(background, under)
        else:
            context["bg"] = under if under == "token" else None
    if line is not None and line != "inherit":
        context["line"] = line if line[0] == "mult" else ("px", line[1])
    kind, amount = context["line"]
    line_px = amount * context["font_px"] if kind == "mult" else amount
    shown_colors = [context["color"]]
    if link and color in context["links"]:
        shown_colors = list(context["links"])  # unvisited or visited, either may show
    context["text_ok"] = (context["font_px"] >= _MIN_TEXT_PX
                          and all(shade is not None and _readable(shade, context["bg"]) for shade in shown_colors)
                          and _MIN_LINE * context["font_px"] <= line_px <= _MAX_LINE * context["font_px"])

    if name in ("bdo", "marquee"):
        return unknown()  # reversed or moving text
    if "popover" in attrs and displays:
        return unknown()  # a popover shown by inline CSS: not the stated hiding, not plain text either
    won: dict = {}
    for prop, value, important in declarations:
        if prop not in won or important or not won[prop][1]:
            won[prop] = (value, important)
    font = context["font_px"]
    for prop, value, _important in declarations:
        uses_var = "var(" in value or "env(" in value or "attr(" in value
        if prop.startswith("--") or (prop.startswith("-") and not prop.startswith(_VENDOR_PREFIXES)):
            continue  # custom properties do nothing alone; other dash names are not CSS and are ignored
        if prop in _EXACT_VALUES:
            if value not in _EXACT_VALUES[prop]:
                return unknown()
        elif prop in ("font-size", "color", "line-height", "background", "background-color", "background-image"):
            continue  # judged above
        elif prop in ("background-clip", "-webkit-background-clip"):
            if "text" in value.split():
                return unknown()
        elif prop.startswith("text-decoration"):
            if _reach(value, font, combined=False) > 0.3 * font:
                return unknown()  # a decoration line thick enough to cover the text
        elif prop in _WIDTHS:
            if (uses_var and custom) or _reach(value, font, combined=prop.startswith("grid")) > _WIDE_PX:
                return unknown()
            if prop in _GRID_TRACKS and display in ("grid", "inline-grid") and not _tracks_ok(value, font):
                return unknown()  # tracks too small for their content: items pile on one another
        elif prop in _GRID_TRACKS:
            if display in ("grid", "inline-grid") and not _tracks_ok(value, font, rows="rows" in prop):
                return unknown()
        elif prop in ("grid", "grid-template"):
            if display in ("grid", "inline-grid") and value not in {"none"} | _WIDE_KEYWORDS:
                return unknown()  # shorthands mixing areas and tracks are not judged
        elif prop.startswith("list-style"):
            if re.search(r"[\"']|\b(?:symbols|counters?|url|image-set|attr)\(", value):
                return unknown()  # a marker made of author text or an image is text x-reader does not see
        elif prop == "quotes":
            if value not in {"auto", "none"} | _WIDE_KEYWORDS:
                return unknown()  # quotation marks are author text around <q>
        elif prop in ("order", "-webkit-order", "-ms-flex-order"):
            if value not in {"0"} | _WIDE_KEYWORDS:
                return unknown()  # shown in a different order than written
        elif prop in ("flex-direction", "flex-flow", "-webkit-flex-direction", "-webkit-flex-flow",
                      "-ms-flex-direction", "grid-auto-flow"):
            if "reverse" in value or "dense" in value:
                return unknown()
        elif prop == "float":
            if value not in {"none", "left", "inline-start"} | _WIDE_KEYWORDS:
                return unknown()  # right floats line up from the right: later ones show first
        elif prop in _BOX_HEIGHTS:
            continue  # judged below, by the declarations that win
        elif prop.startswith(_INERT_PREFIXES) or prop in _INERT_PROPERTIES:
            continue
        elif prop in _NONE_ONLY or prop in ("all", "font"):
            if value != "none" or prop in ("all", "font"):
                return unknown()
        elif prop == "display":
            if value not in _VISIBLE_DISPLAYS:
                return unknown()
        elif prop == "font-family":
            if _SYMBOL_FONTS.search(value):
                return unknown()
        elif prop == "unicode-bidi":
            if "override" in value:
                return unknown()
        elif prop in ("tab-size", "-moz-tab-size"):
            number = _PLAIN_NUMBER.fullmatch(value)
            amount = _length_px(value, font)
            if not (value in _WIDE_KEYWORDS or (number and float(number.group(1)) <= 16)
                    or (amount is not None and 0 <= amount <= 200)):
                return unknown()
        elif prop == "vertical-align":
            if value in _VALIGN_KEYWORDS or (uses_var and not custom):
                continue
            amount = _box_px(value, font)
            if amount is None or abs(amount[0]) > (200 if amount[1] else 2 * font):
                return unknown()
        elif prop in ("text-indent", "letter-spacing", "word-spacing"):
            if value == "normal" and prop != "text-indent":
                continue
            amount = _length_px(value, font)
            low, high = {"text-indent": (-3 * font, 1000),
                         "letter-spacing": (-0.1 * font, font),
                         "word-spacing": (-0.25 * font, 2 * font)}[prop]
            if amount is None or not low <= amount <= high:
                return unknown()
        elif prop in _MARGINS or prop in _PADDINGS:
            padding = prop in _PADDINGS
            for token in value.split():
                if token in _WIDE_KEYWORDS or (token == "auto" and not padding):
                    continue
                if token.startswith("var("):
                    if custom or "," in token:
                        return unknown()
                    continue
                amount = _box_px(token, font)
                if amount is None:
                    return unknown()
                low, high = ((0, 50) if padding else (-20, 50)) if amount[1] else ((0, 1000) if padding else (-200, 1000))
                if not low <= amount[0] <= high:
                    return unknown()
        else:
            return unknown()  # outside the allowlist
    if won.get("position", ("",))[0] in ("relative", "sticky") and any(
            _box_px(token, font) is None or abs(_box_px(token, font)[0]) * (10 if _box_px(token, font)[1] else 1) > font / 2
            for prop in set(won) & _OFFSETS for token in won[prop][0].split() if token not in {"auto"} | _WIDE_KEYWORDS):
        return unknown()  # offsets beyond half a line can move it off-screen
    boxed = display not in ("inline", "contents") and (display is not None or name not in _INLINE_TAGS)
    if boxed and name not in ("html", "body") and any(
            winner is not None and not _height_open(winner, custom, parent["definite_h"])
            for winner in (_winner(declarations, prop, lambda value: value) for prop in _BOX_HEIGHTS)):
        return unknown()  # a set height its text can overrun, spilling over what follows
    rtl = (attrs.get("dir") or "").strip().lower() == "rtl" or _winner(
        declarations, "direction", lambda value: value) == "rtl"
    if rtl and display in _FLOWING_DISPLAYS:
        return unknown()  # flex and grid items line up from the right: shown in a different order than written
    if until_found:
        return dict(context, visibility="hidden")  # content-visibility:hidden, whatever the display says
    if displays:
        return context  # an inline display overrides the hidden attribute
    if "hidden" in attrs or "popover" in attrs:
        return dict(context, visibility="hidden")
    return context


def _merge_background(declarations: list, paint, shorthand):
    """The background color that wins between background-color and the
    background shorthand (which resets it): later beats earlier, !important
    beats both."""
    if paint is None:
        return shorthand
    if shorthand is None:
        return paint
    last = {"background-color": (-1, False), "background": (-1, False)}
    for index, (prop, _value, important) in enumerate(declarations):
        if prop in last and (important or not last[prop][1]):
            last[prop] = (index, important)
    (paint_at, paint_important), (short_at, short_important) = last["background-color"], last["background"]
    if paint_important != short_important:
        return paint if paint_important else shorthand
    return paint if paint_at > short_at else shorthand


def _declares_fixed(attrs: dict) -> bool:
    """Fixed positioning, which escapes the clipping of every box around it."""
    declarations = _inline_declarations(attrs.get("style") or "")
    return declarations is None or any(
        prop == "position" and value not in ("static", "relative", "absolute", "sticky")
        for prop, value, _important in declarations)


def _shown_by_style(attrs: dict) -> bool:
    """An element browsers hide by default that an inline display shows."""
    declarations = _inline_declarations(attrs.get("style") or "")
    return declarations is None or any(
        prop == "display" and value in _VISIBLE_DISPLAYS for prop, value, _important in declarations)


_SVG = "{http://www.w3.org/2000/svg}"
_MATHML = "{http://www.w3.org/1998/Math/MathML}"


def _foreign_cover(tag: str, name: str, attrs: dict, parent: dict) -> bool:
    """_covers for SVG and MathML: an <svg> root is checked with its size
    attributes and must keep its default clipping (overflow visible lets its
    drawing spill over the page); shapes inside it are clipped to it; MathML
    elements and HTML inside <foreignObject> are positioned like HTML, and
    MathML size attributes count as geometry."""
    if parent["in_svg"] and tag.startswith(_SVG):
        return False  # drawn inside an <svg> box, which clips it unless its overflow is visible (checked there)
    declarations = _inline_declarations(attrs.get("style") or "")
    if declarations is None:
        return True
    if tag.startswith(_SVG):
        declarations = _presentational("svg", attrs, parent) + declarations
        overflow = [(attrs.get("overflow") or "").strip().lower()] + [
            value for prop, value, _important in declarations if prop.startswith("overflow")]
        if any(value in ("visible", "auto") for value in overflow):
            return True
    if tag.startswith(_MATHML):
        for attr in ("width", "height", "depth", "lspace", "rspace", "voffset", "mathsize"):
            if (attrs.get(attr) or "").strip() and _reach(attrs[attr].lower(), parent["font_px"]) > _FAR_PX:
                return True
    return _covers(name, attrs, declarations, parent["font_px"], parent["custom"])


class _Page(str):
    """A downloaded page's text, which remembers the response's media type."""

    media_type = "text/html"


def _plain_text_page(text: str) -> dict:
    """A text/plain response as browsers show it: every character as text."""
    content = "\n".join(line.rstrip() for line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    content = content.strip()
    if len(content) > MAX_CONTENT_CHARS:
        return {"title": "", "content": "", "embeds": 0, "page_text": "", "primary_used": False,
                "visibility_unknown": True, "layout_risk": False, "templates": False, "too_complex": True,
                "client_redirect": False}
    return {"title": "", "content": content, "embeds": 0, "page_text": content, "primary_used": False,
            "visibility_unknown": bool(_BIDI_OVERRIDE.search(content)), "layout_risk": False, "templates": False,
            "too_complex": False, "client_redirect": False}


def _split_tracks(value: str) -> list:
    """Track-list tokens split on spaces outside (), with [line names] left out."""
    tokens, current, depth = [], [], 0
    for char in value + " ":
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char.isspace() and depth == 0:
            token = "".join(current)
            if token and not token.startswith("["):
                tokens.append(token)
            current = []
            continue
        current.append(char)
    return tokens


def _track_ok(token: str, font_px: float, rows: bool = False) -> bool:
    """A grid track that holds its text: flexible or content-sized; a column of
    at least 1em (5%) also does, while a fixed row height can be overrun."""
    if token in ("auto", "min-content", "max-content"):
        return True
    flexible = re.fullmatch(rf"({_NUMBER})fr", token)
    if flexible:
        return float(flexible.group(1)) > 0  # 0fr takes no share: the track stays at its minimum
    for function in ("repeat(", "minmax(", "fit-content("):
        if token.startswith(function) and token.endswith(")"):
            inner = token[len(function):-1]
            if function == "repeat(":
                _count, comma, tracks = inner.partition(",")
                return bool(comma) and bool(_split_tracks(tracks)) and all(
                    _track_ok(track, font_px, rows) for track in _split_tracks(tracks))
            if function == "minmax(":
                low, comma, high = inner.partition(",")
                if not comma:
                    return False
                if rows:  # the row may end at its maximum: that must grow with the content
                    return _track_ok(high.strip(), font_px, rows)
                return _track_ok(high.strip(), font_px) or _track_ok(low.strip(), font_px)
            return not rows and _track_ok(inner.strip(), font_px)
    if rows:
        return False
    amount = _box_px(token, font_px)
    return amount is not None and amount[0] >= (5 if amount[1] else font_px)


def _tracks_ok(value: str, font_px: float, rows: bool = False) -> bool:
    if value in {"none", "subgrid", "masonry"} | _WIDE_KEYWORDS:
        return True
    tokens = _split_tracks(value)
    return bool(tokens) and all(_track_ok(token, font_px, rows) for token in tokens)


def _height_open(value: str, custom: bool, parent_definite: bool = False) -> bool:
    """A height that leaves a box as tall as its content needs: auto, content
    sizing, or a percentage of a parent whose own height is not set (which
    computes to auto); a design token from the stylesheet is judged like
    external CSS."""
    if value in {"auto", "none", "max-content", "min-content", "fit-content", "stretch", "-webkit-fill-available"} \
            | _WIDE_KEYWORDS:
        return True
    if re.fullmatch(r"var\(\s*--[\w-]+\s*\)", value):
        return not custom
    return bool(re.fullmatch(rf"{_NUMBER}%", value)) and not parent_definite


def _definite_height(name: str, declarations: list, parent_definite: bool) -> bool:
    """Whether the element's height is set (so percentages inside it resolve):
    a length, or a percentage of a set height (the root's is the viewport's)."""
    height = _winner(declarations, "height", lambda value: value)
    if height is None or height in {"auto", "max-content", "min-content", "fit-content"} | _WIDE_KEYWORDS:
        return False
    if re.fullmatch(rf"{_NUMBER}%", height):
        return parent_definite or name == "html"
    return not re.fullmatch(r"var\(\s*--[\w-]+\s*\)", height)


def _join_text(parts: list, spaced: bool = False) -> str:
    """Text as browsers lay it out: inline chunks run on with nothing added and
    whitespace runs collapse to one space; a line break (_LINE_BREAK) ends a
    line and a block ("\n") ends a paragraph, which a blank line separates.
    spaced=True puts a space at every element boundary (_BOUNDARY) instead."""
    blocks, lines, current = [], [], []
    gap = " " if spaced else ""

    def end_line() -> None:
        line = " ".join("".join(current).split())
        current.clear()
        if line:
            lines.append(line)

    for chunk in parts:
        if chunk == "\n":
            end_line()
            if lines:
                blocks.append("\n".join(lines))
                lines.clear()
        elif chunk == _LINE_BREAK:
            end_line()
        elif chunk == _BOUNDARY:
            current.append(gap)
        else:
            current.append(chunk)
    end_line()
    if lines:
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# Parse budgets, enforced inside the parser: tokens read, attributes per tag
# (checked before tokenizing, whose duplicate-attribute check is quadratic),
# elements and comments created (clones included), attribute entries,
# open depth, tree work (each token charged for the open elements and active
# formatting entries its steps walk, plus scope and formatting-list searches)
# and text copying.
MAX_PARSE_TOKENS = 400_000
MAX_TAG_ATTRIBUTES = 400
MAX_PARSE_NODES = 60_000
MAX_PARSE_ATTRIBUTES = 200_000
MAX_PARSE_DEPTH = 1_500
MAX_PARSE_WORK = 6_000_000  # the largest real pages checked need about a quarter of this
MAX_TEXT_COPY = 64_000_000
MAX_CONTENT_CHARS = 1_000_000
MAX_PARSE_ERRORS = 20_000  # real pages checked: under 200
MAX_STYLE_TOTAL = 1_000_000  # inline style characters on a page (real pages checked: under 100k)
MAX_STYLE_DECLARATIONS = 50_000
# Raw-text elements whose text x-reader never reads.
_UNREAD_TEXT = {"script", "style", "noscript", "iframe", "noembed", "noframes"}


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


MAX_INVALID_CODEPOINTS = 1_000


def _error_heavy(html: str) -> bool:
    """More code points html5lib reports as parse errors (controls,
    noncharacters in every plane, lone surrogates; its own pattern) than
    MAX_INVALID_CODEPOINTS: it drains that error queue in quadratic time."""
    from html5lib._inputstream import invalid_unicode_re

    for count, _match in enumerate(invalid_unicode_re.finditer(html), 1):
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

                    # html5lib copies, searches and shifts the child list for these
                    def insertBefore(self, node, refNode):
                        builder._charge(len(self._element))
                        return super().insertBefore(node, refNode)

                    def removeChild(self, node):
                        builder._charge(len(self._element))
                        return super().removeChild(node)

                    def reparentChildren(self, newParent):
                        builder._charge(len(self._element) + len(newParent._element))
                        return super().reparentChildren(newParent)

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

        def elementInActiveFormattingElements(self, name):
            self._charge(len(self.activeFormattingElements))
            return super().elementInActiveFormattingElements(name)

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
        """Wraps the tokenizer: counts and charges tokens, notes template start tags."""

        def __init__(self, tokenizer, parser):
            object.__setattr__(self, "_tokenizer", tokenizer)
            object.__setattr__(self, "_parser", parser)

        def __iter__(self):
            start_tag = 3  # html5lib.constants.tokenTypes["StartTag"]
            tree = self._parser.tree
            for count, token in enumerate(self._tokenizer, 1):
                if count > MAX_PARSE_TOKENS:
                    raise ParseBudgetExceeded("too many tokens")
                # The tree steps one token triggers walk the open elements and the
                # active formatting list (end tags of formatting elements walk both).
                tree._charge(1 + len(tree.openElements) + len(tree.activeFormattingElements))
                if token.get("type") == start_tag and token.get("name") == "template":
                    self._parser.saw_template = True
                yield token

        def __getattr__(self, name):
            return getattr(self._tokenizer, name)

        def __setattr__(self, name, value):
            setattr(self._tokenizer, name, value)

    class Parser(html5lib.HTMLParser):
        saw_template = False
        parse_errors = 0

        def parseError(self, errorcode="XXX-undefined-error", datavars=None):
            # html5lib keeps a tuple per error (and works out its line and
            # column); x-reader only counts them.
            self.parse_errors += 1
            if self.parse_errors > MAX_PARSE_ERRORS:
                raise ParseBudgetExceeded("too many parse errors")

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
    layout_risk, templates, too_complex, client_redirect}.

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
    placeholders, text box values and option labels count only there. embeds
    counts every player, frame and plugin on the page, however small or hidden.
    visibility_unknown: content includes text whose visibility x-reader cannot
    settle (see _style_context: inline styles, presentational attributes,
    browser default sizes and colors, bidi overrides), or any rendered element
    on the page could paint over text or carry it out of view (layout_risk, see
    _covers and _foreign_cover). templates: the page
    has a <template>, whose contents and shadow roots the parser does not
    model. client_redirect: a <meta http-equiv=refresh> names a target URL.
    """
    from x_reader.evidence import MIN_GENERIC_BODY_CHARS, prose_chars

    refused = {"title": "", "content": "", "embeds": 0, "page_text": "", "primary_used": False,
               "visibility_unknown": True, "layout_risk": True, "templates": False, "too_complex": True,
               "client_redirect": False}
    if _attribute_heavy(html) or _error_heavy(html):
        return refused
    parser = _budgeted_parser()
    try:
        root = parser.parse(html, scripting=True)
    except (ParseBudgetExceeded, RecursionError):
        return refused
    style_chars = style_declarations = 0
    for element in root.iter():
        style = element.get("style") if isinstance(element.tag, str) else None
        if style:
            style_chars += len(style)
            style_declarations += style.count(":")
    if style_chars > MAX_STYLE_TOTAL or style_declarations > MAX_STYLE_DECLARATIONS:
        return refused  # more inline styling than x-reader judges within its time limits
    templates = parser.saw_template or any(True for _ in root.iter("template"))
    title_element = next(iter(root.iter("title")), None)
    title, title_truncated = "", False
    if title_element is not None:
        pieces, kept = [], 0
        for piece in title_element.itertext():
            pieces.append(piece[:1000 - kept])
            kept += len(pieces[-1])
            if kept >= 1000:
                break
        title = " ".join("".join(pieces).split())
        length = 0
        for piece in title_element.itertext():
            length += len(piece)
            if length > 1000:
                title_truncated = True  # walls past the first 1000 characters would go unseen
                break

    parts: dict = {"gate": [], "body": [], "main": [], "article": []}
    unknown_in: set = set()
    open_groups: set = set()
    embeds = 0
    layout_risk = False
    box_regions: dict = {}  # clip box -> regions holding text inside it
    annotations: list = []  # ruby annotations
    covered_boxes: set = set()
    client_redirect = any(
        (meta.get("http-equiv") or "").strip().lower() == "refresh" and _parse_refresh(meta.get("content") or "")
        for meta in root.iter("meta")
    )
    base = {"skip": False, "foreign": False, "boiler": False, "main": False, "article": False,
            "hidden": False, "unknown": False, "block": False, "control": False, "collapsed": None,
            "font_px": 16.0, "custom": False, "color": _BLACK, "bg": _WHITE, "line": ("mult", 1.2),
            "text_ok": True, "sectioned": False, "links": _LINKS, "box": None, "in_svg": False,
            "flowing": False, "pre": False, "annotation": False, "definite_h": False}

    def emit(text, state) -> None:
        if not text or state["skip"]:
            return
        if state["pre"] and text not in ("\n", _LINE_BREAK) and "\n" in text:
            for index, line in enumerate(text.replace("\r\n", "\n").split("\n")):
                if index:
                    emit(_LINE_BREAK, state)
                emit(line, state)
            return
        chunk = text
        if chunk == _BOUNDARY:
            parts["gate"].append(chunk)
            return
        if state["annotation"]:
            if not chunk.isspace():
                annotations.append(chunk)  # shown above the base text: walls count, the base text runs on
            return
        parts["gate"].append(chunk)
        if state["foreign"] or state["boiler"] or state["hidden"] or state["control"]:
            return
        regions = ["body"] + [region for region in ("main", "article") if state[region]]
        shown = chunk not in ("\n", _LINE_BREAK) and not chunk.isspace()
        if shown and (state["unknown"] or not state["text_ok"] or _BIDI_OVERRIDE.search(chunk)):
            unknown_in.update(regions)
        if shown and state["box"] is not None:
            box_regions.setdefault(state["box"], set()).update(regions)
        for region in regions:
            parts[region].append(chunk)

    def child_state(element, parent) -> dict:
        nonlocal embeds, layout_risk
        tag = element.tag
        foreign = parent["foreign"] or tag.startswith("{")
        name = tag.rsplit("}", 1)[-1].lower()
        attrs = {str(key).rsplit("}", 1)[-1]: value for key, value in element.attrib.items()}
        if name in _EMBED_TAGS and not parent["skip"]:
            embeds += 1  # however small or hidden: ancestors and author CSS can show or enlarge it
        shadow_root = name == "template" and (
            (attrs.get("shadowrootmode") or attrs.get("shadowroot") or "").strip().lower() in ("open", "closed"))
        if foreign:
            style = {key: parent[key] for key in ("font_px", "custom", "color", "bg", "line", "text_ok", "sectioned",
                                                  "links")}
            style.update(visibility="visible", cover=_foreign_cover(tag, name, attrs, parent), box_shown=False,
                         clip_box=False, display=None, floated=False, pre=parent["pre"], definite_h=False)
        else:
            style = _style_context(name, attrs, parent)
        if style["cover"] and not (parent["skip"] or parent["hidden"] or parent["control"]) and (
                style["visibility"] != "hidden" or style["box_shown"]) and (
                name not in _UNRENDERED or _shown_by_style(attrs)):
            # This element could paint over text or carry it out of view: the
            # text of the clip box it sits in, or (fixed, or in no clip box) the page.
            if parent["box"] is not None and not _declares_fixed(attrs):
                covered_boxes.add(parent["box"])
            else:
                layout_risk = True
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
            # a block starts a new line, unless its parent lays children out side by
            # side (flex, grid) or it floats; cells sit on one line, a space apart
            "block": not foreign and not parent["flowing"] and not style["floated"] and (
                style["display"] in _BLOCK_DISPLAYS or (style["display"] is None and name in _BLOCK_TAGS)),
            "cell": not foreign and (style["display"] == "table-cell" or (style["display"] is None and name in _CELL_TAGS)),
            "line_break": not foreign and name == "br",
            "annotation": parent["annotation"] or (not foreign and name in ("rt", "rtc")),
            "flowing": not foreign and style["display"] in _FLOWING_DISPLAYS,
            "pre": style["pre"], "definite_h": style["definite_h"],
            "control": parent["control"] or (not foreign and name in _CONTROL_TAGS),
            "collapsed": {"summary_seen": False, "outer_hidden": own_hidden} if closed and name == "details" else None,
            "font_px": style["font_px"], "custom": style["custom"], "color": style["color"], "bg": style["bg"],
            "line": style["line"], "text_ok": style["text_ok"], "sectioned": style["sectioned"],
            "links": style["links"], "box": id(element) if style["clip_box"] else parent["box"],
            "in_svg": parent["in_svg"] or tag.startswith(_SVG),
            "button_text": name == "input" and (attrs.get("type") or "").strip().lower() in _BUTTON_INPUTS
            and (attrs.get("value") or ""),
            "placeholder": attrs.get("placeholder") or "" if name in ("input", "textarea") else "",
            "field_value": attrs.get("value") or "" if name == "input"
            and (attrs.get("type") or "").strip().lower() not in _NON_TEXT_INPUTS else "",
            "option_label": attrs.get("label") or "" if name in ("option", "optgroup") else "",
            "alt_text": attrs.get("alt") or "" if name == "img" or (
                name == "input" and (attrs.get("type") or "").strip().lower() == "image") else "",
        }

    stack: list = []

    def enter(element, parent) -> None:
        state = child_state(element, parent)
        if state["block"]:
            emit("\n", state)
        elif state["line_break"]:
            emit(_LINE_BREAK, state)
        elif state["cell"]:
            emit(" ", state)
        else:
            emit(_BOUNDARY, state)
        for label in (state["button_text"], state["placeholder"], state["field_value"], state["option_label"],
                      state["alt_text"]):
            if label:  # visible wording that is not a text node, in its own box: a wall's button or prompt
                emit(f" {label} ", dict(state, control=True))
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
            elif state["cell"]:
                emit(" ", state)
            else:
                emit(_BOUNDARY, state)
            emit(element.tail, parent)
        elif not isinstance(child.tag, str):  # comment or processing instruction
            emit(child.tail, state)
        else:
            enter(child, state)

    for box in covered_boxes:
        unknown_in.update(box_regions.get(box, ()))
    content, primary_used, chosen = _join_text(parts["body"]), False, "body"
    for name in ("main", "article"):
        primary = _join_text(parts[name])
        if primary and prose_chars(primary) >= MIN_GENERIC_BODY_CHARS:
            content, primary_used, chosen = primary, True, name
            break
    if len(content) > MAX_CONTENT_CHARS:
        return refused  # too much text to judge, or to hand on, within the service's limits
    return {
        "title": title[:200], "title_scanned": title, "title_truncated": title_truncated, "content": content,
        "embeds": embeds,
        "page_text": "\n\n".join(filter(None, [_join_text(parts["gate"]), _join_text(parts["gate"], spaced=True), "\n".join(
            " ".join(note.split()) for note in annotations)])), "primary_used": primary_used,
        "visibility_unknown": chosen in unknown_in or layout_risk, "layout_risk": layout_risk,
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
    media_type = getattr(html, "media_type", "text/html")
    page = _plain_text_page(html) if media_type == "text/plain" else _extract_html_page(html)
    if page["too_complex"]:
        raise ParseBudgetExceeded("The page has more markup than x-reader parses")
    title, content = page["title"], page["content"]
    # Walls announced anywhere on the page count, not only in the extracted text;
    # a standalone "Read more" call counts only inside the text itself. A cookie
    # banner or consent overlay beside substantial marked main content hides
    # nothing from this read (the article is in the HTML), so consent wording
    # counts only inside the text. The signals cover the text shown, so the
    # receipt reuses them instead of scanning it again.
    scanned = page.get("title_scanned", title)  # the whole title up to 1000 characters
    signals = set(gate_signals(scanned, page["page_text"], cta_text=content, markup=False))
    if page["primary_used"]:
        signals -= {"consent", "consent_title"}
        signals |= set(consent_signals(scanned, content, markup=False))
    if page["visibility_unknown"]:
        signals.add("visibility_unknown")
    if page["client_redirect"]:
        signals.add("client_redirect")
    if page.get("title_truncated"):
        signals.add("scan_budget_exceeded")  # the title runs past what x-reader checks for walls
    if page["templates"]:
        signals.add("template_markup")
    if media_type == "application/xhtml+xml":
        signals.add("xml_markup")  # browsers parse it as XML (an error page if it is not well formed)
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
            page = _Page(body.decode(encoding, errors="replace"))
            page.media_type = raw_content_type.split(";", 1)[0].strip().lower() or "text/html"
            return page, current
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
