#!/usr/bin/env python3
# Scenario: Preview platform routing for a list of URLs (no fetch).
# Learning goal: `UniversalReader._detect_platform` drives fetcher choice.
# Prereqs: install package editable; uses a private method for introspection.
# Edge case: scheme-less URLs differ from CLI auto-prefix behavior.
# Run: python examples/cookbook/02_platform_routing_preview.py

from __future__ import annotations

from x_reader.reader import UniversalReader

SAMPLES = [
    "https://x.com/user/status/1",
    "https://www.youtube.com/watch?v=1",
    "https://example.com/feed.xml",
]


def main() -> None:
    r = UniversalReader()
    for u in SAMPLES:
        print(u, "->", r._detect_platform(u))


if __name__ == "__main__":
    main()
