# -*- coding: utf-8 -*-
"""Backward-compatible source checkout entrypoint for the packaged MCP server."""

from x_reader.mcp_server import (
    detect_platform,
    list_inbox,
    main,
    mcp,
    read_batch,
    read_url,
    run_server,
)

__all__ = [
    "detect_platform",
    "list_inbox",
    "main",
    "mcp",
    "read_batch",
    "read_url",
    "run_server",
]


if __name__ == "__main__":
    raise SystemExit(main())
