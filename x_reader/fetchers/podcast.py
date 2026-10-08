"""Public podcast-page ingestion with optional bounded preview/full-short ASR.

Page show notes and episode metadata are *not* spoken content.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from loguru import logger

from x_reader.fetchers.jina import fetch_direct_html, fetch_via_jina


async def fetch_podcast(url: str, preview_seconds: int = 0,
                        full_short_audio: bool = False) -> dict:
    """Return metadata normally; optionally sample 30s or prove a short full source.

    For source portability Apple Podcasts is metadata-only for now.
    Only public Xiaoyuzhou episodes are eligible for local audio preview.
    """
    if preview_seconds and full_short_audio:
        raise ValueError("Preview and short-full audio modes are mutually exclusive")
    try:
        data = await asyncio.to_thread(fetch_direct_html, url)
    except Exception as exc:
        logger.warning(f"Podcast direct text fetch failed ({type(exc).__name__}); trying Jina")
        data = await asyncio.to_thread(fetch_via_jina, url)

    title = str(data.get("title") or "").strip()
    notes = str(data.get("content") or "").strip()
    if not title or len(notes) < 40:
        raise RuntimeError("Podcast page returned insufficient episode metadata")

    result = {
        "title": title,
        "description": notes,
        "author": "",
        "url": str(data.get("url") or url),
        "platform": "podcast",
        "fetch_method": str(data.get("fetch_method") or "direct_html"),
        "media_status": "present",
        "has_transcript": False,
        "transcript_coverage": "none",
        "preview_seconds": 0,
        "preview_transcript": "",
    }

    if full_short_audio:
        host = urlsplit(url).hostname or ""
        if host not in {"xiaoyuzhoufm.com", "www.xiaoyuzhoufm.com"}:
            result["full_error"] = "full_audio_unsupported_for_this_podcast_host"
        else:
            try:
                from x_reader.full_audio import transcribe_full_short
                full = await asyncio.to_thread(transcribe_full_short, url)
                result.update(full)
            except Exception as exc:
                # Fail only the optional media upgrade. Never claim PASS when
                # byte-for-byte source or decoded coverage proof is missing.
                logger.warning(f"Full short podcast audio not verified: {type(exc).__name__}")
                result["full_error"] = type(exc).__name__
    elif preview_seconds > 0:
        host = urlsplit(url).hostname or ""
        if host not in {"xiaoyuzhoufm.com", "www.xiaoyuzhoufm.com"}:
            result["preview_error"] = "preview_unsupported_for_this_podcast_host"
        else:
            try:
                from x_reader.media_preview import transcribe_preview
                preview = await asyncio.to_thread(transcribe_preview, url, preview_seconds)
                result.update({
                    "preview_transcript": preview["text"],
                    "preview_seconds": preview["preview_seconds"],
                    "transcript_coverage": "preview",
                    "transcription_method": preview["transcription_method"],
                })
            except Exception as exc:
                # Fail the *enrichment*, not the valid page text. Do not expose
                # raw CDN URLs or provider responses in serialised receipts.
                logger.warning(f"Podcast audio preview not completed: {type(exc).__name__}")
                result["preview_error"] = type(exc).__name__
    return result
