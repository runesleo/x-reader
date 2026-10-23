#!/usr/bin/env python3
"""Validate a structured reading-coverage receipt (offline, no network).

Checks record completeness for inventoried sources (text, media, comments, quotes,
external links). It does not prove a model understood the material or that
evidence references are honest — those still need independent inspection.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

KINDS = frozenset({"text", "image", "video", "audio", "comments", "quotes", "external"})
REQUIRED_KINDS = frozenset({"text", "comments", "quotes"})
MAX_INPUT_BYTES = 2 * 1024 * 1024


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_receipt(value: Any, target_id: str) -> dict[str, Any]:
    """Check target id, inventoried sources, and per-item observations."""
    blockers: list[dict[str, str]] = []

    def block(code: str, item: str = "") -> None:
        blockers.append({"code": code, "item": item})

    data = value if isinstance(value, dict) else {}
    if not isinstance(value, dict):
        block("INVALID_RECEIPT")
    if not isinstance(target_id, str) or not re.fullmatch(r"[0-9A-Za-z:_-]{1,128}", target_id):
        block("INVALID_EXPECTED_TARGET")
    if data.get("target_id") != target_id and data.get("post_id") != target_id:
        block("TARGET_MISMATCH")

    inventory = data.get("inventory")
    inventory = inventory if isinstance(inventory, dict) else {}
    if inventory.get("complete") is not True:
        block("INVENTORY_INCOMPLETE")
    if not _text(inventory.get("evidence_ref")):
        block("MISSING_INVENTORY_EVIDENCE")
    items = inventory.get("items")
    if not isinstance(items, list) or not items:
        block("INVALID_INVENTORY")
        items = []

    expected: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or not _text(item.get("id")):
            block("INVALID_INVENTORY_ITEM")
            continue
        key, kind = item["id"], item.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            block("INVALID_ITEM_KIND", key)
            continue
        if key in expected:
            block("DUPLICATE_ITEM", key)
        expected[key] = kind
    for kind in sorted(REQUIRED_KINDS - set(expected.values())):
        block("REQUIRED_CATEGORY_MISSING", kind)

    observations = data.get("observations")
    if not isinstance(observations, list):
        block("INVALID_OBSERVATIONS")
        observations = []
    seen: dict[str, dict[str, Any]] = {}
    for item in observations:
        if not isinstance(item, dict) or not _text(item.get("id")):
            block("INVALID_OBSERVATION")
            continue
        key = item["id"]
        if key in seen:
            block("DUPLICATE_OBSERVATION", key)
        if key not in expected:
            block("UNINVENTORIED_OBSERVATION", key)
        seen[key] = item

    for key in expected:
        item = seen.get(key)
        if item is None:
            block("MISSING_OBSERVATION", key)
            continue
        if item.get("status") != "read":
            block("ITEM_NOT_READ", key)
        if not _text(item.get("evidence_ref")):
            block("MISSING_READ_EVIDENCE", key)
        if item.get("pagination_exhausted") is not True:
            block("PAGINATION_NOT_EXHAUSTED", key)
        if not isinstance(item.get("pending_cursors"), list) or item["pending_cursors"]:
            block("PENDING_CURSORS", key)
        if not isinstance(item.get("pending_branches"), list) or item["pending_branches"]:
            block("PENDING_BRANCHES", key)

    complete = not blockers
    return {
        "target_id": target_id,
        "reading_complete": complete,
        "coverage_complete": complete,
        "blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-id",
        required=True,
        help="Exact target (post id, url slug, or stable receipt id)",
    )
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("receipt exceeds input limit")
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        output = {
            "target_id": args.target_id,
            "reading_complete": False,
            "coverage_complete": False,
            "blockers": [{"code": "INVALID_JSON", "item": type(exc).__name__}],
        }
        print(json.dumps(output, ensure_ascii=False))
        return 2
    result = validate_receipt(data, args.target_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["coverage_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
