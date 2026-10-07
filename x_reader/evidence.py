"""Canonical evidence receipt projection for x-reader outputs.

This module intentionally contains no network or storage code. It translates an
existing UnifiedContent JSON payload into the public PASS/PARTIAL/FAIL/UNKNOWN
contract used by the Agent Skill and the Evidence Receipt web surface.
"""

from __future__ import annotations

from typing import Any


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


def classify_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical evidence classification for a CLI JSON payload."""
    source = str(payload.get("source_type") or "")
    content = str(payload.get("content") or "").strip()
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
                    f"post text retrieved via {method}; attached media is present "
                    "but was not consumed by the base read"
                ),
                "components": {"post_text": PASS, "attached_media": PARTIAL},
            }
        if media_status == "none":
            return {
                "status": PASS,
                "reason_code": "source_complete",
                "evidence": f"post text retrieved via {method}; no attached media reported",
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
