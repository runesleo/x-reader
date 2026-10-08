"""No-key media-preview correctness, opt-in boundaries and podcast source contract."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from x_reader.cli import parse_media_preview_option
from x_reader.evidence import build_receipt, classify_payload
from x_reader.fetchers.podcast import fetch_podcast
from x_reader.media_preview import (
    _discover_audio, _download_media_prefix, _tool_env, _validated_media_url,
    transcribe_preview,
)
from x_reader.reader import UniversalReader
from x_reader.schema import MediaType, SourceType, UnifiedContent, from_podcast

EPISODE = "https://www.xiaoyuzhoufm.com/episode/6abb9b69195d838e2aeb9c2b"
AUDIO = "https://media.xyzcdn.net/some/public-episode.m4a"
HTML = {
    "title": "第一财经 · AI 商业化",
    "content": "这是公开节目的页面简介与时间线，不包含任何实际音频转录。" * 8,
    "url": EPISODE,
    "fetch_method": "direct_html_pinned",
}


class MediaPreviewSecurityTests(unittest.TestCase):
    def test_audio_origin_allowlist_is_strict(self):
        for url in (
            "https://127.0.0.1/private",
            "http://media.xyzcdn.net/audio.m4a",
            "https://media.xyzcdn.net.evil.example/audio.m4a",
            "https://foo:bar@media.xyzcdn.net/audio.m4a",
            "https://media.xyzcdn.net:444/audio.m4a",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    _validated_media_url(url)

    def test_approved_audio_domain_is_publicly_resolved(self):
        with patch("x_reader.media_preview.resolve_safe_ips", return_value=["203.0.113.12"]) as dns:
            self.assertEqual(_validated_media_url(AUDIO), AUDIO)
            dns.assert_called_once()

    def test_isolated_subprocess_environment_does_not_inherit_keys(self):
        with patch.dict("os.environ", {"PATH": "/usr/bin", "HF_TOKEN": "secret", "TG_API_HASH": "secret"}):
            env = _tool_env("/temporary/path")
        self.assertEqual(env["HOME"], "/temporary/path")
        self.assertNotIn("HF_TOKEN", env)
        self.assertNotIn("TG_API_HASH", env)

    def test_preview_rejects_zero_or_long_duration_before_network(self):
        for seconds in (0, 31, -1, True, 2.5):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    transcribe_preview(EPISODE, seconds)

    def test_locator_rejects_unapproved_audio_without_ffmpeg(self):
        info = {"url": "https://internal.example/metadata"}
        mock_result = subprocess.CompletedProcess([], 0, json.dumps(info), "")
        with patch("x_reader.media_preview.subprocess.run", return_value=mock_result) as call:
            with self.assertRaisesRegex(ValueError, "approved public CDN"):
                _discover_audio(EPISODE, "/tmp/example")
        self.assertEqual(call.call_count, 1)

    def test_pinned_bounded_audio_range_fetch_rejects_redirect(self):
        from unittest.mock import MagicMock
        import tempfile

        response = MagicMock()
        response.status = 302
        response.headers = {"Location": "http://127.0.0.1/private"}
        pool = MagicMock()
        with tempfile.TemporaryDirectory() as temp:
            with patch("x_reader.media_preview._validated_media_url", return_value=AUDIO):
                with patch("x_reader.fetchers.jina._request_pinned", return_value=(pool, response, "203.0.113.12")):
                    with self.assertRaisesRegex(RuntimeError, "byte-range"):
                        _download_media_prefix(AUDIO, Path(temp) / "source.m4a")
        response.release_conn.assert_called_once()
        pool.close.assert_called_once()

    def test_pinned_bounded_audio_range_fetch_accepts_only_audio(self):
        from unittest.mock import MagicMock
        import tempfile

        response = MagicMock()
        response.status = 206
        response.headers = {"Content-Type": "audio/mp4", "Content-Range": "bytes 0-16383/4000000"}
        response.read.side_effect = [b"a" * 16384, b""]
        pool = MagicMock()
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "source.m4a"
            with patch("x_reader.media_preview._validated_media_url", return_value=AUDIO):
                with patch("x_reader.fetchers.jina._request_pinned", return_value=(pool, response, "203.0.113.12")) as req:
                    size = _download_media_prefix(AUDIO, target)
            self.assertEqual(size, 16384)
            self.assertEqual(target.stat().st_size, size)
        req.assert_called_once()
        response.release_conn.assert_called_once()
        pool.close.assert_called_once()

    def test_pinned_bounded_audio_range_fetch_rejects_html(self):
        from unittest.mock import MagicMock
        import tempfile

        response = MagicMock()
        response.status = 206
        response.headers = {"Content-Type": "text/html", "Content-Range": "bytes 0-16383/4000000"}
        pool = MagicMock()
        with tempfile.TemporaryDirectory() as temp:
            with patch("x_reader.media_preview._validated_media_url", return_value=AUDIO):
                with patch("x_reader.fetchers.jina._request_pinned", return_value=(pool, response, "203.0.113.12")):
                    with self.assertRaisesRegex(RuntimeError, "non-audio"):
                        _download_media_prefix(AUDIO, Path(temp) / "source.m4a")

    def test_bounded_audio_preview_is_explicit_and_not_full_coverage(self):
        commands = []
        def fake_run(command, **kwargs):
            commands.append(command)
            if command[0] == "yt-dlp":
                return subprocess.CompletedProcess(command, 0, json.dumps({"url": AUDIO}), "")
            if command[0] == "ffmpeg":
                Path(command[-1]).write_bytes(b"RIFF" + b"\x00" * 512)
                return subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError("unexpected process")

        class FakeModel:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs

            def transcribe(self, path, **kwargs):
                return iter([types.SimpleNamespace(text="节目音频片段的真实机器转录")]), types.SimpleNamespace(language="zh")

        def fake_download(_url, target):
            Path(target).write_bytes(b"compressed-media-prefix" * 1200)
            return Path(target).stat().st_size

        with patch("x_reader.media_preview._public_episode_url", return_value=EPISODE):
            with patch("x_reader.media_preview.resolve_safe_ips", return_value=["203.0.113.12"]):
                with patch("x_reader.media_preview._download_media_prefix", side_effect=fake_download):
                    with patch("x_reader.media_preview.subprocess.run", side_effect=fake_run):
                        with patch.dict(sys.modules, {"faster_whisper": types.SimpleNamespace(WhisperModel=FakeModel)}):
                            output = transcribe_preview(EPISODE, 12, cache_dir="/tmp/xreader-test-cache")
        self.assertEqual(output["preview_seconds"], 12)
        self.assertEqual(output["transcript_coverage"], "preview")
        self.assertEqual(output["transcription_method"], "local_whisper_tiny_cpu")
        self.assertIn("真实机器转录", output["text"])
        self.assertEqual(len(commands), 2)
        self.assertIn("-t", commands[1])
        self.assertEqual(commands[1][commands[1].index("-t") + 1], "12")
        self.assertEqual(commands[1][commands[1].index("-protocol_whitelist") + 1], "file")
        self.assertNotIn(AUDIO, commands[1])
        self.assertTrue(commands[1][commands[1].index("-i") + 1].endswith("source.m4a"))
        self.assertNotIn("--cookies-from-browser", commands[0])
        self.assertIn("--ignore-config", commands[0])


class PodcastContractTests(unittest.IsolatedAsyncioTestCase):
    def test_detects_xiaoyuzhou_and_apple_podcasts(self):
        reader = UniversalReader()
        self.assertEqual(reader._detect_platform(EPISODE), "podcast")
        self.assertEqual(
            reader._detect_platform("https://podcasts.apple.com/us/podcast/example/id123"),
            "podcast",
        )

    def test_media_preview_constructor_bounds(self):
        for value in (-1, 31, True, "10"):
            with self.assertRaises(ValueError):
                UniversalReader(media_preview_seconds=value)
        self.assertEqual(UniversalReader(media_preview_seconds=12).media_preview_seconds, 12)

    async def test_default_podcast_read_does_not_invoke_audio_tools(self):
        with patch("x_reader.fetchers.podcast.fetch_direct_html", return_value=HTML):
            with patch("x_reader.media_preview.transcribe_preview") as audio:
                data = await fetch_podcast(EPISODE)
                audio.assert_not_called()
        payload = from_podcast(data).to_dict()
        self.assertEqual(payload["source_type"], "podcast")
        self.assertEqual(payload["media_type"], "audio")
        self.assertFalse(payload["extra"]["has_transcript"])
        self.assertEqual(payload["extra"]["transcript_coverage"], "none")
        receipt = build_receipt(payload)
        self.assertEqual(receipt["status"], "PARTIAL")
        self.assertEqual(receipt["reason_code"], "spoken_media_unread")
        self.assertEqual(receipt["components"]["spoken_media"], "PARTIAL")

    async def test_preview_remains_partial_and_is_marked_in_content(self):
        clip = {"text": "这是首十二秒真实音频转录文本", "preview_seconds": 12,
                "transcription_method": "local_whisper_tiny_cpu",
                "transcript_coverage": "preview"}
        with patch("x_reader.fetchers.podcast.fetch_direct_html", return_value=HTML):
            with patch("x_reader.media_preview.transcribe_preview", return_value=clip) as audio:
                data = await fetch_podcast(EPISODE, preview_seconds=12)
                audio.assert_called_once_with(EPISODE, 12)
        payload = from_podcast(data).to_dict()
        self.assertIn("NOT a full-episode transcript", payload["content"])
        self.assertIn(clip["text"], payload["content"])
        self.assertFalse(payload["extra"]["has_transcript"])
        receipt = build_receipt(payload)
        self.assertEqual(receipt["status"], "PARTIAL")
        self.assertEqual(receipt["reason_code"], "spoken_media_preview_only")
        self.assertEqual(receipt["transcript_coverage"], "preview")
        self.assertEqual(receipt["preview_seconds"], 12)
        self.assertNotIn("content", receipt)
        self.assertNotIn(clip["text"], json.dumps(receipt, ensure_ascii=False))

    async def test_failed_optional_preview_preserves_metadata_without_pass(self):
        with patch("x_reader.fetchers.podcast.fetch_direct_html", return_value=HTML):
            with patch("x_reader.media_preview.transcribe_preview", side_effect=RuntimeError("no media")):
                data = await fetch_podcast(EPISODE, preview_seconds=12)
        self.assertEqual(data["transcript_coverage"], "none")
        self.assertEqual(data["preview_error"], "RuntimeError")
        receipt = build_receipt(from_podcast(data).to_dict())
        self.assertEqual(receipt["status"], "PARTIAL")

    async def test_reader_routes_podcast_to_dedicated_fetcher(self):
        with patch("x_reader.fetchers.podcast.fetch_podcast", new_callable=AsyncMock,
                   return_value={"title": "Episode", "description": "Long show notes " * 8, "url": EPISODE} ) as fetch:
            item = await UniversalReader(media_preview_seconds=12)._fetch("podcast", EPISODE)
        fetch.assert_awaited_once_with(EPISODE, preview_seconds=12)
        self.assertEqual(item.source_type, SourceType.PODCAST)

    async def test_apple_preview_never_runs_audio_pipeline(self):
        apple = "https://podcasts.apple.com/us/podcast/example/id123"
        metadata = dict(HTML, url=apple)
        with patch("x_reader.fetchers.podcast.fetch_direct_html", return_value=metadata):
            with patch("x_reader.media_preview.transcribe_preview") as audio:
                data = await fetch_podcast(apple, preview_seconds=12)
                audio.assert_not_called()
        self.assertEqual(data["preview_error"], "preview_unsupported_for_this_podcast_host")

    def test_cli_preview_is_explicit_and_validated(self):
        self.assertEqual(parse_media_preview_option([EPISODE, "--json"]), ([EPISODE, "--json"], 0))
        self.assertEqual(
            parse_media_preview_option([EPISODE, "--media-preview-seconds", "12", "--json"]),
            ([EPISODE, "--json"], 12),
        )
        for args in (
            [EPISODE, "--media-preview-seconds"],
            [EPISODE, "--media-preview-seconds", "31"],
            [EPISODE, "--media-preview-seconds", "0"],
            [EPISODE, "--media-preview-seconds", "nan"],
        ):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    parse_media_preview_option(args)

    def test_podcast_roundtrip_in_unified_content(self):
        item = from_podcast({"title":"Episode", "description":"Long show notes " * 8,"url":EPISODE})
        restored = UnifiedContent.from_dict(item.to_dict())
        self.assertEqual(restored.source_type, SourceType.PODCAST)
        self.assertEqual(restored.media_type, MediaType.AUDIO)

    def test_even_false_full_transcript_flag_does_not_upgrade_to_pass(self):
        payload = from_podcast({"title":"Episode", "description":"Show notes " * 20,"url":EPISODE}).to_dict()
        payload["extra"]["has_transcript"] = True
        payload["extra"]["transcript_coverage"] = "preview"
        self.assertEqual(classify_payload(payload)["status"], "PARTIAL")


if __name__ == "__main__":
    unittest.main()
