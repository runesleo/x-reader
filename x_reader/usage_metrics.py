"""Opt-in, request-only daily aggregates for the existing hosted x-reader.

No raw URL, IP, user agent, referrer, cookie, content, per-request event or
identifier is persisted. There is no public statistics endpoint or scheduler.
The operator may query the same private SQLite file through this module's CLI.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
import sqlite3
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

_DB_ENV = "X_READER_USAGE_DB"
_RETENTION_DAYS = 90
_STATES = {"PASS", "PARTIAL", "FAIL", "UNKNOWN"}
_SOURCES = {"twitter", "youtube", "manual", "generic"}
_ROUTES = {
    ("GET", "/"): "landing_en",
    ("GET", "/en"): "landing_en",
    ("GET", "/en/"): "landing_en",
    ("GET", "/zh"): "landing_zh",
    ("GET", "/zh/"): "landing_zh",
    ("GET", "/app"): "app",
    ("GET", "/app/"): "app",
    ("POST", "/api/read"): "read",
}
_HTTP_CODES = {200, 400, 408, 413, 414, 415, 422, 429, 431, 500, 503}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_usage (
    day TEXT NOT NULL,
    route TEXT NOT NULL,
    http_code TEXT NOT NULL,
    receipt_status TEXT NOT NULL,
    source_type TEXT NOT NULL,
    error_class TEXT NOT NULL,
    latency_bucket TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    latency_ms_total INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (
        day, route, http_code, receipt_status, source_type,
        error_class, latency_bucket
    )
)
"""
_UPSERT = """
INSERT INTO daily_usage (
    day, route, http_code, receipt_status, source_type,
    error_class, latency_bucket, count, latency_ms_total
) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
ON CONFLICT (
    day, route, http_code, receipt_status, source_type,
    error_class, latency_bucket
) DO UPDATE SET
    count = count + 1,
    latency_ms_total = latency_ms_total + excluded.latency_ms_total
"""
_WARNED_STORAGE = False


def _private_database(path: Path) -> None:
    """Refuse relative paths, symlinks, unsafe modes and missing state dirs."""
    if not path.is_absolute() or not path.parent.is_dir():
        raise ValueError("invalid_usage_storage")
    try:
        fd = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        pass
    else:
        os.close(fd)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise ValueError("unsafe_usage_storage_mode")


def _route(method: str, request_target: str) -> str | None:
    try:
        return _ROUTES.get((method, urlsplit(request_target).path))
    except ValueError:
        return None


def _latency_bucket(ms: int) -> str:
    if ms < 500:
        return "lt_500ms"
    if ms < 2000:
        return "lt_2s"
    if ms < 5000:
        return "lt_5s"
    if ms < 15000:
        return "lt_15s"
    return "gte_15s"


def record_response(
    *,
    method: str,
    request_target: str,
    http_status: int,
    receipt: dict | None = None,
    elapsed_ms: int = 0,
) -> None:
    """Best-effort aggregate write; a storage outage never breaks a user read."""
    configured = os.environ.get(_DB_ENV, "").strip()
    if not configured:
        return
    route = _route(method, request_target)
    if route is None:  # Ignore health checks, bots hitting random URLs and assets.
        return
    status = receipt.get("status") if isinstance(receipt, dict) else None
    status = status if status in _STATES and route == "read" else "NA"
    source = receipt.get("source_type") if isinstance(receipt, dict) else None
    source = source if source in _SOURCES and route == "read" else "NA"
    reason = receipt.get("reason_code") if isinstance(receipt, dict) else None
    error = "timeout" if reason in {"timeout", "source_timeout"} else (
        "fetch_failure" if reason in {
            "source_fetch_failed", "source_unreadable", "invalid_cli_output",
            "invalid_cli_payload", "output_too_large",
        } else "none"
    )
    code = str(http_status) if http_status in _HTTP_CODES else "other"
    latency_ms = min(max(0, int(elapsed_ms)), 300000)
    now = datetime.now(timezone.utc).date()
    cutoff = now - timedelta(days=_RETENTION_DAYS)
    try:
        path = Path(configured)
        _private_database(path)
        # A sqlite connection context commits/rolls back but does NOT close FDs.
        # Do not hold up an HTTP response when another writer owns the DB.
        # Zero busy timeout makes this explicitly lossy under contention.
        with closing(sqlite3.connect(path, timeout=0.0)) as conn:
            with conn:
                conn.execute(_SCHEMA)
                conn.execute(_UPSERT, (
                    now.isoformat(), route, code, status, source, error,
                    _latency_bucket(latency_ms), latency_ms,
                ))
                conn.execute("DELETE FROM daily_usage WHERE day < ?", (cutoff.isoformat(),))
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        global _WARNED_STORAGE
        if not _WARNED_STORAGE:
            print(
                f"x-reader aggregate_usage_disabled error_type={type(exc).__name__}",
                file=sys.stderr,
            )
            _WARNED_STORAGE = True


def read_summary(path: Path, *, days: int = 14) -> dict:
    """Operator-only read; never creates a new database or exposes user inputs."""
    if not 1 <= days <= _RETENTION_DAYS:
        raise ValueError("days_out_of_range")
    # Support CLI-relative --db paths without following a symlink target.
    path = path.absolute()
    if not path.is_file() or path.is_symlink():
        return {"configured": False, "rows": []}
    metadata = path.stat()
    if metadata.st_mode & 0o077:
        raise ValueError("unsafe_usage_storage_mode")
    start = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    # Path.as_uri percent-escapes '?' and '#'; a literal file path must not
    # become a SQLite URI query string or silently read/create a different DB.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as conn:
        ready = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'daily_usage'"
        ).fetchone()
        rows = conn.execute(
            "SELECT day, route, http_code, receipt_status, source_type, "
            "error_class, latency_bucket, count, latency_ms_total "
            "FROM daily_usage WHERE day >= ? ORDER BY day, route, http_code, "
            "receipt_status, source_type, error_class, latency_bucket",
            (start,),
        ).fetchall() if ready else []
    return {
        "configured": True,
        "days": days,
        "note": "Request aggregates only; not unique visitors, users or completed installs.",
        "rows": [
            dict(zip((
                "day", "route", "http_code", "receipt_status", "source_type",
                "error_class", "latency_bucket", "count", "latency_ms_total",
            ), row))
            for row in rows
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read private x-reader aggregate request counts")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    configured = args.db or Path(os.environ[_DB_ENV]) if os.environ.get(_DB_ENV) else args.db
    result = read_summary(configured, days=args.days) if configured else {
        "configured": False, "rows": [],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None))


if __name__ == "__main__":
    main()
