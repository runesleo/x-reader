#!/usr/bin/env python3
# Scenario: Zip URLs to per-URL outcomes after a batch gather (like `read_batch`).
# Learning goal: keep alignment between request list and mixed results.
# Prereqs: stdlib only; mirrors `zip(urls, results)` in `reader.py`.
# Edge case: shorter results list indicates a bug — assert equal lengths.
# Run: python examples/cookbook/10_zip_url_outcomes.py

from __future__ import annotations

from typing import Any

URLS = ["https://example.com/a", "https://example.com/b"]
RESULTS: list[Any] = ["ok", ValueError("x")]


def main() -> None:
    for url, res in zip(URLS, RESULTS):
        if isinstance(res, Exception):
            print(url, "ERR", type(res).__name__)
        else:
            print(url, res)


if __name__ == "__main__":
    main()
