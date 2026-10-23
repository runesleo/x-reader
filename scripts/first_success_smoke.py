#!/usr/bin/env python3
"""Live first-success smoke for x-reader.

Runs the local CLI against one or more public URLs and emits coverage receipts.
The public PASS/PARTIAL/FAIL/UNKNOWN decision is delegated to x_reader.evidence;
the historical `coverage` field is retained temporarily for compatibility.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from x_reader.evidence import FAIL, PASS, PARTIAL, UNKNOWN, classify_payload


ROOT = Path(__file__).resolve().parents[1]


def _legacy_components(components: dict[str, str]) -> dict[str, str] | None:
    out: dict[str, str] = {}
    for key, value in components.items():
        if value == PASS:
            out[key] = "READ"
        elif value == FAIL:
            out[key] = "FAILED"
        elif value == PARTIAL:
            out[key] = "PARTIAL"
        # Historical smoke receipts did not expose UNKNOWN subcomponents.
    return out or None


def classify(payload: dict[str, Any]) -> tuple[str, str, dict[str, str] | None]:
    """Compatibility projection for existing smoke tests/receipts."""
    result = classify_payload(payload)
    if result["reason_code"] == "auth_required":
        coverage = "AUTH_REQUIRED"
    else:
        coverage = {
            PASS: "READ",
            PARTIAL: "PARTIAL",
            FAIL: "FAILED",
            UNKNOWN: "PARTIAL",
        }[result["status"]]
    return coverage, result["evidence"], _legacy_components(result.get("components") or {})


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
        auth_required = looks_auth_required(combined)
        return {
            "url": url,
            "status": FAIL,
            "coverage": "AUTH_REQUIRED" if auth_required else "FAILED",
            "reason_code": "auth_required" if auth_required else "source_fetch_failed",
            "evidence": (proc.stderr or proc.stdout).strip()[-800:],
        }

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "url": url,
            "status": FAIL,
            "coverage": "FAILED",
            "reason_code": "invalid_cli_output",
            "evidence": f"CLI stdout was not JSON: {proc.stdout[-500:]}",
        }

    canonical = classify_payload(payload)
    coverage, evidence, components = classify(payload)
    receipt = {
        "url": url,
        "status": canonical["status"],
        "coverage": coverage,
        "reason_code": canonical["reason_code"],
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
