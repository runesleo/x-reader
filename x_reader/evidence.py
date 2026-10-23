"""Canonical evidence receipt projection for x-reader outputs.

This module intentionally contains no network or storage code. It translates an
existing UnifiedContent JSON payload into the public PASS/PARTIAL/FAIL/UNKNOWN
contract used by the Agent Skill and the Evidence Receipt web surface.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse
import html
import json
import hashlib
import math
import re
import unicodedata

from x_reader.lookalikes import LOOKALIKES


PASS = "PASS"
PARTIAL = "PARTIAL"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

# Subtitle cues prove spoken coverage only when no stretch of the video longer
# than this is uncaptioned (before the first cue, between cues, after the last)
# and the cues span at least MIN_TRANSCRIPT_UNION_RATIO of the duration.
MAX_TRANSCRIPT_GAP_SECONDS = 30.0
MIN_TRANSCRIPT_UNION_RATIO = 0.5
# Encoded and decoded audio may differ only by codec priming/padding (MP3 and
# AAC add well under 0.1 s); the bound does not grow with the episode length.
AUDIO_DURATION_TOLERANCE_SECONDS = 0.5
MIN_AUDIO_COVERAGE_RATIO = 0.95
# Less visible prose than this is too thin to show it is a page's body rather
# than navigation, a teaser, a cookie wall or an error shell.
MIN_GENERIC_BODY_CHARS = 280


def audio_duration_matches(declared: float, measured: float) -> bool:
    """True when measured audio covers the declared duration up to codec padding."""
    return (
        declared > 0
        and abs(measured - declared) <= AUDIO_DURATION_TOLERANCE_SECONDS
        and measured / declared >= MIN_AUDIO_COVERAGE_RATIO
    )


def _finite_positive(value: Any) -> float | None:
    number = _finite_non_negative(value)
    return number if number else None


def _finite_non_negative(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _subtitle_coverage_proven(extra: dict) -> bool:
    """Subtitle cues cover the whole video: no long uncaptioned start, gap or tail."""
    if extra.get("transcript_source") != "subtitles":
        return False
    duration = _finite_positive(extra.get("duration"))
    start = _finite_non_negative(extra.get("transcript_start_seconds"))
    end = _finite_positive(extra.get("transcript_end_seconds"))
    covered = _finite_positive(extra.get("transcript_covered_seconds"))
    gap = _finite_non_negative(extra.get("transcript_max_gap_seconds"))
    if None in (duration, start, end, covered, gap):
        return False
    if start >= end or end > duration * 1.05 + 1 or covered > end - start + 0.01:
        return False
    return (
        start <= MAX_TRANSCRIPT_GAP_SECONDS
        and duration - end <= MAX_TRANSCRIPT_GAP_SECONDS
        and gap <= MAX_TRANSCRIPT_GAP_SECONDS
        and covered / duration >= MIN_TRANSCRIPT_UNION_RATIO
    )


# Each pattern stops at the next opening bracket, so scanning stays linear.
_MD_IMAGE = re.compile(r"!\[[^\[\]]*\]\([^()]*\)")
_MD_LINK = re.compile(r"\[([^\[\]]*)\]\([^()]*\)")
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_BARE_URL = re.compile(r"https?://\S+")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def visible_text(content: str, markup: bool = True) -> str:
    """Text a reader would see: no markup, image links, link targets or bare
    URLs. With markup=False the text is already what the page shows (a DOM
    read, a text/plain page): only bare URLs go, and nothing is unescaped."""
    text = content
    if markup:
        text = _MD_IMAGE.sub(" ", text)
        text = _MD_LINK.sub(r"\1", text)
        text = html.unescape(_HTML_TAG.sub(" ", text))
    text = _BARE_URL.sub(" ", text)
    return "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()


# Counting by search keeps memory flat however long a line is.
_TWENTY_CJK = re.compile(r"(?:[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af][^\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]*){20}")
_SENTENCE_END = re.compile(r"[.!?;:](?:[\"')\]]*)(?:\s|$)|[。！？；]")


def prose_chars(text: str) -> int:
    """Characters in sentence-like lines.

    A line counts when it has six or more words, or 20+ CJK characters, and
    contains sentence punctuation; menus, headline lists, buttons, captions and
    character runs do not count.
    """
    total = 0
    for line in text.splitlines():
        long_enough = (len(line) >= 40 and len(line.split(None, 5)) >= 6) or _TWENTY_CJK.search(line)
        if long_enough and _SENTENCE_END.search(line):
            total += len(line)
    return total


_PAYWALL_PHRASES = (
    "subscribe to continue", "subscribe to read", "subscribe now to continue",
    "subscribe to unlock", "subscribers only", "for subscribers only",
    "this article is for subscribers", "this content is for subscribers",
    "this post is for paid subscribers", "this post is for paying subscribers",
    "become a member to read", "this story is only available to members",
    "members-only content", "unlock this article", "sign up to read the full",
    "register to continue reading", "create a free account to continue",
    "log in to read the full", "sign in to read the full",
    "reached your free article limit", "reached your article limit",
    "upgrade to paid to read", "keep reading with a 7-day free trial", "to keep reading this post",
    "订阅后阅读", "订阅后查看", "付费阅读", "付费后阅读", "会员专享", "开通会员后",
    "登录后查看全文", "登录后阅读全文", "解锁全文", "购买后阅读",
)
_CONSENT_PHRASES = (
    "we value your privacy", "accept all cookies", "manage cookie preferences",
    "cookie consent", "we use cookies", "this site uses cookies", "uses cookies to",
    "your privacy choices", "consent to the use of cookies", "consent preferences",
)
# Common in consent dialogs, but also in prose and footer links: they only add weight.
_CONSENT_WEAK_PHRASES = (
    "reject all", "and our partners", "legitimate interest", "manage preferences",
    "cookie settings", "cookie policy",
)
_CONSENT_TITLES = ("before you continue", "cookie", "consent", "privacy choices", "your privacy")
# Pages whose substance is audio/video x-reader does not read on the generic path.
_MEDIA_PAGE_HOSTS = (
    "vimeo.com", "tiktok.com", "instagram.com", "soundcloud.com", "open.spotify.com",
    "twitch.tv", "dailymotion.com", "rumble.com", "loom.com", "music.apple.com",
    "douyin.com", "kuaishou.com", "ixigua.com", "v.qq.com", "youku.com",
)
# WeChat's removal, block and verification notices; the generic ones also occur
# in ordinary writing, so they count only as a whole line or title.
_WECHAT_UNAVAILABLE_STRONG = (
    "此内容因违规无法查看", "该内容已被发布者删除", "此内容被投诉且经审核涉嫌侵权",
    "此内容因涉嫌违反相关法律法规", "该公众号已迁移", "此帐号已被屏蔽", "此帐号已自主注销",
    "该内容暂时无法查看", "完成验证后即可继续访问",
)
_WECHAT_UNAVAILABLE = _WECHAT_UNAVAILABLE_STRONG + ("内容无法查看", "当前环境异常", "参数错误", "链接已过期")


def _wechat_notice(line: str) -> bool:
    """A strong notice anywhere in the line, or the line is a generic notice."""
    bare = re.sub(r"[\s\W_]+", "", line)
    return any(phrase in line for phrase in _WECHAT_UNAVAILABLE_STRONG) or bare in _WECHAT_UNAVAILABLE
_READ_MORE_TAIL = (
    "read more", "continue reading", "read the full", "read the rest",
    "阅读全文", "阅读原文", "查看全文", "点击阅读",
)


def _host_in(url: str, domains: tuple) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


_QUOTES = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u02bc": "'", "\u2032": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " ",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u2800": " ",  # the blank braille cell draws as a space
})
# Code points that draw nothing (Unicode's default-ignorable ones): a word they
# split reads as one word. Hangul fillers may also draw as a blank, by font
# (see _normalized_lines).
_IGNORABLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164"
    "\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
_BLANK_FILLERS = re.compile("[\u115f\u1160\u3164\uffa0]")
_MARKS = None  # combining marks (Mn, Me), filled in on first use


def _drop_marks(text: str) -> str:
    global _MARKS
    if _MARKS is None:
        _MARKS = dict.fromkeys(code for code in range(0x300, 0x20000) if unicodedata.category(chr(code)) in ("Mn", "Me"))
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", text).translate(_MARKS))


def _normalize(text: str) -> str:
    """The form wall rules match on: NFKC; letters that look like ASCII ones
    read as those (see lookalikes); casefolded; plain quotes and dashes; code
    points that draw nothing removed; accents and other combining marks
    dropped; hyphenated words split ("Sign-in" -> "sign in"); whitespace
    collapsed."""
    if not text.isascii():
        text = unicodedata.normalize("NFKC", text).translate(LOOKALIKES)
    text = text.casefold()
    if not text.isascii():
        text = _IGNORABLE.sub("", text.translate(_QUOTES))
        if not text.isascii():
            text = _drop_marks(text).translate(LOOKALIKES)
    return " ".join(re.sub(r"(?<=\w)-(?=\w)", " ", text).split())


# What a notice may open with before its words: punctuation, bullets, arrows,
# emoji, "Oops!", "Sorry, but", "Error:", "Notice:".
_LEAD = re.compile(
    r"^[\W_]*(?:(?:oops|whoops|sorry|uh oh|oh no|hmm+|error|we're sorry|we are sorry|but|unfortunately"
    r"|apologies|notice|alert|warning)\b[\W_]*|(?:抱歉|对不起|哎呀|错误|提示)[\W_]*)*"
)
_SENTENCE_BREAK = re.compile(r"(?<=[.!?…:;])\s+|(?<=[。！？：；])")
_END = r"(?=\s*(?:$|[.!?…:;,|)\]]))"  # the phrase is a whole statement, not the start of a longer one

# Login walls: prompts about reaching the content. Prompts to comment, vote or
# open one's own account ("Sign in to view your account") are not walls.
_SIGN_IN = r"(?:sign ?in|log ?in)"
# What the visitor is let into: content (a wall) or their own account and the
# discussion (not a wall: "Sign in to view your account", "to continue commenting").
_NOT_CONTENT = (
    r"(?:your|my|our|comments?|commenting|repl(?:y|ies|ying)|discussions?|conversations?|vot(?:e|es|ing)"
    r"|likes?|follow(?:ing|ers)?|bookmarks?|saved|notifications?|profile|account|settings|dashboard|inbox"
    r"|messages?|subscriptions?|watchlist|cart|orders?)"
)
_GATE_ACTION = (
    r"(?:to (?:continue|proceed|read|view|access|see|watch|listen|download|unlock|keep reading|finish reading"
    r"|get (?:full )?access)|before (?:continuing|proceeding|reading|viewing|accessing"
    r"|you (?:can )?(?:continue|proceed|read|view)))\b"
    rf"(?!(?: (?:the|all|any|more|and|to|with|in|on|this|these|leaving|posting|writing))* {_NOT_CONTENT}\b)"
)
_JOIN = r"(?:register|sign up|create (?:an? )?(?:free )?account|authenticate|verify your (?:email|account))"
_SIGN_IN_OR_JOIN = (
    rf"(?:please )?(?:{_SIGN_IN}|{_JOIN})(?: or (?:{_SIGN_IN}|{_JOIN}|subscribe))?"
    r"(?: (?:now|first|today|here|again|below|for free))?"
)
_MUST_SIGN_IN = r"you (?:must|need to|have to|will need to|'ll need to) (?:be )?(?:signed|logged|sign|log) ?in"
_ZH_SIGN_IN = r"(?:(?:请|您需要|需要)(?:先)?登录(?:后)?|登录后)(?:才能|即可|方可|可)?(?:查看|阅读|继续|浏览|观看|下载)"
_AUTH_START = re.compile("|".join((
    rf"{_SIGN_IN_OR_JOIN} {_GATE_ACTION}",
    rf"(?:please )?{_SIGN_IN} (?:to|into) (?:your|an?) (?:\w+ )?account {_GATE_ACTION}",
    rf"please {_SIGN_IN}(?: (?:first|again|now|below|here))?{_END}",
    rf"{_MUST_SIGN_IN}(?: {_GATE_ACTION}|{_END})",
    rf"(?:{_SIGN_IN}|authentication|authorization|registration) (?:is )?required(?:{_END}| {_GATE_ACTION})",
    _ZH_SIGN_IN,
)))
# A title part like "Sign in to Example" names a login page (judged like a notice line).
_AUTH_TITLE_START = re.compile(rf"(?:please )?{_SIGN_IN} (?:to|with|into)\b")
# The same prompts inside a sentence: they keep a page from passing, nothing more.
_AUTH_ANY = re.compile("|".join((
    rf"\b{_SIGN_IN_OR_JOIN} {_GATE_ACTION}",
    rf"\b{_SIGN_IN} (?:to|into) (?:your|an?) (?:\w+ )?account {_GATE_ACTION}",
    rf"\bplease {_SIGN_IN}(?: (?:first|again|now|below|here))?{_END}",
    rf"\b{_MUST_SIGN_IN} {_GATE_ACTION}",
    _ZH_SIGN_IN,
)))
_AUTH_TITLES = {
    "sign in", "log in", "login", "signin", "sign in to continue", "log in to continue",
    "login to continue", "sign in or sign up", "sign in or register", "log in or sign up",
    "login or sign up", "authentication required", "login required", "please log in",
    "please sign in", "account login", "member login", "user login", "log in to your account",
    "sign in to your account", "401", "401 unauthorized", "401 authorization required", "error 401",
    "401 error", "unauthorized", "authorization required", "登录", "用户登录", "会员登录", "请登录", "账号登录",
}

# Bot checks and blocks. Server-error status lines are only hints: articles list them too.
_CHALLENGE_START = re.compile("|".join((
    r"checking (?:your browser|if the site connection is secure|the site connection)\b",
    r"(?:please )?(?:verify|confirm|verifying) (?:that )?you(?:'re| are) (?:a )?(?:human|not a (?:robot|bot))\b",
    r"(?:please )?(?:enable|turn on|allow) (?:javascript and cookies|cookies and javascript|javascript|js|cookies)"
    r"(?: in your browser)? to (?:continue|proceed|view|access|see)\b",
    r"you(?:'ve| have) been blocked\b",
    r"your (?:request|access|ip(?: address)?|connection|computer|browser|network) (?:has been|was|is|appears to be"
    r"|may be|might be) (?:blocked|denied|rejected|flagged|making automated requests)\b",
    rf"(?:the |this |your )?request (?:was |has been |is )?(?:blocked|rejected|denied){_END}",
    rf"access (?:to (?:this|the requested) (?:page|site|website|resource|url) )?(?:has been |is |was )?denied{_END}",
    r"(?:please )?complete (?:the|this) (?:security check|captcha|challenge|verification)\b",
    rf"one more step{_END}",
    r"(?:ddos protection|performance (?:&|and) security) by\b",
    r"this (?:website|site) is using a security service\b",
    r"are you a (?:robot|human)(?=\s*(?:$|[?.!]))",
    r"press (?:&|and) hold\b",
    r"(?:we(?:'ve| have) )?detected unusual (?:activity|traffic)\b",
    r"our systems have detected unusual traffic\b",
    rf"(?:just a moment|attention required){_END}",
    r"please wait (?:while|until) .{0,60}?\b(?:check|checking|verif\w*|confirm\w*|secur\w*|browser|human)\b",
    r"(?:we(?:'re| are) )?(?:checking|verifying) (?:your browser|your connection|that you(?:'re| are) (?:a )?human)\b",
    r"访问被拒绝|请(?:完成|进行)(?:安全|人机)?验证|检测到(?:您的)?(?:网络)?(?:异常|可疑)(?:流量|访问|请求|行为)",
)))
_CHALLENGE_ANY = re.compile("|".join((
    r"\b(?:check|checking|verify|verifying) your browser\b", r"checking if the site connection is secure",
    r"verify (?:that )?you(?:'re| are) (?:a )?human", r"verify you(?:'re| are) not a (?:robot|bot)",
    r"unusual traffic from your (?:computer|network)", r"you(?:'ve| have) been blocked",
    r"enable javascript and cookies to continue", r"complete the security check",
    r"review the security of your connection", r"ddos protection by",
    r"performance (?:&|and) security by cloudflare", r"cloudflare ray id",
    r"\b(?:403 forbidden|429 too many requests|500 internal server error|502 bad gateway"
    r"|503 service (?:temporarily )?unavailable|504 gateway time ?out)\b",
    r"请完成安全验证", r"访问被拒绝",
)))
_CHALLENGE_TITLES = {
    "just a moment", "attention required", "access denied", "access to this page has been denied",
    "forbidden", "403", "403 forbidden", "error 403", "403 error", "error 403 forbidden",
    "request blocked", "request rejected", "the request is blocked", "security check",
    "security checkpoint", "one more step", "please wait", "are you a robot", "are you human",
    "human verification", "verify you are human", "verification required", "captcha", "ddos guard",
    "too many requests", "429 too many requests", "error 429", "you have been blocked",
    "pardon our interruption", "robot check", "bot verification", "checking your browser",
    "please verify you are a human", "access blocked", "bad gateway", "502 bad gateway",
    "service unavailable", "503 service unavailable", "500 internal server error",
    "internal server error", "504 gateway timeout", "gateway timeout", "error", "site maintenance",
    "under maintenance", "temporarily unavailable", "安全验证", "访问被拒绝", "验证码", "人机验证", "访问受限",
}

# Missing pages ("soft 404s" answer HTTP 200).
_SUBJECT = r"(?:the |this |that )?(?:page|url|link|content|article|post|story|resource|file|document)"
_SOUGHT = (
    r"(?: you(?:'re| are| were)? (?:looking for|trying to (?:reach|access|view|find|open))"
    r"| (?:that )?you(?:'ve)? requested| requested)?"
)
_MISSING_START = re.compile("|".join((
    r"(?:http |error )?(?:404|410)(?:\s*[.!)\]]*\s*$|\s*[:|.!(\[-]?\s*(?:error|not found|page not found|file not found"
    r"|gone|that's an error|oops|sorry|page|the page|this page|we can't|we couldn't|looks like|nothing)\b)",
    r"(?:page|file|document|content|article|post|story|resource|url|video|product|item)s? not found\b",
    rf"not found{_END}",
    rf"{_SUBJECT}{_SOUGHT} (?:could not|cannot|can't|couldn't|was not|wasn't|is not|isn't|may not|might not"
    r"|can no longer) (?:be )?(?:found|located|reached|displayed)\b",
    rf"{_SUBJECT}{_SOUGHT} (?:does not|doesn't|did not|didn't|no longer) exists?\b",
    rf"{_SUBJECT}{_SOUGHT} (?:is|was|has been|appears to (?:be|have been)|may have been|might have been)"
    r" (?:no longer available|unavailable|not available|removed|deleted|moved|taken down|expired|gone)\b",
    rf"{_SUBJECT} (?:unavailable|not available|no longer available|removed|gone){_END}",
    rf"{_SUBJECT}{_SOUGHT} isn't (?:available|here)\b",
    r"we (?:can't|cannot|couldn't|could not|were unable to|are unable to) (?:find|locate)"
    r" (?:the|that|this|your|any) (?:page|url|content|article|post)\b",
    r"we (?:can't|cannot|couldn't|could not) find what you(?:'re| are) looking for\b",
    r"(?:there's|there is) nothing (?:here|at this (?:address|url|location))\b",
    rf"nothing (?:was )?found{_END}",
    r"looks like (?:this|the|that) page (?:doesn't|does not|can't|cannot|no longer|isn't|is not)\b",
    r"the requested (?:url|page|resource|document)\b.{0,200}?\b(?:was not|could not be|cannot be|can't be) found\b",
    r"页面不存在|页面不见了|页面未找到|页面找不到|找不到(?:该|此|您要的|您要访问的|您访问的)?页面"
    r"|您(?:所)?(?:访问|要找|查找|请求)的页面(?:不存在|已删除|已被删除|找不到)|(?:该|此)?(?:内容|文章|页面)(?:已被?删除|不存在)",
)))
_MISSING_TITLE = re.compile(
    r"(?:error |http )?(?:404|410)(?: (?:error|not found|page not found|file not found|gone))?"
    r"|(?:page |file )?not found|page (?:does not|doesn't) exist|page unavailable"
)
# Notice wording inside a sentence. "Page not found" and "404 error" are left
# out: articles about those errors say them all the time.
_MISSING_ANY = re.compile("|".join((
    r"\bpage you(?:'re| are| were)? looking for\b", r"\bpage you(?:'ve)? requested\b",
    r"\brequested url was not found\b", r"\bthis page (?:doesn't|does not|no longer) exists?\b",
    r"\bthis page (?:is no longer|isn't|is not) available\b",
    r"\bthis content (?:is no longer available|has been removed)\b",
    r"页面不存在", r"页面不见了", r"内容已删除", r"该内容已被删除", r"内容不存在",
)))

# A cut-off article: a call to read the rest elsewhere, opening or closing a block.
_TEASER_START = re.compile("|".join((
    r"(?:点击)?(?:continue|keep) reading(?=\s*(?:$|[.!?…:;,|)\]→»›>])| (?:this|the) (?:article|story|post|piece"
    r"|report)\b| (?:on|at|in) \S| here\b)",
    r"read (?:the )?(?:rest|remainder)\b",
    r"read (?:the )?(?:full|whole|entire|complete) (?:article|story|post|piece|report|text|interview|review|essay"
    r"|column|thread)\b",
    r"read (?:the )?original (?:article|story|post)\b",
    r"(?:view|see|show) (?:the )?(?:full|whole|entire|complete) (?:article|story|post|text|content)\b",
    r"(?:full|whole) (?:article|story)(?=\s*(?:$|[→»›>:…]))",
    r"read more (?:of|from|in) (?:this|the) (?:article|story|post)\b",
    r"(?:(?:this|the) (?:article|story|post) (?:continues|is continued)|story continues) (?:on|at|in)\b",
    r"(?:点击)?(?:继续阅读|阅读全文|展开全文|查看全文|阅读原文|展开阅读|查看完整(?:内容|文章|版)|阅读完整(?:内容|文章|版))",
)))
# A block that is only a call to read on ("Read more →"); checked in the text shown.
_TEASER_LINE = re.compile(
    r"(?:read more|read (?:the )?full (?:story|article|post)|read (?:the )?(?:article|story)|full (?:story|article)"
    r"|阅读更多|查看更多)[\s→»›>….:!]*$"
)
_TEASER_END = re.compile(
    r"(?:(?:continue|keep) reading(?: (?:this|the) (?:article|story|post))?"
    r"|read (?:the )?(?:rest|full (?:article|story|post))(?: of (?:this|the) (?:article|story|post))?"
    r"|继续阅读|阅读全文|展开全文|查看全文)[\s→»›>….:!]*$"
)


def _title_segments(title: str) -> set:
    """A normalized title split at its separators ("Just a moment... | Example")."""
    parts = re.split(r"\s+[|·•—–-]\s+|\s*[|·•]\s*", title)
    return {re.sub(r"^[\W_]+|[\W_]+$", "", part) for part in parts} - {""}


# Wall scans read at most this much visible text (and lines of at most this
# length); a bigger page cannot be checked and does not pass.
MAX_WALL_SCAN_CHARS = 2_000_000
MAX_WALL_LINE_CHARS = 100_000
MAX_WALL_SCAN_LINES = 100_000
_CONSENT_ANY = re.compile("|".join(re.escape(p) for p in (*_CONSENT_PHRASES, *_CONSENT_WEAK_PHRASES)))
_PAYWALL_ANY = re.compile("|".join(re.escape(p) for p in _PAYWALL_PHRASES))
# Access notes citation styles append ("(subscription required)."); not walls.
_ACCESS_NOTE = re.compile(
    r"[(\[](?:free |paid )?(?:registration|subscription|login|log in|sign in|sign up|payment|account)"
    r" (?:is )?required[)\]][.,;]?"
)
_HAS_SENTENCE_BREAK = re.compile(r"[.!?…:;。！？：；]")
_RUN_ON_CHARS = 80  # each line is also read run on from this much of the text before it


def _normalized_lines(title: str, text: str, markup: bool = True):
    """Yield normalized lines, the title first; raise ValueError past the scan budget."""
    if len(text) > 2 * MAX_WALL_SCAN_CHARS:
        raise ValueError("page too large to check")
    visible = visible_text(text, markup)
    if len(visible) > MAX_WALL_SCAN_CHARS or visible.count("\n") > MAX_WALL_SCAN_LINES:
        raise ValueError("page too large to check")
    budget = 2 * MAX_WALL_SCAN_CHARS  # NFKC can expand text
    for raw in (title[:1000], *visible.splitlines()):
        if len(raw) > MAX_WALL_LINE_CHARS:
            raise ValueError("line too long to check")
        line = _normalize(raw)
        budget -= len(line)
        if budget < 0:
            raise ValueError("page too large to check")
        if line:
            yield line
        if _BLANK_FILLERS.search(raw):  # read both ways: drawn as nothing, and as a blank
            spaced = _normalize(_BLANK_FILLERS.sub(" ", raw))
            budget -= len(spaced)
            if budget < 0:
                raise ValueError("page too large to check")
            if spaced and spaced != line:
                yield spaced


def _run_on(recent: list, line: str):
    """A line read run on from the lines before it (recent): (parts, openings,
    closes): up to two short earlier lines, or the last _RUN_ON_CHARS of a
    longer one, then the line's first 2 * _RUN_ON_CHARS (closes: all of it);
    openings, the part lists that start where an earlier line starts."""
    head, closes = line[:2 * _RUN_ON_CHARS], len(line) <= 2 * _RUN_ON_CHARS
    parts, openings = [head], []
    for earlier in reversed(recent):
        whole = len(earlier) <= 2 * _RUN_ON_CHARS
        parts.insert(0, earlier if whole else earlier[-_RUN_ON_CHARS:])
        if whole:
            openings.append(list(parts))
        if not whole or len(earlier) > _RUN_ON_CHARS:
            break  # a longer line ends the run
    return parts, openings, closes


def _joiners(parts: list) -> tuple:
    """Run-on lines join by a space, and beside other scripts also by nothing."""
    return (" ",) if all(part.isascii() for part in parts) else (" ", "")


def _consent_level(strong: set, weak: set) -> bool:
    return bool(strong) or len(strong) + len(weak) >= 3


def consent_signals(title: str, text: str, markup: bool = True) -> list:
    """Cookie/consent wording in the text: "consent", plus "consent_title" for a consent-style title."""
    found = set()
    try:
        recent: list = []  # (wording split over lines reads as one, as in gate_signals)
        for line in _normalized_lines(title, text, markup):
            found.update(match.group(0) for match in _CONSENT_ANY.finditer(line))
            parts, _openings, _closes = _run_on(recent, line)
            if len(parts) > 1:
                for joiner in _joiners(parts):
                    found.update(match.group(0) for match in _CONSENT_ANY.finditer(joiner.join(parts)))
            recent = (recent + [line])[-2:]
    except ValueError:
        return ["scan_budget_exceeded"]
    if not _consent_level(found & set(_CONSENT_PHRASES), found & set(_CONSENT_WEAK_PHRASES)):
        return []
    title_n = _normalize(title[:1000])
    return ["consent", "consent_title"] if any(m in title_n for m in _CONSENT_TITLES) else ["consent"]


def gate_signals(title: str, text: str, cta_text: str | None = None, markup: bool = True,
                 teasers: bool = True) -> list:
    """Walls a page announces, wherever they appear: login, challenge, consent,
    missing page, paywall or teaser.

    Rules run on normalized text, line by line, over lines of any length. A
    login, challenge or missing-page notice is a line (or title part) that opens
    with one; a wall title (auth_title, challenge_title) names the page itself.
    The same wording opening a later sentence, or a prompt inside one, is a hint
    (auth_hint, challenge_hint, missing_hint): it keeps a page from passing and
    nothing more. A block that is only "Read more" counts in cta_text (the text
    shown; default text). The fetcher runs this over every visible block of the
    page, nav and footer included, with markup=False: its text is already what
    the page shows, so nothing in it is taken for a tag. Past the scan budget
    the only signal is scan_budget_exceeded. teasers=False leaves out "Read
    more"-style teasers (for text around an article, whose links are not it).
    """
    title_n = _normalize(title[:1000])
    segments = _title_segments(title_n)
    signals = set()
    consent_found = set()
    flags = dict.fromkeys(("auth", "auth_hint", "challenge", "challenge_hint", "missing", "missing_hint"), False)

    def check_start(head: str, annotation: bool, strong: bool) -> None:
        # A citation's access note ("(registration required).") is not a login
        # notice; any other bracketed wording is read like a line.
        suffix = "" if strong else "_hint"
        if not annotation and _AUTH_START.match(head):
            flags["auth" + suffix] = True
        if not annotation and _CHALLENGE_START.match(head):
            flags["challenge" + suffix] = True
        if _MISSING_START.match(head):
            flags["missing" + suffix] = True

    def check_opening(line: str) -> None:
        """Notices a line opens with (line: text that starts where a line starts)."""
        head = _LEAD.sub("", line, count=1)
        check_start(head, bool(_ACCESS_NOTE.fullmatch(line)), strong=True)
        if teasers and _TEASER_START.match(head):
            signals.add("paywall")

    def check_anywhere(line: str, closes: bool = True) -> None:
        """Wording anywhere in the text (closes: it ends where a line ends)."""
        if _HAS_SENTENCE_BREAK.search(line):
            for part in _SENTENCE_BREAK.split(line)[1:]:
                if part:
                    check_start(_LEAD.sub("", part, count=1), bool(_ACCESS_NOTE.fullmatch(part)), strong=False)
        flags["auth_hint"] |= bool(_AUTH_ANY.search(line))
        flags["challenge_hint"] |= bool(_CHALLENGE_ANY.search(line))
        flags["missing_hint"] |= bool(_MISSING_ANY.search(line))
        consent_found.update(match.group(0) for match in _CONSENT_ANY.finditer(line))
        if _PAYWALL_ANY.search(line) or (teasers and closes and _TEASER_END.search(line)):
            signals.add("paywall")

    try:
        # Each line is also read run on from the lines before it (up to two
        # short ones, or the last _RUN_ON_CHARS of a longer one, into the
        # line's first 2 * _RUN_ON_CHARS; by a space and, beside other
        # scripts, by nothing), from each place one of them starts: a notice
        # split over lines or paragraphs reads as one to a person.
        for line in _normalized_lines(title, "", markup):  # the title, read as a line of its own
            check_opening(line)
            check_anywhere(line)
        recent: list = []
        for line in _normalized_lines("", text, markup):
            check_opening(line)
            check_anywhere(line)
            parts, openings, closes = _run_on(recent, line)
            if len(parts) > 1:
                for joiner in _joiners(parts):
                    check_anywhere(joiner.join(parts), closes)
                    for opening in openings:
                        check_opening(joiner.join(opening))
            recent = (recent + [line])[-2:]
        for line in _normalized_lines("", (text if cta_text is None else cta_text) if teasers else "", markup):
            if _TEASER_LINE.match(_LEAD.sub("", line, count=1)):
                signals.add("paywall")
    except ValueError:
        return ["scan_budget_exceeded"]
    for segment in segments:
        head = _LEAD.sub("", segment, count=1)
        check_start(head, False, strong=True)
        if _AUTH_TITLE_START.match(head):
            flags["auth"] = True
        if _MISSING_TITLE.fullmatch(segment):
            flags["missing"] = True
    if segments & _AUTH_TITLES:
        signals.add("auth_title")
    if segments & _CHALLENGE_TITLES:
        signals.add("challenge_title")
    for kind, strong_name in (("auth", "auth_required"), ("challenge", "challenge_or_error"),
                              ("missing", "missing_page")):
        if flags[kind]:
            signals.add(strong_name)
        elif flags[kind + "_hint"]:
            signals.add(kind + "_hint")
    if _consent_level(consent_found & set(_CONSENT_PHRASES), consent_found & set(_CONSENT_WEAK_PHRASES)):
        signals.add("consent")
        if any(marker in title_n for marker in _CONSENT_TITLES):
            signals.add("consent_title")
    return sorted(signals)


def _ends_with_read_more(text: str) -> bool:
    tail = text.strip().lower()[-300:]
    return tail.endswith(("…", "...", "[…]", "[...]")) or any(m in tail for m in _READ_MORE_TAIL)


def _podcast_full_audio_proof_valid(content: str, extra: dict) -> bool:
    """Verify the short-full audio proof, not just a self-asserted boolean."""
    if (
        extra.get("transcript_coverage") != "full"
        or extra.get("has_transcript") is not True
        or extra.get("verified_complete_bytes") is not True
        or extra.get("coverage_basis") != "complete_encoded_bytes_and_full_decoded_pcm"
        or extra.get("transcription_method") != "local_whisper_tiny_cpu"
        or len(content) < 4
    ):
        return False

    hex_sha = re.compile(r"[0-9a-f]{64}").fullmatch
    if not all(hex_sha(str(extra.get(key) or "")) for key in (
        "media_sha256", "media_url_sha256", "transcript_sha256"
    )):
        return False
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != extra["transcript_sha256"]:
        return False

    try:
        size = extra["audio_bytes"]
        segments = extra["asr_segments"]
        if type(size) is not int or not 8192 <= size <= 2 * 1024 * 1024:
            return False
        if type(segments) is not int or segments < 1:
            return False
        duration = float(extra["media_duration_seconds"])
        decoded = float(extra["decoded_duration_seconds"])
        processed = float(extra["processed_seconds"])
        ratio = float(extra["coverage_ratio"])
        if not all(math.isfinite(v) for v in (duration, decoded, processed, ratio)):
            return False
        if not 0 < duration <= 120 or not audio_duration_matches(duration, decoded):
            return False
        if abs(processed - decoded) > 0.1 or abs(ratio - processed / duration) > 0.001:
            return False
        intervals = extra.get("coverage_intervals")
        if not isinstance(intervals, list) or len(intervals) != 1:
            return False
        span = intervals[0]
        start = float(span["start_seconds"])
        end = float(span["end_seconds"])
        if not math.isfinite(start) or not math.isfinite(end):
            return False
        if abs(start) > 0.01 or abs(end - decoded) > 0.1:
            return False
    except (TypeError, KeyError, ValueError, OverflowError):
        return False
    return True


def _podcast_long_audio_proof_valid(content: str, extra: dict) -> bool:
    """Validate chunk/PCM/ASR manifest consistency before claiming coverage.

    This reconstructs all interval positions and text hashes; the runtime is
    still responsible for real pinned-IP retrieval and media digest checks.
    """
    if (
        extra.get("transcript_coverage") != "full"
        or extra.get("coverage_basis") != "verified_chunk_manifest_and_contiguous_pcm_asr"
        or extra.get("has_transcript") is not True
        or extra.get("verified_complete_bytes") is not True
        or extra.get("transcription_method") != "local_whisper_tiny_cpu"
        or not content.strip()
    ):
        return False
    digest = lambda obj: hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    hex_sha = re.compile(r"[a-f0-9]{64}").fullmatch
    for key in (
        "media_sha256", "media_url_sha256", "transcript_sha256",
        "chunk_manifest_sha256", "segments_manifest_sha256",
    ):
        if not hex_sha(str(extra.get(key) or "")):
            return False
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != extra["transcript_sha256"]:
        return False

    try:
        byte_count = extra["audio_bytes"]
        chunk_count = extra["chunk_count"]
        segment_count = extra["segment_count"]
        if (type(byte_count) is not int or not 8192 <= byte_count <= 64 * 1024 * 1024
                or type(chunk_count) is not int or not 1 <= chunk_count <= 64
                or type(segment_count) is not int or not 1 <= segment_count <= 90):
            return False
        if chunk_count != (byte_count + 1024 * 1024 - 1) // (1024 * 1024):
            return False
        chunks = extra.get("chunks_manifest")
        if not isinstance(chunks, dict) or len(chunks) != chunk_count:
            return False
        if digest(chunks) != extra["chunk_manifest_sha256"]:
            return False
        for index in range(chunk_count):
            item = chunks.get(str(index))
            if not isinstance(item, dict):
                return False
            start = index * 1024 * 1024
            end = min(byte_count, start + 1024 * 1024) - 1
            if (item.get("start") != start or item.get("end") != end
                    or not hex_sha(str(item.get("sha256") or ""))):
                return False

        segments = extra.get("segments_manifest")
        intervals = extra.get("coverage_intervals")
        if (not isinstance(segments, list) or len(segments) != segment_count
                or not isinstance(intervals, list) or len(intervals) != segment_count):
            return False
        if digest(segments) != extra["segments_manifest_sha256"]:
            return False
        parts = content.split("\n")
        if len(parts) != segment_count:
            return False

        expected_frames = 0
        expected_asr_segments = 0
        expected_boundary_adjustments = 0
        max_boundary_overrun = 0.0
        for index, (segment, interval, spoken) in enumerate(zip(segments, intervals, parts)):
            if not isinstance(segment, dict) or not isinstance(interval, dict):
                return False
            frames = segment.get("sample_frames")
            asr_count = segment.get("asr_segment_count")
            if (segment.get("index") != index
                    or type(frames) is not int or not 0 < frames <= 60 * 16000
                    or type(asr_count) is not int or asr_count < 0
                    or segment.get("transcript_chars") != len(spoken)
                    or not hex_sha(str(segment.get("pcm_sha256") or ""))):
                return False
            if hashlib.sha256(spoken.encode("utf-8")).hexdigest() != segment.get("transcript_sha256"):
                return False
            start = float(segment["start_seconds"])
            end = float(segment["end_seconds"])
            if not math.isfinite(start) or not math.isfinite(end):
                return False
            if (abs(start - expected_frames / 16000) > 0.011
                    or abs(end - (expected_frames + frames) / 16000) > 0.011):
                return False
            bounds = (float(interval["start_seconds"]), float(interval["end_seconds"]))
            if not all(math.isfinite(bound) for bound in bounds) or (
                    abs(bounds[0] - start) > 0.01 or abs(bounds[1] - end) > 0.01):
                return False
            boundary_adjustments = segment.get("asr_boundary_adjustments", 0)
            boundary_overrun = float(segment.get("max_boundary_overrun_seconds", 0.0))
            if (
                type(boundary_adjustments) is not int
                or boundary_adjustments not in (0, 1)
                or not math.isfinite(boundary_overrun)
                or not 0.0 <= boundary_overrun <= 2.0
                or (boundary_adjustments == 0 and abs(boundary_overrun) > 0.001)
                or (boundary_adjustments == 1 and boundary_overrun <= 0)
            ):
                return False
            expected_boundary_adjustments += boundary_adjustments
            max_boundary_overrun = max(max_boundary_overrun, boundary_overrun)
            expected_frames += frames
            expected_asr_segments += asr_count

        declared_overrun = float(extra.get("max_boundary_overrun_seconds", 0.0))
        if (
            extra.get("asr_boundary_adjustments", 0) != expected_boundary_adjustments
            or not math.isfinite(declared_overrun)
            or abs(declared_overrun - max_boundary_overrun) > 0.001
        ):
            return False
        if (type(extra.get("asr_segments")) is not int
                or extra["asr_segments"] != expected_asr_segments
                or expected_asr_segments == 0):
            return False
        processed = expected_frames / 16000
        declared = float(extra["media_duration_seconds"])
        decoded = float(extra["decoded_duration_seconds"])
        recorded = float(extra["processed_seconds"])
        ratio = float(extra["coverage_ratio"])
        if not all(math.isfinite(x) for x in (declared, decoded, recorded, ratio)):
            return False
        if (not 0 < declared <= 90 * 60
                or not audio_duration_matches(declared, decoded)
                or abs(processed - decoded) > 0.1
                or abs(recorded - processed) > 0.1
                or abs(ratio - processed / declared) > 0.001):
            return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return True


_VERIFIED_X_METHODS = {"oembed", "fxtwitter", "playwright"}
# Players, frames and plugins in a feed item's HTML.
_FEED_MEDIA = re.compile(r"<(?:audio|video|iframe|embed|object)\b", re.IGNORECASE)


def page_wall_signals(title: str, page_text: str) -> list:
    """Walls anywhere in a page's text as the browser shows it (an overlay, a
    prompt outside the article): login, bot-check, missing-page, consent and
    paywall wording and the WeChat unavailable notices; not teasers, since the
    links around an article are not the article."""
    signals = set(gate_signals(title, page_text, markup=False, teasers=False))
    if any(_wechat_notice(line) for line in page_text.splitlines()):
        signals.add("unavailable_notice")
    return sorted(signals)


def _article_walls(source: str, title: str, content: str, prose: int, around=()):
    """The receipt for login, bot-check, missing-page, consent and paywall
    wording in an article read (as on generic pages), or around it (around:
    the signals page_wall_signals found on the whole page), or None."""
    # the text read both as markup and as the words a browser shows (a DOM read is
    # never markup), also with the lines inside a paragraph joined, by a space or not
    signals = set(gate_signals(title, content)) | set(gate_signals(title, content, markup=False)) | set(around)
    for joiner in (" ", ""):
        signals |= set(gate_signals(title, re.sub(r"(?<!\n)\n(?!\n)", joiner, content), markup=False))
    if "unavailable_notice" in signals:
        return {"status": UNKNOWN, "reason_code": "possible_unavailable_notice",
                "evidence": f"the {source} page shows a removed, blocked or verification notice",
                "components": {"text_body": UNKNOWN}}
    thin = prose < 600
    if "auth_title" in signals or ("auth_required" in signals and thin):
        return {"status": FAIL, "reason_code": "auth_required",
                "evidence": f"{source} returned a login/authentication shell", "components": {"text_body": FAIL}}
    if "challenge_title" in signals or ("challenge_or_error" in signals and thin) or (
            "consent" in signals and (thin or "consent_title" in signals)):
        return {"status": FAIL, "reason_code": "challenge_or_error",
                "evidence": f"{source} returned an error, challenge or consent shell", "components": {"text_body": FAIL}}
    if "missing_page" in signals:
        return {"status": FAIL if thin else UNKNOWN, "reason_code": "source_unavailable" if thin else "possibly_missing_page",
                "evidence": f"{source} text reports that the content is missing or removed",
                "components": {"text_body": FAIL if thin else UNKNOWN}}
    if "scan_budget_exceeded" in signals:
        return {"status": UNKNOWN, "reason_code": "page_too_large_to_check",
                "evidence": f"the {source} text is too large for x-reader to check for walls",
                "components": {"text_body": UNKNOWN}}
    if signals & {"auth_required", "challenge_or_error", "auth_hint", "challenge_hint"}:
        return {"status": UNKNOWN, "reason_code": "possible_access_wall",
                "evidence": f"{source} text shows or mentions a login, bot check or block, so it may not be the article",
                "components": {"text_body": UNKNOWN}}
    if signals & {"missing_hint", "consent"}:
        return {"status": UNKNOWN, "reason_code": "possibly_missing_page" if "missing_hint" in signals
                else "consent_text_present",
                "evidence": f"{source} text mentions a missing page or carries consent wording",
                "components": {"text_body": UNKNOWN}}
    if "paywall" in signals:
        return {"status": PARTIAL, "reason_code": "paywall_or_teaser",
                "evidence": f"some {source} text was read, but it gates or cuts off the rest",
                "components": {"text_body": PARTIAL}}
    return None


def _wechat_article(url: str):
    """Which WeChat article a URL names: ("short", id) for /s/<id>, ("long",
    (__biz, mid, idx, sn)) for /s?...; None for anything else."""
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() != "mp.weixin.qq.com" or (
            port not in (None, 443 if parsed.scheme == "https" else 80)):
        return None
    if parsed.path.startswith("/s/") and parsed.path.strip("/") != "s":
        return ("short", parsed.path[3:].strip("/"))
    if parsed.path in ("/s", "/s/"):
        query = parse_qs(parsed.query)
        key = tuple((query.get(name) or [""])[0] for name in ("__biz", "mid", "idx", "sn"))
        return ("long", key) if key[0] and key[1] else None
    return None


def _classify_article(source: str, title: str, content: str, extra: dict, url: str = "") -> dict[str, Any]:
    """WeChat article: unavailable notices fail; walls count as on generic pages;
    image-carried, container-less reads and reads that could not count the
    article's players and frames do not pass."""
    browser = extra.get("extraction") == "container"
    body = visible_text(content, markup=not browser)  # (a browser read is the words shown, never markup)
    noticed_title = _wechat_notice(title)
    noticed_head = any(_wechat_notice(line) for line in body.splitlines())
    if noticed_title or (noticed_head and prose_chars(body) < 600):
        return {
            "status": FAIL,
            "reason_code": "source_unavailable",
            "evidence": f"{source} returned a removed, blocked or verification notice instead of the article",
            "components": {"text_body": FAIL},
        }
    if noticed_head:
        return {
            "status": UNKNOWN,
            "reason_code": "possible_unavailable_notice",
            "evidence": f"{source} text opens with a removed, blocked or verification notice beside longer text",
            "components": {"text_body": UNKNOWN},
        }
    if extra.get("extraction") == "page_body":
        return {
            "status": UNKNOWN,
            "reason_code": "article_container_missing",
            "evidence": f"the {source} article container was not found; whole-page text cannot prove the body",
            "components": {"text_body": UNKNOWN},
        }
    walls = _article_walls(source, title, content, prose_chars(body), extra.get("page_signals") or ())
    if walls is not None and walls["status"] == FAIL:
        return walls  # a login, challenge or missing-page shell, wherever the browser ended
    final = str(extra.get("final_url") or "")
    if browser and not (final.startswith("https://") and _wechat_article(final) is not None
                        and _wechat_article(final) == _wechat_article(url)):
        return {
            "status": UNKNOWN,
            "reason_code": "article_identity_unverified",
            "evidence": f"the browser did not end on the requested {source} article, so the text cannot be tied to it",
            "components": {"text_body": UNKNOWN},
        }
    if walls is not None:
        return walls
    risks = extra.get("visual_risks")
    if browser and not isinstance(extra.get("page_signals"), list):
        risks = ["page_walls_unchecked"]  # the page around the article was not looked at for walls
    if browser and (not isinstance(risks, list) or risks):
        return {
            "status": UNKNOWN,
            "reason_code": "visibility_unverified",
            "evidence": f"the {source} text read cannot vouch that readers see it as read"
                        + (f" ({', '.join(map(str, risks))})" if isinstance(risks, list) else " (not checked)"),
            "components": {"text_body": UNKNOWN},
        }
    images = content.count("![")
    if prose_chars(body) < MIN_GENERIC_BODY_CHARS:
        if images:
            return {
                "status": PARTIAL,
                "reason_code": "attached_media_unread",
                "evidence": f"{source} article is carried mainly by {images} image(s) that were not analyzed",
                "components": {"text_body": PASS, "images": PARTIAL},
            }
        return {
            "status": UNKNOWN,
            "reason_code": "thin_article_body",
            "evidence": f"{source} returned too little article text to prove the body was read",
            "components": {"text_body": UNKNOWN},
        }
    media = extra.get("embedded_media")
    if not isinstance(media, int) or isinstance(media, bool):
        return {
            "status": PARTIAL,
            "reason_code": "media_presence_unknown",
            "evidence": f"{source} article text retrieved; players or frames in the article could not be counted",
            "components": {"text_body": PASS, "attached_media": UNKNOWN},
        }
    if media > 0:
        return {
            "status": PARTIAL,
            "reason_code": "embedded_media_unread",
            "evidence": f"{source} article text retrieved; {media} player(s) or frame(s) in it were not",
            "components": {"text_body": PASS, "media": PARTIAL},
        }
    note = f"; {images} inline image(s) not analyzed" if images else ""
    return {
        "status": PASS,
        "reason_code": "text_body_read",
        "evidence": f"{source} article text retrieved{note}",
        "components": {"text_body": PASS},
    }


def _classify_generic(payload: dict[str, Any], content: str, extra: dict) -> dict[str, Any]:
    """Generic page: walls fail, teasers are partial, and PASS needs substantial prose.

    Prose length comes from the text shown; wall signals come from that text and,
    when x-reader loaded the page itself, from the whole page (page_signals).
    """
    title = str(payload.get("title") or "")
    page_signals = extra.get("page_signals")
    # A direct read's page signals already cover the text shown (a subset of the
    # page), and its text is what the page shows: nothing in it is markup.
    direct = extra.get("fetch_method") == "direct_html_pinned" and isinstance(page_signals, list)
    prose = prose_chars(visible_text(content, markup=not direct))
    signals = set() if direct else set(gate_signals(title, content))
    if isinstance(page_signals, list):
        signals.update(str(item) for item in page_signals)
    # A wall title names the page itself. A notice line fails a page with little
    # else on it; with substantial prose beside it, it may be a wall over the
    # content or a quoted message, so the page cannot pass or fail on it.
    thin_shell = prose < 600
    if "auth_title" in signals or ("auth_required" in signals and thin_shell):
        return {
            "status": FAIL,
            "reason_code": "auth_required",
            "evidence": "generic reader returned a login/authentication shell",
            "components": {"text_body": FAIL},
        }
    if ("challenge_title" in signals or ("challenge_or_error" in signals and thin_shell)
            or ("consent" in signals and (thin_shell or "consent_title" in signals))):
        return {
            "status": FAIL,
            "reason_code": "challenge_or_error",
            "evidence": "generic reader returned an error, challenge or consent shell",
            "components": {"text_body": FAIL},
        }
    # A page that announces it is missing never passes, however much help text
    # follows (soft 404s answer HTTP 200); thin ones fail outright.
    if "missing_page" in signals:
        if thin_shell:
            return {
                "status": FAIL,
                "reason_code": "source_unavailable",
                "evidence": "the page reports that the content is missing or removed",
                "components": {"text_body": FAIL},
            }
        return {
            "status": UNKNOWN,
            "reason_code": "possibly_missing_page",
            "evidence": "the page announces a missing page, so its text may not be the requested content",
            "components": {"text_body": UNKNOWN},
        }
    if "auth_required" in signals or "challenge_or_error" in signals:
        return {
            "status": UNKNOWN,
            "reason_code": "possible_access_wall",
            "evidence": "the page shows a login, bot-check or block notice beside its text, so it may not show the content",
            "components": {"text_body": UNKNOWN},
        }
    if "scan_budget_exceeded" in signals:
        return {
            "status": UNKNOWN,
            "reason_code": "page_too_large_to_check",
            "evidence": "the page is too large for x-reader to check for walls, so it cannot pass",
            "components": {"text_body": UNKNOWN},
        }
    if "xml_markup" in signals:
        return {
            "status": UNKNOWN,
            "reason_code": "page_structure_unverified",
            "evidence": "the page is served as XHTML, which browsers parse as XML; x-reader reads it as HTML",
            "components": {"text_body": UNKNOWN},
        }
    if "template_markup" in signals:
        return {
            "status": UNKNOWN,
            "reason_code": "page_structure_unverified",
            "evidence": "the page uses <template> markup, which x-reader's parser does not model, so where its text sits cannot be confirmed",
            "components": {"text_body": UNKNOWN},
        }
    if "client_redirect" in signals:
        return {
            "status": UNKNOWN,
            "reason_code": "client_redirect",
            "evidence": "the page sends visitors on to another URL (meta refresh), so its text may not be what they see",
            "components": {"text_body": UNKNOWN},
        }
    if "visibility_unknown" in signals:
        return {
            "status": UNKNOWN,
            "reason_code": "visibility_unverified",
            "evidence": "part of the page text sits under an inline style x-reader cannot resolve, so it may be hidden",
            "components": {"text_body": UNKNOWN},
        }
    # Nothing below may claim any text was read from the page unless x-reader
    # loaded the page itself and the text shown matches it.
    media = extra.get("embedded_media")
    media_count = media if isinstance(media, int) and not isinstance(media, bool) else None
    if (extra.get("redirects_verified") is not True or extra.get("body_verified") is not True
            or media_count is None or not isinstance(page_signals, list)):
        return {
            "status": UNKNOWN,
            "reason_code": "page_unverified",
            "evidence": (
                "page text was read, but x-reader could not load the page itself to check "
                "where it leads, what it embeds, whether it shows a wall and that the text shown is the page's"
            ),
            "components": {"text_body": UNKNOWN},
        }
    # Cookie wording in long text: a consent wall and an article carrying a
    # banner look alike from here, so the page cannot pass.
    if "consent" in signals:
        return {
            "status": UNKNOWN,
            "reason_code": "consent_text_present",
            "evidence": "the page includes cookie/consent wording, so it may be a consent wall rather than the content",
            "components": {"text_body": UNKNOWN},
        }
    # Wall wording that is not a notice of its own (inside a sentence, or an
    # article title about the error) still keeps the page from passing.
    if "missing_hint" in signals:
        return {
            "status": UNKNOWN,
            "reason_code": "possibly_missing_page",
            "evidence": "the page mentions a missing page, so its text may not be the requested content",
            "components": {"text_body": UNKNOWN},
        }
    if "auth_hint" in signals or "challenge_hint" in signals:
        return {
            "status": UNKNOWN,
            "reason_code": "possible_access_wall",
            "evidence": "the page mentions a login, bot check or block, so it may not show the content",
            "components": {"text_body": UNKNOWN},
        }
    if "paywall" in signals:
        return {
            "status": PARTIAL,
            "reason_code": "paywall_or_teaser",
            "evidence": "some page text was read, but the page gates or cuts off the rest",
            "components": {"text_body": PARTIAL},
        }
    page_url = str(payload.get("url") or "")
    if media_count > 0 or _host_in(page_url, _MEDIA_PAGE_HOSTS):
        substantial = prose >= MIN_GENERIC_BODY_CHARS
        return {
            "status": PARTIAL,
            "reason_code": "embedded_media_unread",
            "evidence": ("page text was read, but the page embeds players, frames or plugins whose content was not"
                         if substantial else "the page embeds players, frames or plugins whose content was not read, "
                         "and its text is too thin to prove source coverage"),
            "components": {"text_body": PASS if substantial else UNKNOWN, "media": PARTIAL},
        }
    if prose < MIN_GENERIC_BODY_CHARS:
        return {
            "status": UNKNOWN,
            "reason_code": "thin_generic_body",
            "evidence": "generic reader returned text, but it is too thin to prove source coverage",
            "components": {"text_body": UNKNOWN},
        }
    return {
        "status": PASS,
        "reason_code": "text_body_read",
        "evidence": (
            f"{prose} characters of page prose read; no login, paywall, consent, teaser, "
            "missing-page notice or embedded content detected on the page"
        ),
        "components": {"text_body": PASS},
    }


def classify_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical evidence classification for a CLI JSON payload."""
    source = str(payload.get("source_type") or "")
    raw_content = str(payload.get("content") or "")
    content = raw_content.strip()
    media_type = str(payload.get("media_type") or "")
    extra = payload.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}

    if source == "twitter":
        if not content:
            return {
                "status": FAIL,
                "reason_code": "empty_source",
                "evidence": "X/Twitter source returned no readable post text",
                "components": {"post_text": FAIL},
            }

        method = str(extra.get("fetch_method") or "unknown")
        if method not in _VERIFIED_X_METHODS:
            # Page readers can return a login shell or a profile instead of the post.
            return {
                "status": UNKNOWN,
                "reason_code": "post_text_unverified",
                "evidence": f"X page text came from {method}, so it could not be confirmed as the post itself",
                "components": {"post_text": UNKNOWN},
            }
        if extra.get("post_id_verified") is not True:
            return {
                "status": UNKNOWN,
                "reason_code": "post_identity_unverified",
                "evidence": f"X text came from {method}, but it could not be tied to the requested post ID",
                "components": {"post_text": UNKNOWN},
            }
        components: dict[str, str] = {"post_text": PASS}
        gaps: list[tuple[str, str]] = []
        media_status = str(extra.get("media_status") or "unknown")
        if media_status == "present":
            components["attached_media"] = PARTIAL
            gaps.append(("attached_media_unread", "attached media present but not retrieved"))
        elif media_status != "none":
            components["attached_media"] = UNKNOWN
            gaps.append(("media_presence_unknown", "attached-media presence could not be verified"))
        # With the post's structure checked, each layer is clear only as "none"
        # or "read"; any other state (damaged, unread or missing) is a gap. An
        # unchecked structure is one gap of its own (below).
        checked = extra.get("structure_status") == "verified"
        quote = extra.get("quote_status") if checked else extra.get("quote_status") or "none"
        if quote == "read":
            components["quoted_post"] = PASS
        elif quote != "none":
            components["quoted_post"] = PARTIAL
            gaps.append(("quoted_post_incomplete", "the quoted post or its media was not fully retrieved"))
        poll = extra.get("poll_status") if checked else extra.get("poll_status") or "none"
        if poll == "read":
            components["poll"] = PASS
        elif poll != "none":
            components["poll"] = UNKNOWN
            gaps.append(("poll_unread", "a poll is attached but its options were not retrieved"))
        article = extra.get("article_status") if checked else extra.get("article_status") or "none"
        if article == "present":
            components["article_body"] = PARTIAL
            gaps.append(("article_body_unread", "the post carries an X Article whose body was not retrieved"))
        elif article != "none":
            components["article_body"] = UNKNOWN
            gaps.append(("article_body_unread", "the post's X Article layer could not be checked"))
        if extra.get("thread_marker") is True:
            components["thread"] = PARTIAL
            gaps.append(("thread_continuation_unread", "the post is part of a thread; the other posts were not read"))
        if extra.get("structure_status") != "verified":
            components["post_structure"] = UNKNOWN
            gaps.append((
                "post_structure_unverified",
                "quoted posts, polls and X Articles could not be checked",
            ))
        if not gaps:
            return {
                "status": PASS,
                "reason_code": "source_complete",
                "evidence": (
                    f"post text retrieved via {method}; no media, unread quoted post, "
                    "X Article or thread continuation reported"
                ),
                "components": components,
            }
        return {
            "status": PARTIAL,
            "reason_code": gaps[0][0],
            "evidence": f"post text retrieved via {method}; " + "; ".join(text for _, text in gaps),
            "components": components,
        }

    if source == "youtube" and media_type == "video":
        # Page metadata and transcript are rated on their own evidence: a
        # transcript never vouches for a page that is not the video's.
        page_state = extra.get("page_state")
        video_id = str(extra.get("video_id") or "")
        # Only a single-video URL has a video to tie the page or transcript to.
        metadata = {"ok": PASS, "unavailable": FAIL}.get(page_state, UNKNOWN) if video_id else UNKNOWN
        has_transcript = extra.get("has_transcript") is True and bool(video_id)
        # Only subtitles yt-dlp attributed to this very video can outweigh the page.
        transcript_ours = has_transcript and extra.get("transcript_video_id") == video_id
        if not has_transcript or (metadata != PASS and not transcript_ours):
            if metadata == FAIL:
                return {
                    "status": FAIL,
                    "reason_code": "source_unavailable",
                    "evidence": "YouTube returned an unavailable, private, removed or restricted-video page",
                    "components": {"page_metadata": FAIL, "spoken_media": UNKNOWN, "visual_media": UNKNOWN},
                }
            if metadata == UNKNOWN:
                return {
                    "status": UNKNOWN,
                    "reason_code": "video_unidentified",
                    "evidence": "the YouTube page text could not be tied to the requested video",
                    "components": {"page_metadata": UNKNOWN, "spoken_media": UNKNOWN, "visual_media": UNKNOWN},
                }
            return {
                "status": PARTIAL,
                "reason_code": "spoken_media_unread",
                "evidence": "YouTube page metadata/description was read; spoken and visual media were not",
                "components": {"page_metadata": PASS, "spoken_media": PARTIAL, "visual_media": PARTIAL},
            }
        page_note = "" if metadata == PASS else "; the page itself could not be confirmed as the video's"
        # x-reader reads speech, never pictures: a video is at most PARTIAL.
        if transcript_ours and _subtitle_coverage_proven(extra):
            return {
                "status": PARTIAL,
                "reason_code": "visual_media_unread",
                "evidence": (
                    "subtitle cues for this video cover the whole spoken timeline (no uncaptioned stretch over "
                    f"{int(MAX_TRANSCRIPT_GAP_SECONDS)}s); the video's visual content was not analyzed{page_note}"
                ),
                "components": {"page_metadata": metadata, "spoken_media": PASS, "visual_media": PARTIAL},
            }
        return {
            "status": PARTIAL,
            "reason_code": "transcript_coverage_unproven",
            "evidence": (
                "a transcript was read, but it could not be shown to cover the whole video; "
                f"visual content was not analyzed{page_note}"
            ),
            "components": {"page_metadata": metadata, "spoken_media": PARTIAL, "visual_media": PARTIAL},
        }

    if source == "podcast" and media_type == "audio":
        if not content:
            return {
                "status": FAIL,
                "reason_code": "empty_source",
                "evidence": "Podcast source returned no readable episode metadata",
                "components": {"page_metadata": FAIL, "spoken_media": UNKNOWN},
            }
        if extra.get("transcript_coverage") == "full":
            if (
                _podcast_full_audio_proof_valid(raw_content, extra)
                or _podcast_long_audio_proof_valid(raw_content, extra)
            ):
                seconds = round(float(extra["processed_seconds"]), 2)
                return {
                    "status": PASS,
                    "reason_code": "spoken_media_complete_verified",
                    "evidence": (
                        f"Full public audio source verified byte-for-byte and all {seconds}s "
                        "of decoded audio processed by local ASR; transcript is machine-generated "
                        "and may still contain recognition errors"
                    ),
                    "components": {"page_metadata": PASS, "spoken_media": PASS},
                }
            return {
                "status": PARTIAL,
                "reason_code": "full_media_proof_unverified",
                "evidence": "Full audio claimed but byte, duration, transcript, or coverage proof is incomplete",
                "components": {"page_metadata": PASS, "spoken_media": PARTIAL},
            }
        if extra.get("transcript_coverage") == "preview" and extra.get("preview_transcript_chars", 0):
            seconds = max(0, min(30, int(extra.get("preview_seconds") or 0)))
            return {
                "status": PARTIAL,
                "reason_code": "spoken_media_preview_only",
                "evidence": (
                    f"Podcast page metadata read and first {seconds}s of audio sampled "
                    "with local ASR; the rest of the episode remains unread"
                ),
                "components": {"page_metadata": PASS, "spoken_media": PARTIAL},
            }
        return {
            "status": PARTIAL,
            "reason_code": "spoken_media_unread",
            "evidence": "Podcast show notes were read; spoken episode audio was not transcribed",
            "components": {"page_metadata": PASS, "spoken_media": PARTIAL},
        }

    if source == "bilibili" and media_type == "video":
        return {
            "status": PARTIAL,
            "reason_code": "spoken_media_unread",
            "evidence": "Bilibili page metadata/description was read; spoken and visual media were not",
            "components": {"page_metadata": PASS, "spoken_media": PARTIAL, "visual_media": PARTIAL},
        }

    if source == "rss" and content:
        body = visible_text(content)
        lowered = body.lower()
        media = extra.get("embedded_media") if isinstance(extra.get("embedded_media"), int) else 0
        media = max(media, len(_FEED_MEDIA.findall(content)))
        if extra.get("rss_content") == "full" and media:
            return {
                "status": PARTIAL,
                "reason_code": "embedded_media_unread",
                "evidence": f"the feed carries the article body, but {media} player(s), frame(s) or enclosure(s) in it "
                            "were not read",
                "components": {"text_body": PASS, "media": PARTIAL},
            }
        if (
            extra.get("rss_content") == "full"
            and prose_chars(body) >= MIN_GENERIC_BODY_CHARS
            and not _ends_with_read_more(body)
            and not any(phrase in lowered for phrase in _PAYWALL_PHRASES)
        ):
            return {
                "status": PASS,
                "reason_code": "text_body_read",
                "evidence": "the feed carries the article body (checked against the article page)",
                "components": {"text_body": PASS},
            }
        if extra.get("rss_content") == "unverified":
            return {
                "status": PARTIAL,
                "reason_code": "feed_body_unverified",
                "evidence": "the feed carries a long body, but the article page could not be read to confirm it is complete",
                "components": {"text_body": PARTIAL},
            }
        return {
            "status": PARTIAL,
            "reason_code": "feed_summary_only",
            "evidence": "the feed carried only a summary or teaser; the full article was not retrieved",
            "components": {"text_body": PARTIAL},
        }

    if source in {"xhs", "telegram"} and content:
        media_status = str(extra.get("media_status") or "unknown")
        if media_status == "none":
            return {
                "status": PASS,
                "reason_code": "text_body_read",
                "evidence": f"{source} text retrieved; no attached media reported",
                "components": {"text_body": PASS},
            }
        if media_status == "present":
            return {
                "status": PARTIAL,
                "reason_code": "attached_media_unread",
                "evidence": f"{source} text retrieved; attached media present but not retrieved",
                "components": {"text_body": PASS, "attached_media": PARTIAL},
            }
        return {
            "status": PARTIAL,
            "reason_code": "media_presence_unknown",
            "evidence": f"{source} text retrieved; images or video in the post could not be verified",
            "components": {"text_body": PASS, "attached_media": UNKNOWN},
        }

    if source == "wechat" and content:
        return _classify_article(
            "wechat", str(payload.get("title") or ""), content, extra, str(payload.get("url") or ""),
        )

    if source == "manual" and content:
        return _classify_generic(payload, content, extra)
    if source == "manual" and "scan_budget_exceeded" in (extra.get("page_signals") or []):
        return {
            "status": UNKNOWN,
            "reason_code": "page_too_large_to_check",
            "evidence": "the page has more markup than x-reader parses, so it cannot be checked",
            "components": {"text_body": UNKNOWN},
        }

    if content:
        return {
            "status": UNKNOWN,
            "reason_code": "coverage_unproven",
            "evidence": "non-empty content returned, but full-source coverage is not proven",
            "components": {"source": UNKNOWN},
        }

    return {
        "status": FAIL,
        "reason_code": "empty_source",
        "evidence": "source returned no readable content",
        "components": {"source": FAIL},
    }


def build_receipt(payload: dict[str, Any], requested_url: str | None = None) -> dict[str, Any]:
    """Build a stable, UI-ready Evidence Receipt from UnifiedContent JSON."""
    classification = classify_payload(payload)
    extra = payload.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}

    receipt: dict[str, Any] = {
        "status": classification["status"],
        "reason_code": classification["reason_code"],
        "evidence": classification["evidence"],
        "components": classification.get("components") or {},
        "requested_url": requested_url or payload.get("url") or "",
        "canonical_url": payload.get("url") or requested_url or "",
        "source_type": payload.get("source_type") or "unknown",
        "title": payload.get("title") or "",
        "fetched_at": payload.get("fetched_at") or "",
        "content_chars": len(str(payload.get("content") or "")),
    }

    fetch_method = extra.get("fetch_method")
    if fetch_method:
        receipt["fetch_method"] = fetch_method
    media_status = extra.get("media_status")
    if media_status:
        receipt["media_status"] = media_status
    if "has_transcript" in extra:
        receipt["has_transcript"] = bool(extra.get("has_transcript"))
    if "transcript_coverage" in extra:
        receipt["transcript_coverage"] = str(extra.get("transcript_coverage"))
    if "preview_seconds" in extra:
        receipt["preview_seconds"] = int(extra.get("preview_seconds") or 0)
    if payload.get("source_type") == "podcast" and extra.get("transcript_coverage") == "full":
        # The receipt includes only integrity metadata, never full transcripts,
        # signed CDN URLs, private session state or model cache paths.
        for key in (
            "media_duration_seconds", "decoded_duration_seconds",
            "processed_seconds", "coverage_ratio", "audio_bytes",
            "media_sha256", "media_url_sha256", "transcript_sha256",
            "coverage_intervals", "asr_segments", "verified_complete_bytes",
            "coverage_basis", "chunk_count", "chunk_manifest_sha256",
            "segment_count", "segments_manifest_sha256",
            "reused_source_chunks", "reused_asr_segments",
            "asr_boundary_adjustments", "max_boundary_overrun_seconds",
        ):
            if key in extra:
                receipt[key] = extra[key]
    return receipt


def failure_receipt(
    requested_url: str,
    reason_code: str,
    evidence: str,
    *,
    source_type: str = "unknown",
) -> dict[str, Any]:
    """Build a canonical FAIL receipt when no UnifiedContent payload exists."""
    return {
        "status": FAIL,
        "reason_code": reason_code,
        "evidence": evidence,
        "components": {"source": FAIL},
        "requested_url": requested_url,
        "canonical_url": requested_url,
        "source_type": source_type,
        "title": "",
        "fetched_at": "",
        "content_chars": 0,
    }
