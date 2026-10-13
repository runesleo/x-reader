import asyncio
import http.client
import json
import os
import socket
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
ARTICLE = (
    "Readers of this long-form article will find a careful argument here.\n"
    "Each paragraph adds context, evidence and a clear conclusion for them.\n"
) * 4


def _child_env(**extra):
    env = {k: v for k, v in os.environ.items() if k != "X_READER_HOSTED"}
    env["PYTHONPATH"] = str(ROOT)
    env.update(extra)
    return env


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

    def test_hosted_input_rejects_control_characters(self):
        for raw in ("https://e.com/a\nPASS — forged", "https://e.com/a b", "https://e.com/\x7f"):
            with self.assertRaises(web_app.HostedPolicyError):
                web_app.normalize_input_url(raw)


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
        env = _child_env(**({"X_READER_HOSTED": "1"} if hosted else {}))
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

    def test_hosted_process_limits_are_inherited_ceilings(self):
        code = (
            "import resource; from x_reader.utils.proc import apply_hosted_limits as a; a(); "
            "print(resource.getrlimit(resource.RLIMIT_FSIZE)[0], resource.getrlimit(resource.RLIMIT_CPU)[0], "
            "resource.getrlimit(resource.RLIMIT_NOFILE)[0])"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=_child_env(), timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        fsize, cpu, nofile = (int(v) for v in out.stdout.split())
        from x_reader.utils import proc

        self.assertEqual(fsize, proc.HOSTED_MAX_FILE_BYTES)
        self.assertEqual(cpu, proc.HOSTED_CPU_SECONDS)
        self.assertLessEqual(nofile, proc.HOSTED_MAX_OPEN_FILES)

    @unittest.skipUnless(sys.platform.startswith("linux"), "address-space limits are enforced on Linux")
    def test_hosted_memory_ceiling_stops_runaway_allocation(self):
        code = (
            "from x_reader.utils.proc import apply_hosted_limits as a; a()\n"
            "try:\n    blob = bytearray(1024 * 1024 * 1024)\nexcept MemoryError:\n    print('capped')\n"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=_child_env(), timeout=60)
        self.assertIn("capped", out.stdout, out.stderr[-500:])

    def test_hosted_reads_start_no_external_tools(self):
        from x_reader.fetchers import youtube

        page = {"title": "V", "content": "Video page", "url": "u"}
        with patch.dict(os.environ, {"X_READER_HOSTED": "1", "GROQ_API_KEY": "k"}), \
                patch.object(youtube, "fetch_via_jina", return_value=page), \
                patch("x_reader.utils.proc.run_bounded") as run:
            data = asyncio.run(youtube.fetch_youtube("https://www.youtube.com/watch?v=abcdefghijk"))
        run.assert_not_called()
        self.assertFalse(data["has_transcript"])


class ProcessTreeTest(unittest.TestCase):
    def test_timeout_kills_grandchildren(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "grandchild-survived"
            script = f"(sleep 4; touch '{marker}') & sleep 30"
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_bounded(["sh", "-c", script], timeout=0.5, new_session=True)
            self.assertLess(time.monotonic() - started, 10)
            time.sleep(4.5)
            self.assertFalse(marker.exists())

    def test_leftovers_are_swept_when_the_tool_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "leftover-survived"
            script = f"(sleep 4; touch '{marker}') >/dev/null 2>&1 &"
            result = run_bounded(["sh", "-c", script], timeout=10, new_session=True)
            self.assertEqual(result.returncode, 0)
            time.sleep(4.5)
            self.assertFalse(marker.exists())

    def test_output_budget_is_enforced_while_the_tool_runs(self):
        code = "import sys, time; sys.stdout.write('x' * 200000); sys.stdout.flush(); time.sleep(30)"
        # Without streaming enforcement this would end in TimeoutExpired after 20 s.
        started = time.monotonic()
        with self.assertRaises(ValueError):
            run_bounded([sys.executable, "-c", code], timeout=20, max_output_bytes=1000, new_session=True)
        self.assertLess(time.monotonic() - started, 15)

    def test_hosted_tools_stay_in_the_request_group(self):
        # The hosted server kills the CLI's group; tools the CLI starts must be in it.
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "nested-survived"
            inner = (
                "from x_reader.utils.proc import run_bounded; "
                f"run_bounded(['sh', '-c', 'sleep 5; touch {marker}'], timeout=30)"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                run_bounded([sys.executable, "-c", inner], timeout=3, new_session=True,
                            env=_child_env(X_READER_HOSTED="1"))
            time.sleep(5.5)
            self.assertFalse(marker.exists())


class SafeFetchTest(unittest.TestCase):
    def test_private_targets_are_refused_before_connecting(self):
        from x_reader.fetchers.jina import fetch_public_bytes
        from x_reader.fetchers.rss import FEED_TYPES

        for url in ("http://127.0.0.1/feed", "http://169.254.169.254/latest", "http://100.100.100.100/feed"):
            with self.assertRaises(ValueError, msg=url):
                fetch_public_bytes(url, FEED_TYPES)

    def _feed(self, *items: bytes) -> bytes:
        return (b'<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
                b"<channel><title>F</title>" + b"".join(items) + b"</channel></rss>")

    def test_rss_parses_downloaded_bytes_and_judges_full_bodies(self):
        from x_reader.fetchers import rss

        body = ARTICLE.replace("\n", "</p><p>").encode()
        image = ("<img src='https://cdn.example.com/" + "a" * 400 + ".jpg'>Teaser").encode()
        xml = self._feed(
            b"<item><title>A</title><link>https://e.com/a</link><description>teaser</description>"
            b"<content:encoded><![CDATA[<p>" + body + b"</p>]]></content:encoded></item>",
            b"<item><title>B</title><link>https://e.com/b</link><description>only teaser</description></item>",
            b"<item><title>C</title><link>https://e.com/c</link>"
            b"<content:encoded><![CDATA[" + image + b"]]></content:encoded></item>",
        )
        with patch.object(rss, "fetch_public_bytes", return_value=(xml, "application/rss+xml", "https://e.com/feed")) as fetch:
            items = asyncio.run(rss.fetch_rss("https://e.com/feed"))
        fetch.assert_called_once()
        self.assertEqual(items[0]["content_kind"], "full")
        self.assertEqual(items[1]["content_kind"], "summary")
        self.assertEqual(items[2]["content_kind"], "summary")

    def test_full_feed_bodies_are_checked_against_the_article(self):
        from x_reader.fetchers import rss

        story = " ".join(
            f"Paragraph {i} of the story explains another step of the argument in plain words." for i in range(40)
        )
        story_page = story.replace(". ", ".\n")  # sentence per line, like an article page
        item = {"content_kind": "full", "url": "https://e.com/a", "summary": f"<p>{story}</p>"}
        page = {"title": "A", "content": story_page, "url": "https://e.com/a", "embedded_media": 0}

        def verdict(**changes):
            with patch("x_reader.fetchers.jina.fetch_html_page", return_value=dict(page, **changes)):
                return asyncio.run(rss.verify_full_body(dict(item)))

        self.assertEqual(verdict(), "full")
        longer = story_page + "\n" + "\n".join(
            f"Later section {i} adds detail that the feed never carried, sentence after sentence." for i in range(80))
        self.assertEqual(verdict(content=longer), "summary")
        unrelated = "\n".join(f"An unrelated page talks about gardening tips number {i} in detail." for i in range(60))
        self.assertEqual(verdict(content=unrelated), "unverified")
        self.assertEqual(verdict(content="Sign in to continue reading."), "unverified")
        paragraphs = [f"Section {i} of the long report walks through finding number {i} with care." for i in range(100)]
        with patch("x_reader.fetchers.jina.fetch_html_page",
                   return_value=dict(page, content="\n".join(paragraphs))):
            partial = dict(item, summary="<p>" + " ".join(paragraphs[:62]) + "</p>")
            self.assertEqual(asyncio.run(rss.verify_full_body(partial)), "summary")  # 62 % is not full
        repeated = "\n".join(["The same refrain returns again and again in this very long page."] * 100)
        with patch("x_reader.fetchers.jina.fetch_html_page", return_value=dict(page, content=repeated)):
            five = dict(item, summary="<p>" + " ".join([repeated.splitlines()[0]] * 5) + "</p>")
            self.assertEqual(asyncio.run(rss.verify_full_body(five)), "summary")  # repeats are counted
        russian = "\n".join(f"Раздел {i} длинной статьи подробно объясняет следующий шаг рассуждения." for i in range(40))
        with patch("x_reader.fetchers.jina.fetch_html_page", return_value=dict(page, content=russian)):
            self.assertEqual(asyncio.run(rss.verify_full_body(dict(item, summary=f"<p>{russian}</p>"))), "full")
        self.assertEqual(verdict(embedded_media=1), "unverified")
        with patch("x_reader.fetchers.jina.fetch_html_page", side_effect=ValueError("HTTP status 403")):
            self.assertEqual(asyncio.run(rss.verify_full_body(dict(item))), "unverified")
        video = dict(item, url="https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(asyncio.run(rss.verify_full_body(video)), "unverified")

    def test_pinned_requests_try_ipv4_first_and_move_on_after_a_failed_connect(self):
        from urllib3.exceptions import NewConnectionError
        from x_reader.fetchers import jina

        tried = []

        class Pool:
            def __init__(self, host, **_kw):
                self.host = host

            def request(self, *_a, **_kw):
                tried.append(self.host)
                if self.host in ("198.51.100.7", "2001:db8::1"):
                    raise NewConnectionError(None, "no route to host")
                return "response"

            def close(self):
                pass

        with patch("x_reader.utils.url_validator.resolve_safe_ips",
                   return_value=["2001:db8::1", "198.51.100.7", "203.0.113.5"]), \
                patch.object(jina, "HTTPSConnectionPool", Pool):
            _pool, response, ip = jina._request_pinned("https://e.com/a", 5)
        self.assertEqual(tried, ["198.51.100.7", "203.0.113.5"])
        self.assertEqual((response, ip), ("response", "203.0.113.5"))

    def test_pinned_requests_stop_after_three_addresses(self):
        from urllib3.exceptions import NewConnectionError
        from x_reader.fetchers import jina

        tried = []

        class Pool:
            def __init__(self, host, **_kw):
                self.host = host

            def request(self, *_a, **_kw):
                tried.append(self.host)
                raise NewConnectionError(None, "unreachable")

            def close(self):
                pass

        addresses = [f"198.51.100.{i}" for i in range(1, 9)]
        with patch("x_reader.utils.url_validator.resolve_safe_ips", return_value=addresses), \
                patch.object(jina, "HTTPSConnectionPool", Pool):
            with self.assertRaises(NewConnectionError):
                jina._request_pinned("https://e.com/a", 5)
        self.assertEqual(len(tried), jina.MAX_CONNECT_ATTEMPTS)

    def test_feeds_with_generic_content_types_are_sniffed(self):
        from x_reader.fetchers.jina import _read_bounded
        from x_reader.fetchers.rss import FEED_TYPES, looks_like_feed

        class Response:
            def __init__(self, content_type, body):
                self.headers = {"Content-Type": content_type} if content_type else {}
                self._chunks = [body, b""]

            def read(self, _n):
                return self._chunks.pop(0)

        feed = self._feed(b"<item><title>A</title></item>")
        deadline = time.monotonic() + 5
        for content_type in ("text/plain", "", "application/octet-stream"):
            body, _ = _read_bounded(Response(content_type, feed), deadline, FEED_TYPES, sniff=looks_like_feed)
            self.assertEqual(body, feed)
        for content_type, body in (("text/plain", b"<html><body>hi</body></html>"), ("text/html", feed)):
            with self.assertRaises(ValueError, msg=content_type):
                _read_bounded(Response(content_type, body), deadline, FEED_TYPES, sniff=looks_like_feed)
        self.assertTrue(looks_like_feed(b'\xef\xbb\xbf{"version": "https://jsonfeed.org/version/1.1"}'))


class GenericReadTest(unittest.TestCase):
    PAGE = {"title": "A", "content": ARTICLE, "url": "https://e.com/a", "author": "",
            "fetch_method": "direct_html_pinned", "embedded_media": 0}
    JINA = {"title": "A (Jina)", "content": "Jina text. " * 40, "url": "https://e.com/a", "fetch_method": "jina_pinned"}

    def _read(self, url, hosted=False):
        from x_reader.reader import UniversalReader

        env = {"X_READER_HOSTED": "1"} if hosted else {}
        with patch.dict(os.environ, env), patch("x_reader.reader.validate_url", side_effect=lambda u: u):
            if not hosted:
                os.environ.pop("X_READER_HOSTED", None)
            return asyncio.run(UniversalReader()._fetch("generic", url))

    def _hop_to(self, url):
        def direct(_url, hop_guard=None):
            hop_guard(url)
            raise AssertionError("hop guard should have stopped the chain")
        return direct

    def test_redirect_into_a_disallowed_platform_is_refused(self):
        with patch.dict(os.environ, {"X_READER_ALLOWED_PLATFORMS": "generic,twitter,youtube"}), \
                patch("x_reader.fetchers.jina.fetch_html_page", side_effect=self._hop_to("https://t.me/chan/1")), \
                patch("x_reader.reader.fetch_via_jina") as jina, \
                patch("x_reader.fetchers.telegram.fetch_telegram", AsyncMock()) as telegram:
            with self.assertRaises(ValueError):
                self._read("https://short.example/abc")
        telegram.assert_not_awaited()
        jina.assert_not_called()

    def test_redirect_into_x_goes_to_the_x_reader(self):
        post = {"text": "hello", "url": "https://x.com/a/status/1", "fetch_method": "fxtwitter",
                "media_status": "none", "structure_status": "verified"}
        with patch("x_reader.fetchers.jina.fetch_html_page", side_effect=self._hop_to("https://x.com/a/status/1")), \
                patch("x_reader.fetchers.twitter.fetch_twitter", AsyncMock(return_value=post)) as fetch:
            content = self._read("https://short.example/abc")
        fetch.assert_awaited_once_with("https://x.com/a/status/1")
        self.assertEqual(content.source_type.value, "twitter")

    def test_blocked_destinations_abort_without_asking_jina(self):
        from x_reader.fetchers.jina import BlockedDestination

        with patch("x_reader.fetchers.jina.fetch_html_page", side_effect=BlockedDestination("Blocked")), \
                patch("x_reader.reader.fetch_via_jina") as jina:
            with self.assertRaises(ValueError):
                self._read("https://e.com/a")
        jina.assert_not_called()

    def test_hosted_reads_return_the_text_they_checked(self):
        with patch("x_reader.fetchers.jina.fetch_html_page", return_value=dict(self.PAGE)), \
                patch("x_reader.reader.fetch_via_jina") as jina:
            content = self._read("https://e.com/a", hosted=True)
        jina.assert_not_called()
        self.assertEqual(content.extra["fetch_method"], "direct_html_pinned")
        self.assertIs(content.extra["redirects_verified"], True)
        self.assertEqual(content.extra["embedded_media"], 0)

    def test_hosted_reads_never_use_jina(self):
        thin = dict(self.PAGE, content="Loading the application, please wait a moment.")
        with patch("x_reader.fetchers.jina.fetch_html_page", return_value=thin), \
                patch("x_reader.reader.fetch_via_jina") as jina:
            content = self._read("https://e.com/a", hosted=True)
        jina.assert_not_called()
        self.assertEqual(content.extra["fetch_method"], "direct_html_pinned")
        with patch("x_reader.fetchers.jina.fetch_html_page", side_effect=TimeoutError()), \
                patch("x_reader.reader.fetch_via_jina") as jina:
            with self.assertRaises(TimeoutError):
                self._read("https://e.com/a", hosted=True)
        jina.assert_not_called()

    def test_local_reads_use_jina_text_with_direct_evidence(self):
        with patch("x_reader.fetchers.jina.fetch_html_page", return_value=dict(self.PAGE, embedded_media=1)), \
                patch("x_reader.reader.fetch_via_jina", return_value=dict(self.JINA)):
            content = self._read("https://e.com/a")
        self.assertEqual(content.extra["fetch_method"], "jina_pinned")
        self.assertIs(content.extra["redirects_verified"], True)
        self.assertEqual(content.extra["embedded_media"], 1)

    def test_unreadable_pages_are_recorded_as_unverified(self):
        with patch("x_reader.fetchers.jina.fetch_html_page", side_effect=TimeoutError()), \
                patch("x_reader.reader.fetch_via_jina", return_value=dict(self.JINA)):
            content = self._read("https://e.com/a")
        self.assertIs(content.extra["redirects_verified"], False)
        self.assertIsNone(content.extra["embedded_media"])

    def test_page_extraction_drops_boilerplate_and_counts_embeds(self):
        from x_reader.fetchers.jina import _extract_html_page

        html = (
            "<html><head><title>T</title></head><body><nav>Home Pricing Blog Careers Contact</nav>"
            "<main><article>" + "".join(f"<p>{line}</p>" for line in ARTICLE.splitlines()) +
            "<video src='/a.mp4'></video><iframe src='https://player.example.org/v/1'></iframe>"
            "<iframe src='/player/video'></iframe><embed src='/a.swf'>"
            "<iframe src='https://t.example.com/px' width='0' height='0'></iframe>"
            "<noscript><iframe src='https://tags.example.com/ns'></iframe></noscript>"
            "</article><article><p>The second article on the page is kept as well, word for word.</p></article></main>"
            "<footer>Copyright and legal links for this site, all rights reserved here.</footer></body></html>"
        )
        title, content, embeds = _extract_html_page(html)
        self.assertEqual(title, "T")
        self.assertEqual(embeds, 4)  # video, two frames, embed; hidden and noscript frames ignored
        self.assertNotIn("Pricing", content)
        self.assertNotIn("Copyright", content)
        self.assertIn("careful argument", content)
        self.assertIn("second article on the page", content)

    def test_banners_outside_the_marked_content_stay_out(self):
        from x_reader.evidence import classify_payload
        from x_reader.fetchers.jina import _extract_html_page

        banner = ("<div class='cmp'>We value your privacy. We and our partners use cookies. "
                  "Accept all cookies or manage preferences. Reject all.</div>")
        html = ("<html><head><title>Essay</title></head><body>" + banner + "<main>" +
                "".join(f"<p>{line}</p>" for line in ARTICLE.splitlines()) + "</main></body></html>")
        title, content, embeds = _extract_html_page(html)
        self.assertNotIn("cookies", content)
        receipt = classify_payload({"source_type": "manual", "title": title, "content": content,
                                    "extra": {"redirects_verified": True, "embedded_media": embeds}})
        self.assertEqual(receipt["status"], "PASS")


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


class YoutubeTest(unittest.TestCase):
    def test_only_single_video_urls_are_rebuilt_for_yt_dlp(self):
        from x_reader.fetchers.youtube import canonical_video_url

        watch = "https://www.youtube.com/watch?v=abcdefghijk"
        for url in ("https://youtu.be/abcdefghijk?si=x", "https://www.youtube.com/watch?v=abcdefghijk&list=PL1",
                    "https://m.youtube.com/shorts/abcdefghijk", "https://youtube.com/live/abcdefghijk"):
            self.assertEqual(canonical_video_url(url), watch, url)
        for url in ("https://consent.youtube.com/m?continue=http%3A%2F%2F127.0.0.1%2F",
                    "https://www.youtube.com/watch?v=abcdefghijk&v=zzzzzzzzzzz",
                    "https://www.youtube.com/@channel", "https://youtube.com.evil.example/watch?v=abcdefghijk",
                    "https://www.youtube.com/redirect?q=http://127.0.0.1/", "https://youtu.be/short"):
            self.assertEqual(canonical_video_url(url), "", url)

    def test_non_video_urls_never_reach_yt_dlp(self):
        from x_reader.fetchers import youtube

        page = {"title": "Consent", "content": "Before you continue to YouTube", "url": "u"}
        with patch.object(youtube, "fetch_via_jina", return_value=page), \
                patch("x_reader.utils.proc.run_bounded") as run:
            data = asyncio.run(youtube.fetch_youtube(
                "https://consent.youtube.com/m?continue=http%3A%2F%2F127.0.0.1%3A1%2Flatest"))
        run.assert_not_called()
        self.assertFalse(data["has_transcript"])

    def test_srt_cues_and_coverage(self):
        from x_reader.fetchers import youtube

        with tempfile.TemporaryDirectory() as tmp:
            srt = Path(tmp) / "s.srt"
            srt.write_text("1\n00:00:01,000 --> 00:00:04,500\nhello\n\n2\n00:00:04,500 --> 00:00:08,000\nhello\nworld\n\n"
                           "3\n00:09:50,000 --> 00:09:58,250\nhello\n")
            text, cues = youtube._parse_srt(str(srt))
            # rolling repeats collapse, a later repeat is real speech and stays
            self.assertEqual(text, "hello world hello")
            pause = Path(tmp) / "p.srt"
            pause.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n\n2\n00:01:02,000 --> 00:01:03,000\nhello\n")
            self.assertEqual(youtube._parse_srt(str(pause))[0], "hello hello")
            coverage = youtube.cue_coverage(cues)
            self.assertEqual(coverage["transcript_start_seconds"], 1.0)
            self.assertEqual(coverage["transcript_end_seconds"], 598.25)
            self.assertEqual(coverage["transcript_covered_seconds"], 15.25)
            self.assertEqual(coverage["transcript_max_gap_seconds"], 582.0)
            info = Path(tmp) / "s.info.json"
            info.write_text(json.dumps({"duration": 600}))
            self.assertEqual(youtube._read_duration(str(info)), 600.0)
            info.write_text(json.dumps({"duration": "NaN"}))
            self.assertIsNone(youtube._read_duration(str(info)))

    def test_yt_dlp_calls_are_pinned_to_youtube_and_bounded(self):
        from x_reader.fetchers import youtube

        seen = []

        def fake_run(cmd, timeout, **_kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        watch = "https://www.youtube.com/watch?v=abcdefghijk"
        with patch("x_reader.utils.proc.run_bounded", side_effect=fake_run), \
                patch("x_reader.utils.url_validator.validate_url", side_effect=lambda u: u), \
                patch.dict(os.environ, {"GROQ_API_KEY": "k", "X_READER_HOSTED": "1"}):
            youtube._get_subtitles_via_ytdlp(watch)
            youtube._transcribe_via_whisper(watch)
            youtube._get_subtitles_via_ytdlp("https://youtu.be/abcdefghijk")  # not canonical: refused
        self.assertEqual(len(seen), 2)
        for cmd in seen:
            self.assertEqual(cmd[cmd.index("--ies") + 1], "default,-generic")
            self.assertIn("--no-playlist", cmd)
            self.assertIn("--ignore-config", cmd)
            self.assertEqual(cmd[-1], watch)
        self.assertIn("--max-filesize", seen[1])


class AudioBoundsTest(unittest.TestCase):
    def test_duration_tolerance_does_not_grow_with_length(self):
        from x_reader.evidence import audio_duration_matches

        self.assertTrue(audio_duration_matches(120.0, 119.9))
        self.assertTrue(audio_duration_matches(1.0, 0.952))
        self.assertFalse(audio_duration_matches(0.8, 0.1))
        self.assertFalse(audio_duration_matches(5400.0, 5346.0))  # 54 s missing from 90 min
        self.assertFalse(audio_duration_matches(600.0, 600.6))

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


class CliUsageJsonTest(unittest.TestCase):
    def test_usage_errors_stay_json_under_json_flag(self):
        for args in (["--json", "--media-preview-seconds", "nope", "https://example.com"],
                     ["--json", "--media-full-short", "--media-full-long", "https://example.com"],
                     ["--json", "frobnicate"]):
            out = subprocess.run([sys.executable, "-m", "x_reader.cli", *args], capture_output=True,
                                 text=True, cwd=ROOT, env=_child_env(), timeout=60)
            self.assertEqual(out.returncode, 2, args)
            payload = json.loads(out.stdout)
            self.assertEqual(payload["error_type"], "UsageError", args)
            self.assertFalse(payload["ok"])


class HostedServerTest(unittest.TestCase):
    def setUp(self):
        self.server = web_app.BoundedThreadingHTTPServer(("127.0.0.1", 0), web_app.EvidenceRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(self, method, path="/api/read", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response, response.read()

    def test_non_json_posts_are_refused(self):
        response, _ = self._request("POST", body='{"url":"https://example.com"}', headers={"Content-Type": "text/plain"})
        self.assertEqual(response.status, 415)
        self.assertIn("base-uri 'none'", response.getheader("Content-Security-Policy") or "")

    def test_invalid_utf8_is_a_stable_json_error(self):
        response, body = self._request("POST", body=b'{"url":"\xff"}', headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid_json")

    def test_other_methods_get_json_errors(self):
        response, body = self._request("PUT", body=b"{}", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 405)
        self.assertEqual(response.getheader("Allow"), "GET, POST")
        self.assertEqual(json.loads(body)["error"], "method_not_allowed")
        response, body = self._request("BREW", path="/")
        self.assertEqual(response.getheader("Content-Type"), "application/json; charset=utf-8")
        self.assertFalse(json.loads(body)["ok"])

    def test_server_header_has_no_interpreter_version(self):
        response, _ = self._request("GET", path="/healthz")
        self.assertEqual(response.getheader("Server"), "x-reader-evidence")

    def test_unparseable_request_target_is_a_json_400(self):
        sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
        try:
            sock.sendall(b"GET http://[ HTTP/1.1\r\nHost: x\r\n\r\n")
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        self.assertIn(b" 400 ", data.split(b"\r\n", 1)[0])
        self.assertIn(b'"bad_request"', data)

    def test_failed_thread_start_gives_the_slot_back(self):
        server = web_app.BoundedThreadingHTTPServer(("127.0.0.1", 0), web_app.EvidenceRequestHandler, max_threads=1)
        try:
            client = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
            request, address = server.get_request()
            with patch("threading.Thread.start", side_effect=RuntimeError("can't start new thread")):
                with self.assertRaises(RuntimeError):
                    server.process_request(request, address)
            client.close()
            self.assertTrue(server._thread_slots.acquire(blocking=False))
        finally:
            server.server_close()

    def test_slow_clients_are_cut_off_at_the_read_deadline(self):
        with patch.object(web_app, "REQUEST_READ_DEADLINE_SECONDS", 1):
            sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
            try:
                started = time.monotonic()
                sock.sendall(b"POST /api/read HTTP/1.1\r\nHost: x\r\n")
                closed = False
                while time.monotonic() - started < 10:
                    try:
                        sock.sendall(b"X-Drip: 1\r\n")
                    except OSError:
                        closed = True
                        break
                    time.sleep(0.2)
                    try:
                        sock.settimeout(0.05)
                        if sock.recv(1) == b"":
                            closed = True
                            break
                    except socket.timeout:
                        pass
                self.assertTrue(closed)
                self.assertLess(time.monotonic() - started, 8)
            finally:
                sock.close()

    def test_handler_has_socket_deadline(self):
        self.assertEqual(web_app.EvidenceRequestHandler.timeout, web_app.SOCKET_TIMEOUT_SECONDS)

    def test_connection_cap_returns_json_503(self):
        capped = web_app.BoundedThreadingHTTPServer(("127.0.0.1", 0), web_app.EvidenceRequestHandler, max_threads=0)
        thread = threading.Thread(target=capped.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", capped.server_port, timeout=3)
            conn.request("GET", "/healthz")
            response = conn.getresponse()
            self.assertEqual(response.status, 503)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertEqual(json.loads(response.read())["error"], "busy")
        finally:
            capped.shutdown()
            capped.server_close()
            thread.join(timeout=2)

    def test_cli_failure_text_is_not_public(self):
        proc = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "error": "secret /var/lib/path", "error_type": "OSError"}), "")
        receipt = web_app._parse_cli_failure(proc, "https://e.com", "generic")
        self.assertNotIn("secret", json.dumps(receipt))
        self.assertEqual(receipt["reason_code"], "source_fetch_failed")
        renamed = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "error": "x", "error_type": "SomeNewLibError"}), "")
        self.assertEqual(web_app._parse_cli_failure(renamed, "https://e.com", "generic")["reason_code"], "source_fetch_failed")


if __name__ == "__main__":
    unittest.main()
