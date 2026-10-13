import asyncio
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from x_reader import routing, web_app
from x_reader.routing import detect_platform
from x_reader.utils.proc import run_bounded

ROOT = Path(__file__).resolve().parents[1]


class RoutingTest(unittest.TestCase):
    def test_exact_hosts_not_substrings(self):
        self.assertEqual(detect_platform("https://x.com/a/status/1"), "twitter")
        self.assertEqual(detect_platform("https://mobile.twitter.com/a/status/1"), "twitter")
        self.assertEqual(detect_platform("https://dropbox.com/s/abc"), "generic")
        self.assertEqual(detect_platform("https://pmt.me/page"), "generic")
        self.assertEqual(detect_platform("https://t.me/s/channel/12"), "telegram")

    def test_feed_paths_by_segment(self):
        self.assertEqual(detect_platform("https://example.com/feed"), "rss")
        self.assertEqual(detect_platform("https://example.com/blog/rss.xml"), "rss")
        self.assertEqual(detect_platform("https://example.com/feedback"), "generic")

    def test_hosted_policy_and_reader_agree(self):
        with self.assertRaises(web_app.HostedPolicyError):
            web_app.read_public_url("https://attacker.example/feed", runner=lambda u: None, validator=lambda u: u)


class HostedChildTest(unittest.TestCase):
    def test_child_env_is_hosted_and_drops_proxies_and_secrets(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://proxy", "GROQ_API_KEY": "k", "OUTPUT_DIR": "/x"}):
            env = web_app._minimal_child_env(Path("/tmp/h"), Path("/tmp/h/inbox.json"))
        self.assertEqual(env["X_READER_HOSTED"], "1")
        self.assertEqual(env["X_READER_ALLOWED_PLATFORMS"], "generic,twitter,youtube")
        for key in ("HTTP_PROXY", "GROQ_API_KEY", "OUTPUT_DIR"):
            self.assertNotIn(key, env)

    def _dotenv_calls(self, hosted: bool) -> int:
        code = (
            "import dotenv; calls=[]; dotenv.load_dotenv=lambda *a, **k: calls.append(1); "
            "import x_reader.cli; print(len(calls))"
        )
        env = {k: v for k, v in os.environ.items() if k != "X_READER_HOSTED"}
        if hosted:
            env["X_READER_HOSTED"] = "1"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        return int(out.stdout.strip().splitlines()[-1])

    def test_real_subprocess_skips_dotenv_only_in_hosted_mode(self):
        self.assertEqual(self._dotenv_calls(hosted=True), 0)
        self.assertEqual(self._dotenv_calls(hosted=False), 1)

    def test_reader_refuses_platforms_outside_the_allowlist(self):
        from x_reader.reader import UniversalReader

        with patch.dict(os.environ, {"X_READER_ALLOWED_PLATFORMS": "twitter,youtube,generic"}), \
                patch("x_reader.reader.validate_url", side_effect=lambda u: u):
            with self.assertRaises(ValueError):
                asyncio.run(UniversalReader().read("https://example.com/feed"))

    def test_hosted_reads_never_persist(self):
        from x_reader.reader import UniversalReader
        from x_reader.schema import SourceType, UnifiedContent

        item = UnifiedContent(source_type=SourceType.MANUAL, source_name="e", title="t", content="c", url="https://e.com")
        inbox = unittest.mock.MagicMock()
        with patch.dict(os.environ, {"X_READER_HOSTED": "1"}), \
                patch("x_reader.reader.validate_url", side_effect=lambda u: u), \
                patch.object(UniversalReader, "_fetch", AsyncMock(return_value=item)), \
                patch("x_reader.utils.storage.save_to_markdown") as save_md:
            asyncio.run(UniversalReader(inbox=inbox).read("https://e.com/a"))
        inbox.add.assert_not_called()
        save_md.assert_not_called()


class ProcessTreeTest(unittest.TestCase):
    def test_timeout_kills_grandchildren(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "grandchild-survived"
            script = f"(sleep 2; touch '{marker}') & sleep 30"
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_bounded(["sh", "-c", script], timeout=0.5)
            self.assertLess(time.monotonic() - started, 5)
            time.sleep(2.5)
            self.assertFalse(marker.exists())

    def test_output_budget(self):
        with self.assertRaises(ValueError):
            run_bounded([sys.executable, "-c", "print('x' * 5000)"], timeout=10, max_output_bytes=1000)


class SafeFetchTest(unittest.TestCase):
    def test_private_targets_are_refused_before_connecting(self):
        from x_reader.fetchers.jina import fetch_public_bytes
        from x_reader.fetchers.rss import FEED_TYPES

        for url in ("http://127.0.0.1/feed", "http://169.254.169.254/latest", "http://100.100.100.100/feed"):
            with self.assertRaises(ValueError, msg=url):
                fetch_public_bytes(url, FEED_TYPES)

    def test_rss_parses_downloaded_bytes_and_keeps_full_body(self):
        from x_reader.fetchers import rss

        xml = (b'<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
               b"<channel><title>F</title><item><title>A</title><link>https://e.com/a</link>"
               b"<description>teaser</description><content:encoded>full body</content:encoded></item>"
               b"<item><title>B</title><link>https://e.com/b</link><description>only teaser</description></item>"
               b"</channel></rss>")
        with patch.object(rss, "fetch_public_bytes", return_value=(xml, "application/rss+xml", "https://e.com/feed")) as fetch:
            items = asyncio.run(rss.fetch_rss("https://e.com/feed"))
        fetch.assert_called_once()
        self.assertEqual(items[0]["content_kind"], "full")
        self.assertEqual(items[0]["summary"], "full body")
        self.assertEqual(items[1]["content_kind"], "summary")


class TelegramRoutingTest(unittest.TestCase):
    def test_message_urls_fetch_that_exact_message(self):
        from x_reader.reader import UniversalReader

        msg = {"text": "hi", "url": "https://t.me/chan/123", "message_id": 123, "media_status": "none"}
        for url in ("https://t.me/chan/123", "https://t.me/s/chan/123"):
            with patch("x_reader.fetchers.telegram.fetch_telegram", AsyncMock(return_value=[msg])) as fetch:
                content = asyncio.run(UniversalReader()._fetch("telegram", url))
            fetch.assert_awaited_once_with("chan", limit=1, message_id=123)
            self.assertEqual(content.extra["message_id"], 123)
            self.assertEqual(content.extra["media_status"], "none")


class YoutubeCoverageInputsTest(unittest.TestCase):
    def test_srt_end_time_and_info_duration(self):
        from x_reader.fetchers import youtube

        with tempfile.TemporaryDirectory() as tmp:
            srt = Path(tmp) / "s.srt"
            srt.write_text("1\n00:00:01,000 --> 00:00:04,500\nhello\n\n2\n00:09:50,000 --> 00:09:58,250\nbye\n")
            text, end = youtube._parse_srt(str(srt))
            self.assertEqual(text, "hello bye")
            self.assertAlmostEqual(end, 598.25)
            info = Path(tmp) / "s.info.json"
            info.write_text(json.dumps({"duration": 600}))
            self.assertEqual(youtube._read_duration(str(info)), 600.0)
            info.write_text(json.dumps({"duration": "NaN"}))
            self.assertIsNone(youtube._read_duration(str(info)))

    def test_yt_dlp_calls_skip_playlists_and_cap_downloads(self):
        from x_reader.fetchers import youtube

        seen = []

        def fake_run(cmd, timeout, **_kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("x_reader.utils.proc.run_bounded", side_effect=fake_run), \
                patch("x_reader.utils.url_validator.validate_url", side_effect=lambda u: u), \
                patch.dict(os.environ, {"GROQ_API_KEY": "k"}):
            youtube._get_subtitles_via_ytdlp("https://youtu.be/abcdefghijk")
            youtube._transcribe_via_whisper("https://youtu.be/abcdefghijk")
        self.assertIn("--no-playlist", seen[0])
        self.assertIn("--no-playlist", seen[1])
        self.assertIn("--max-filesize", seen[1])


class AudioBoundsTest(unittest.TestCase):
    def test_short_decode_caps_output_size(self):
        from x_reader import full_audio

        captured = {}

        def fake_run(cmd, **_kw):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 1, "", "")

        with patch("x_reader.full_audio.subprocess.run", side_effect=fake_run), self.assertRaises(RuntimeError):
            full_audio._decode_entire_file(Path("/nonexistent"), Path("/tmp/x.wav"), "/tmp", 10.0)
        self.assertIn("-fs", captured["cmd"])

    def test_long_decode_watchdog_stops_a_stalled_reader(self):
        from x_reader.long_audio import _ReadWatchdog

        proc = subprocess.Popen(["sleep", "30"], stdout=subprocess.PIPE, start_new_session=True)
        dog = _ReadWatchdog(proc, idle_seconds=1)
        started = time.monotonic()
        try:
            self.assertEqual(dog.read(1024), b"")
            self.assertTrue(dog.fired)
            self.assertLess(time.monotonic() - started, 6)
        finally:
            dog.close()
            proc.wait(timeout=5)
            proc.stdout.close()


class HostedServerTest(unittest.TestCase):
    def _serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return thread

    def test_non_json_posts_are_refused(self):
        server = web_app.BoundedThreadingHTTPServer(("127.0.0.1", 0), web_app.EvidenceRequestHandler)
        thread = self._serve(server)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request("POST", "/api/read", body='{"url":"https://example.com"}', headers={"Content-Type": "text/plain"})
            response = conn.getresponse()
            self.assertEqual(response.status, 415)
            self.assertIn("base-uri 'none'", response.getheader("Content-Security-Policy") or "")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_connection_cap_returns_503(self):
        server = web_app.BoundedThreadingHTTPServer(("127.0.0.1", 0), web_app.EvidenceRequestHandler, max_threads=0)
        thread = self._serve(server)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request("GET", "/healthz")
            self.assertEqual(conn.getresponse().status, 503)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_handler_has_socket_deadline(self):
        self.assertEqual(web_app.EvidenceRequestHandler.timeout, web_app.SOCKET_TIMEOUT_SECONDS)

    def test_cli_failure_text_is_not_public(self):
        proc = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "error": "secret /var/lib/path", "error_type": "OSError"}), "")
        receipt = web_app._parse_cli_failure(proc, "https://e.com", "generic")
        self.assertNotIn("secret", json.dumps(receipt))
        self.assertEqual(receipt["reason_code"], "source_fetch_failed_oserror")


if __name__ == "__main__":
    unittest.main()
