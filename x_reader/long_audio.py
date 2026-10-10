"""Resumable, bounded full-source podcast audio ingestion (opt-in only).

Designed for public Xiaoyuzhou episodes on approved media CDN. Every byte
chunk is version-bound and SHA-256 checked; decoded PCM is divided into
contiguous fixed-length segments; ASR checkpoints are tied to PCM hashes.
No source audio, raw CDN URL, or account credentials are included in receipts.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import wave

from x_reader.full_audio import _probe_local_duration as _probe_short_duration
from x_reader.media_preview import (
    AUDIO_TYPES, _discover_audio, _public_episode_url, _tool_env,
    _validated_media_url,
)

MAX_LONG_AUDIO_BYTES = 64 * 1024 * 1024
MAX_LONG_AUDIO_SECONDS = 90 * 60
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
PCM_SEGMENT_SECONDS = 60
SAMPLE_RATE = 16000
MAX_SEGMENTS = 90
SHA256 = re.compile(r"[a-f0-9]{64}").fullmatch
_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_MODEL = "local_whisper_tiny_cpu"


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _private_directory(path: Path) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise RuntimeError("Unsafe long-media checkpoint directory")
    existed = path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("Unsafe long-media checkpoint directory")
    if existed:
        # Never chmod an existing user-selected path; it might be a valuable
        # shared workspace. Reject it instead if already accessible to others.
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RuntimeError("Unsafe long-media cache: existing directory is not private")
    else:
        os.chmod(path, 0o700)
    return path


def _write_private_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".pending")
    descriptor = os.open(
        str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(_canonical(payload))
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextmanager
def _locked_job(cache_root: str | None, episode: str, media_url: str):
    root = _private_directory(
        Path(cache_root or os.getenv("X_READER_LONG_CACHE_DIR") or
             (Path.home() / ".cache" / "x-reader" / "long-audio"))
    )
    identity = _digest((episode + "\n" + media_url).encode("utf-8"))[:32]
    job = _private_directory(root / identity)
    lockpath = job / ".lock"
    descriptor = os.open(
        str(lockpath), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield job
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _range_response(url: str, start: int, end: int, etag: str = ""):
    from x_reader.fetchers.jina import _request_pinned
    headers = {
        "Range": f"bytes={start}-{end}",
        "Accept": "audio/*,application/octet-stream",
        "User-Agent": "x-reader-long-audio/0.1",
    }
    if etag:
        headers["If-Range"] = etag
    return _request_pinned(_validated_media_url(url), timeout_seconds=20,
                           headers=headers)


def _checked_response(response, start: int, end: int,
                      expected_total: int | None = None,
                      expected_etag: str = "") -> tuple[int, str]:
    if response.status != 206:
        raise RuntimeError("Long-audio CDN did not honor HTTP 206 Range")
    mime = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if mime not in AUDIO_TYPES:
        raise RuntimeError("Long-audio CDN returned a non-audio body")
    cr = str(response.headers.get("Content-Range") or "").strip()
    match = _RANGE.fullmatch(cr)
    if not match:
        raise RuntimeError("Long-audio CDN Range could not be verified")
    actual_start, actual_end, total = [int(i) for i in match.groups()]
    if (actual_start, actual_end) != (start, end):
        raise RuntimeError("Long-audio CDN returned incorrect Range offset")
    if expected_total is not None and total != expected_total:
        raise RuntimeError("Long-audio source version changed size")
    if not 8192 <= total <= MAX_LONG_AUDIO_BYTES:
        raise RuntimeError("Long-audio source exceeds 64 MiB resource limit")
    etag = str(response.headers.get("ETag") or "").strip()
    if not etag or etag.startswith("W/") or len(etag) > 180:
        raise RuntimeError("Long-audio source lacks a strong ETag validator")
    if expected_etag and etag != expected_etag:
        raise RuntimeError("Long-audio source version changed ETag")
    clen = response.headers.get("Content-Length")
    if clen and (not str(clen).isdecimal() or int(clen) != end - start + 1):
        raise RuntimeError("Long-audio CDN returned incorrect Content-Length")
    return total, etag


def _probe(url: str) -> tuple[int, str]:
    pool, response, _ip = _range_response(url, 0, 0)
    try:
        return _checked_response(response, 0, 0)
    finally:
        response.release_conn()
        pool.close()


def _read_chunk(url: str, start: int, end: int,
                size: int, etag: str) -> bytes:
    last = None
    for attempt in range(3):
        pool = response = None
        try:
            pool, response, _ip = _range_response(url, start, end, etag)
            _checked_response(response, start, end, size, etag)
            desired = end - start + 1
            data = bytearray()
            deadline = time.monotonic() + 25
            while len(data) < desired:
                if time.monotonic() > deadline:
                    raise TimeoutError("Long-audio chunk exceeded deadline")
                part = response.read(min(65536, desired - len(data)))
                if not part:
                    raise RuntimeError("Long-audio byte chunk was truncated")
                data.extend(part)
            if len(data) != desired:
                raise RuntimeError("Long-audio byte chunk had wrong length")
            return bytes(data)
        except (Exception,) as exc:
            last = exc
            if attempt == 2:
                break
            time.sleep(0.2 * (attempt + 1))
        finally:
            if response is not None:
                response.release_conn()
            if pool is not None:
                pool.close()
    raise RuntimeError("Long-audio chunk retrieval failed after 3 attempts") from last


def _manifest_identity(episode: str, media_url: str, size: int, etag: str) -> dict:
    return {
        "format": "x-reader-long-audio-chunks-v1",
        "episode_sha256": _digest(episode.encode("utf-8")),
        "media_url_sha256": _digest(media_url.encode("utf-8")),
        "etag_sha256": _digest(etag.encode("utf-8")),
        "total_bytes": size,
        "chunk_bytes": DOWNLOAD_CHUNK_BYTES,
    }


def _read_json(path: Path) -> dict | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        # Chunk and 60s ASR checkpoints are tiny. Refuse oversized cached
        # JSON instead of loading an unbounded attacker/corruption payload.
        if path.stat().st_size > 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _chunk_path(job: Path, index: int) -> Path:
    return job / f"chunk-{index:04d}.bin"


def _write_chunk(path: Path, content: bytes) -> None:
    temporary = path.with_name(path.name + ".pending")
    descriptor = os.open(str(temporary),
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def fetch_chunked_source(episode: str, media_url: str, job: Path,
                         *, max_new_chunks: int | None = None) -> dict:
    """Persist and verify bounded ranges; optional interruption for canaries.

    No completion return is produced until every byte range has a verified
    hash and the entire source has been assembled and rehashed.
    """
    if max_new_chunks is not None and (
        type(max_new_chunks) is not int or max_new_chunks < 1
    ):
        raise ValueError("max_new_chunks must be a positive integer")
    _public_episode_url(episode)
    _validated_media_url(media_url)
    size, etag = _probe(media_url)
    identity = _manifest_identity(episode, media_url, size, etag)
    manifest_path = job / "chunks.json"
    manifest = _read_json(manifest_path) or dict(identity, chunks={})
    for field, expected in identity.items():
        if manifest.get(field) != expected:
            raise RuntimeError("Long-audio checkpoint belongs to a different source version")
    chunks = manifest.get("chunks")
    if not isinstance(chunks, dict):
        raise RuntimeError("Invalid saved long-audio chunk manifest")

    count = math.ceil(size / DOWNLOAD_CHUNK_BYTES)
    added = 0
    root_hasher = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix="xreader-assemble-") as folder:
        assembled = Path(folder) / "source.audio"
        with assembled.open("wb") as destination:
            for index in range(count):
                start = index * DOWNLOAD_CHUNK_BYTES
                end = min(size, start + DOWNLOAD_CHUNK_BYTES) - 1
                name = str(index)
                path = _chunk_path(job, index)
                expected_length = end - start + 1
                cached = chunks.get(name)
                valid = False
                if isinstance(cached, dict) and path.is_file() and not path.is_symlink():
                    if path.stat().st_size == expected_length:
                        raw = path.read_bytes()
                        valid = (
                            _digest(raw) == cached.get("sha256")
                            and cached.get("start") == start
                            and cached.get("end") == end
                        )
                if not valid:
                    if max_new_chunks is not None and added >= max_new_chunks:
                        raise RuntimeError(
                            "Long-audio download interrupted after bounded chunk budget; "
                            "verified chunks remain cached for resumption"
                        )
                    raw = _read_chunk(media_url, start, end, size, etag)
                    _write_chunk(path, raw)
                    chunks[name] = {
                        "start": start, "end": end,
                        "sha256": _digest(raw),
                    }
                    _write_private_json(manifest_path, manifest)
                    added += 1
                destination.write(raw)
                root_hasher.update(raw)
        if assembled.stat().st_size != size:
            raise RuntimeError("Long-audio reassembly did not contain all source bytes")

    return {
        "media_sha256": root_hasher.hexdigest(),
        "media_url_sha256": identity["media_url_sha256"],
        "etag_sha256": identity["etag_sha256"],
        "audio_bytes": size,
        "chunk_count": count,
        "new_chunks": added,
        "reused_chunks": count - added,
        "chunk_manifest_sha256": _digest(_canonical(manifest["chunks"])),
        "chunk_manifest": manifest["chunks"],
    }


def _assemble_into(job: Path, chunk_count: int, target: Path) -> int:
    count = 0
    with target.open("wb") as out:
        for index in range(chunk_count):
            path = _chunk_path(job, index)
            with path.open("rb") as src:
                while True:
                    data = src.read(65536)
                    if not data:
                        break
                    out.write(data)
                    count += len(data)
    return count


def _probe_long_duration(source: Path, home: str) -> float:
    # Reuse the same ffprobe subprocess isolation, with a longer limit.
    result = subprocess.run([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file",
        "-show_entries", "format=duration", "-of", "json", str(source),
    ], capture_output=True, text=True, timeout=20, env=_tool_env(home))
    if result.returncode:
        raise RuntimeError("Cannot establish long-audio source duration")
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Invalid long-audio duration metadata") from exc
    if not math.isfinite(duration) or not 0 < duration <= MAX_LONG_AUDIO_SECONDS:
        raise RuntimeError("Long audio exceeds 90-minute source budget")
    return duration


def _decode_segments(source: Path, home: str, duration: float):
    """Yield contiguous WAV segments while streaming the whole local audio.

    No remote protocol or full decoded PCM buffer is used. Caller must
    consume the iterator fully; then we verify total decoded PCM duration.
    """
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
        "-protocol_whitelist", "file", "-i", str(source),
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1",
        "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=_tool_env(home),
    )
    if process.stdout is None:
        raise RuntimeError("Unable to open local decoded audio stream")
    bytes_per_frame = 2
    segment_frames = PCM_SEGMENT_SECONDS * SAMPLE_RATE
    total_frames = 0
    try:
        index = 0
        while True:
            with tempfile.NamedTemporaryFile(
                prefix="xreader-pcm-segment-", suffix=".wav", dir=home,
                delete=False,
            ) as tmp:
                path = Path(tmp.name)
            copied = 0
            hasher = hashlib.sha256()
            try:
                with wave.open(str(path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(SAMPLE_RATE)
                    while copied < segment_frames:
                        requested = min(65536, (segment_frames - copied) * bytes_per_frame)
                        part = process.stdout.read(requested)
                        if not part:
                            break
                        if len(part) % bytes_per_frame:
                            raise RuntimeError("Unaligned audio PCM byte stream")
                        frames = len(part) // bytes_per_frame
                        copied += frames
                        total_frames += frames
                        if total_frames > (MAX_LONG_AUDIO_SECONDS + 2) * SAMPLE_RATE:
                            raise RuntimeError("Decoded PCM exceeded long-audio duration cap")
                        wav.writeframesraw(part)
                        hasher.update(part)
                if copied == 0:
                    path.unlink(missing_ok=True)
                    break
                start = (total_frames - copied) / SAMPLE_RATE
                end = total_frames / SAMPLE_RATE
                index += 1
                if index > MAX_SEGMENTS:
                    raise RuntimeError("Too many decoded ASR segments")
                yield {
                    "index": index - 1,
                    "start_seconds": round(start, 4),
                    "end_seconds": round(end, 4),
                    "sample_frames": copied,
                    "pcm_sha256": hasher.hexdigest(),
                    "wav_path": str(path),
                }
            finally:
                path.unlink(missing_ok=True)
        status = process.wait(timeout=45)
        if status:
            raise RuntimeError("Long-audio ffmpeg decode failed")
        decoded_seconds = total_frames / SAMPLE_RATE
        if abs(decoded_seconds - duration) > max(0.75, duration * 0.01):
            raise RuntimeError("Long-audio decoded timeline does not match source duration")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def _transcribe_segment(model, segment: dict) -> dict:
    chunks, _meta = model.transcribe(
        segment["wav_path"], beam_size=1, vad_filter=False,
        condition_on_previous_text=False,
    )
    utterances = []
    boundary_adjustments = 0
    max_boundary_overrun = 0.0
    span = segment["end_seconds"] - segment["start_seconds"]
    # Whisper occasionally predicts the final utterance ending just beyond
    # a 60-second PCM window, even though no audio exists beyond that window.
    # A small *end-of-window* timestamp anomaly is not missing source audio.
    # Never accept a new utterance that starts after PCM ends, or an
    # arbitrarily extended recognition interval.
    allowed_overrun = min(2.0, max(0.5, span * 0.035))
    for part in chunks:
        start = float(part.start)
        end = float(part.end)
        if not (math.isfinite(start) and math.isfinite(end)) or (
            start < -0.5 or end < start or start >= span
        ):
            raise RuntimeError("ASR timestamps outside decoded audio segment")
        if end > span:
            overrun = end - span
            if (
                overrun > allowed_overrun
                or start < max(0.0, span - 10.0)
                or boundary_adjustments >= 1
            ):
                raise RuntimeError("ASR timestamps outside decoded audio segment")
            boundary_adjustments += 1
            max_boundary_overrun = overrun
            # The logical evidence interval still ends at the physical PCM
            # boundary. Preserve the recognized text but record the anomaly.
            end = span
        text = " ".join(str(part.text or "").split())
        if text:
            utterances.append(text)
    return {
        "index": segment["index"],
        "start_seconds": segment["start_seconds"],
        "end_seconds": segment["end_seconds"],
        "sample_frames": segment["sample_frames"],
        "pcm_sha256": segment["pcm_sha256"],
        "transcript": " ".join(utterances),
        "asr_segment_count": len(utterances),
        "asr_boundary_adjustments": boundary_adjustments,
        "max_boundary_overrun_seconds": round(max_boundary_overrun, 4),
    }


def _segment_checkpoint(job: Path, source_hash: str, segment: dict, model) -> tuple[dict, bool]:
    index = segment["index"]
    path = job / f"asr-{index:04d}.json"
    saved = _read_json(path)
    identity = {
        "format": "x-reader-long-asr-v1",
        "source_sha256": source_hash,
        "model": _MODEL,
        "index": index,
        "start_seconds": segment["start_seconds"],
        "end_seconds": segment["end_seconds"],
        "sample_frames": segment["sample_frames"],
        "pcm_sha256": segment["pcm_sha256"],
    }
    if saved and all(saved.get(k) == v for k, v in identity.items()):
        transcript = saved.get("transcript")
        if (
            isinstance(transcript, str)
            and saved.get("transcript_sha256") == _digest(transcript.encode("utf-8"))
            and type(saved.get("asr_segment_count")) is int
            and saved["asr_segment_count"] >= 0
        ):
            return saved, True
    result = _transcribe_segment(model, segment)
    record = dict(identity, **result,
                  transcript_sha256=_digest(result["transcript"].encode("utf-8")))
    _write_private_json(path, record)
    return record, False


def transcribe_long(url: str, cache_dir: str | None = None,
                    model_cache_dir: str | None = None) -> dict:
    """Explicit public long-audio ingestion with resumable chunks/checkpoints."""
    public_episode = _public_episode_url(url)
    with tempfile.TemporaryDirectory(prefix="xreader-long-discovery-") as home:
        media_url = _discover_audio(public_episode, home)
    with _locked_job(cache_dir, public_episode, media_url) as job:
        transfer = fetch_chunked_source(public_episode, media_url, job)
        with tempfile.TemporaryDirectory(prefix="xreader-long-work-") as home:
            source_file = Path(home) / "full_source.audio"
            actual_bytes = _assemble_into(job, transfer["chunk_count"], source_file)
            if actual_bytes != transfer["audio_bytes"]:
                raise RuntimeError("Chunk assembly total did not match source")
            if _digest(source_file.read_bytes()) != transfer["media_sha256"]:
                raise RuntimeError("Assembled audio hash did not match source proof")
            duration = _probe_long_duration(source_file, home)
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    'Install local ASR: pip install "x-reader[media]"'
                ) from exc
            model_cache = Path(model_cache_dir or os.getenv("X_READER_ASR_CACHE_DIR")
                               or (Path.home() / ".cache" / "x-reader" / "asr"))
            model = WhisperModel(
                "tiny", device="cpu", compute_type="int8",
                cpu_threads=2, num_workers=1, download_root=str(model_cache),
            )
            segment_records = []
            cache_hits = 0
            for seg in _decode_segments(source_file, home, duration):
                record, reused = _segment_checkpoint(
                    job, transfer["media_sha256"], seg, model
                )
                segment_records.append(record)
                cache_hits += int(reused)

            if not segment_records:
                raise RuntimeError("No audio PCM segments decoded")
            if sum(i["asr_segment_count"] for i in segment_records) < 1:
                raise RuntimeError("No transcribable speech found in long audio")

            # One transcript line per PCM segment, including blank lines for
            # silence. This lets the receipt recompute per-segment hashes.
            transcript = "\n".join(i["transcript"] for i in segment_records)
            frames = sum(i["sample_frames"] for i in segment_records)
            processed = frames / SAMPLE_RATE
            if abs(processed - duration) > max(0.75, duration * 0.01):
                raise RuntimeError("ASR interval coverage missed decoded duration")
            proof_segments = [
                {
                    "index": item["index"],
                    "start_seconds": item["start_seconds"],
                    "end_seconds": item["end_seconds"],
                    "sample_frames": item["sample_frames"],
                    "pcm_sha256": item["pcm_sha256"],
                    "transcript_sha256": item["transcript_sha256"],
                    "transcript_chars": len(item["transcript"]),
                    "asr_segment_count": item["asr_segment_count"],
                    "asr_boundary_adjustments": item.get("asr_boundary_adjustments", 0),
                    "max_boundary_overrun_seconds": item.get("max_boundary_overrun_seconds", 0.0),
                }
                for item in segment_records
            ]
            return {
                "full_transcript": transcript,
                "has_transcript": True,
                "transcript_coverage": "full",
                "transcription_method": _MODEL,
                "coverage_basis": "verified_chunk_manifest_and_contiguous_pcm_asr",
                "coverage_intervals": [
                    {"start_seconds": p["start_seconds"], "end_seconds": p["end_seconds"]}
                    for p in proof_segments
                ],
                "media_duration_seconds": round(duration, 4),
                "decoded_duration_seconds": round(processed, 4),
                "processed_seconds": round(processed, 4),
                "coverage_ratio": 1.0,
                "audio_bytes": transfer["audio_bytes"],
                "media_sha256": transfer["media_sha256"],
                "media_url_sha256": transfer["media_url_sha256"],
                "transcript_sha256": _digest(transcript.encode("utf-8")),
                "asr_segments": sum(i["asr_segment_count"] for i in segment_records),
                "asr_boundary_adjustments": sum(
                    i["asr_boundary_adjustments"] for i in proof_segments
                ),
                "max_boundary_overrun_seconds": max(
                    i["max_boundary_overrun_seconds"] for i in proof_segments
                ),
                "verified_complete_bytes": True,
                "chunk_count": transfer["chunk_count"],
                "chunk_manifest_sha256": transfer["chunk_manifest_sha256"],
                "chunks_manifest": transfer["chunk_manifest"],
                "segment_count": len(proof_segments),
                "segments_manifest": proof_segments,
                "segments_manifest_sha256": _digest(_canonical(proof_segments)),
                "reused_source_chunks": transfer["reused_chunks"],
                "reused_asr_segments": cache_hits,
            }
