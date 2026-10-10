#!/usr/bin/env python3
"""Run the x-reader moat benchmark against one or more URL-reading providers.

The benchmark intentionally separates transport success from useful source reads.
A 200/login shell is not scored as a successful read.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

BLOCK_MARKERS = (
    "security restriction",
    "安全限制",
    "account abnormal",
    "switch account and retry",
    "sorry, this page isn't available right now",
    "this page isn't available right now",
    "你访问的页面不见了",
    "请打开小红书app扫码查看",
    "sign in to continue",
    "log in to continue",
    "you must be logged in",
)

def load_corpus(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows

def classify(row: dict[str, Any], result: dict[str, Any]) -> str:
    if result.get("skipped"):
        return "SKIP"
    if not result.get("ok"):
        return "EXPECTED_BLOCK" if (row.get("requires_auth") or row.get("expected_block")) else "FAIL"

    content = str(result.get("content") or "")
    low = content.lower()
    shell_probe = low[:3000]
    canonical = str(result.get("canonical_url") or "").lower()
    title = str(result.get("title") or "").lower()

    strong_shell = any(marker in shell_probe for marker in BLOCK_MARKERS)
    auth_redirect = (
        "/login" in canonical
        or "accounts.google.com" in canonical
        or "sign in to github" in title
        or "sign in to gmail" in shell_probe
    )

    if row.get("requires_auth"):
        if strong_shell or auth_redirect:
            return "AUTH_SHELL"
        return "AUTH_UNVERIFIED"

    if row.get("expected_block"):
        return "EXPECTED_BLOCK" if strong_shell else "CONTROL_READ_REVIEW"

    if strong_shell:
        return "BLOCK_SHELL"
    if result.get("media_complete"):
        return "MEDIA_COMPLETE"
    if len(content.strip()) < int(row.get("min_chars") or 200):
        return "THIN"
    if row.get("media_expected") is True or result.get("media_status") == "present":
        return "PARTIAL_MEDIA"
    return "READ"

def x_reader_provider(row: dict[str, Any], timeout: int,
                      podcast_preview_seconds: int = 0,
                      podcast_full_short: bool = False,
                      podcast_full_long: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="xr-moat-") as tmp:
        env = os.environ.copy()
        env["INBOX_FILE"] = str(Path(tmp) / "inbox.json")
        # Benchmark reads must not write into a user's Obsidian vault or
        # persistent content hub, even if the invoking shell has those set.
        env.pop("OBSIDIAN_VAULT", None)
        env["OUTPUT_DIR"] = tmp
        if podcast_full_long:
            # A benchmark's default must not silently persist raw audio or
            # public transcripts in the user's normal ~/.cache directory.
            # Explicit X_READER_LONG_CACHE_DIR enables a deliberate resume.
            env["X_READER_LONG_CACHE_DIR"] = os.environ.get(
                "X_READER_LONG_CACHE_DIR", str(Path(tmp) / "long-audio")
            )
        command = [sys.executable, "-m", "x_reader.cli", row["url"], "--json"]
        if row.get("category") == "podcast" and podcast_preview_seconds:
            command += ["--media-preview-seconds", str(podcast_preview_seconds)]
        if row.get("category") == "podcast" and podcast_full_short:
            command.append("--media-full-short")
        if row.get("category") == "podcast" and podcast_full_long:
            command.append("--media-full-long")
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    latency = round((time.perf_counter() - started) * 1000)
    if proc.returncode != 0:
        error = proc.stderr.strip()
        try:
            payload = json.loads(proc.stdout)
            error = str(payload.get("error") or error)
        except Exception:
            pass
        return {"ok": False, "latency_ms": latency, "error": error[:800], "content": ""}
    payload = json.loads(proc.stdout)
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    extra = payload.get("extra") or {}
    content = str(payload.get("content") or "")
    media_status = extra.get("media_status")
    media_complete = bool(extra.get("has_transcript")) and extra.get("transcript_coverage") != "preview"
    evidence_status = "PASS"
    if payload.get("source_type") == "podcast":
        from x_reader.evidence import build_receipt
        receipt = build_receipt(payload)
        evidence_status = receipt["status"]
        media_complete = bool(
            extra.get("transcript_coverage") == "full"
            and receipt["status"] == "PASS"
        )
    elif media_status == "present" and not media_complete:
        evidence_status = "PARTIAL"
    return {
        "ok": True,
        "latency_ms": latency,
        "title": payload.get("title") or "",
        "canonical_url": payload.get("url") or row["url"],
        "source_type": payload.get("source_type") or "",
        "content": content,
        "content_chars": len(content),
        "fetch_method": extra.get("fetch_method") or "",
        "media_status": media_status,
        "media_complete": media_complete,
        "transcript_coverage": extra.get("transcript_coverage") or "unknown",
        "preview_seconds": int(extra.get("preview_seconds") or 0),
        "processed_seconds": extra.get("processed_seconds") or 0,
        "media_duration_seconds": extra.get("media_duration_seconds") or 0,
        "coverage_ratio": extra.get("coverage_ratio") or 0,
        "audio_bytes": extra.get("audio_bytes") or 0,
        "media_sha256": extra.get("media_sha256") or "",
        "chunk_count": extra.get("chunk_count") or 0,
        "segment_count": extra.get("segment_count") or 0,
        "reused_source_chunks": extra.get("reused_source_chunks") or 0,
        "reused_asr_segments": extra.get("reused_asr_segments") or 0,
        "asr_boundary_adjustments": extra.get("asr_boundary_adjustments") or 0,
        "max_boundary_overrun_seconds": extra.get("max_boundary_overrun_seconds") or 0.0,
        "evidence_status": evidence_status,
    }

def jina_provider(row: dict[str, Any], timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    req = urllib.request.Request(
        "https://r.jina.ai/" + row["url"],
        headers={"Accept": "application/json", "User-Agent": "x-reader-moat-benchmark/0.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "http_status": exc.code,
            "error": f"HTTP {exc.code}: {exc.reason}",
            "content": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
            "content": "",
        }
    latency = round((time.perf_counter() - started) * 1000)
    text = raw.decode("utf-8", errors="replace")
    title = ""
    canonical_url = row["url"]
    content = text
    try:
        payload = json.loads(text)
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        if isinstance(data, dict):
            title = str(data.get("title") or "")
            canonical_url = str(data.get("url") or row["url"])
            content = str(data.get("content") or data.get("text") or "")
    except json.JSONDecodeError:
        pass
    return {
        "ok": status == 200,
        "latency_ms": latency,
        "http_status": status,
        "title": title,
        "canonical_url": canonical_url,
        "content": content,
        "content_chars": len(content),
    }

def firecrawl_provider(row: dict[str, Any], timeout: int) -> dict[str, Any]:
    key = os.getenv("FIRECRAWL_API_KEY")
    if not key:
        return {"ok": False, "skipped": True, "error": "missing FIRECRAWL_API_KEY", "content": ""}
    started = time.perf_counter()
    body = json.dumps({"url": row["url"], "formats": ["markdown"], "onlyMainContent": True}).encode()
    req = urllib.request.Request(
        "https://api.firecrawl.dev/v2/scrape",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "x-reader-moat-benchmark/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read())
            status = response.status
    except urllib.error.HTTPError as exc:
        return {"ok": False, "latency_ms": round((time.perf_counter()-started)*1000), "http_status": exc.code, "error": f"HTTP {exc.code}: {exc.reason}", "content": ""}
    except Exception as exc:
        return {"ok": False, "latency_ms": round((time.perf_counter()-started)*1000), "error": f"{type(exc).__name__}: {exc}", "content": ""}
    data = payload.get("data") or {}
    content = str(data.get("markdown") or data.get("content") or "")
    meta = data.get("metadata") or {}
    return {
        "ok": bool(payload.get("success", status == 200)),
        "latency_ms": round((time.perf_counter()-started)*1000),
        "http_status": status,
        "title": str(meta.get("title") or ""),
        "canonical_url": str(meta.get("sourceURL") or row["url"]),
        "content": content,
        "content_chars": len(content),
    }

PROVIDERS = {
    "x_reader": x_reader_provider,
    "jina": jina_provider,
    "firecrawl": firecrawl_provider,
}

def run_one(provider: str, row: dict[str, Any], timeout: int,
            podcast_preview_seconds: int = 0,
            podcast_full_short: bool = False,
            podcast_full_long: bool = False) -> dict[str, Any]:
    result = (x_reader_provider(
                  row, timeout, podcast_preview_seconds, podcast_full_short,
                  podcast_full_long
              )
              if provider == "x_reader" else PROVIDERS[provider](row, timeout))
    result["provider"] = provider
    result["id"] = row["id"]
    result["category"] = row["category"]
    result["url"] = row["url"]
    result["requires_auth"] = bool(row.get("requires_auth"))
    result["expected_block"] = bool(row.get("expected_block"))
    result["media_expected"] = row.get("media_expected")
    result["outcome"] = classify(row, result)
    result["content_chars"] = int(result.get("content_chars") or len(str(result.get("content") or "")))
    result["content_excerpt"] = str(result.get("content") or "")[:500]
    result.pop("content", None)
    return result

def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"total_results": len(results), "providers": {}}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        grouped[item["provider"]].append(item)
    for provider, items in grouped.items():
        usable = [i for i in items if i["outcome"] != "SKIP"]
        latencies = [i["latency_ms"] for i in usable if i.get("latency_ms") is not None]
        by_category: dict[str, Any] = {}
        for category in sorted({i["category"] for i in items}):
            cat = [i for i in items if i["category"] == category]
            eligible = [i for i in cat if not i.get("requires_auth") and not i.get("expected_block")]
            counts = Counter(i["outcome"] for i in cat)
            eligible_counts = Counter(i["outcome"] for i in eligible)
            full_reads = eligible_counts.get("READ", 0) + eligible_counts.get("MEDIA_COMPLETE", 0)
            usable_reads = full_reads + eligible_counts.get("PARTIAL_MEDIA", 0)
            by_category[category] = {
                "n": len(cat),
                "eligible_n": len(eligible),
                "outcomes": dict(counts),
                "full_read_rate": round(full_reads / len(eligible), 3) if eligible else None,
                "usable_text_rate": round(usable_reads / len(eligible), 3) if eligible else None,
            }
        counts = Counter(i["outcome"] for i in items)
        eligible = [i for i in items if not i.get("requires_auth") and not i.get("expected_block")]
        eligible_counts = Counter(i["outcome"] for i in eligible)
        full_reads = eligible_counts.get("READ", 0) + eligible_counts.get("MEDIA_COMPLETE", 0)
        usable_reads = full_reads + eligible_counts.get("PARTIAL_MEDIA", 0)
        summary["providers"][provider] = {
            "n": len(items),
            "eligible_n": len(eligible),
            "outcomes": dict(counts),
            "full_read_rate": round(full_reads / len(eligible), 3) if eligible else None,
            "usable_text_rate": round(usable_reads / len(eligible), 3) if eligible else None,
            "available_rate": round(len(usable) / max(1, len(items)), 3),
            "median_latency_ms": round(statistics.median(latencies)) if latencies else None,
            "categories": by_category,
        }
    return summary

def main() -> int:
    parser = argparse.ArgumentParser()
    default_corpus = Path(__file__).with_name("corpus.jsonl")
    parser.add_argument("--corpus", type=Path, default=default_corpus)
    parser.add_argument("--providers", default="x_reader,jina")
    parser.add_argument("--category", action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--podcast-preview-seconds", type=int, default=0,
                        choices=range(0, 31),
                        help="Opt-in local ASR for exactly one podcast case with --workers 1")
    parser.add_argument("--podcast-full-short", action="store_true",
                        help="Opt-in complete source/ASR proof for exactly one <=120s podcast")
    parser.add_argument("--podcast-full-long", action="store_true",
                        help="Opt-in resumable full-source ASR proof for exactly one <=90m / 64MiB podcast")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    rows = load_corpus(args.corpus)
    if args.category:
        wanted = set(args.category)
        rows = [r for r in rows if r["category"] in wanted]
    if args.limit:
        rows = rows[: args.limit]

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    unknown = [p for p in providers if p not in PROVIDERS]
    if unknown:
        parser.error(f"unknown providers: {', '.join(unknown)}")

    jobs = [(p, r) for p in providers for r in rows]
    media_modes = (
        bool(args.podcast_preview_seconds), args.podcast_full_short,
        args.podcast_full_long,
    )
    if sum(media_modes) > 1:
        parser.error("Preview, full-short and full-long audio flags are mutually exclusive")
    if args.podcast_full_long and args.timeout < 120:
        parser.error("Full-long media benchmark requires --timeout >= 120")
    if any(media_modes):
        if not (providers == ["x_reader"] and len(rows) == 1
                and rows[0]["category"] == "podcast" and args.workers == 1):
            parser.error(
                "Podcast ASR requires exactly one x_reader podcast case "
                "(--providers x_reader --category podcast --limit 1 --workers 1)"
            )
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        future_map = {
            pool.submit(run_one, p, r, args.timeout, args.podcast_preview_seconds,
                        args.podcast_full_short, args.podcast_full_long): (p, r)
            for p, r in jobs
        }
        for future in concurrent.futures.as_completed(future_map):
            p, row = future_map[future]
            try:
                item = future.result()
            except subprocess.TimeoutExpired:
                item = {
                    "provider": p, "id": row["id"], "category": row["category"],
                    "url": row["url"], "requires_auth": bool(row.get("requires_auth")),
                    "media_expected": row.get("media_expected"), "outcome": "FAIL",
                    "ok": False, "error": "timeout",
                }
            except Exception as exc:
                item = {
                    "provider": p, "id": row["id"], "category": row["category"],
                    "url": row["url"], "requires_auth": bool(row.get("requires_auth")),
                    "media_expected": row.get("media_expected"), "outcome": "FAIL",
                    "ok": False, "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(item)
            print(json.dumps({k: item.get(k) for k in ("provider","id","category","outcome","latency_ms","content_chars","error") if k in item}, ensure_ascii=False), flush=True)

    results.sort(key=lambda x: (x.get("provider",""), x.get("id","")))
    summary = summarize(results)
    output = args.output or Path("artifacts") / "moat-benchmark" / f"run-{int(time.time())}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in results) + "\n", encoding="utf-8")
    summary_path = output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"results": str(output), "summary": str(summary_path), **summary}, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
