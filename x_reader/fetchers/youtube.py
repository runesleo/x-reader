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

from x_reader.fetchers.jina import fetch_via_jina


def _extract_video_id(url: str) -> str:
    """Extract video ID from YouTube URL."""
    match = re.search(r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})', url)
    return match.group(1) if match else ""


MAX_SUBTITLE_BYTES = 5 * 1024 * 1024
MAX_INFO_JSON_BYTES = 2 * 1024 * 1024
_CUE_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})")


def _get_subtitles_via_ytdlp(url: str, lang: str = "en") -> dict:
    """
    Download subtitles (and the video's info JSON) using yt-dlp.

    Returns {"text", "end_seconds", "duration"}; text is empty when unavailable.
    end_seconds is the last cue's end time and duration comes from yt-dlp's
    metadata, so callers can prove how much of the video the transcript covers.
    """
    from x_reader.utils.proc import run_bounded
    from x_reader.utils.url_validator import validate_url
    validate_url(url)  # Security: validate before passing to a subprocess

    empty = {"text": "", "end_seconds": None, "duration": None}
    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = os.path.join(tmpdir, "sub")

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--write-auto-sub",
            "--write-sub",
            "--write-info-json",
            "--sub-lang", lang,
            "--sub-format", "srt",
            "--skip-download",
            "-o", output_path,
            url,
        ]

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
                text, end_seconds = _parse_srt(sub_file)
                return {"text": text, "end_seconds": end_seconds, "duration": duration}

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


def _parse_srt(filepath: str) -> tuple:
    """Parse SRT/VTT into clean text plus the last cue's end time (seconds or None)."""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    text_lines = []
    seen = set()
    end_seconds = None

    for line in lines:
        line = line.strip()
        if '-->' in line:
            times = _CUE_TIME.findall(line)
            if len(times) >= 2:
                h, m, s, ms = (int(x) for x in times[1])
                end_seconds = max(end_seconds or 0.0, h * 3600 + m * 60 + s + ms / 1000)
            continue
        if not line or line.isdigit():
            continue
        if line.startswith('[') and line.endswith(']'):
            continue
        if line not in seen:
            seen.add(line)
            text_lines.append(line)

    return " ".join(text_lines), end_seconds


def _transcribe_via_whisper(url: str) -> str:
    """
    Download audio with yt-dlp and transcribe via Groq Whisper API.

    Requires: GROQ_API_KEY env var + yt-dlp + ffmpeg installed.
    Groq Whisper limit: 25MB audio file.
    Returns transcript text, or empty string if unavailable.
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.info("GROQ_API_KEY not set, skipping Whisper transcription")
        return ""

    # Security: Validate URL before passing to subprocess
    from x_reader.utils.url_validator import validate_url
    validate_url(url)

    with tempfile.TemporaryDirectory() as tmpdir:
        output_template = os.path.join(tmpdir, "audio.%(ext)s")

        cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "m4a",
            "--audio-quality", "5",
            "--max-filesize", "25M",
            "-o", output_template,
            "--no-playlist",
            url,
        ]

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
    video_id = _extract_video_id(url)

    # Step 1: Get metadata via Jina (fast, always works)
    jina_data = fetch_via_jina(url)
    title = jina_data["title"]

    # Step 2: Try yt-dlp subtitles (with timing, so coverage can be proven)
    logger.info(f"Extracting subtitles ({sub_lang})...")
    subs = _get_subtitles_via_ytdlp(url, lang=sub_lang)
    transcript = subs["text"]
    transcript_source = "subtitles" if transcript else ""

    # Step 3: No subtitles? Try Whisper transcription
    if not transcript:
        logger.info("No subtitles available, trying Whisper transcription...")
        transcript = _transcribe_via_whisper(url)
        transcript_source = "whisper" if transcript else ""

    if transcript:
        logger.info(f"Got transcript: {len(transcript)} chars")
        content = transcript
        has_transcript = True
    else:
        logger.info("No transcript available, using page description")
        content = jina_data["content"]
        has_transcript = False

    return {
        "title": title,
        "description": content,
        "author": jina_data.get("author", ""),
        "url": url,
        "video_id": video_id,
        "has_transcript": has_transcript,
        "transcript_source": transcript_source,
        "transcript_end_seconds": subs["end_seconds"] if transcript_source == "subtitles" else None,
        "duration": subs["duration"],
        "platform": "youtube",
    }
