"""Canonical evidence receipt projection for x-reader outputs.

This module intentionally contains no network or storage code. It translates an
existing UnifiedContent JSON payload into the public PASS/PARTIAL/FAIL/UNKNOWN
contract used by the Agent Skill and the Evidence Receipt web surface.
"""

from __future__ import annotations

from typing import Any
import json
import hashlib
import math
import re


PASS = "PASS"
PARTIAL = "PARTIAL"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"


def _generic_shell_state(title: str, content: str) -> str | None:
    title_l = title.strip().lower()
    prefix = content.strip().lower()[:1200]

    auth_titles = {"sign in", "log in", "login", "authentication required"}
    auth_phrases = (
        "sign in to continue",
        "please sign in",
        "please log in",
        "login required",
        "authentication required",
    )
    error_titles = ("access denied", "forbidden", "404", "page not found")
    error_phrases = (
        "checking your browser before accessing",
        "enable javascript and cookies to continue",
        "captcha challenge",
        "request blocked",
    )

    if title_l in auth_titles or any(phrase in prefix for phrase in auth_phrases):
        return "auth_required"
    if any(title_l.startswith(marker) for marker in error_titles) or any(
        phrase in prefix for phrase in error_phrases
    ):
        return "challenge_or_error"
    return None


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
        if not 0 < duration <= 120 or abs(decoded - duration) > max(0.75, duration * 0.01):
            return False
        if abs(processed - decoded) > 0.1 or not 0.999 <= ratio <= 1.001:
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
                or abs(decoded - declared) > max(0.75, declared * 0.01)
                or abs(processed - decoded) > 0.1
                or abs(recorded - processed) > 0.1
                or not 0.999 <= ratio <= 1.001):
            return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return True


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
        media_status = str(extra.get("media_status") or "unknown")
        if media_status == "present":
            return {
                "status": PARTIAL,
                "reason_code": "attached_media_unread",
                "evidence": (
                    f"post text retrieved via {method}; attached media present but not retrieved"
                ),
                "components": {"post_text": PASS, "attached_media": PARTIAL},
            }
        if media_status == "none":
            return {
                "status": PASS,
                "reason_code": "source_complete",
                "evidence": f"post text retrieved via {method}; no media reported",
                "components": {"post_text": PASS},
            }
        return {
            "status": PARTIAL,
            "reason_code": "media_presence_unknown",
            "evidence": (
                f"post text retrieved via {method}; attached-media presence could not "
                "be verified"
            ),
            "components": {"post_text": PASS, "attached_media": UNKNOWN},
        }

    if source == "youtube" and media_type == "video":
        has_transcript = bool(extra.get("has_transcript"))
        if has_transcript:
            return {
                "status": PASS,
                "reason_code": "spoken_media_read",
                "evidence": "YouTube source returned spoken-content transcript evidence",
                "components": {"page_metadata": PASS, "spoken_media": PASS},
            }
        return {
            "status": PARTIAL,
            "reason_code": "spoken_media_unread",
            "evidence": "YouTube page metadata/description was read, but spoken media was not proven",
            "components": {"page_metadata": PASS, "spoken_media": PARTIAL},
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
            "evidence": "Bilibili page metadata/description was read, but spoken media was not proven",
            "components": {"page_metadata": PASS, "spoken_media": PARTIAL},
        }

    if source in {"wechat", "xhs", "telegram", "rss"} and content:
        return {
            "status": PASS,
            "reason_code": "text_body_read",
            "evidence": f"{source} text body retrieved",
            "components": {"text_body": PASS},
        }

    if source == "manual":
        shell_state = _generic_shell_state(str(payload.get("title") or ""), content)
        if shell_state == "auth_required":
            return {
                "status": FAIL,
                "reason_code": "auth_required",
                "evidence": "generic reader returned a login/authentication shell",
                "components": {"text_body": FAIL},
            }
        if shell_state == "challenge_or_error":
            return {
                "status": FAIL,
                "reason_code": "challenge_or_error",
                "evidence": "generic reader returned an error/challenge shell",
                "components": {"text_body": FAIL},
            }
        if len(content) >= 40:
            return {
                "status": PASS,
                "reason_code": "text_body_read",
                "evidence": "substantial web body retrieved via generic reader",
                "components": {"text_body": PASS},
            }
        if content:
            return {
                "status": UNKNOWN,
                "reason_code": "thin_generic_body",
                "evidence": "generic reader returned text, but it is too thin to prove source coverage",
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
