"""Canonical evidence receipt projection for x-reader outputs.

This module intentionally contains no network or storage code. It translates an
existing UnifiedContent JSON payload into the public PASS/PARTIAL/FAIL/UNKNOWN
contract used by the Agent Skill and the Evidence Receipt web surface.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse
import html
import json
import hashlib
import math
import re


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


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_BARE_URL = re.compile(r"https?://\S+")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def visible_text(content: str) -> str:
    """Text a reader would see: no markup, image links, link targets or bare URLs."""
    text = _MD_IMAGE.sub(" ", content)
    text = _MD_LINK.sub(r"\1", text)
    text = html.unescape(_HTML_TAG.sub(" ", text))
    text = _BARE_URL.sub(" ", text)
    return "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()


_SENTENCE_END = re.compile(r"[.!?;:](?:[\"')\]]*)(?:\s|$)|[。！？；]")


def prose_chars(text: str) -> int:
    """Characters in sentence-like lines.

    A line counts when it has six or more words, or 20+ CJK characters, and
    contains sentence punctuation; menus, headline lists, buttons, captions and
    character runs do not count.
    """
    total = 0
    for line in text.splitlines():
        long_enough = (len(line) >= 40 and len(line.split()) >= 6) or len(_CJK.findall(line)) >= 20
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
_UNAVAILABLE_PHRASES = (
    "page not found", "404 not found", "this page doesn't exist", "this page does not exist",
    "this page isn't available", "this page is no longer available",
    "content is no longer available", "this content has been removed",
    "页面不存在", "页面不见了", "内容已删除", "该内容已被删除", "内容不存在",
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
_TEASER_TAIL = (
    "continue reading", "keep reading", "read the full story", "read the full article",
    "read the rest of", "继续阅读", "阅读全文", "展开全文", "查看全文", "阅读原文",
)
_TEASER_LINE = re.compile(
    r"^(?:点击)?(?:continue reading|keep reading|read the full (?:story|article|post)|read the rest"
    r"|继续阅读(?:全文)?|阅读全文|展开全文|查看全文|阅读原文)[\s→»›>…\.:!！。-]*$"
)
# Pages whose substance is audio/video x-reader does not read on the generic path.
_MEDIA_PAGE_HOSTS = (
    "vimeo.com", "tiktok.com", "instagram.com", "soundcloud.com", "open.spotify.com",
    "twitch.tv", "dailymotion.com", "rumble.com", "loom.com", "music.apple.com",
    "douyin.com", "kuaishou.com", "ixigua.com", "v.qq.com", "youku.com",
)
_WECHAT_UNAVAILABLE = (
    "此内容因违规无法查看", "该内容已被发布者删除", "此内容被投诉且经审核涉嫌侵权",
    "此内容因涉嫌违反相关法律法规", "该公众号已迁移", "此帐号已被屏蔽", "此帐号已自主注销",
    "内容无法查看", "该内容暂时无法查看", "当前环境异常", "完成验证后即可继续访问",
    "参数错误", "链接已过期",
)
_READ_MORE_TAIL = (
    "read more", "continue reading", "read the full", "read the rest",
    "阅读全文", "阅读原文", "查看全文", "点击阅读",
)


def _host_in(url: str, domains: tuple) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


_APOLOGY = re.compile(r"^(?:oops|sorry|uh[- ]oh|抱歉|对不起|哎呀)[\s!,.:;—–-]*")
_NOTICES = set(_UNAVAILABLE_PHRASES) | {
    "404", "not found", "error 404", "404 error", "404 not found", "404 page not found",
    "error 404 page not found", "page not found 404", "error page not found",
}


_SEGMENT_SPLIT = re.compile(r"\s[|·—–-]\s|[|·]|:\s|[.!?。！？]\s")
_STATUS_CODE = re.compile(r"[\(\[]\s*(?:error\s*)?\d{3}\s*[\)\]]")
_AUTH_TITLES = {"sign in", "log in", "login", "authentication required"}
_AUTH_PHRASES = (
    "sign in to continue", "please sign in", "please log in", "login required",
    "authentication required",
)
_ERROR_TITLES = {"access denied", "forbidden", "403", "403 forbidden", "error 403", "just a moment...",
                 "attention required!", "attention required! | cloudflare"}
_CHALLENGE_PHRASES = (
    "checking your browser before accessing", "enable javascript and cookies to continue",
    "captcha challenge", "request blocked",
)


def _is_notice(text: str) -> bool:
    """True when a short heading, line or title segment is a missing-page notice.

    Segments around ": ", " | ", " - " are tried one by one, so "Page not found:
    /old-slug" and "Error: This page does not exist" match while "How to fix Page
    Not Found errors" does not.
    """
    for segment in _SEGMENT_SPLIT.split(_STATUS_CODE.sub(" ", text)):
        segment = _APOLOGY.sub("", segment.strip().lower())
        segment = re.sub(r"[\s|·:—–-]+", " ", segment).strip(" !.。！?？")
        if segment in _NOTICES:
            return True
    return False


def gate_signals(title: str, text: str) -> list:
    """Walls a page announces, wherever they appear: login, challenge, consent,
    missing page, paywall or teaser. Every line is checked (no first-N cut-off);
    the fetcher runs this over every visible block of the page, nav and footer
    included, so a notice outside the extracted article still counts."""
    body = visible_text(text)
    lowered = body.lower()
    title_l = title.strip().lower()
    signals = set()
    title_segments = {segment.strip() for segment in _SEGMENT_SPLIT.split(title_l)}
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    # Notices are short lines that open with the phrase ("Please sign in to
    # continue."), not articles about them ("Troubleshooting authentication ...").
    notices = [_APOLOGY.sub("", line.lower()) for line in lines if len(line) <= 120]
    if title_l in _AUTH_TITLES or any(
        line.startswith(_AUTH_PHRASES) for line in (*notices, title_l)
    ):
        signals.add("auth_required")
    if (title_segments & _ERROR_TITLES or title_l in _ERROR_TITLES
            or any(line.startswith(_CHALLENGE_PHRASES) for line in notices)):
        signals.add("challenge_or_error")
    strong = sum(phrase in lowered for phrase in _CONSENT_PHRASES)
    weak = sum(phrase in lowered for phrase in _CONSENT_WEAK_PHRASES)
    if strong or strong + weak >= 3:
        signals.add("consent")
        if any(marker in title_l for marker in _CONSENT_TITLES):
            signals.add("consent_title")
    if _is_notice(title) or any(len(line) <= 80 and _is_notice(line) for line in lines):
        signals.add("missing_page")
    if any(phrase in lowered or phrase in title_l for phrase in _PAYWALL_PHRASES):
        signals.add("paywall")
    # A line that is only a teaser link or button ("Continue reading →"), not a
    # link that happens to start with the words ("Continue reading our research").
    if any(_TEASER_LINE.match(line.lower()) for line in lines):
        signals.add("paywall")
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
            if (abs(float(interval["start_seconds"]) - start) > 0.01
                    or abs(float(interval["end_seconds"]) - end) > 0.01):
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

        if (
            extra.get("asr_boundary_adjustments", 0) != expected_boundary_adjustments
            or abs(float(extra.get("max_boundary_overrun_seconds", 0.0))
                   - max_boundary_overrun) > 0.001
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


def _classify_article(source: str, title: str, content: str, extra: dict) -> dict[str, Any]:
    """WeChat article: unavailable notices fail; image-carried or container-less reads do not pass."""
    body = visible_text(content)
    head = f"{title}\n{body[:600]}"
    if prose_chars(body) < 600 and any(phrase in head for phrase in _WECHAT_UNAVAILABLE):
        return {
            "status": FAIL,
            "reason_code": "source_unavailable",
            "evidence": f"{source} returned a removed, blocked or verification notice instead of the article",
            "components": {"text_body": FAIL},
        }
    if extra.get("extraction") == "page_body":
        return {
            "status": UNKNOWN,
            "reason_code": "article_container_missing",
            "evidence": f"the {source} article container was not found; whole-page text cannot prove the body",
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
    prose = prose_chars(visible_text(content))
    page_signals = extra.get("page_signals")
    signals = set(gate_signals(title, content))
    if isinstance(page_signals, list):
        signals.update(str(item) for item in page_signals)
    if "auth_required" in signals:
        return {
            "status": FAIL,
            "reason_code": "auth_required",
            "evidence": "generic reader returned a login/authentication shell",
            "components": {"text_body": FAIL},
        }
    if "challenge_or_error" in signals or (
        "consent" in signals and (prose < 600 or "consent_title" in signals)
    ):
        return {
            "status": FAIL,
            "reason_code": "challenge_or_error",
            "evidence": "generic reader returned an error, challenge or consent shell",
            "components": {"text_body": FAIL},
        }
    # A page that announces it is missing never passes, however much help text
    # follows (soft 404s answer HTTP 200); thin ones fail outright.
    if "missing_page" in signals:
        if prose < 600:
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
    # Cookie wording in long text: a consent wall and an article carrying a
    # banner look alike from here, so the page cannot pass.
    if "consent" in signals:
        return {
            "status": UNKNOWN,
            "reason_code": "consent_text_present",
            "evidence": "the page includes cookie/consent wording, so it may be a consent wall rather than the content",
            "components": {"text_body": UNKNOWN},
        }
    if "paywall" in signals:
        return {
            "status": PARTIAL,
            "reason_code": "paywall_or_teaser",
            "evidence": "some page text was read, but the page gates or cuts off the rest",
            "components": {"text_body": PARTIAL},
        }
    media = extra.get("embedded_media")
    media_count = media if isinstance(media, int) and not isinstance(media, bool) else None
    page_url = str(payload.get("url") or "")
    if (media_count or 0) > 0 or _host_in(page_url, _MEDIA_PAGE_HOSTS):
        return {
            "status": PARTIAL,
            "reason_code": "embedded_media_unread",
            "evidence": "page text was read, but the page embeds players, frames or plugins whose content was not",
            "components": {"text_body": PASS, "media": PARTIAL},
        }
    if prose < MIN_GENERIC_BODY_CHARS:
        return {
            "status": UNKNOWN,
            "reason_code": "thin_generic_body",
            "evidence": "generic reader returned text, but it is too thin to prove source coverage",
            "components": {"text_body": UNKNOWN},
        }
    if (extra.get("redirects_verified") is not True or extra.get("body_verified") is not True
            or media_count != 0 or not isinstance(page_signals, list)):
        return {
            "status": UNKNOWN,
            "reason_code": "page_unverified",
            "evidence": (
                "page text was read, but x-reader could not load the page itself to check "
                "where it leads, what it embeds and whether it shows a wall"
            ),
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
        components: dict[str, str] = {"post_text": PASS}
        gaps: list[tuple[str, str]] = []
        media_status = str(extra.get("media_status") or "unknown")
        if media_status == "present":
            components["attached_media"] = PARTIAL
            gaps.append(("attached_media_unread", "attached media present but not retrieved"))
        elif media_status != "none":
            components["attached_media"] = UNKNOWN
            gaps.append(("media_presence_unknown", "attached-media presence could not be verified"))
        quote = extra.get("quote_status")
        if quote == "read":
            components["quoted_post"] = PASS
        elif quote in ("media_unread", "incomplete", "unknown"):
            components["quoted_post"] = PARTIAL
            gaps.append(("quoted_post_incomplete", "the quoted post or its media was not fully retrieved"))
        if extra.get("poll_status") == "read":
            components["poll"] = PASS
        elif extra.get("poll_status") == "unknown":
            components["poll"] = UNKNOWN
            gaps.append(("poll_unread", "a poll is attached but its options were not retrieved"))
        if extra.get("article_status") == "present":
            components["article_body"] = PARTIAL
            gaps.append(("article_body_unread", "the post carries an X Article whose body was not retrieved"))
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
        page_state = extra.get("page_state")
        if page_state == "unavailable" and not extra.get("has_transcript"):
            return {
                "status": FAIL,
                "reason_code": "source_unavailable",
                "evidence": "YouTube returned an unavailable, private, removed or restricted-video page",
                "components": {"page_metadata": FAIL, "spoken_media": UNKNOWN, "visual_media": UNKNOWN},
            }
        if page_state != "ok" and not extra.get("has_transcript"):
            return {
                "status": UNKNOWN,
                "reason_code": "video_unidentified",
                "evidence": "the YouTube page text could not be tied to the requested video",
                "components": {"page_metadata": UNKNOWN, "spoken_media": UNKNOWN, "visual_media": UNKNOWN},
            }
        # x-reader reads speech, never pictures: a video is at most PARTIAL.
        if extra.get("has_transcript") and _subtitle_coverage_proven(extra):
            return {
                "status": PARTIAL,
                "reason_code": "visual_media_unread",
                "evidence": (
                    "subtitle cues cover the whole spoken timeline (no uncaptioned stretch over "
                    f"{int(MAX_TRANSCRIPT_GAP_SECONDS)}s); the video's visual content was not analyzed"
                ),
                "components": {"page_metadata": PASS, "spoken_media": PASS, "visual_media": PARTIAL},
            }
        if extra.get("has_transcript"):
            return {
                "status": PARTIAL,
                "reason_code": "transcript_coverage_unproven",
                "evidence": (
                    "a transcript was read, but it could not be shown to cover the whole video; "
                    "visual content was not analyzed"
                ),
                "components": {"page_metadata": PASS, "spoken_media": PARTIAL, "visual_media": PARTIAL},
            }
        return {
            "status": PARTIAL,
            "reason_code": "spoken_media_unread",
            "evidence": "YouTube page metadata/description was read; spoken and visual media were not",
            "components": {"page_metadata": PASS, "spoken_media": PARTIAL, "visual_media": PARTIAL},
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
            "wechat", str(payload.get("title") or ""), content, extra,
        )

    if source == "manual" and content:
        return _classify_generic(payload, content, extra)

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
