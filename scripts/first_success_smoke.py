#!/usr/bin/env python3
"""Live first-success smoke for x-reader.

Runs the local CLI against one or more public URLs and emits coverage receipts.
This is intentionally not a CI test: public sites change and may require auth.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


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
        return "AUTH_REQUIRED"
    if any(title_l.startswith(marker) for marker in error_titles) or any(
        phrase in prefix for phrase in error_phrases
    ):
        return "FAILED"
    return None


def classify(payload: dict[str, Any]) -> tuple[str, str, dict[str, str] | None]:
    source = payload.get("source_type", "")
    content = (payload.get("content") or "").strip()
    media_type = payload.get("media_type", "")
    extra = payload.get("extra") or {}

    if source in {"youtube", "bilibili"} and media_type == "video":
        return (
            "PARTIAL",
            "base CLI returned page metadata/description; spoken media not proven",
            {"page_metadata": "READ", "spoken_media": "PARTIAL"},
        )

    if source == "twitter" and content:
        method = extra.get("fetch_method") or "unknown"
        media_status = extra.get("media_status") or "unknown"
        if media_status == "present":
            return (
                "PARTIAL",
                f"post text retrieved via {method}; attached media present but not retrieved",
                {"post_text": "READ", "attached_media": "PARTIAL"},
            )
        if media_status == "none":
            return "READ", f"post text retrieved via {method}; no media reported", {
                "post_text": "READ"
            }
        return (
            "PARTIAL",
            f"post text retrieved via {method}; media presence could not be verified",
            {"post_text": "READ"},
        )

    if source in {"wechat", "xhs", "telegram", "rss"} and content:
        return "READ", f"{source} text body retrieved", None

    if source == "manual":
        shell_state = _generic_shell_state(payload.get("title") or "", content)
        if shell_state == "AUTH_REQUIRED":
            return "AUTH_REQUIRED", "generic reader returned a login/authentication shell", None
        if shell_state == "FAILED":
            return "FAILED", "generic reader returned an error/challenge shell", None
        if len(content) >= 40:
            return "READ", "substantial web body retrieved via generic reader", None

    if content:
        return (
            "PARTIAL",
            "non-empty content returned, but full-source coverage is not proven",
            None,
        )

    return "FAILED", "empty content", None


_AUTH_MARKERS = (
    "x-reader login",
    "login required",
    "please log in",
    "requires login",
    "login wall",
)


def looks_auth_required(message: str) -> bool:
    normalized = message.lower()
    return any(marker in normalized for marker in _AUTH_MARKERS)


def run_one(url: str) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "x_reader.cli", url, "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if proc.returncode != 0:
        combined = f"{proc.stdout}\n{proc.stderr}"
        state = "AUTH_REQUIRED" if looks_auth_required(combined) else "FAILED"
        return {
            "url": url,
            "coverage": state,
            "evidence": (proc.stderr or proc.stdout).strip()[-800:],
        }

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "url": url,
            "coverage": "FAILED",
            "evidence": f"CLI stdout was not JSON: {proc.stdout[-500:]}",
        }

    coverage, evidence, components = classify(payload)
    receipt = {
        "url": url,
        "coverage": coverage,
        "source_type": payload.get("source_type"),
        "title": payload.get("title"),
        "evidence": evidence,
    }
    if components:
        receipt["components"] = components
    media_status = (payload.get("extra") or {}).get("media_status")
    if media_status:
        receipt["media_status"] = media_status
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", nargs="+", help="public URLs to probe")
    args = parser.parse_args()

    receipts = [run_one(url) for url in args.urls]
    print(json.dumps(receipts, ensure_ascii=False, indent=2))
    return 0 if all(r["coverage"] in {"READ", "PARTIAL", "AUTH_REQUIRED"} for r in receipts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
