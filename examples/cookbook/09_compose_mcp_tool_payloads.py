#!/usr/bin/env python3
# Scenario: Build JSON payloads matching MCP tool shapes (`read_batch`).
# Learning goal: glue layer between agent and `mcp_server` contracts.
# Prereqs: stdlib json only; does not start MCP.
# Edge case: empty list should be rejected by server — validate len ≥ 1.
# Run: python examples/cookbook/09_compose_mcp_tool_payloads.py

from __future__ import annotations

import json

from x_reader.utils.url_validator import validate_url


def build_read_batch(urls: list[str]) -> str:
    cleaned = [validate_url(u) for u in urls]
    return json.dumps({"urls": cleaned}, ensure_ascii=False)


def main() -> None:
    payload = build_read_batch(
        ["https://example.com/a", "https://example.com/b"],
    )
    print(payload)


if __name__ == "__main__":
    main()
