"""Explicit, bounded, no-key *preview* transcription for public podcast audio.

This module never claims full episode coverage, and is not exposed by the
public hosted web service. It runs only when a local caller opts in to an
audio preview of at most 30 seconds.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from x_reader.utils.url_validator import resolve_safe_ips, validate_url

MAX_PREVIEW_SECONDS = 30
PODCAST_HOSTS = {"xiaoyuzhoufm.com", "www.xiaoyuzhoufm.com"}
# For now only accept the publicly documented Xiaoyuzhou episode CDN.
# Never hand a generic URL extracted from arbitrary HTML to ffmpeg.
AUDIO_HOSTS = {"media.xyzcdn.net"}
MAX_AUDIO_BYTES = 2 * 1024 * 1024  # Never download a full podcast by accident.
AUDIO_TYPES = {"audio/mp4", "audio/x-m4a", "audio/mpeg", "application/octet-stream"}


def _validated_media_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in AUDIO_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or any(ch in url for ch in ("\n", "\r", "\x00"))
    ):
        raise ValueError("Audio URL is not on the approved public CDN")
    # DNS/address check before invoking an external process.
    resolve_safe_ips(parsed.hostname, 443, timeout=5)
    return url


def _public_episode_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in PODCAST_HOSTS
        or not parsed.path.startswith("/episode/")
        or len(parsed.path.split("/")) != 3
        or not parsed.path.split("/")[-1].isalnum()
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Audio preview supports only public Xiaoyuzhou episode URLs")
    validate_url(url)
    return url


def _tool_env(home: str) -> dict[str, str]:
    # Avoid inheriting API keys, proxy variables, browser login states, or
    # ~/.config/yt-dlp from the invoking machine.
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "LANG": "C.UTF-8",
    }


def _discover_audio(url: str, home: str) -> str:
    command = [
        "yt-dlp", "--ignore-config", "--no-playlist", "--no-warnings",
        "--no-update", "--dump-single-json", "--skip-download",
        "--socket-timeout", "8", "--retries", "0", url,
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=30, env=_tool_env(home),
    )
    if result.returncode != 0:
        raise RuntimeError("Public podcast audio location is not available")
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Invalid audio locator response") from exc
    candidate = data.get("url")
    if not isinstance(candidate, str):
        raise RuntimeError("No public audio stream in podcast metadata")
    return _validated_media_url(candidate)


def _download_media_prefix(url: str, target: Path) -> int:
    """Range-fetch only the first 2 MiB over a pinned, validated IP.

    Reject redirects, non-audio content and hosts that are not on the
    allowlist. ffmpeg subsequently processes only this local file.
    """
    from x_reader.fetchers.jina import _request_pinned

    approved = _validated_media_url(url)
    pool, response, _ip = _request_pinned(
        approved, timeout_seconds=20,
        headers={
            "Range": f"bytes=0-{MAX_AUDIO_BYTES - 1}",
            "Accept": "audio/*,application/octet-stream",
            "User-Agent": "x-reader-media-preview/0.1",
        },
    )
    try:
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        content_range = str(response.headers.get("Content-Range") or "").lower()
        if response.status != 206:
            raise RuntimeError("Public audio CDN did not honor a bounded byte-range request")
        if content_type not in AUDIO_TYPES:
            raise RuntimeError("Public audio CDN returned a non-audio response")
        if not content_range.startswith("bytes 0-"):
            raise RuntimeError("Audio response range did not start at byte zero")

        deadline = time.monotonic() + 25
        size = 0
        with target.open("wb") as stream:
            while size < MAX_AUDIO_BYTES:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Bounded audio fetch exceeded deadline")
                chunk = response.read(min(64 * 1024, MAX_AUDIO_BYTES - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise RuntimeError("Audio response exceeded byte limit")
                stream.write(chunk)
        if size < 8192:
            raise RuntimeError("Audio response too thin for local decoding")
        return size
    finally:
        response.release_conn()
        pool.close()


def transcribe_preview(url: str, seconds: int, cache_dir: str | None = None) -> dict:
    """Transcribe the first N seconds; use only via explicit local opt-in.

    This is a sampling tool, not a full-media transcription. Audio stays in
    a TemporaryDirectory and is deleted before return. The caller controls
    when optional model weights are downloaded to its local cache.
    """
    if type(seconds) is not int or not 1 <= seconds <= MAX_PREVIEW_SECONDS:
        raise ValueError("Audio preview must be an integer from 1 to 30 seconds")

    safe_url = _public_episode_url(url)
    with tempfile.TemporaryDirectory(prefix="xreader-media-preview-") as work:
        audio_url = _discover_audio(safe_url, work)
        source_file = Path(work) / "source.m4a"
        _download_media_prefix(audio_url, source_file)
        wav_file = Path(work) / "preview.wav"
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file",
            "-i", str(source_file), "-t", str(seconds),
            "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-y", str(wav_file),
        ]
        process = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=60, env=_tool_env(work),
        )
        if process.returncode != 0 or not wav_file.is_file():
            raise RuntimeError("Bounded audio clip decode failed")
        size = wav_file.stat().st_size
        if size <= 44 or size > 32_000 * seconds + 1_000_000:
            raise RuntimeError("Audio clip failed size validation")

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                'Local ASR optional extra missing: pip install "x-reader[media]"'
            ) from exc

        cache = Path(cache_dir or os.getenv("X_READER_ASR_CACHE_DIR") or
                     (Path.home() / ".cache" / "x-reader" / "asr"))
        # Only the opt-in path downloads/model-caches local Whisper weights.
        model = WhisperModel(
            "tiny", device="cpu", compute_type="int8",
            cpu_threads=2, num_workers=1, download_root=str(cache),
        )
        segments, _info = model.transcribe(
            str(wav_file), beam_size=1,
            vad_filter=False, condition_on_previous_text=False,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        if not text:
            raise RuntimeError("No speech detected in audio preview")

        return {
            "text": text,
            "preview_seconds": seconds,
            "transcription_method": "local_whisper_tiny_cpu",
            "transcript_coverage": "preview",
        }
