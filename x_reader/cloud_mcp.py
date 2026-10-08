"""Owner-only OAuth Remote MCP for x-reader.

This is an independent cloud entrypoint. The local x-reader-mcp stdio/SSE
launcher is unchanged. The tool uses the same public-only CLI and canonical
Evidence Receipt code as the web MVP; it never inherits browser/X cookies.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.server.auth import AuthContext
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from starlette.responses import JSONResponse

from x_reader.cloud_config import CloudConfig, load_cloud_config

# Restrict one-process / one-Volume instance. GitHub OAuth is not a free
# public reading API; the login allowlist gates every exposed tool.
CONCURRENT_READS = threading.BoundedSemaphore(2)
BUDGET_LOCK = threading.Lock()
BUDGET_WINDOW_SECONDS = 3600
MAX_READS_PER_WINDOW = 60
budget_window_start = time.monotonic()
budget_count = 0


def owner_only(ctx: AuthContext, expected_login: str) -> bool:
    token = ctx.token
    if token is None or not isinstance(token.claims, dict):
        return False
    login = token.claims.get("login")
    return isinstance(login, str) and login.lower() == expected_login.lower()


def reserve_request() -> bool:
    global budget_window_start, budget_count
    with BUDGET_LOCK:
        now = time.monotonic()
        if now - budget_window_start >= BUDGET_WINDOW_SECONDS:
            budget_window_start = now
            budget_count = 0
        if budget_count >= MAX_READS_PER_WINDOW:
            return False
        budget_count += 1
        return True


def build_mcp(config: CloudConfig) -> FastMCP:
    # Explicit encryption key + disk store on a Railway Volume. No plaintext
    # bearer/upstream tokens are persisted, and registrations survive restart.
    storage = FernetEncryptionWrapper(
        key_value=DiskStore(directory="/data/oauth"),
        fernet=Fernet(config.storage_fernet_key.encode("ascii")),
    )
    auth = GitHubProvider(
        client_id=config.client_id,
        client_secret=config.client_secret,
        base_url=config.public_url,
        jwt_signing_key=config.jwt_signing_key,
        client_storage=storage,
    )
    server = FastMCP("x-reader Cloud", auth=auth)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request):
        return JSONResponse({"ok": True, "service": "x-reader-cloud"})

    @server.tool(auth=lambda ctx: owner_only(ctx, config.allowed_login))
    def read_url(url: str) -> dict:
        """Read a public X/YouTube/web URL with canonical evidence coverage."""
        from x_reader.web_app import read_public_url
        if not CONCURRENT_READS.acquire(blocking=False):
            return {"ok": False, "error": "busy"}
        try:
            if not reserve_request():
                return {"ok": False, "error": "hourly_budget_exhausted"}
            try:
                return read_public_url(url)
            except (ValueError, TypeError):
                return {"ok": False, "error": "invalid_or_disallowed_public_url"}
            except Exception:
                # No full URLs, response bodies, tokens, or stack traces.
                return {"ok": False, "error": "source_read_failed"}
        finally:
            CONCURRENT_READS.release()

    return server


def main() -> None:
    config = load_cloud_config()
    # The entrypoint checks that /data is a real mount. Do not silently use
    # ephemeral container storage for sensitive OAuth registration state.
    if not Path("/data/oauth").is_dir():
        raise RuntimeError("Persistent OAuth directory not provisioned")
    server = build_mcp(config)
    server.run(transport="http", host="0.0.0.0", port=config.port)


if __name__ == "__main__":
    main()
