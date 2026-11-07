#!/usr/bin/env python3
"""Opt-in GitHub PR source evidence report; read-only, advisory, never a fact-check."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable
from urllib.parse import urlsplit

from x_reader.evidence import build_receipt, failure_receipt
# URL discovery only. Canonical x-reader handles fetching, SSRF validation
# and evidence classification, including coverage of linked media.
_REFS = re.compile(r"https?://[^\s<>\[\]\"'\x60]+", re.I)
_FENCES = re.compile(r"\x60{3}[\s\S]*?\x60{3}")


def extract_pr_body_urls(markdown: str) -> list[str]:
    if not isinstance(markdown, str) or len(markdown.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("SOURCE_SECTION_TOO_LARGE")
    refs = []
    for match in _REFS.findall(_FENCES.sub("", markdown)):
        url = match.rstrip(".,;:!?)]}")
        if len(url.encode("utf-8")) > 2048:
            raise ValueError("SOURCE_URL_TOO_LARGE")
        try:
            parts = urlsplit(url)
            if (parts.scheme not in ("http", "https") or not parts.hostname
                    or parts.username or parts.password):
                raise ValueError("INVALID_REFERENCE_URL")
            if parts.port is not None and not 1 <= parts.port <= 65535:
                raise ValueError("INVALID_REFERENCE_PORT")
        except ValueError:
            raise ValueError("INVALID_REFERENCE_URL") from None
        if url not in refs:
            refs.append(url)
        if len(refs) > MAX_SOURCES:
            raise ValueError("SOURCE_BUDGET_EXCEEDED")
    return refs

OPEN = "<!-- x-reader:evidence -->"
CLOSE = "<!-- /x-reader:evidence -->"
MAX_SOURCES = 3
MAX_BODY_BYTES = 32768
MAX_OUTPUT_BYTES = 6 * 1024 * 1024
SAFE_REASON = re.compile(r"[A-Za-z0-9_-]{1,85}\Z")
HEAD_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
ROOT = Path(__file__).resolve().parents[1]


def source_urls(body: str | None) -> list[str] | None:
    if body is None:
        return None
    if not isinstance(body, str) or len(body.encode()) > MAX_BODY_BYTES:
        raise ValueError("PR_BODY_OVER_BUDGET")
    if OPEN not in body and CLOSE not in body:
        return None
    if body.count(OPEN) != 1 or body.count(CLOSE) != 1:
        raise ValueError("SOURCE_MARKERS_NOT_UNIQUE")
    start, end = body.index(OPEN) + len(OPEN), body.index(CLOSE)
    if end <= start:
        raise ValueError("SOURCE_MARKERS_OUT_OF_ORDER")
    urls = extract_pr_body_urls(body[start:end])
    if not urls:
        raise ValueError("EMPTY_SOURCE_SECTION")
    if len(urls) > MAX_SOURCES:
        raise ValueError("SOURCE_BUDGET_EXCEEDED")
    if any(urlsplit(url).scheme != "https" for url in urls):
        raise ValueError("HTTPS_SOURCES_ONLY")
    return urls


def read_source(url: str) -> dict:
    """Invoke the existing x-reader once, with no inherited credentials or user HOME."""
    with tempfile.TemporaryDirectory(prefix="xreader-gh-pr-") as home:
        env = {
            "HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "X_READER_ALLOW_EXTERNAL_SESSION_COOKIES": "0",
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-B", "-m", "x_reader.cli", url, "--json"],
                cwd=ROOT, env=env, capture_output=True, text=True,
                timeout=40, check=False,
            )
        except subprocess.TimeoutExpired:
            return failure_receipt(url, "reader_timeout", "Source read exceeded time budget")
        except OSError:
            return failure_receipt(url, "reader_unavailable", "Reader process unavailable")
    if proc.returncode or len(proc.stdout.encode()) > MAX_OUTPUT_BYTES:
        return failure_receipt(url, "reader_failed", "Reader failed or exceeded output budget")
    try:
        payload = json.loads(proc.stdout)
        if not isinstance(payload, dict) or not payload.get("source_type"):
            raise ValueError("INVALID_SOURCE")
        result = build_receipt(payload, requested_url=url)
        if result.get("status") not in {"PASS", "PARTIAL", "UNKNOWN", "FAIL"}:
            raise ValueError("INVALID_RECEIPT")
        return result
    except (TypeError, ValueError, KeyError):
        return failure_receipt(url, "reader_unverified", "Source receipt could not be verified")


def _source_tag(url: str, index: int) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if not re.fullmatch(r"[a-z0-9.:-]{1,85}", host):
        host = "public-source"
    return f"{index}. {host} [{hashlib.sha256(url.encode()).hexdigest()[:8]}]"


def evaluate_pr(event: dict, read: Callable[[str], dict] = read_source) -> dict:
    pr = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(pr, dict):
        raise ValueError("GITHUB_PR_EVENT_REQUIRED")
    head = pr.get("head")
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not HEAD_SHA.fullmatch(sha):
        raise ValueError("INVALID_PR_HEAD_SHA")
    if type(pr.get("number")) is not int or pr["number"] < 1:
        raise ValueError("INVALID_PR_NUMBER")
    if event.get("action") not in ("opened", "edited", "synchronize", "reopened"):
        raise ValueError("UNSUPPORTED_PR_EVENT_ACTION")
    rows = []
    try:
        urls = source_urls(pr.get("body"))
        if urls is None:
            status, reason, urls = "NOT_REQUESTED", "opt_in_marker_absent", []
        else:
            status, reason = "UNKNOWN", "not_evaluated"
    except ValueError as exc:
        status, reason, urls = "UNKNOWN", str(exc), []
    for i, url in enumerate(urls, 1):
        try:
            receipt = read(url)
        except Exception:
            receipt = failure_receipt(url, "reader_exception", "Reader raised an error")
        state = str(receipt.get("status") or "UNKNOWN")
        if state not in ("PASS", "PARTIAL", "FAIL", "UNKNOWN"):
            state = "UNKNOWN"
        reason_code = str(receipt.get("reason_code") or "reason_unknown")
        if not SAFE_REASON.fullmatch(reason_code):
            reason_code = "reason_untrusted"
        rows.append({"source": _source_tag(url, i), "status": state, "reason": reason_code})
    if rows:
        counts = Counter(row["status"] for row in rows)
        status = ("FAIL" if counts["FAIL"] else "PARTIAL" if counts["PARTIAL"]
                  else "UNKNOWN" if counts["UNKNOWN"] else "PASS")
        reason = "source_evidence_assessed"
    return {
        "head_sha": sha, "pr_number": pr["number"],
        "status": status,
        "reason": reason if SAFE_REASON.fullmatch(reason) else "source_input_invalid",
        "sources": rows, "source_count": len(urls),
        "advisory": True, "changed_files_checked": False,
        "factual_truth_verified": False, "github_write_performed": False,
    }


def summary_markdown(report: dict) -> str:
    lines = [
        "## x-reader Source Evidence (advisory)",
        f"**Coverage: {report['status']}** · PR #{report['pr_number']} · head {report['head_sha'][:12]}",
        "",
        "**Scope:** Opted-in PR description sources only. No changed files were checked.",
        "**Not a fact-check.** This job does not block a merge or change branch protection.",
        "",
    ]
    if not report["sources"]:
        lines.append("No coverage claim: " + report["reason"] + ".")
    else:
        lines += ["| Source | Status | Reason |", "| --- | --- | --- |"]
        for item in report["sources"]:
            lines.append("| " + item["source"] + " | " + item["status"] + " | "
                         + item["reason"] + " |")
    lines += ["", "Add an opt-in block to the PR description:", "", OPEN,
              "- https://x.com/example/status/123", CLOSE, "",
              "PASS only means coverage of the explicitly supplied source layers.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    try:
        event = json.loads(args.event.read_text(encoding="utf-8"))
        report = evaluate_pr(event)
    except (OSError, ValueError, TypeError):
        print("SOURCE_EVIDENCE=INFRA_ERROR", file=sys.stderr)
        return 2
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    with args.summary.open("a", encoding="utf-8") as file:
        file.write(summary_markdown(report))
    print("SOURCE_EVIDENCE=" + report["status"] +
          " HEAD=" + report["head_sha"][:12] +
          " COUNT=" + str(report["source_count"]) + " ADVISORY=true")
    return 0  # Evidence gaps do not silently become mandatory branch rules.


if __name__ == "__main__":
    raise SystemExit(main())
