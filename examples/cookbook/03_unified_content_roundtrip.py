#!/usr/bin/env python3
# Scenario: Serialize `UnifiedContent` to dict and back (tool composition).
# Learning goal: stable schema for passing between CLI, MCP, and storage.
# Prereqs: `x_reader.schema` imports only.
# Edge case: unknown keys in dict are stripped by `from_dict`.
# Run: python examples/cookbook/03_unified_content_roundtrip.py

from __future__ import annotations

import json

from x_reader.schema import MediaType, Priority, SourceType, UnifiedContent


def main() -> None:
    u = UnifiedContent(
        source_type=SourceType.MANUAL,
        source_name="demo",
        title="t",
        content="body",
        url="https://example.com",
        media_type=MediaType.TEXT,
        priority=Priority.NORMAL,
    )
    raw = json.dumps(u.to_dict(), ensure_ascii=False)
    back = UnifiedContent.from_dict(json.loads(raw))
    assert back.title == u.title
    print("roundtrip ok", back.id)


if __name__ == "__main__":
    main()
