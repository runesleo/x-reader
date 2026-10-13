"""Minimal local web surface for x-reader Evidence Receipts.

The server deliberately shells out to the existing x-reader CLI in a sanitized
per-request environment. That keeps the hosted surface on the same reader path
while preventing accidental reuse of local cookies, Telegram credentials,
Groq keys, inbox files, or Obsidian output settings.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from x_reader.evidence import build_receipt, failure_receipt
from x_reader.routing import ALLOWED_PLATFORMS_ENV, HOSTED_ENV, detect_platform, host_matches
from x_reader.utils.proc import run_bounded


MAX_BODY_BYTES = 4096
MAX_URL_CHARS = 2048
MAX_PREVIEW_CHARS = 1200
REQUEST_TIMEOUT_SECONDS = 75
REQUEST_SLOTS = threading.BoundedSemaphore(3)
ALLOWED_PLATFORMS = {"twitter", "youtube", "generic"}
SOCKET_TIMEOUT_SECONDS = 15
# Absolute budget for the request line, headers and body together: a client
# dripping one byte at a time cannot hold a connection thread past it.
REQUEST_READ_DEADLINE_SECONDS = 20
# 24 connection threads + the watcher + 3 reader processes stay inside the service's 64 tasks.
MAX_CONNECTION_THREADS = 24
MAX_CLI_OUTPUT_BYTES = 8 * 1024 * 1024

# Public failure reasons are a fixed set; exception names never reach clients.
_FAILURE_REASONS = {
    "ValueError": "source_unreadable",
    "TimeoutError": "source_timeout",
    "ReadTimeout": "source_timeout",
    "ConnectTimeout": "source_timeout",
    "TooManyRedirects": "too_many_redirects",
}
SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    (
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; object-src 'none'; form-action 'self'",
    ),
)


class HostedPolicyError(ValueError):
    """The URL is valid input but outside the public hosted MVP policy."""


# Routing is shared with the CLI reader (x_reader.routing), so the policy check
# below and the reader that actually fetches can never disagree about a URL.
_host_matches = host_matches


def normalize_input_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url:
        raise HostedPolicyError("URL is required")
    if len(url) > MAX_URL_CHARS:
        raise HostedPolicyError("URL is too long")
    # Control characters or inner whitespace could forge extra lines in a
    # shared receipt; real URLs percent-encode them.
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise HostedPolicyError("URL must not contain spaces or control characters")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HostedPolicyError("Only absolute http(s) URLs are supported")
    if parsed.username or parsed.password:
        raise HostedPolicyError("Credential-bearing URLs are not supported")
    from x_reader.utils.url_validator import canonical_url

    try:
        # Policy, reader and connection all see the hostname as it is connected to.
        return canonical_url(url)
    except ValueError as exc:
        raise HostedPolicyError("URL hostname is not valid") from exc


def validate_public_url(url: str) -> str:
    from x_reader.utils.url_validator import validate_url

    return validate_url(url)


def _minimal_child_env(home: Path, inbox_file: Path) -> dict[str, str]:
    """Pass only non-secret process settings needed by the public reader.

    Proxies are not inherited: a proxy would fetch on the server's behalf and
    bypass the pinned, per-hop validated connections. X_READER_HOSTED makes the
    child skip .env loading and all persistence; the platform allowlist makes the
    reader refuse anything the hosted policy did not approve.
    """
    allow = (
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "SYSTEMROOT",
    )
    env = {key: os.environ[key] for key in allow if os.environ.get(key)}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["TMPDIR"] = str(home / "tmp")
    env["INBOX_FILE"] = str(inbox_file)
    env["X_READER_ALLOW_EXTERNAL_SESSION_COOKIES"] = "0"
    env[HOSTED_ENV] = "1"
    env[ALLOWED_PLATFORMS_ENV] = ",".join(sorted(ALLOWED_PLATFORMS))
    return env


def run_cli(url: str) -> subprocess.CompletedProcess[str]:
    """Run the existing CLI with no inherited auth/session/LLM secrets."""
    with tempfile.TemporaryDirectory(prefix="x-reader-evidence-") as tmp:
        home = Path(tmp)
        (home / "tmp").mkdir(parents=True, exist_ok=True)
        env = _minimal_child_env(home, home / "inbox.json")
        # The CLI gets its own session and process group; tools it starts stay in
        # that group (hosted mode), so exit, timeout or overflow kills all of them.
        return run_bounded(
            [sys.executable, "-m", "x_reader.cli", url, "--json"],
            timeout=REQUEST_TIMEOUT_SECONDS,
            env=env,
            max_output_bytes=MAX_CLI_OUTPUT_BYTES,
            new_session=True,
        )


def _parse_cli_failure(
    proc: subprocess.CompletedProcess[str],
    url: str,
    platform: str,
) -> dict[str, Any]:
    # Only a reason from a fixed set is public; the exception type goes to the
    # service log (no URL, no message) and raw error text is dropped.
    error_type = ""
    try:
        payload = json.loads(proc.stdout)
        if isinstance(payload, dict) and payload.get("error"):
            error_type = "".join(ch for ch in str(payload.get("error_type") or "") if ch.isalnum())[:40]
    except (json.JSONDecodeError, TypeError):
        pass
    print(f"x-reader-evidence read_failed error_type={error_type or 'unknown'}", file=sys.stderr)
    reason = _FAILURE_REASONS.get(error_type, "source_fetch_failed")
    evidence = "x-reader could not fetch the public source reliably"
    return failure_receipt(url, reason, evidence, source_type=platform)


def read_public_url(
    raw_url: str,
    *,
    runner: Callable[[str], subprocess.CompletedProcess[str]] | None = None,
    validator: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Read one public URL and return a bounded UI/API response."""
    url = normalize_input_url(raw_url)
    platform = detect_platform(url)
    if platform not in ALLOWED_PLATFORMS:
        raise HostedPolicyError(
            f"Hosted MVP supports public X, YouTube, and generic web URLs; {platform} is disabled"
        )

    (validator or validate_public_url)(url)

    try:
        proc = (runner or run_cli)(url)
    except ValueError:  # CLI output exceeded the byte budget
        receipt = failure_receipt(
            url,
            "output_too_large",
            "x-reader returned more data than the hosted surface accepts",
            source_type=platform,
        )
        return _response_from_receipt(receipt, None)
    except subprocess.TimeoutExpired:
        receipt = failure_receipt(
            url,
            "timeout",
            "x-reader did not finish within the hosted request deadline",
            source_type=platform,
        )
        return _response_from_receipt(receipt, None)

    if proc.returncode != 0:
        return _response_from_receipt(_parse_cli_failure(proc, url, platform), None)

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        receipt = failure_receipt(
            url,
            "invalid_cli_output",
            "x-reader completed but did not return machine-readable JSON",
            source_type=platform,
        )
        return _response_from_receipt(receipt, None)

    if not isinstance(payload, dict):
        receipt = failure_receipt(
            url,
            "invalid_cli_payload",
            "x-reader returned an unexpected payload shape",
            source_type=platform,
        )
        return _response_from_receipt(receipt, None)

    receipt = build_receipt(payload, requested_url=url)
    return _response_from_receipt(receipt, payload)


def _response_from_receipt(
    receipt: dict[str, Any],
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    source = None
    if payload is not None:
        content = str(payload.get("content") or "")
        source = {
            "title": payload.get("title") or "",
            "canonical_url": payload.get("url") or receipt.get("canonical_url") or "",
            "source_type": payload.get("source_type") or receipt.get("source_type") or "unknown",
            "content_preview": content[:MAX_PREVIEW_CHARS],
            "content_truncated": len(content) > MAX_PREVIEW_CHARS,
        }

    return {
        "ok": True,
        "receipt": receipt,
        "source": source,
        "policy": {
            "public_urls_only": True,
            "allowed_platforms": sorted(ALLOWED_PLATFORMS),
            "saved_sessions": False,
            "external_session_cookies": False,
            "external_transcription_keys": False,
            "persistent_inbox": False,
        },
    }


def _asset_html(name: str = "index.html") -> bytes:
    return files("x_reader.web").joinpath(name).read_bytes()


_ERROR_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    408: "request_timeout",
    413: "request_body_too_large_or_empty",
    414: "uri_too_long",
    431: "request_header_fields_too_large",
    501: "method_not_allowed",
    503: "busy",
    505: "http_version_not_supported",
}


def _error_json(status: int) -> bytes:
    return json.dumps({"ok": False, "error": _ERROR_CODES.get(status, "error")}).encode("utf-8")


class _ReadDeadlines:
    """One watcher thread for every connection's absolute request-read deadline.

    A thread per connection would double the service's task count under load.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[float, Any]] = {}
        self._watcher: threading.Thread | None = None

    def add(self, handler: Any, seconds: float) -> None:
        with self._lock:
            self._pending[id(handler)] = (time.monotonic() + seconds, handler)
            if self._watcher is None or not self._watcher.is_alive():
                self._watcher = threading.Thread(target=self._watch, daemon=True)
                self._watcher.start()

    def discard(self, handler: Any) -> None:
        with self._lock:
            self._pending.pop(id(handler), None)

    def _watch(self) -> None:
        while True:
            time.sleep(0.25)
            now = time.monotonic()
            with self._lock:
                expired = [h for deadline, h in self._pending.values() if deadline <= now]
                for handler in expired:
                    self._pending.pop(id(handler), None)
            for handler in expired:
                handler._abort_slow_read()


_READ_DEADLINES = _ReadDeadlines()


class EvidenceRequestHandler(BaseHTTPRequestHandler):
    server_version = "x-reader-evidence"
    sys_version = ""
    # Per-read socket timeout; REQUEST_READ_DEADLINE_SECONDS bounds the whole read.
    timeout = SOCKET_TIMEOUT_SECONDS

    def version_string(self) -> str:
        return self.server_version  # no interpreter version in the Server header

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def setup(self) -> None:
        super().setup()
        self._read_aborted = False
        _READ_DEADLINES.add(self, REQUEST_READ_DEADLINE_SECONDS)

    def _abort_slow_read(self) -> None:
        self._read_aborted = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _request_read(self) -> bool:
        """Mark the request fully read; False if the read deadline already cut it off."""
        _READ_DEADLINES.discard(self)
        if self._read_aborted:
            self.close_connection = True
            return False
        return True

    def _path(self) -> str | None:
        """Request path, or None after answering 400 for an unparseable request-target."""
        try:
            return urlparse(self.path).path
        except ValueError:
            self._json(400, {"ok": False, "error": "bad_request"})
            return None

    def finish(self) -> None:
        _READ_DEADLINES.discard(self)
        try:
            super().finish()
        except OSError:
            pass

    def _headers(self, status: int, content_type: str, length: int,
                 extra: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for name, value in (*SECURITY_HEADERS, *extra):
            self.send_header(name, value)
        self.end_headers()

    def _json(self, status: int, payload: dict[str, Any],
              extra: tuple[tuple[str, str], ...] = ()) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Every stdlib error path (bad request line, oversized headers, ...) answers in JSON."""
        self.close_connection = True
        try:
            self._json(code, {"ok": False, "error": _ERROR_CODES.get(code, "error")})
        except OSError:
            pass

    def _method_not_allowed(self) -> None:
        if self._request_read():
            self._json(405, {"ok": False, "error": "method_not_allowed"}, (("Allow", "GET, POST"),))

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed  # noqa: N815

    def do_GET(self) -> None:  # noqa: N802
        if not self._request_read():
            return
        path = self._path()
        if path is None:
            return
        page = {
            "/": "landing-en.html",
            "/en": "landing-en.html",
            "/en/": "landing-en.html",
            "/zh": "landing-zh.html",
            "/zh/": "landing-zh.html",
            "/app": "index.html",
            "/app/": "index.html",
        }.get(path)
        if page:
            body = _asset_html(page)
            self._headers(200, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if path == "/healthz":
            self._json(200, {"ok": True, "service": "x-reader-evidence"})
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
        except ValueError:
            if self._request_read():
                self._json(400, {"ok": False, "error": "bad_request"})
            return
        if path != "/api/read":
            if self._request_read():
                self._json(404, {"ok": False, "error": "not_found"})
            return

        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            if self._request_read():
                self._json(415, {"ok": False, "error": "json_content_type_required"})
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            if self._request_read():
                self._json(400, {"ok": False, "error": "invalid_content_length"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            if self._request_read():
                self._json(413, {"ok": False, "error": "request_body_too_large_or_empty"})
            return

        try:
            raw_body = self.rfile.read(length)
        except OSError:
            raw_body = b""
        if not self._request_read():
            return
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            self._json(400, {"ok": False, "error": "invalid_json"})
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
            self._json(400, {"ok": False, "error": "url_string_required"})
            return

        if not REQUEST_SLOTS.acquire(blocking=False):
            self._json(429, {"ok": False, "error": "busy"})
            return
        try:
            try:
                response = read_public_url(payload["url"])
            except HostedPolicyError as exc:
                self._json(422, {"ok": False, "error": "policy_rejected", "detail": str(exc)})
                return
            except ValueError:
                self._json(422, {
                    "ok": False,
                    "error": "unsafe_or_invalid_url",
                    "detail": "Use a public http(s) URL; private, local and special addresses are not allowed.",
                })
                return
            except Exception:
                self._json(500, {"ok": False, "error": "internal_error"})
                return
            self._json(200, response)
        finally:
            REQUEST_SLOTS.release()


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a hard cap on concurrent connection threads."""

    daemon_threads = True

    def __init__(self, *args: Any, max_threads: int = MAX_CONNECTION_THREADS, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._thread_slots = threading.BoundedSemaphore(max_threads)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._thread_slots.acquire(blocking=False):
            body = _error_json(503)
            head = [
                "HTTP/1.1 503 Service Unavailable",
                "Content-Type: application/json; charset=utf-8",
                f"Content-Length: {len(body)}",
                "Connection: close",
                *(f"{name}: {value}" for name, value in SECURITY_HEADERS),
            ]
            try:
                request.settimeout(2)
                request.sendall("\r\n".join(head).encode("ascii") + b"\r\n\r\n" + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            thread = threading.Thread(
                target=self._process_and_release, args=(request, client_address), daemon=True,
            )
            thread.start()
        except BaseException:
            # No worker owns the slot or the socket: give both back before failing.
            self._thread_slots.release()
            self.shutdown_request(request)
            raise

    def _process_and_release(self, request: Any, client_address: Any) -> None:
        try:
            self.process_request_thread(request, client_address)
        finally:
            self._thread_slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Clients that hang up or hit the read deadline are routine, not tracebacks.
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local x-reader Evidence Receipt MVP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    server = BoundedThreadingHTTPServer((args.host, args.port), EvidenceRequestHandler)
    print(f"x-reader Evidence Receipt: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
