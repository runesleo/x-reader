#!/usr/bin/env python3
# Scenario: Convert a minimal RSS article dict to `UnifiedContent`.
# Learning goal: `from_rss` maps `link`/`url` fields defensively.
# Prereqs: schema only.
# Edge case: missing summary becomes empty string.
# Run: python examples/cookbook/08_from_rss_minimal.py

from __future__ import annotations

from x_reader.schema import from_rss


def main() -> None:
    article = {
        "source": "Example Feed",
        "title": "Hello",
        "summary": "World",
        "link": "https://example.com/a",
    }
    u = from_rss(article)
    print(u.url, u.title)


if __name__ == "__main__":
    main()
