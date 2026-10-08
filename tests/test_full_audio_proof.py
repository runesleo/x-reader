"""Whole-audio coverage proof must fail closed on range/size/duration/hash gaps."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from x_reader.cli import main as cli_main
from x_reader.evidence import build_receipt, classify_payload
from x_reader.fetchers.podcast import fetch_podcast
from x_reader.full_audio import (
    MAX_FULL_AUDIO_BYTES, _checked_range, _decode_entire_file,
    _download_complete_audio, _probe_local_duration, _verified_complete_download,
    transcribe_full_short,
)
from x_reader.reader import UniversalReader
from x_reader.schema import from_podcast

EPISODE = "https://www.xiaoyuzhoufm.com/episode/6a14e9dd3209346094186445"
CDN = "https://media.xyzcdn.net/public/test.mp3"
TRANSCRIPT = "这是一个完整的十三秒公开短播客转录测试。"


def valid_episode():
    proof = {
        "full_transcript": TRANSCRIPT,
        "has_transcript": True,
        "transcript_coverage": "full",
        "transcription_method": "local_whisper_tiny_cpu",
        "coverage_basis": "complete_encoded_bytes_and_full_decoded_pcm",
        "coverage_intervals": [{"start_seconds": 0.0, "end_seconds": 13.12}],
        "media_duration_seconds": 13.12,
        "decoded_duration_seconds": 13.12,
        "processed_seconds": 13.12,
        "coverage_ratio": 1.0,
        "audio_bytes": 97989,
        "media_sha256": "a" * 64,
        "media_url_sha256": "b" * 64,
        "transcript_sha256": hashlib.sha256(TRANSCRIPT.encode()).hexdigest(),
        "asr_segments": 4,
        "verified_complete_bytes": True,
    }
    return {"title": "每日极客资讯短集",
            "description": "这是公开的短播客节目简介。" * 6,
            "url": EPISODE, **proof}


class FullSourceRangeTests(unittest.TestCase):
    @staticmethod
    def response(status=206, *, total=10000, end=9999,
                 ctype="audio/mpeg", length="10000"):
        response = MagicMock()
        response.status = status
        response.headers = {
            "Content-Type": ctype,
            "Content-Range": f"bytes 0-{end}/{total}",
            "Content-Length": length,
        }
        return response

    def test_verified_range_accepts_exact_matching_total(self):
        self.assertEqual(_checked_range(self.response(), 0, 9999, 10000), 10000)

    def test_verified_range_rejects_wrong_status_and_response_type(self):
        for variant in (
            self.response(status=200),
            self.response(status=302),
            self.response(ctype="text/html"),
            self.response(total=10001),
            self.response(end=9000),
        ):
            with self.subTest(headers=variant.headers):
                with self.assertRaises(RuntimeError):
                    _checked_range(variant, 0, 9999, 10000)

    def test_verified_range_rejects_oversized_source_without_body_read(self):
        response = self.response(total=MAX_FULL_AUDIO_BYTES + 1, end=0, length="1")
        with self.assertRaisesRegex(RuntimeError, "2 MiB"):
            _checked_range(response, 0, 0)

    def test_exact_body_is_hashed_and_downloaded(self):
        response = self.response()
        response.read.return_value = b"a" * 10000
        pool = MagicMock()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "full.mp3"
            with patch("x_reader.full_audio._probe_total_bytes", return_value=(10000, '"etag"')):
                with patch("x_reader.full_audio._pinned_range",
                           return_value=(pool, response, "203.0.113.10")):
                    meta = _download_complete_audio(CDN, target)
            self.assertEqual(meta["audio_bytes"], 10000)
            self.assertEqual(target.stat().st_size, 10000)
            self.assertEqual(
                meta["media_sha256"], hashlib.sha256(b"a" * 10000).hexdigest()
            )
        pool.close.assert_called_once()
        response.release_conn.assert_called_once()

    def test_early_close_is_not_counted_as_full(self):
        response = self.response()
        response.read.side_effect = [b"a" * 4096, b""]
        pool = MagicMock()
        with tempfile.TemporaryDirectory() as folder:
            with patch("x_reader.full_audio._probe_total_bytes", return_value=(10000, '"etag"')):
                with patch("x_reader.full_audio._pinned_range",
                           return_value=(pool, response, "203.0.113.10")):
                    with self.assertRaisesRegex(RuntimeError, "truncated"):
                        _download_complete_audio(CDN, Path(folder) / "partial.mp3")

    def test_content_length_mismatch_rejected(self):
        response = self.response(length="9999")
        with tempfile.TemporaryDirectory() as folder:
            with patch("x_reader.full_audio._probe_total_bytes", return_value=(10000, '')):
                with patch("x_reader.full_audio._pinned_range",
                           return_value=(MagicMock(), response, "203.0.113.10")):
                    with self.assertRaisesRegex(RuntimeError, "Content-Length"):
                        _download_complete_audio(CDN, Path(folder) / "audio.mp3")

    def test_bounded_retry_can_recover_after_one_200_or_incomplete(self):
        with patch("x_reader.full_audio._download_complete_audio",
                   side_effect=[
                       RuntimeError("Audio CDN refused a verified HTTP 206 byte range"),
                       {"audio_bytes": 10000, "media_sha256": "a"*64},
                   ]) as get:
            with patch("x_reader.full_audio.time.sleep"):
                result = _verified_complete_download(CDN, Path("unused-audio.tmp"))
        self.assertEqual(get.call_count, 2)
        self.assertEqual(result["audio_bytes"], 10000)

    def test_bounded_retry_never_accepts_three_truncated_attempts(self):
        with patch("x_reader.full_audio._download_complete_audio",
                   side_effect=RuntimeError("Full audio body was truncated")) as get:
            with patch("x_reader.full_audio.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                    _verified_complete_download(CDN, Path("unused-audio.tmp"))
        self.assertEqual(get.call_count, 3)

    def test_bad_media_type_fails_immediately_without_retry(self):
        with patch("x_reader.full_audio._download_complete_audio",
                   side_effect=RuntimeError("Audio CDN returned non-audio content")) as get:
            with self.assertRaisesRegex(RuntimeError, "non-audio"):
                _verified_complete_download(CDN, Path("unused-audio.tmp"))
        get.assert_called_once()

    def test_source_over_120_seconds_fails(self):
        completed = subprocess.CompletedProcess([], 0, '{"format": {"duration": "121.5"}}', "")
        with patch("x_reader.full_audio.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "120-second"):
                _probe_local_duration(Path("source.audio"), "/tmp")


class LocalCompleteDecodeTests(unittest.TestCase):
    def test_full_local_decode_checks_untrimmed_duration(self):
        import wave
        def fake_ffmpeg(command, **_kwargs):
            self.assertNotIn("-t", command)
            self.assertEqual(
                command[command.index("-protocol_whitelist") + 1], "file"
            )
            with wave.open(command[-1], "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b"\x00\x00" * (16000 * 13))
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as folder:
            with patch("x_reader.full_audio.subprocess.run", side_effect=fake_ffmpeg):
                decoded = _decode_entire_file(
                    Path(folder) / "source.audio", Path(folder) / "all.wav",
                    folder, duration=13,
                )
        self.assertEqual(decoded, 13)

    def test_full_decode_rejects_shorter_pcm(self):
        import wave
        def fake_ffmpeg(command, **_kwargs):
            with wave.open(command[-1], "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b"\x00\x00" * (16000 * 5))
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as folder:
            with patch("x_reader.full_audio.subprocess.run", side_effect=fake_ffmpeg):
                with self.assertRaisesRegex(RuntimeError, "coverage"):
                    _decode_entire_file(
                        Path(folder) / "source.audio", Path(folder) / "short.wav",
                        folder, duration=13,
                    )


class FullAudioContractTests(unittest.TestCase):
    def test_proof_upgrades_only_verified_podcast_to_pass(self):
        episode = valid_episode()
        payload = from_podcast(episode).to_dict()
        receipt = build_receipt(payload)
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["reason_code"], "spoken_media_complete_verified")
        self.assertEqual(receipt["audio_bytes"], 97989)
        self.assertEqual(receipt["media_duration_seconds"], 13.12)
        self.assertEqual(receipt["coverage_ratio"], 1.0)
        self.assertEqual(payload["content"], TRANSCRIPT)
        self.assertEqual(receipt["transcript_sha256"],
                         hashlib.sha256(payload["content"].encode()).hexdigest())
        self.assertNotIn("content", receipt)
        self.assertNotIn("full_transcript", receipt)
        self.assertNotIn("media_url", receipt)
        self.assertNotIn("signed_cdn_url", receipt)

    def test_every_missing_integrity_field_reverts_full_claim_to_partial(self):
        base = from_podcast(valid_episode()).to_dict()
        invalid = {
            "missing_bytes": {"audio_bytes": 0},
            "bytes_too_large": {"audio_bytes": MAX_FULL_AUDIO_BYTES + 1},
            "wrong_hash": {"transcript_sha256": "0" * 64},
            "wrong_audio_hash": {"media_sha256": "invalid"},
            "wrong_uri_digest": {"media_url_sha256": ""},
            "no_fetched_bytes": {"verified_complete_bytes": False},
            "no_transcript": {"has_transcript": False},
            "unknown_method": {"transcription_method": "unknown"},
            "source_too_long": {"media_duration_seconds": 121.0},
            "decoded_short": {"decoded_duration_seconds": 10.0},
            "processed_short": {"processed_seconds": 6.0},
            "ratio_short": {"coverage_ratio": 0.6},
            "interval_gap": {"coverage_intervals": [
                {"start_seconds": 1.0, "end_seconds": 13.12}]},
            "interval_end_short": {"coverage_intervals": [
                {"start_seconds": 0.0, "end_seconds": 8.0}]},
            "no_asr_segments": {"asr_segments": 0},
            "wrong_basis": {"coverage_basis": "unknown"},
        }
        for name, changes in invalid.items():
            payload = json.loads(json.dumps(base))
            payload["extra"].update(changes)
            with self.subTest(name=name):
                classified = classify_payload(payload)
                self.assertEqual(classified["status"], "PARTIAL")
                self.assertEqual(classified["reason_code"],
                                 "full_media_proof_unverified")

    def test_preview_mode_never_upgrades_to_full(self):
        data = from_podcast({
            "title": "Short audio", "url": EPISODE,
            "description": "Show notes " * 20,
            "preview_transcript": "short audio sample",
            "preview_seconds": 12,
        }).to_dict()
        self.assertEqual(build_receipt(data)["status"], "PARTIAL")
        self.assertEqual(build_receipt(data)["reason_code"],
                         "spoken_media_preview_only")

    def test_transcribe_full_short_generates_bounded_provenance(self):
        class FakeWhisper:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs
            def transcribe(self, path, **kwargs):
                return iter([
                    types.SimpleNamespace(start=0, end=5, text="你好"),
                    types.SimpleNamespace(start=5, end=13.12, text="世界"),
                ]), types.SimpleNamespace(language="zh")

        with patch("x_reader.full_audio._public_episode_url", return_value=EPISODE):
            with patch("x_reader.full_audio._discover_audio", return_value=CDN):
                with patch("x_reader.full_audio._verified_complete_download",
                           return_value={"audio_bytes": 97989, "media_sha256": "a"*64}):
                    with patch("x_reader.full_audio._probe_local_duration", return_value=13.12):
                        with patch("x_reader.full_audio._decode_entire_file", return_value=13.12):
                            with patch.dict(sys.modules, {
                                "faster_whisper": types.SimpleNamespace(WhisperModel=FakeWhisper)
                            }):
                                r = transcribe_full_short(EPISODE, cache_dir="/tmp/test-media-cache")
        self.assertEqual(r["full_transcript"], "你好 世界")
        self.assertEqual(r["transcript_coverage"], "full")
        self.assertEqual(r["coverage_ratio"], 1)
        self.assertEqual(r["asr_segments"], 2)
        self.assertEqual(r["processed_seconds"], 13.12)
        self.assertTrue(r["verified_complete_bytes"])
        self.assertEqual(len(r["media_url_sha256"]), 64)

    def test_asr_segment_beyond_source_end_rejected(self):
        class BadWhisper:
            def __init__(self, *args, **kwargs):
                pass
            def transcribe(self, path, **kwargs):
                return iter([
                    types.SimpleNamespace(start=0, end=99, text="bad"),
                ]), types.SimpleNamespace(language="zh")

        with patch("x_reader.full_audio._public_episode_url", return_value=EPISODE):
            with patch("x_reader.full_audio._discover_audio", return_value=CDN):
                with patch("x_reader.full_audio._verified_complete_download",
                           return_value={"audio_bytes": 97989, "media_sha256": "a"*64}):
                    with patch("x_reader.full_audio._probe_local_duration", return_value=13.12):
                        with patch("x_reader.full_audio._decode_entire_file", return_value=13.12):
                            with patch.dict(sys.modules, {
                                "faster_whisper": types.SimpleNamespace(WhisperModel=BadWhisper)
                            }):
                                with self.assertRaisesRegex(RuntimeError, "timestamps"):
                                    transcribe_full_short(EPISODE)


class FullAudioReaderTests(unittest.IsolatedAsyncioTestCase):
    def test_constructor_rejects_mixed_modes(self):
        with self.assertRaisesRegex(ValueError, "cannot be enabled together"):
            UniversalReader(media_preview_seconds=12, full_short_audio=True)

    async def test_non_podcast_url_is_rejected_before_fetch(self):
        with patch("x_reader.reader.validate_url", return_value=None):
            with self.assertRaisesRegex(ValueError, "podcast episodes only"):
                await UniversalReader(full_short_audio=True).read("https://example.com")

    async def test_full_short_podcast_routes_to_correct_mode(self):
        with patch("x_reader.fetchers.podcast.fetch_podcast", new_callable=AsyncMock,
                   return_value=valid_episode()) as fetch:
            item = await UniversalReader(full_short_audio=True)._fetch("podcast", EPISODE)
        fetch.assert_awaited_once_with(EPISODE, full_short_audio=True)
        self.assertEqual(build_receipt(item.to_dict())["status"], "PASS")

    async def test_full_mode_rejects_multi_url_batch(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            await UniversalReader(full_short_audio=True).read_batch([EPISODE, EPISODE])

    async def test_full_audio_upgrade_failure_keeps_show_notes_partial(self):
        notes = {"title": "Short episode", "content": "Episode show notes " * 20,
                 "url": EPISODE, "fetch_method": "direct_html_pinned"}
        with patch("x_reader.fetchers.podcast.fetch_direct_html", return_value=notes):
            with patch("x_reader.full_audio.transcribe_full_short",
                       side_effect=RuntimeError("Exceeds limit")):
                result = await fetch_podcast(EPISODE, full_short_audio=True)
        self.assertEqual(result["transcript_coverage"], "none")
        self.assertEqual(result["full_error"], "RuntimeError")
        self.assertEqual(build_receipt(from_podcast(result).to_dict())["status"], "PARTIAL")

    def test_cli_explicit_full_short_flag_is_one_podcast(self):
        with patch.object(sys, "argv",
                          ["x-reader", EPISODE, "--media-full-short", "--json"]):
            with patch("x_reader.cli.cmd_fetch") as fetch:
                cli_main()
        fetch.assert_called_once_with([EPISODE], json_output=True, full_short_audio=True)

    def test_cli_legacy_command_is_unchanged(self):
        with patch.object(sys, "argv", ["x-reader", EPISODE, "--json"]):
            with patch("x_reader.cli.cmd_fetch") as fetch:
                cli_main()
        fetch.assert_called_once_with([EPISODE], json_output=True)

    def test_cli_refuses_full_with_preview(self):
        with patch.object(sys, "argv", [
            "x-reader", EPISODE, "--media-full-short",
            "--media-preview-seconds", "12", "--json",
        ]):
            with self.assertRaises(SystemExit) as failure:
                cli_main()
        self.assertEqual(failure.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
