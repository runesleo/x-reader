# -*- coding: utf-8 -*-
"""
YouTube video fetcher — three-tier content extraction:

1. yt-dlp auto-subtitles (fastest, best quality for subtitled videos)
2. yt-dlp audio download → Groq Whisper API transcription (for non-subtitled videos)
3. Jina Reader fallback (page description only)

Requires: yt-dlp installed (brew install yt-dlp / pip install yt-dlp)
Optional: GROQ_API_KEY env var for Whisper transcription
"""

import re
import os
import subprocess
import tempfile
from loguru import logger
from typing import Dict, Any
from urllib.parse import parse_qs, urlparse

from x_reader.fetchers.jina import fetch_via_jina


_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_PATH_ID_PREFIXES = {"shorts", "live", "embed", "v"}


def canonical_video_url(url: str) -> str:
    """Return https://www.youtube.com/watch?v=<id> for a single-video URL, else "".

    Only this rebuilt URL is ever handed to yt-dlp, so consent pages, redirect
    parameters, playlists and look-alike subdomains cannot steer it anywhere else.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    candidate = ""
    if host in _SHORT_HOSTS:
        candidate = parts[0] if len(parts) == 1 else ""
    elif host in _VIDEO_HOSTS:
        if parts == ["watch"]:
            values = parse_qs(parsed.query).get("v") or [""]
            candidate = values[0] if len(values) == 1 else ""
        elif len(parts) == 2 and parts[0] in _PATH_ID_PREFIXES:
            candidate = parts[1]
    if not _VIDEO_ID.fullmatch(candidate):
        return ""
    return f"https://www.youtube.com/watch?v={candidate}"


def _extract_video_id(url: str) -> str:
    """Extract the 11-character video ID from a YouTube video URL."""
    canonical = canonical_video_url(url)
    return canonical.rsplit("=", 1)[1] if canonical else ""


MAX_SUBTITLE_BYTES = 5 * 1024 * 1024
MAX_INFO_JSON_BYTES = 2 * 1024 * 1024
_CUE_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})")


def _get_subtitles_via_ytdlp(video_url: str, lang: str = "en") -> dict:
    """
    Download subtitles (and the video's info JSON) using yt-dlp.

    video_url must come from canonical_video_url(). Returns {"text", "cues",
    "duration"}; text is empty when unavailable. cues are the subtitle
    (start, end) times and duration comes from yt-dlp's metadata, so callers
    can prove how much of the video the transcript covers.
    """
    from x_reader.routing import hosted_mode
    from x_reader.utils.proc import run_bounded
    from x_reader.utils.url_validator import validate_url

    empty = {"text": "", "cues": [], "duration": None}
    if not video_url or canonical_video_url(video_url) != video_url:
        return empty
    validate_url(video_url)  # Security: validate before passing to a subprocess

    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = os.path.join(tmpdir, "sub")

        cmd = [
            "yt-dlp",
            # Never fall back to the generic extractor: it would follow redirects
            # from the page to arbitrary hosts.
            "--ies", "default,-generic",
            "--no-playlist",
            "--write-auto-sub",
            "--write-sub",
            "--write-info-json",
            "--sub-lang", lang,
            "--sub-format", "srt",
            "--skip-download",
            "-o", output_path,
        ]
        if hosted_mode():
            cmd.append("--ignore-config")
        cmd.append(video_url)

        try:
            run_bounded(cmd, timeout=60)
        except FileNotFoundError:
            logger.warning("yt-dlp not found. Install with: brew install yt-dlp")
            return empty
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp subtitle download timed out")
            return empty
        except ValueError:
            logger.warning("yt-dlp produced more output than allowed")
            return empty

        duration = _read_duration(output_path + ".info.json")
        for ext in [f".{lang}.srt", f".{lang}.vtt"]:
            sub_file = output_path + ext
            if os.path.exists(sub_file):
                if os.path.getsize(sub_file) > MAX_SUBTITLE_BYTES:
                    logger.warning("Subtitle file exceeds the size limit; ignoring it")
                    return {**empty, "duration": duration}
                text, cues = _parse_srt(sub_file)
                return {"text": text, "cues": cues, "duration": duration}

    return {**empty}


def _read_duration(info_path: str):
    """Video duration in seconds from yt-dlp's info JSON, or None."""
    import json
    try:
        if os.path.getsize(info_path) > MAX_INFO_JSON_BYTES:
            return None
        with open(info_path, "r", encoding="utf-8") as f:
            value = json.load(f).get("duration")
        value = float(value)
        return value if value > 0 and value == value else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _cue_seconds(match: tuple) -> float:
    h, m, s, ms = (int(x) for x in match)
    return h * 3600 + m * 60 + s + ms / 1000


def _parse_srt(filepath: str) -> tuple:
    """Parse SRT/VTT into clean text plus every cue's (start, end) seconds."""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    text_lines = []
    cues = []

    for line in lines:
        line = line.strip()
        if '-->' in line:
            times = _CUE_TIME.findall(line)
            if len(times) >= 2:
                cues.append((_cue_seconds(times[0]), _cue_seconds(times[1])))
            continue
        if not line or line.isdigit():
            continue
        if line.startswith('[') and line.endswith(']'):
            continue
        # Auto-captions repeat the previous line as they roll; a line repeated
        # later (a chorus, a catchphrase) is real speech and is kept.
        if not text_lines or text_lines[-1] != line:
            text_lines.append(line)

    return " ".join(text_lines), cues


def cue_coverage(cues: list) -> dict:
    """Summarise how subtitle cues cover the timeline: start, end, union and largest gap."""
    merged = []
    for start, end in sorted((float(a), float(b)) for a, b in cues):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    if not merged:
        return {}
    gaps = [later[0] - earlier[1] for earlier, later in zip(merged, merged[1:])]
    return {
        "transcript_start_seconds": round(merged[0][0], 3),
        "transcript_end_seconds": round(merged[-1][1], 3),
        "transcript_covered_seconds": round(sum(end - start for start, end in merged), 3),
        "transcript_max_gap_seconds": round(max(gaps, default=0.0), 3),
    }


def _transcribe_via_whisper(video_url: str) -> str:
    """
    Download audio with yt-dlp and transcribe via Groq Whisper API.

    video_url must come from canonical_video_url().
    Requires: GROQ_API_KEY env var + yt-dlp + ffmpeg installed.
    Groq Whisper limit: 25MB audio file.
    Returns transcript text, or empty string if unavailable.
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.info("GROQ_API_KEY not set, skipping Whisper transcription")
        return ""
    if not video_url or canonical_video_url(video_url) != video_url:
        return ""

    # Security: Validate URL before passing to subprocess
    from x_reader.routing import hosted_mode
    from x_reader.utils.url_validator import validate_url
    validate_url(video_url)

    with tempfile.TemporaryDirectory() as tmpdir:
        output_template = os.path.join(tmpdir, "audio.%(ext)s")

        cmd = [
            "yt-dlp",
            "--ies", "default,-generic",
            "-x",
            "--audio-format", "m4a",
            "--audio-quality", "5",
            "--max-filesize", "25M",
            "-o", output_template,
            "--no-playlist",
        ]
        if hosted_mode():
            cmd.append("--ignore-config")
        cmd.append(video_url)

        from x_reader.utils.proc import run_bounded
        try:
            run_bounded(cmd, timeout=180)
        except FileNotFoundError:
            logger.warning("yt-dlp not found for audio download")
            return ""
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp audio download timed out")
            return ""
        except ValueError:
            logger.warning("yt-dlp produced more output than allowed")
            return ""

        # Find the downloaded audio file
        audio_path = os.path.join(tmpdir, "audio.m4a")
        if not os.path.exists(audio_path):
            for f in os.listdir(tmpdir):
                if f.startswith("audio."):
                    audio_path = os.path.join(tmpdir, f)
                    break
            else:
                logger.warning("No audio file downloaded")
                return ""

        file_size = os.path.getsize(audio_path)
        if file_size > 25 * 1024 * 1024:
            logger.warning(f"Audio file too large ({file_size // 1024 // 1024}MB > 25MB limit)")
            return ""

        logger.info(f"Transcribing {file_size // 1024}KB audio via Groq Whisper...")

        import requests
        try:
            with open(audio_path, "rb") as f:
                response = requests.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (os.path.basename(audio_path), f, "audio/mp4")},
                    data={"model": "whisper-large-v3", "response_format": "text"},
                    timeout=120,
                )

            if response.status_code == 200:
                transcript = response.text.strip()
                logger.info(f"Whisper transcript: {len(transcript)} chars")
                return transcript
            else:
                logger.warning(f"Groq Whisper API error: {response.status_code} {response.text[:200]}")
                return ""
        except Exception as e:
            logger.warning(f"Whisper transcription failed: {e}")
            return ""


async def fetch_youtube(url: str, sub_lang: str = "en") -> Dict[str, Any]:
    """
    Fetch YouTube video content with three-tier extraction.

    Strategy:
    1. yt-dlp auto-subtitles (full transcript, fastest)
    2. yt-dlp audio + Groq Whisper API (for non-subtitled videos)
    3. Jina Reader fallback (page description only)

    Args:
        url: YouTube video URL
        sub_lang: Subtitle language code (default: "en")

    Returns:
        Dict with: title, description, author, url, video_id, has_transcript, platform
    """
    logger.info(f"Fetching YouTube: {url}")
    # Channel, playlist and consent URLs have no single video: they get the page
    # text only, and yt-dlp never sees them.
    video_url = canonical_video_url(url)
    video_id = _extract_video_id(url)

    # Step 1: Get metadata via Jina (fast, always works)
    jina_data = fetch_via_jina(video_url or url)
    title = jina_data["title"]

    # Step 2: Try yt-dlp subtitles (with timing, so coverage can be proven)
    subs = {"text": "", "cues": [], "duration": None}
    if video_url:
        logger.info(f"Extracting subtitles ({sub_lang})...")
        subs = _get_subtitles_via_ytdlp(video_url, lang=sub_lang)
    transcript = subs["text"]
    transcript_source = "subtitles" if transcript else ""

    # Step 3: No subtitles? Try Whisper transcription
    if not transcript and video_url:
        logger.info("No subtitles available, trying Whisper transcription...")
        transcript = _transcribe_via_whisper(video_url)
        transcript_source = "whisper" if transcript else ""

    if transcript:
        logger.info(f"Got transcript: {len(transcript)} chars")
        content = transcript
        has_transcript = True
    else:
        logger.info("No transcript available, using page description")
        content = jina_data["content"]
        has_transcript = False

    result = {
        "title": title,
        "description": content,
        "author": jina_data.get("author", ""),
        "url": url,
        "video_id": video_id,
        "has_transcript": has_transcript,
        "transcript_source": transcript_source,
        "duration": subs["duration"],
        "platform": "youtube",
    }
    if transcript_source == "subtitles":
        result.update(cue_coverage(subs["cues"]))
    return result
