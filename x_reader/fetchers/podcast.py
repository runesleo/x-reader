"""Public podcast-page ingestion with opt-in, bounded audio preview.

Page show notes and episode metadata are *not* spoken content.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from loguru import logger

from x_reader.fetchers.jina import fetch_direct_html, fetch_via_jina


async def fetch_podcast(url: str, preview_seconds: int = 0) -> dict:
    """Return metadata normally; optionally sample at most 30s of audio.

    For source portability Apple Podcasts is metadata-only for now.
    Only public Xiaoyuzhou episodes are eligible for local audio preview.
    """
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

    if preview_seconds > 0:
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
