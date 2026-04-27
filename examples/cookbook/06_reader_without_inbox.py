#!/usr/bin/env python3
# Scenario: Construct `UniversalReader` without disk inbox (read-only session).
# Learning goal: inbox is optional; skips persistence side effects in `read`.
# Prereqs: package installed; still performs network if you call `read`.
# Edge case: passing `inbox=None` disables `add/save` path inside `read`.
# Run: python examples/cookbook/06_reader_without_inbox.py  (no network here)

from __future__ import annotations

from x_reader.reader import UniversalReader


def main() -> None:
    r = UniversalReader(inbox=None)
    assert r.inbox is None
    print("reader ready without inbox")


if __name__ == "__main__":
    main()
