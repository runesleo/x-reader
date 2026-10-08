"""Validated fail-closed configuration for the owner-only x-reader Cloud MCP."""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

REQUIRED = (
    "PUBLIC_URL",
    "GITHUB_OAUTH_CLIENT_ID",
    "GITHUB_OAUTH_CLIENT_SECRET",
    "X_READER_ALLOWED_GITHUB_LOGIN",
    "X_READER_JWT_SIGNING_KEY",
    "X_READER_STORAGE_FERNET_KEY",
)


@dataclass(frozen=True)
class CloudConfig:
    public_url: str
    client_id: str
    client_secret: str
    allowed_login: str
    jwt_signing_key: str
    storage_fernet_key: str
    port: int


def load_cloud_config(env: Mapping[str, str] | None = None) -> CloudConfig:
    """Fail on absent/weak keys or an HTTP callback URL, without printing secrets."""
    source = os.environ if env is None else env
    missing = [key for key in REQUIRED if not source.get(key, "").strip()]
    if missing:
        raise ValueError("Missing required Cloud configuration: " + ", ".join(missing))

    raw_url = source["PUBLIC_URL"].strip().rstrip("/")
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PUBLIC_URL must be an HTTPS origin without a path or credentials")

    owner = source["X_READER_ALLOWED_GITHUB_LOGIN"].strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner):
        raise ValueError("X_READER_ALLOWED_GITHUB_LOGIN is not a valid GitHub login")

    jwt_key = source["X_READER_JWT_SIGNING_KEY"].strip()
    if len(jwt_key) < 32:
        raise ValueError("X_READER_JWT_SIGNING_KEY must be at least 32 characters")
    fernet_key = source["X_READER_STORAGE_FERNET_KEY"].strip()
    try:
        if len(base64.urlsafe_b64decode(fernet_key.encode("ascii"))) != 32:
            raise ValueError("Invalid Fernet length")
    except (ValueError, UnicodeError, base64.binascii.Error) as exc:
        raise ValueError("X_READER_STORAGE_FERNET_KEY must encode 32 random bytes") from exc

    try:
        port = int(source.get("PORT", "8000"))
    except ValueError as exc:
        raise ValueError("PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("PORT out of range")

    return CloudConfig(
        public_url=raw_url,
        client_id=source["GITHUB_OAUTH_CLIENT_ID"].strip(),
        client_secret=source["GITHUB_OAUTH_CLIENT_SECRET"].strip(),
        allowed_login=owner.lower(),
        jwt_signing_key=jwt_key,
        storage_fernet_key=fernet_key,
        port=port,
    )
