# -*- coding: utf-8 -*-
"""Packaged x-reader MCP server entrypoint."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from x_reader.reader import UniversalReader
from x_reader.schema import UnifiedInbox

load_dotenv()

mcp = FastMCP(
    "x-reader",
    instructions="Universal content reader — give it any URL, get structured content back.",
)

reader = UniversalReader(inbox=UnifiedInbox())


@mcp.tool()
async def read_url(url: str) -> str:
    """Read content from one URL and return the structured result as JSON."""

    content = await reader.read(url)
    return json.dumps(content.to_dict(), ensure_ascii=False, indent=2)


@mcp.tool()
async def read_batch(urls: list[str]) -> str:
    """Read multiple URLs concurrently and return a JSON array of results."""

    contents = await reader.read_batch(urls)
    return json.dumps([content.to_dict() for content in contents], ensure_ascii=False, indent=2)


@mcp.tool()
async def list_inbox() -> str:
    """List previously fetched content in the local inbox as JSON."""

    return json.dumps([item.to_dict() for item in reader.inbox.items], ensure_ascii=False, indent=2)


@mcp.tool()
async def detect_platform(url: str) -> str:
    """Detect the platform handler x-reader would use for a URL."""

    return reader._detect_platform(url)


def run_server(transport: str, host: str, port: int) -> None:
    """Run FastMCP using the configuration surface supported by MCP 1.x."""

    if transport == "sse":
        mcp.settings.host = host
        mcp.settings.port = port
    mcp.run(transport=transport)


def build_parser() -> argparse.ArgumentParser:
    """Build the MCP server CLI parser."""

    parser = argparse.ArgumentParser(description="x-reader MCP Server")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse"],
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Host to bind SSE server (default: 127.0.0.1). "
            "WARNING: binding to 0.0.0.0 or external IP exposes the server to the network "
            "without authentication — use at your own risk. For production, use a reverse proxy with authentication."
        ),
    )
    parser.add_argument("--port", type=int, default=8000, help="SSE port (default: 8000)")
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help=(
            "EXPLICITLY allow external network binding (--host 0.0.0.0). "
            "This is insecure for production use. You must understand the risks."
        ),
        dest="allow_external",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the packaged x-reader MCP server."""

    args = build_parser().parse_args(argv)

    if args.host in ("0.0.0.0", "::", "") and not args.allow_external:
        print(
            "\n⚠️  SECURITY WARNING: Refusing to bind to external interface by default.\n"
            "   To expose the server externally, you must use --allow-external\n"
            "   and understand this is insecure without additional authentication.\n"
            "   For production, use a reverse proxy (nginx, cloudflare, etc.)\n"
        )
        args.host = "127.0.0.1"

    run_server(args.transport, args.host, args.port)
    return 0
