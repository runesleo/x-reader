"""Complete-source audio ingestion for SHORT public Xiaoyuzhou episodes only.

This is deliberately not a generic URL fetcher or a background transcriber.
Full-source claims require exact encoded-file retrieval, duration checks,
full decoded PCM processing, transcript hashes and an auditable coverage record.
"""
from __future__ import annotations

import hashlib
import json
from http.client import IncompleteRead
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from urllib3.exceptions import ProtocolError, ReadTimeoutError

from x_reader.media_preview import (
    AUDIO_TYPES, _discover_audio, _public_episode_url, _tool_env,
    _validated_media_url,
)

MAX_FULL_AUDIO_BYTES = 2 * 1024 * 1024
MAX_FULL_AUDIO_SECONDS = 120
MIN_AUDIO_BYTES = 8192
SAMPLE_RATE = 16000


def _pinned_range(url: str, start: int, end: int, *, if_range: str = ""):
    """Return a pinned-IP HTTP range response, with no redirect following."""
    from x_reader.fetchers.jina import _request_pinned

    headers = {
        "Range": f"bytes={start}-{end}",
        "Accept": "audio/*,application/octet-stream",
        "User-Agent": "x-reader-full-audio/0.1",
    }
    if if_range:
        headers["If-Range"] = if_range
    return _request_pinned(_validated_media_url(url), timeout_seconds=20, headers=headers)


def _checked_range(response, start: int, end: int, total: int | None = None) -> int:
    if response.status != 206:
        raise RuntimeError("Audio CDN refused a verified HTTP 206 byte range")
    ctype = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if ctype not in AUDIO_TYPES:
        raise RuntimeError("Audio CDN returned non-audio content")
    cr = str(response.headers.get("Content-Range") or "").strip()
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", cr)
    if not match:
        raise RuntimeError("Audio CDN returned unverifiable byte-range length")
    actual_start, actual_end, actual_total = (int(v) for v in match.groups())
    if actual_start != start or actual_end != end:
        raise RuntimeError("Audio CDN returned the wrong byte range")
    if total is not None and actual_total != total:
        raise RuntimeError("Audio source size changed between requests")
    if actual_total < MIN_AUDIO_BYTES or actual_total > MAX_FULL_AUDIO_BYTES:
        raise RuntimeError("Full audio exceeds the strict 2 MiB source budget")
    return actual_total


def _probe_total_bytes(url: str) -> tuple[int, str]:
    pool, response, _ip = _pinned_range(url, 0, 0)
    try:
        total = _checked_range(response, 0, 0)
        etag = str(response.headers.get("ETag") or "").strip()
        # ETag never appears in receipts or public outputs. If-Range only
        # enables an additional consistency check when the server supports it.
        return total, etag
    finally:
        response.release_conn()
        pool.close()


def _download_complete_audio(url: str, target: Path) -> dict:
    """Download *exactly* the full source body or fail closed on truncation."""
    size, etag = _probe_total_bytes(url)
    pool, response, _ip = _pinned_range(url, 0, size - 1, if_range=etag)
    try:
        _checked_range(response, 0, size - 1, size)
        declared = str(response.headers.get("Content-Length") or "").strip()
        if declared and (not declared.isdecimal() or int(declared) != size):
            raise RuntimeError("Audio Content-Length does not match probed source size")

        sha = hashlib.sha256()
        downloaded = 0
        deadline = time.monotonic() + 30
        with target.open("wb") as output:
            while downloaded < size:
                if time.monotonic() > deadline:
                    raise TimeoutError("Whole-source audio download exceeded deadline")
                chunk = response.read(min(65536, size - downloaded))
                if not chunk:
                    raise RuntimeError("Full audio body was truncated")
                downloaded += len(chunk)
                if downloaded > size:
                    raise RuntimeError("Full audio body exceeded declared length")
                output.write(chunk)
                sha.update(chunk)
        if downloaded != size or target.stat().st_size != size:
            raise RuntimeError("Source audio bytes were not completely retrieved")
        return {"audio_bytes": size, "media_sha256": sha.hexdigest()}
    finally:
        response.release_conn()
        pool.close()


def _verified_complete_download(url: str, target: Path) -> dict:
    """Retry a source/transport race at most 3 times, never waive proof."""
    retriable_errors = (
        "refused a verified HTTP 206",
        "source size changed between requests",
        "Full audio body was truncated",
        "Content-Length does not match",
    )
    for attempt in range(3):
        try:
            # Every attempt re-probes total bytes and ETag before retrieval.
            return _download_complete_audio(url, target)
        except (ProtocolError, ReadTimeoutError, IncompleteRead, OSError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError("Verified full audio retrieval failed after 3 attempts") from exc
        except RuntimeError as exc:
            if not any(marker in str(exc) for marker in retriable_errors):
                raise
            if attempt == 2:
                raise RuntimeError("Verified full audio retrieval failed after 3 attempts") from exc
        time.sleep(0.35 * (attempt + 1))
    raise RuntimeError("Verified full audio retrieval failed")


def _probe_local_duration(source: Path, home: str) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-protocol_whitelist", "file",
            "-show_entries", "format=duration", "-of", "json", str(source),
        ],
        capture_output=True, text=True, timeout=15, env=_tool_env(home),
    )
    if result.returncode:
        raise RuntimeError("Could not prove encoded audio duration")
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError("Encoded audio duration is not verifiable") from exc
    if not 0 < duration <= MAX_FULL_AUDIO_SECONDS:
        raise RuntimeError("Full audio duration exceeds the strict 120-second limit")
    return duration


def _decode_entire_file(source: Path, target: Path, home: str, duration: float) -> float:
    """Decode the complete local file, with no -t trim or remote protocol."""
    import wave

    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
        "-protocol_whitelist", "file",
        "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
        "-f", "wav", "-y", str(target),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60, env=_tool_env(home),
    )
    if result.returncode or not target.is_file():
        raise RuntimeError("Full local audio decode failed")
    with wave.open(str(target), "rb") as stream:
        if stream.getnchannels() != 1 or stream.getframerate() != SAMPLE_RATE:
            raise RuntimeError("Decoded audio format mismatch")
        decoded = stream.getnframes() / stream.getframerate()
    tolerance = max(0.75, duration * 0.01)
    if decoded <= 0 or abs(decoded - duration) > tolerance:
        raise RuntimeError("Decoded audio coverage does not match encoded duration")
    if target.stat().st_size > int(2 * SAMPLE_RATE * (MAX_FULL_AUDIO_SECONDS + 2)) + 4096:
        raise RuntimeError("Decoded audio exceeded the bounded PCM budget")
    return decoded


def transcribe_full_short(url: str, cache_dir: str | None = None) -> dict:
    """Explicit local full-source transcription for <=120s / <=2MiB audio."""
    safe_url = _public_episode_url(url)
    with tempfile.TemporaryDirectory(prefix="xreader-full-audio-") as work:
        audio_url = _discover_audio(safe_url, work)
        source_file = Path(work) / "source.audio"
        acquired = _verified_complete_download(audio_url, source_file)
        encoded_duration = _probe_local_duration(source_file, work)
        decoded_file = Path(work) / "all_audio.wav"
        decoded_duration = _decode_entire_file(
            source_file, decoded_file, work, encoded_duration
        )

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError('Install optional local ASR: pip install "x-reader[media]"') from exc
        cache = Path(cache_dir or os.getenv("X_READER_ASR_CACHE_DIR") or
                     (Path.home() / ".cache" / "x-reader" / "asr"))
        model = WhisperModel(
            "tiny", device="cpu", compute_type="int8",
            cpu_threads=2, num_workers=1, download_root=str(cache),
        )
        segments, _info = model.transcribe(
            str(decoded_file), beam_size=1, vad_filter=False,
            condition_on_previous_text=False,
        )
        parts = []
        asr_segments = 0
        for segment in segments:
            start = float(segment.start)
            end = float(segment.end)
            if start < -0.5 or end < start or end > decoded_duration + 0.8:
                raise RuntimeError("Local ASR produced inconsistent segment timestamps")
            utterance = segment.text.strip()
            if utterance:
                parts.append(utterance)
                asr_segments += 1
        transcript = " ".join(parts).strip()
        if not transcript:
            raise RuntimeError("Full audio decoded, but no transcribable speech was found")

        # Coverage refers to the PCM sent to ASR, NOT an assertion that every
        # machine-generated word has been manually verified.
        return {
            "full_transcript": transcript,
            "has_transcript": True,
            "transcript_coverage": "full",
            "transcription_method": "local_whisper_tiny_cpu",
            "coverage_basis": "complete_encoded_bytes_and_full_decoded_pcm",
            "coverage_intervals": [{"start_seconds": 0.0, "end_seconds": round(decoded_duration, 3)}],
            "media_duration_seconds": round(encoded_duration, 3),
            "decoded_duration_seconds": round(decoded_duration, 3),
            "processed_seconds": round(decoded_duration, 3),
            "coverage_ratio": 1.0,
            "audio_bytes": acquired["audio_bytes"],
            "media_sha256": acquired["media_sha256"],
            "media_url_sha256": hashlib.sha256(audio_url.encode()).hexdigest(),
            "transcript_sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
            "asr_segments": asr_segments,
            "verified_complete_bytes": True,
        }
