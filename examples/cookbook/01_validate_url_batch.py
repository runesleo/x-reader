#!/usr/bin/env python3
# Scenario: Batch-validate user-supplied URLs before any network I/O (SSRF guard).
# Learning goal: compose `validate_url` in a loop; fail-fast vs collect-errors.
# Prereqs: `pip install -e .` from repo root; no fetch occurs here.
# Edge case: first blocked URL raises — wrap per-URL try for partial reports.
# Run: python examples/cookbook/01_validate_url_batch.py

from __future__ import annotations

from x_reader.utils.url_validator import validate_url

URLS = [
    "https://example.com/article",
    "https://youtube.com/watch?v=dQw4w9WgXcQ",
]


def main() -> None:
    clean = []
    for u in URLS:
        clean.append(validate_url(u))
    print("ok", len(clean))


if __name__ == "__main__":
    main()
