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
        # players and audio or video enclosures in an item are unread layers, whatever its text
        xml = self._feed(
            b"<item><title>D</title><link>https://e.com/d</link><content:encoded><![CDATA[<p>" + body
            + b"</p><audio src='https://e.com/a.mp3'></audio>]]></content:encoded></item>",
            b"<item><title>E</title><link>https://e.com/e</link><enclosure url='https://e.com/e.mp3' length='1' "
            b"type='audio/mpeg'/><content:encoded><![CDATA[<p>" + body + b"</p>]]></content:encoded></item>",
        )
        with patch.object(rss, "fetch_public_bytes", return_value=(xml, "application/rss+xml", "https://e.com/feed")):
            items = asyncio.run(rss.fetch_rss("https://e.com/feed"))
        from x_reader.evidence import build_receipt
        from x_reader.schema import from_rss
        for item in items:
            self.assertEqual(item["embedded_media"], 1, item["title"])
            receipt = build_receipt(from_rss(item).to_dict())
            self.assertEqual(receipt["reason_code"], "embedded_media_unread", item["title"])

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
        noted = story_page + "\n<note " + " ".join(
            f"Later section {i} adds detail that the feed never carried, sentence after sentence." for i in range(80)) + ">"
        self.assertEqual(verdict(content=noted), "summary")  # the page's text is words, never markup
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
        from x_reader.utils import textmatch
        huge = " ".join(f"word{i}" for i in range(textmatch.MAX_TOKENS + 10))
        with patch("x_reader.fetchers.jina.fetch_html_page", return_value=dict(page, content=huge + ".")):
            self.assertEqual(asyncio.run(rss.verify_full_body(dict(item, summary=huge[:200]))), "unverified")
        import unicodedata
        cafe = "\n".join(f"Le café numéro {i} de la rue raconte une histoire assez longue ici." for i in range(40))
        with patch("x_reader.fetchers.jina.fetch_html_page", return_value=dict(page, content=cafe)):
            nfd = unicodedata.normalize("NFD", cafe)
            self.assertEqual(asyncio.run(rss.verify_full_body(dict(item, summary=f"<p>{nfd}</p>"))), "full")
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
            "fetch_method": "direct_html_pinned", "embedded_media": 0, "page_signals": []}
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
        self.assertIs(content.extra["body_verified"], True)
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
                patch("x_reader.reader.fetch_via_jina", return_value=dict(self.JINA, content=ARTICLE)):
            content = self._read("https://e.com/a")
        self.assertEqual(content.extra["fetch_method"], "jina_pinned")
        self.assertIs(content.extra["redirects_verified"], True)
        self.assertIs(content.extra["body_verified"], True)
        self.assertEqual(content.extra["embedded_media"], 1)

    def test_local_jina_text_must_carry_the_whole_page(self):
        paragraphs = [f"Paragraph {i} explains a separate step of the method in plain words for readers." for i in range(20)]
        page = dict(self.PAGE, content="\n".join(paragraphs))
        for jina_text in ("\n".join(paragraphs[:6]), "Completely different text from another page. " * 30):
            with patch("x_reader.fetchers.jina.fetch_html_page", return_value=page), \
                    patch("x_reader.reader.fetch_via_jina", return_value=dict(self.JINA, content=jina_text)):
                content = self._read("https://e.com/a")
            # the direct read is shown instead, so what is shown is what was checked
            self.assertEqual(content.extra["fetch_method"], "direct_html_pinned")
            self.assertEqual(content.content, page["content"])
            self.assertIs(content.extra["body_verified"], True)

    def test_local_jina_text_needs_a_matching_direct_read(self):
        for direct in (dict(self.PAGE, content=""), dict(self.PAGE, content="Loading, please wait.")):
            with patch("x_reader.fetchers.jina.fetch_html_page", return_value=direct), \
                    patch("x_reader.reader.fetch_via_jina", return_value=dict(self.JINA)):
                content = self._read("https://e.com/a")
            self.assertIs(content.extra["body_verified"], False)

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
        page = _extract_html_page(html)
        title, content, embeds = page["title"], page["content"], page["embeds"]
        self.assertEqual(title, "T")
        self.assertEqual(embeds, 5)  # video, three frames (a 0-pixel one too), embed; only noscript is skipped
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
        with patch("x_reader.fetchers.jina._download_html", return_value=(html, "https://e.com/essay")):
            from x_reader.fetchers.jina import fetch_html_page
            page = fetch_html_page("https://e.com/essay")
        self.assertNotIn("cookies", page["content"])
        receipt = classify_payload({"source_type": "manual", "title": page["title"], "content": page["content"],
                                    "extra": {"redirects_verified": True, "body_verified": True,
                                              "embedded_media": page["embedded_media"],
                                              "page_signals": page["page_signals"]}})
        self.assertEqual(receipt["status"], "PASS")

    def _page_receipt(self, html):
        from x_reader.evidence import classify_payload
        from x_reader.fetchers.jina import fetch_html_page

        with patch("x_reader.fetchers.jina._download_html", return_value=(html, "https://e.com/a")):
            page = fetch_html_page("https://e.com/a")
        return page, classify_payload({
            "source_type": "manual", "title": page["title"], "content": page["content"],
            "extra": {"fetch_method": "direct_html_pinned", "redirects_verified": True, "body_verified": True,
                      "embedded_media": page["embedded_media"], "page_signals": page["page_signals"]}})

    def test_walls_beside_the_main_content_still_count(self):
        main = "<main>" + "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines()) + "</main>"
        # beside a substantial main, a login notice may be a wall or a quote: not PASS, not FAIL
        for notice, status in (("<div>Subscribe to continue reading this story.</div>", "PARTIAL"),
                               ("<div>Please sign in to continue.</div>", "UNKNOWN"),
                               ("<div><h1>Page not found</h1></div>", "UNKNOWN")):
            page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body>{notice}{main}</body></html>")
            self.assertEqual(receipt["status"], status, notice)
        hidden_main = ("<html><head><title>T</title></head><body><main hidden>"
                       + "".join(f"<p>{line}</p>" for line in ARTICLE.splitlines()) + "</main><p>Short visible note.</p></body></html>")
        page, receipt = self._page_receipt(hidden_main)
        self.assertNotIn("careful argument", page["content"])
        self.assertNotEqual(receipt["status"], "PASS")

    def test_visible_walls_count_wherever_they_sit(self):
        long_main = "<main>" + "".join(f"<p>{line}</p>" for line in (ARTICLE * 3).splitlines()) + "</main>"

        def page(before="", after="", title="T"):
            return f"<html><head><title>{title}</title></head><body>{before}{long_main}{after}</body></html>"

        cases = (
            (page(after="<div>Please sign in to continue.</div>"), "UNKNOWN"),
            (page(after="<div>Checking your browser before accessing the site.</div>"), "UNKNOWN"),
            (page(title="Just a moment..."), "FAIL"),
            (page(title="Sign in | Example"), "FAIL"),
            (page(after="<h2>Page not found</h2>"), "UNKNOWN"),
            (page(after="<p><a href='/full'>Continue reading</a></p>"), "PARTIAL"),
            (page(after="<footer>Subscribe to continue reading.</footer>"), "PARTIAL"),
            (page(before="<nav>Please sign in to continue</nav>"), "UNKNOWN"),
            (page(title="Subscribe to continue | Example"), "PARTIAL"),
            (page(title="Page not found: /old-slug"), "UNKNOWN"),
            (page(before="<h1>Error: This page does not exist</h1>"), "UNKNOWN"),
        )
        for html, status in cases:
            _page, receipt = self._page_receipt(html)
            self.assertEqual(receipt["status"], status, html[60:160])

    def test_visibility_is_only_trusted_when_inline_css_proves_it(self):
        long_main = "<main>" + "".join(f"<p>{line}</p>" for line in (ARTICLE * 3).splitlines()) + "</main>"
        for notice in ("<div style='display:none; display:block'>Subscribe to continue reading.</div>",
                       "<div hidden style='display: flex'>Subscribe to continue reading.</div>",
                       "<div style='visibility:hidden'><p style='visibility:visible'>Subscribe to continue reading.</p></div>",
                       "<div style='display:block !important; display:none'>Subscribe to continue reading.</div>",
                       "<div style='display:none;/**/display:block'>Subscribe to continue reading.</div>",
                       "<div style='display:none!important;display:block ! important'>Subscribe to continue reading.</div>",
                       """<div style='display:block;--label:"x; display:none ;"'>Subscribe to continue reading.</div>""",
                       "<div style='display:none;dis\\play:block'>Subscribe to continue reading.</div>",
                       "<div style='display:block' style='display:none'>Subscribe to continue reading.</div>",
                       "<div style='display:n/**/one'>Subscribe to continue reading.</div>",
                       "<div style='--x:{color:red;display:none;}'>Subscribe to continue reading.</div>",
                       "<div style='display:none;display:var(--shown)'>Subscribe to continue reading.</div>"):
            _page, receipt = self._page_receipt(
                f"<html><head><title>T</title></head><body>{long_main}{notice}</body></html>")
            # the wall counts (PARTIAL); a display x-reader cannot work out leaves the page unverified first
            self.assertIn(receipt["status"], ("PARTIAL", "UNKNOWN"), notice)

    def test_hidden_markup_never_hides_a_wall(self):
        # author CSS can always show hidden markup, so a hidden wall still counts;
        # an inert <template> is never rendered and does not
        long_main = "<main>" + "".join(f"<p>{line}</p>" for line in (ARTICLE * 3).splitlines()) + "</main>"
        for template, status in (("<div hidden>Subscribe to continue reading.</div>", "PARTIAL"),
                                 ("<div style='display: none'>Please sign in to continue.</div>", "UNKNOWN"),
                                 # html5lib does not model templates: a page with one cannot pass
                                 ("<template><div>Subscribe to continue reading.</div></template>", "UNKNOWN")):
            _page, receipt = self._page_receipt(
                f"<html><head><title>T</title></head><body>{long_main}{template}</body></html>")
            self.assertEqual(receipt["status"], status, template)

    def test_unclosed_elements_end_with_their_parent(self):
        main = "<main>" + "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines()) + "</main>"
        # navigation drawers often leave <nav> open inside an <aside>
        drawer = "<aside class='drawer'><nav><a href='/'>Home</a><nav><a href='/blog'>Blog</a></aside>"
        page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body>{drawer}{main}</body></html>")
        self.assertIn("careful argument", page["content"])
        self.assertEqual(receipt["status"], "PASS")
        # a paragraph's end tag is optional, so its hidden attribute does not hide what follows
        sloppy = f"<html><head><title>T</title></head><body>{main}<p hidden>Template<div>Subscribe to continue reading.</div></body></html>"
        _page, receipt = self._page_receipt(sloppy)
        self.assertEqual(receipt["status"], "PARTIAL")
        # ...and text it hides is not page text, wherever the browser ends the element
        spans = "".join(f"<span>{line}</span>" for line in (ARTICLE * 2).splitlines())
        for body in (f"<main><p hidden>{spans}</p></main>", f"<main><ul><li hidden>{spans}</ul></main>",
                     f"<main><table><tr><td hidden>{spans}</td></tr></table></main>"):
            page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body>{body}</body></html>")
            self.assertNotIn("careful argument", page["content"], body[:30])
            self.assertNotEqual(receipt["status"], "PASS", body[:30])

    def test_content_of_unresolved_or_hidden_visibility_never_passes(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        # an invalid later value is ignored, so the main content really is hidden
        hidden = f"<html><head><title>T</title></head><body><main style='display:none;display:bogus'>{article}</main><p>Short note.</p></body></html>"
        page, receipt = self._page_receipt(hidden)
        self.assertNotEqual(receipt["status"], "PASS")
        unresolved = f"<html><head><title>T</title></head><body><main style='display:var(--d)'>{article}</main></body></html>"
        page, receipt = self._page_receipt(unresolved)
        self.assertIn("visibility_unknown", page["page_signals"])
        self.assertEqual(receipt["reason_code"], "visibility_unverified")

    def test_misnested_hidden_markup_cannot_vouch_for_text(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        # browsers may keep the hidden div open here (adoption agency), so the article may be hidden
        html = f"<html><head><title>T</title></head><body><main><b><div hidden></b>{article}</div></main></body></html>"
        page, receipt = self._page_receipt(html)
        self.assertNotEqual(receipt["status"], "PASS")
        boundary = f"<html><head><title>T</title></head><body><main><div hidden><table><tr><td></div>{article}</main></body></html>"
        _page, receipt = self._page_receipt(boundary)
        self.assertNotEqual(receipt["status"], "PASS")
        # end tags a browser applies the same way leave the text certain
        for template in ("<div hidden><p>Template</p></div>", "<div hidden><p>Template</div>",
                         "<div hidden><span>Template</div>"):
            tidy = f"<html><head><title>T</title></head><body><main>{template}{article}</main></body></html>"
            _page, receipt = self._page_receipt(tidy)
            self.assertEqual(receipt["status"], "PASS", template)

    def test_markup_is_read_the_way_a_browser_builds_it(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        wall = "<div>We value your privacy. Accept all cookies.</div>"
        cases = (
            # a template's end tag cannot close the main around it
            f"<main>{article}<template></main></template>{wall}</main>",
            # a second button closes the first, so the wall is not inside the hidden one
            f"<main>{article}</main><button hidden>old<button>Subscribe to continue reading.</button>",
            # a <head> after the body is ignored, its content stays visible
            f"<main>{article}</main></body><head><div>Subscribe to continue reading.</div>",
            # SVG text and foreignObject content are seen as walls
            f"<main>{article}</main><svg><foreignObject><div>Subscribe to continue reading.</div></foreignObject></svg>",
            # raw-text noscript cannot close the main either
            f"<main>{article}<noscript></main></noscript>{wall}</main>",
        )
        for body in cases:
            page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body>{body}</body></html>")
            self.assertNotEqual(receipt["status"], "PASS", body[-120:])

    def test_only_visibly_shown_css_vouches_for_text(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        for opening in ("<main hidden style='display:block inline'>", "<main style='opacity:calc(0)'>",
                        "<main style='opacity:0e0'>", "<main style='font-size:calc(0px)'>", "<main style='color:#0000'>",
                        "<main style='text-indent:calc(-9999px)'>", "<main style='font: 0/0 a'>",
                        "<main style='position:relative;left:-9999px'>", "<main style='margin-left:-9999px'>",
                        "<main style='overflow:hidden;max-height:3em'>"):
            _page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body>{opening}{article}</main></body></html>")
            self.assertNotEqual(receipt["status"], "PASS", opening)
        for opening in ("<main style='color:rgb(0,0,0)'>", "<main style='color:rgb(255, 0, 0)'>",
                        "<main style='color:#333;font-size:18px'>", "<main style='color:rgba(0,0,0,1)'>",
                        "<main style='display:block;margin:0 auto;max-width:720px'>"):
            _page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body>{opening}{article}</main></body></html>")
            self.assertEqual(receipt["status"], "PASS", opening)

    def test_nested_and_combined_css_cannot_vouch_for_text(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())

        def receipt(body):
            return self._page_receipt(f"<html><head><title>T</title></head><body>{body}</body></html>")[1]

        for body in (f"<main style='opacity:.5'><div style='opacity:.5'><div style='opacity:.3'>{article}</div></div></main>",
                     f"<main style='font-size:40%'><div style='font-size:40%'><div style='font-size:40%'>{article}</div></div></main>",
                     f"<main style='zoom:.3'><div style='zoom:.3'>{article}</div></main>",
                     f"<main style='margin-left:-199em'>{article}</main>", f"<main style='text-indent:999999px'>{article}</main>",
                     f"<main style='overflow:hidden;height:100px'>{article}</main>",
                     f"<main style='display:table-column'>{article}</main>",
                     f"<main style='color:var(--missing,transparent)'>{article}</main>",
                     f"<main style='overflow:var(--missing,hidden);height:0'>{article}</main>",
                     f"<main style='contain:paint;width:0;height:0'>{article}</main>",
                     f"<main style='font-size-adjust:0'>{article}</main>",
                     f"<main style='color:white;background:white'>{article}</main>",
                     f"<main style='letter-spacing:-50px'>{article}</main>",
                     f"<main style='opacity:2'><div style='opacity:.05'>{article}</div></main>",
                     f"<main style='transform:scale(0);transform:normal'>{article}</main>",
                     f"<main style='color:transparent;color:notacolor'>{article}</main>",
                     f"<main style='overflow:hidden visible;width:0'>{article}</main>",
                     f"<main style='--c:transparent;color:var(--c)'>{article}</main>",
                     f"<main style='color:var(--missing,#0000)'>{article}</main>",
                     f"<main style='color:#fff;background:white'>{article}</main>",
                     f"<main style='-webkit-text-fill-color:white;background:white;color:black'>{article}</main>",
                     f"<div style='display:flex'><main style='flex:0 0 0;overflow:hidden'>{article}</main></div>",
                     f"<main popover style='display:block'>{article}</main>",
                     f"<div style='--fs:0px'><main style='font-size:var(--fs)'>{article}</main></div>"):
            self.assertNotEqual(receipt(body)["status"], "PASS", body[:70])
        for body in (f"<main style='opacity:1'><div style='font-size:90%'>{article}</div></main>",
                     f"<main style='color:black;background:#fff'>{article}</main>",
                     f"<main style='font-size:var(--wp--preset--font-size--large);margin-top:var(--gap)'>{article}</main>"):
            self.assertEqual(receipt(body)["status"], "PASS", body[:70])

    def test_text_style_follows_cascade_defaults_and_attributes(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())

        def receipt(head, body):
            return self._page_receipt(f"<html><head><title>T</title></head>{head}{body}</body></html>")[1]

        hidden = (
            # the !important one wins, whatever comes later
            ("<body>", f"<main style='font-size:1px!important;font-size:16px'>{article}</main>"),
            # text against the background behind it, inherited, blended through alpha
            ("<body>", f"<main style='background:black'>{article}</main>"),
            ("<body>", f"<main style='background:#000'><p><span>{article}</span></p></main>"),
            ("<body>", f"<main style='color:rgba(0,0,0,0.05)'>{article}</main>"),
            ("<body>", f"<main style='color:hsl(0 0% 100%)'>{article}</main>"),
            ("<body>", f"<main style='background-image:url(black.png)'>{article}</main>"),
            ("<body>", f"<main style='background-clip:text;background-image:linear-gradient(#fff,#fff)'>{article}</main>"),
            ("<body>", f"<main style='background:#0000ee'><a href='/x'>{article}</a></main>"),
            # presentational attributes
            ("<body>", f"<main><font color='#ffffff'>{article}</font></main>"),
            ("<body text='#fff'>", f"<main>{article}</main>"),
            ("<body>", f"<main><table bgcolor='black'><tr><td>{article}</td></tr></table></main>"),
            ("<body link='#ffffff' vlink='#ffffff'>", f"<main><a href='/x'>{article}</a></main>"),
            ("<body>", f"<main><font face='Webdings'>{article}</font></main>"),
            # browser default sizes, and line heights inherited as lengths
            ("<body>", "<main>" + "<small>" * 6 + article + "</small>" * 6 + "</main>"),
            ("<body>", "<main>" + "<h6><span>" * 4 + article + "</span></h6>" * 4 + "</main>"),
            ("<body>", f"<main style='line-height:0'>{article}</main>"),
            ("<body>", f"<main style='line-height:16px'><span style='font-size:80px'>{article}</span></main>"),
            # spacing, decoration, bidi and fonts
            ("<body>", f"<main style='padding-left:999999px'>{article}</main>"),
            ("<body>", f"<main><span style='vertical-align:5000px'>{article}</span></main>"),
            ("<body>", f"<main><p style='width:99999px'>{article}</p></main>"),
            ("<body>", f"<main><div style='display:flex'><p style='flex:0 0 50000px'>{article}</p></div></main>"),
            ("<body>", f"<main style='text-decoration:line-through 3em'>{article}</main>"),
            ("<body>", f"<main style='unicode-bidi:bidi-override;direction:rtl'>{article}</main>"),
            ("<body>", f"<main><bdo dir=rtl>{article}</bdo></main>"),
            ("<body>", f"<main>{article}\u202e{article}</main>"),
            ("<body>", f"<main style='font-family:Wingdings'>{article}</main>"),
            ("<body>", f"<main style='content:url(x.png)'>{article}</main>"),
            ("<body>", f"<main style='color-scheme:dark'>{article}</main>"),
            ("<body>", f"<main><marquee>{article}</marquee></main>"),
            ("<body>", f"<main style='color:#fff;background:#000'><mark style='color:inherit'>{article}</mark></main>"),
            ("<body>", f"<main><pre style='tab-size:99999'>x\t{article}</pre></main>"),
            ("<body>", f"<main><p style='position:relative;left:-9999px'>{article}</p></main>"),
            # known-transparent text over an unknown backdrop; <mark> defaults lose to author CSS
            ("<body style='background-image:url(x);color:transparent'>", f"<main>{article}</main>"),
            ("<body style='background:black'>",
             f"<main><mark style='color:black;background-color:transparent'>{article}</mark></main>"),
            # em and % line heights are of the element's own font size
            ("<body>", f"<div style='font-size:80px'><main style='font-size:8px;line-height:10%'>{article}</main></div>"),
            ("<body>", f"<div style='font-size:80px'><main style='font-size:8px;line-height:.1em'>{article}</main></div>"),
            # escaped ":" and "!" are not syntax; display:contents paints no background; zero grid tracks
            ("<body>", f"<main style='color:white;color\\:black;background:white'>{article}</main>"),
            ("<body>", f"<main style='color:white;color:black\\!important;background:white'>{article}</main>"),
            ("<body>", f"<main style='display:contents;color:white;background:black'>{article}</main>"),
            ("<body>", f"<main style='display:grid;grid-auto-rows:0'>{article}</main>"),
            ("<body>", f"<main style='display:grid;grid-template-columns:repeat(4,0)'>{article}</main>"),
            ("<body>", f"<main style='display:grid;grid-template:auto / 1fr'>{article}</main>"),
            # escapes that are not name characters leave invalid values
            ("<body>", f"<main style='color:white;color:rgb\\(0\\,0\\,0\\);background:white'>{article}</main>"),
            ("<body>", f"<main style='color:white;color:black\\ ;background:white'>{article}</main>"),
            ("<body>", f"<main style='color:white;color:black\\9;background:white'>{article}</main>"),
            # escaped digits are names, not numbers; values browsers drop are not colors
            ("<body>", f"<main style='color:white;color:rgb(\\30,\\30,\\30)'>{article}</main>"),
            ("<body>", f"<main style='color:white;color:link'>{article}</main>"),
            ("<body>", f"<main style='color:white;color:rgb(0,0,0%)'>{article}</main>"),
            ("<body>", f"<main style='color:white;color:rgb(0,0,0 / 1)'>{article}</main>"),
            ("<body>", f"<main style='color:white;background:white;background:black red'>{article}</main>"),
            ("<body>", f"<main style='display:grid;grid-auto-rows:minmax(0,0fr)'>{article}</main>"),
            ("<body>", f"<main style='color:white;background:white;background:black,none'>{article}</main>"),
            ("<body>", f"<main style='height:0;height:none'>{article}</main>"),
            # geometry that adds up down the tree
            ("<body>", "<main>" + "<div style='margin-left:-16px'>" * 320 + article + "</div>" * 320 + "</main>"),
            ("<body>", "<main>" + "<div style='font-size:400%'>" * 6 + article + "</div>" * 6 + "</main>"),
            ("<body style='height:0'>", f"<main style='height:inherit'>{article}</main><div>{article}</div>"),
            ("<body>", "<div style='font-size:64px'>" + "<div style='padding-left:15em'>" * 2
             + f"<main style='padding-left:15em;font-size:16px'>{article}</main>" + "</div>" * 3),
            ("<body>", f"<main style='border:4000px solid white'>{article}</main>"),
            ("<body>", "<main>" + "<div style='padding-inline:7px 0'>" * 300 + article + "</div>" * 300 + "</main>"),
            ("<body>", "<main>" + "<div style='margin-inline:-7px 0'>" * 300 + article + "</div>" * 300 + "</main>"),
            ("<body>", "<main>" + "<div style='position:relative;left:8px'>" * 300 + article + "</div>" * 300 + "</main>"),
            ("<body>", "<main>" + "<div style='position:relative;top:5px'>" * 2 + article + "</div>" * 2 + "</main>"),
            ("<body>", f"<div style='margin-left:900px'><main style='margin-left:inherit'>{article}</main></div>"),
            ("<body>", "<main>" + "<div style='width:150%'>" * 3 + article + "</div>" * 3 + "</main>"),
            ("<body>", f"<main><div style='padding-left:1000px'><div style='padding-left:900px;text-indent:900px'>"
                       f"{article}</div></div></main>"),
            ("<body>", "<main><p>" + "<span style='vertical-align:1.5em'>" * 4 + article + "</span>" * 4 + "</p></main>"),
            # lines that do not wrap: preserved spaces, nowrap, max-content
            ("<body>", f"<main style='white-space:pre'>{' ' * 1000}{ARTICLE * 2}</main>"),
            ("<body>", f"<main><pre>{' ' * 1000}{ARTICLE * 2}</pre></main>"),
            ("<body>", f"<main style='white-space:nowrap'><p>{ARTICLE.replace(chr(10), ' ') * 2}</p></main>"),
            ("<body>", f"<main style='width:max-content'><p>{ARTICLE.replace(chr(10), ' ') * 2}</p></main>"),
            ("<body>", f"<main style='width:-moz-max-content'><p>{ARTICLE.replace(chr(10), ' ') * 2}</p></main>"),
            # a value browsers drop leaves the one before it in force
            ("<body>", f"<main style='white-space:nowrap;white-space:bogus'><p>{ARTICLE.replace(chr(10), ' ') * 2}</p>"
                       "</main>"),
            ("<body>", f"<main style='width:max-content;width:bogus'><p>{ARTICLE.replace(chr(10), ' ') * 2}</p></main>"),
            ("<body>", f"<main style='text-wrap-mode:nowrap;text-wrap-mode:bogus'><p>{ARTICLE.replace(chr(10), ' ') * 2}"
                       "</p></main>"),
            ("<body>", f"<main style='border-left:4000px solid white;border-left:bogus'>{article}</main>"),
            ("<body>", f"<main style='white-space:pre;white-space:bogus'>{' ' * 1000}{ARTICLE * 2}</main>"),
            # unbreakable lines: long words in preformatted text, <nobr>, <td nowrap>, words joined by no-break spaces
            ("<body>", f"<main><pre>{('A' * 1000 + ' ') * 6}</pre>{article}</main>"),
            ("<body>", f"<main><nobr>{ARTICLE.replace(chr(10), ' ') * 2}</nobr></main>"),
            ("<body>", f"<main><table><tr><td nowrap>{ARTICLE.replace(chr(10), ' ') * 2}</td></tr></table></main>"),
            ("<body>", f"<main><p>{ARTICLE.replace(chr(10), ' ').replace(' ', '&nbsp;') * 2}</p></main>"),
            # percentages inside math functions are of the parent's width; vertical writing is not measured
            ("<body>", "<main>" + "<div style='width:calc(150%)'>" * 2 + article + "</div>" * 2 + "</main>"),
            # an inline box's padding leaves the width of blocks inside it; negative margins widen a block
            ("<body>", "<main><span style='padding:0 900px'>" + "<div style='width:150%'>" * 2 + article
             + "</div>" * 2 + "</span></main>"),
            ("<body>", "<main>" + "<div style='margin-right:-200px'>" * 6 + article + "</div>" * 6 + "</main>"),
            # margins browsers keep (a math margin may be negative); blocks pulled over each other
            ("<body>", f"<main>{article}</main><div style='margin-top:calc(-300px);height:300px;background:white'>x</div>"),
            ("<body>", f"<main>{article}</main><div style='margin-top:-300px;margin-top:bogus;height:300px;"
                       "background:white'>x</div>"),
            ("<body>", "<main>" + f"<div style='line-height:12px;margin:0 0 -12px;background:white'>{article}</div>" * 4
             + "</main>"),
            # lines that do not wrap: a hidden break starts none; sizes by piece; inline boxes take room
            ("<body>", "<main style='white-space:nowrap'>" + "x" * 200 + "<br hidden>" + "y" * 200
             + f"</main><main>{article}</main>"),
            ("<body>", "<main><pre><span style='font-size:40px'>" + "W" * 80 + "</span><span style='font-size:6px'>"
             + "z" * 400 + f"</span></pre>{article}</main>"),
            ("<body>", f"<main style='white-space:nowrap'><span style='display:inline-block;width:1900px'></span>"
                       f"<p>{ARTICLE.replace(chr(10), ' ')}</p></main>"),
            ("<body>", f"<main style='width:max-content;width:calc(foo)'><p>{ARTICLE.replace(chr(10), ' ') * 2}</p>"
                       "</main>"),
            ("<body>", "<main style='writing-mode:vertical-rl'>" + "<div style='padding-block-start:700px'>" * 3
             + article + "</div>" * 3 + "</main>"),
            # revert goes back to the browser's value, which for these is the parent's
            ("<body>", f"<div style='font-size:1px'><main style='font-size:revert'>{article}</main></div>"),
            ("<body>", f"<div style='color:white'><main style='color:revert'>{article}</main></div>"),
            ("<body>", f"<div style='line-height:0'><main style='line-height:revert'>{article}</main></div>"),
            # text a set height cannot hold spills over what follows
            ("<body>", f"<main><div style='height:0'><div>{article}</div></div><div>{article}</div></main>"),
            ("<body>", f"<main style='display:grid;height:0;grid-auto-rows:minmax(0,1fr)'>{article}</main>"),
            ("<body>", f"<main style='display:grid;grid-auto-rows:20px'>{article}</main>"),
            # var() fallbacks nested past any use
            ("<body>", "<main style='font-size:" + "var(--x," * 1100 + "16px" + ")" * 1100 + f"'>{article}</main>"),
            # hidden whatever the display; fallback content browsers do not show
            ("<body>", f"<main hidden='until-found' style='display:block'>{article}</main><p>Short.</p>"),
            ("<body>", f"<main><meter>{article}</meter><progress>{article}</progress></main><p>Short.</p>"),
        )
        for head, body in hidden:
            self.assertNotEqual(receipt(head, body)["status"], "PASS", (head, body[:80]))

    def test_elements_that_can_cover_or_push_text_leave_the_page_unverified(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        covers = (
            "<div style='position:fixed;inset:0;background:white;z-index:9999'></div>",
            "<div style='position:sticky;top:0;height:100vh;background:#fff'></div>",
            "<div style='position:relative;height:0'><div style='height:3000px;background:#fff'></div></div>",
            "<div style='position:relative;top:200px;height:900px;background:#fff'></div>",
            "<span style='outline:500px solid white'></span>",
            "<span style='box-shadow:0 0 0 400px #fff'></span>",
            "<p>x<span style='padding:40px 0;background:#fff'>y</span></p>",
            "<dialog open><div style='height:900px;width:900px'></div></dialog>",
            "<div style='transform:translateY(200px);height:500px;background:#fff'></div>",
            "<div style='-webkit-transform:translateY(300px);height:500px;background:#fff'></div>",
            "<div style='clip-path:circle(4999px);height:0'><div style='height:3000px;background:#fff'></div></div>",
            "<div style='position:relative;height:10px'><div style='height:3000px;background:#fff'></div></div>",
            "<div style='opacity:.99;height:0'><div style='height:3000px;background:#fff'></div></div>",
            "<div style='display:grid'><div style='grid-area:1/1;background:#fff'></div></div>",
            "<div style='margin-top:-300px;height:300px;background:#fff'></div>",
            "<div style='margin-top:-300px;margin-top:calc(1px +);height:300px;background:white'>x</div>",
            "<div style='margin-top:-300px;margin-top:calc(var());height:300px;background:white'>x</div>",
            "<img src='w.png' style='margin-top:-300px' width='900' height='300'>",
            "<div style='height:99999px'></div>", "<div style='height:calc(100px * 1000)'></div>",
            "<div style='margin-top:1e6px'></div>", "<img src='x' vspace='99999'>",
            "<table width='99999'><tr><td>x</td></tr></table>",
            "<div style='--h:99999px;height:var(--h)'></div>",
            "<table><colgroup><col style='visibility:collapse'></colgroup><tr><td>x</td></tr></table>",
            "<svg overflow='visible' width='10' height='10'><rect x='-5000' y='-5000' width='10000' height='10000'/></svg>",
            "<math><mi style='position:fixed;inset:0;background:white'>x</mi></math>",
            "<svg><foreignObject><div style='position:fixed;inset:0;background:#fff'></div></foreignObject></svg>",
            "<script style='display:block;position:fixed;inset:0;background:#fff'></script>",
            "<iframe style='position:fixed;inset:0;width:100%;height:100%' src='about:blank'></iframe>",
            "<div style='position:\\61 bsolute;inset:0;width:100%;height:100%;background:#fff'></div>",
            "<div style='position:relative;overflow:hidden'><div style='position:fixed;inset:0;background:#fff'></div></div>",
            # a relative box that does not clip is no container for what is positioned in it
            "<div style='position:relative'><div style='position:absolute;inset:0;width:100vw;height:100vh;"
            "background:white;z-index:999'></div></div>",
            "<div style='position:relative;overflow:hidden;overflow-y:visible'><div style='position:absolute;inset:0;"
            "width:100vw;height:100vh;background:white'></div></div>",
            # no clip box: display:contents makes no box, inline boxes do not clip, the
            # logical overflow that wins shows, and dropped overflow values clip nothing
            "<div style='position:relative;display:contents;overflow:hidden'><div style='position:absolute;"
            "z-index:999;inset:0;width:100vw;height:100vh;background:white'></div></div>",
            "<span style='position:relative;overflow:hidden'><span style='position:absolute;inset:0;width:100vw;"
            "height:100vh;background:white'></span></span>",
            "<div style='position:relative;overflow:hidden;overflow-block:visible;overflow-inline:visible'>"
            "<div style='position:absolute;inset:0;width:100vw;height:100vh;background:white'></div></div>",
            "<div style='position:relative;overflow-x:hidden nope;overflow-y:hidden nope'><div style='position:absolute;"
            "inset:0;width:100vw;height:100vh;background:white'></div></div>",
            # an <svg> inside MathML is not clipped by any <svg> around it
            "<math><mi><svg style='position:fixed;width:100vw;height:100vh' viewBox='0 0 1 1'>"
            "<rect width='1' height='1' fill='#fff'/></svg></mi></math>",
        )
        for cover in covers:
            for html in (f"<html><head><title>T</title></head><body>{cover}<main>{article}</main></body></html>",
                         f"<html><head><title>T</title></head><body><main>{article}{cover}</main></body></html>"):
                page, receipt = self._page_receipt(html)
                self.assertNotEqual(receipt["status"], "PASS", cover)
                self.assertIn("visibility_unknown", page["page_signals"], cover)

    def test_the_page_read_directly_is_words_when_set_against_jina(self):
        from x_reader.reader import _same_text

        shown = ARTICLE * 2
        note = "<note " + " ".join(f"Section {i} of the page adds its own detail in full." for i in range(60)) + ">"
        self.assertTrue(_same_text(shown, shown))
        self.assertFalse(_same_text(shown, shown + "\n" + note))  # most of the page is not in the Jina text

    def test_lines_that_cannot_wrap_count_every_width_they_take(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        prose = ARTICLE.replace(chr(10), " ")
        # (300 letters run 880px past a 2000px screen: under a tenth of the text, until the box's width counts)
        for line in ("<span style='display:inline-block;min-width:1900px'></span>" + "x" * 300,
                     "<span style='display:inline-block;width:calc(1900px)'></span>" + "x" * 300):
            page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body><main>{article}"
                                               f"<p style='white-space:nowrap'>{line}</p></main></body></html>")
            self.assertNotEqual(receipt["status"], "PASS", line)  # used widths push the text past the screen
        page, receipt = self._page_receipt("<html><head><title>T</title></head><body><main><p style='white-space:"
                                           f"nowrap;letter-spacing:16px'>{prose}</p></main></body></html>")
        self.assertNotEqual(receipt["status"], "PASS")  # spacing between letters widens the line
        page, receipt = self._page_receipt("<html><head><title>T</title></head><body><main>"
                                           + "<div style='width:150%;width:calc(1px +)'>" * 2 + article
                                           + "</div>" * 2 + "</main></body></html>")
        self.assertNotEqual(receipt["status"], "PASS")  # a width browsers drop leaves the one before it
        for main in ("width:calc((1000 + 1000) * 3px)", "width:150%;width:calc(var())",
                     "border-left:4000px solid white;border-left:0 solid dotted white",
                     # a variable's fallback is a value it may take
                     "margin-left:calc(var(--missing, 6000px))", "padding-left:calc(var(--missing, 6000px))",
                     "width:calc(var(--missing, 6000px))", "border-left:calc(var(--missing, 6000px)) solid white"):
            page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body><main style='{main}'>"
                                               f"{article}</main></body></html>")
            self.assertNotEqual(receipt["status"], "PASS", main)  # values worked out, dropped ones left out
        page, receipt = self._page_receipt(
            "<html><head><title>T</title></head><body><p style='white-space:nowrap'><span style='display:inline-block;"
            f"width:2000px'></span><span style='display:inline-block;width:300px;white-space:normal'>{prose}</span>"
            "</p></body></html>")
        self.assertNotEqual(receipt["status"], "PASS")  # a box laid out whole starts where the line has got to
        line = "This is a complete ordinary sentence with enough prose for the reader to see. " * 2
        for spaced in (f"<pre style='word-spacing:32px'>{(line.strip() + chr(10)) * 8}</pre>",
                       "<p style='white-space:nowrap;word-spacing:40px'>" + "a\u00a0" * 300 + f"</p>{article}"):
            page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body><main>{spaced}</main>"
                                               "</body></html>")
            self.assertNotEqual(receipt["status"], "PASS", spaced[:40])  # spacing after every word separator
        words = "".join(f"<span>word{i} </span>" for i in range(120))
        page, receipt = self._page_receipt("<html><head><title>T</title></head><body><main><p style='white-space:"
                                           f"nowrap;font-size:8px'>{words}</p></main></body></html>")
        self.assertNotEqual(receipt["status"], "PASS")  # spaces collapse across text nodes, and still take room
        page, receipt = self._page_receipt(
            f"<html><head><title>T</title></head><body><main>{article}<p style='white-space:nowrap'>"
            f"<span style='display:inline-block;width:300px;white-space:normal'>{prose}</span></p></main></body></html>")
        self.assertEqual(receipt["status"], "PASS")  # a fixed-width box laid out whole wraps its own lines

    def test_a_cover_inside_a_clipping_box_only_unsettles_that_box(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        embed = ("<div style='position:relative;padding-bottom:56.25%;height:0;overflow:hidden'>"
                 "<iframe src='https://www.youtube.com/embed/x' style='position:absolute;top:0;left:0;width:100%;"
                 "height:100%'></iframe></div>")
        page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body><main>{embed}{article}</main></body></html>")
        self.assertEqual(receipt["status"], "PARTIAL")  # the player is unread; the text is not in doubt
        boxed = (f"<div style='position:relative;overflow:hidden'>{article}"
                 "<div style='position:absolute;inset:0;background:#fff'></div></div>")
        page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body><main>{boxed}</main></body></html>")
        self.assertNotEqual(receipt["status"], "PASS")

    def test_ordinary_inline_styling_and_old_markup_still_pass(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        for head, body in (
            ("<body>", f"<main><div style='column-width: calc( 0.9 * 30em );'><p style='margin:0 4em;padding:0 0.25em'>"
                       f"{article}</p></div><span style='background:#ffcc55'>tag</span></main>"),
            ("<body>", f"<main>{article}<p>a <code style='background:#f5f5f5;padding:2px 4px'>x</code></p></main>"),
            ("<body>", f"<main><pre style='background:#272822;color:#f8f8f2'>code here</pre>{article}</main>"),
            ("<body bgcolor='#ffffff' background='bel.gif' text='#000000' link='#000099' vlink='#464646'>",
             f"<main><font size=2 face='verdana'>{article}<font color=#dddddd>[1]</font></font></main>"),
            ("<body>", f"<main><svg width='16' height='16' viewBox='0 0 10000 10000'><rect width='10000' height='10000' "
                       f"style='transform:rotate(45deg)'/></svg>{article}</main>"),
            ("<body>", f"<main><ruby>x<rp>(</rp><rt>y</rt><rp>)</rp></ruby>{article}</main>"),
            ("<body>", f"<main>{article}<p>Note <sup style='position:relative;top:-0.3em'>1</sup></p></main>"),
            ("<body style='position:relative'>", f"<main style='position:relative;max-width:800px'>{article}</main>"),
            ("<body>", f"<main>{article}<p>A <mark>highlighted</mark> phrase.</p></main>"),
            ("<body>", f"<main style='display:grid;grid-template-columns:minmax(0,1fr) 200px;grid-auto-rows:auto'>"
                       f"{article}</main>"),
            ("<body>", f"<main><xmp>shown as written</xmp>{article}</main>"),
            ("<body>", f"<main style='height:100%'>{article}</main>"),
            ("<body>", f"<main style='font-size:1px;font-size:16px;height:0;height:auto'>{article}</main>"),
            ("<body>", f"<main style='color:rgb(0 0 0 / 1)'>{article}</main>"),
            ("<body>", f"<nav style='position:static;left:10000px'>Home</nav><div style='display:flex;order:1;order:0'>"
                       f"Safe order</div><main>{article}</main>"),
            ("<body>", f"<main><p dir='rtl'>مرحبا بالعالم</p>{article}</main>"),
            ("<body>", f"<nav style='position:absolute;position:static'>Home</nav><nav style='position:relative;"
                       f"left:100px;left:0'>Home</nav><main style='float:right;float:none'>{article}</main>"),
            ("<body>", f"<main style='display:flex;grid-auto-rows:0'>{article}</main>"),  # grid tracks need a grid
            ("<body>", f"<main><p>漢<ruby>字<rt>かんじ</rt></ruby>の読み方。</p><img src='a.jpg' style='float:left' "
                       f"alt='A chart'>{article}</main>"),
            ("<body>", f"<main><ul style='list-style-type:disc'><li>{article}</li></ul></main>"),
            ("<body>", f"<main><div style='position:relative;overflow:hidden;height:200px'><img src='x.jpg' "
                       f"style='position:absolute;inset:0;width:100%;height:100%'></div>{article}</main>"),
            ("<body>", f"<main style='font-family:\\5FAE\\8F6F\\96C5\\9ED1'>{article}</main>"),
            # winners decide: geometry reset later, the webkit name of a property, tables in order
            ("<body>", f"<main style='margin-left:1000px;margin-left:1000px;margin-left:1000px;margin-left:0'>"
                       f"{article}</main>"),
            ("<body>", "<main>" + "<div style='margin-left:-16px;margin-left:0'>" * 320 + article + "</div>" * 320
             + "</main>"),
            ("<body>", f"<div style='display:flex;-webkit-flex-direction:row-reverse;flex-direction:row'><span>a</span>"
                       f"<span>b</span></div><main>{article}</main>"),
            ("<body>", f"<main><table><caption>Table 1</caption><thead><tr><td>Name</td></tr></thead><tbody><tr>"
                       f"<td>Row</td></tr></tbody><tfoot><tr><td>Sum</td></tr></tfoot></table>{article}</main>"),
            ("<body>", f"<main><pre>    def f():\n        return 1\n</pre>{article}</main>"),
            ("<body>", f"<main><img src='a.png' align='right' alt=''>{article}</main>"),
            ("<body>", f"<main><p dir='rtl'>\u0645\u0631\u062d\u0628\u0627 Wikipedia \u0662\u0660\u0662\u0664</p>{article}</main>"),
            ("<body>", f"<main style='font-size:revert;color:revert;line-height:revert'>{article}</main>"),
            ("<body>", f"<main style='border:1px solid #ddd;padding:1em'>{article}</main>"),
            ("<body>", f"<main>{article}<p>E=mc<sup>2</sup>, H<sub>2</sub>O, caf\u00e9 and "
                       f"<span style='white-space:nowrap'>2024-01-01</span></p></main>"),
            ("<body>", "<main>" + "<div style='width:100%'>" * 5 + article + "</div>" * 5 + "</main>"),
            # hidden boxes pull nothing; margins by the values that win; hidden table parts; short joined words
            ("<body>", "<div hidden style='margin:-8px'>x</div>" * 63 + f"<main>{article}</main>"),
            ("<body>", f"<main style='margin-left:-300px;margin-left:0'>{article}</main>"),
            ("<body>", "<main>" + "<div style='padding:0 20px'>" * 5 + article + "</div>" * 5 + "</main>"),
            # hidden SVG and MathML place nothing; risky values that lose; hidden table parts; an address
            ("<body>", f"<svg hidden><text>Jan</text><text>Feb</text></svg><svg style='display:none'><text>Jan</text>"
                       f"<text>Feb</text></svg><math hidden><mmultiscripts><mi>x</mi><mprescripts/><mi>y</mi><none/>"
                       f"</mmultiscripts></math><main>{article}</main>"),
            ("<body>", f"<main style='opacity:0;opacity:1;visibility:hidden;visibility:visible;overflow:hidden;"
                       f"overflow:visible'>{article}</main>"),
            ("<body>", f"<table><tr><td>Row</td></tr><caption style='visibility:hidden'>Cap</caption></table>"
                       f"<main>{article}<address>Leo Labs<br>Somewhere 1<br>Town</address></main>"),
            # a long code line is overflow a reader scrolls to, while it is a small part of the text
            ("<body>", f"<main>{article}<pre>{'x' * 300}</pre></main>"),
            ("<body>", f"<table><tr><td>Row</td></tr><caption hidden>Cap</caption></table><main>{article}</main>"),
            ("<body>", f"<main>{article}<p>Price:&nbsp;10&nbsp;USD, 5&nbsp;km and https://example.com/{'a' * 300}"
                       f"</p><p>A <span style='display:inline-block'>badge</span> beside <bdi>Ann</bdi>.</p></main>"),
            ("<body>", f"<main><p>E = <math><mi>m</mi><msup><mi>c</mi><mn>2</mn></msup></math></p><svg width='200' "
                       f"height='40'><text x='0' y='20'>Figure 1</text></svg>{article}</main>"),
            ("<body>", f"<main style='display:none;display:block'>{article}</main>"),
            ("<body>", f"<main style='overflow:hidden;overflow:initial;content:\"x\";content:normal;"
                       f"text-overflow:\"x\";text-overflow:ellipsis;border-left:3px solid #c00;"
                       f"margin:calc(2 * 6px) calc(10% - 50px)'>{article}</main>"),
            ("<body>", f"<svg><text style='display:none'>Jan</text><text style='display:none'>Feb</text></svg>"
                       f"<main style='overflow:hidden;overflow-x:visible;overflow-y:visible;"
                       f"border-left:4000px solid white;border-left:0;margin:calc(10px + 2px)'>{article}</main>"),
            ("<body>", f"<main style='white-space:pre-wrap;width:800px;max-width:100%;column-count:2;"
                       f"border:1px solid rgba(0,0,0,.1);border-left:3px solid #c00'>{article}"
                       f"<h2 style='text-wrap:balance'>A title</h2></main>"),
        ):
            page, receipt = self._page_receipt(f"<html><head><title>T</title></head>{head}{body}</body></html>")
            self.assertEqual(receipt["status"], "PASS", (head, body[:80]))

    def test_walls_are_read_as_the_page_lays_out_its_text(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        for notice in ("<div>Sub<span>scribe</span> to continue.</div>", "<div>Sub<!--x-->scribe to continue.</div>",
                       "<div>Subscribe\nto continue.</div>", "<div>登录后阅<span>读</span>全文</div>",
                       "<div style='display:flex'><div>Sub</div><div>scribe to continue.</div></div>",
                       "<div style='float:left'>Sub</div><div style='float:left'>scribe to continue.</div>",
                       "<div style='display:inline'>Sub</div><div style='display:inline'>scribe to continue.</div>",
                       "<div>&lt;Subscribe to continue&gt;</div>",
                       "<xmp><notice value='Subscribe to continue.'></xmp>",
                       # annotations sit above the base text; order and CSS-made text are not the source
                       "<div><ruby>Sub<rt>ab</rt></ruby>scribe to continue.</div>",
                       "<div style='display:flex'><span style='order:2'>continue.</span>"
                       "<span style='order:1'>Subscribe to </span></div>",
                       "<div style='display:flex;flex-direction:row-reverse'><span>continue.</span>"
                       "<span>Subscribe to </span></div>",
                       "<div dir='rtl' style='display:flex'><span>continue.</span><span>Subscribe to </span></div>",
                       "<div style='float:right'>continue.</div><div style='float:right'>Subscribe to </div>",
                       "<ul><li style=\"list-style-type:'Subscribe '\">to continue.</li></ul>",
                       "<p><q style='quotes:\"Subscribe \" \"\"'>to continue.</q></p>",
                       "<img src='missing.png' alt='Please sign in to continue.'>",
                       # escaped layout values, inherited right-to-left, known gaps at some edges,
                       # boxes display:contents leaves out, whole annotations, data blocks shown by CSS
                       "<div style='display:\\66 lex;gap:20px'><div>Subscribe</div><div>to continue.</div></div>",
                       "<div dir='rtl'><div style='display:flex'><span>continue.</span><span>Subscribe to </span></div></div>",
                       "<div>Sub<b>scribe</b><i style='margin-left:5px'>to</i><em style='margin-left:5px'>continue.</em></div>",
                       "<div style='display:flex;gap:20px'><div style='display:contents'><div>Subscribe</div>"
                       "<div>to continue.</div></div></div>",
                       "<ruby>word<rt>Subscribe <b>to</b> continue.</rt></ruby>",
                       "<script type='text/plain' style='display:block'>Subscribe to continue reading.</script>",
                       # multi-keyword displays, dropped values, CSS-wide keywords, logical floats in rtl
                       "<div style='display:block flex'><div>Sub</div><div>scribe to continue.</div></div>",
                       "<div style='display:flex;display:bogus'><div>Sub</div><div>scribe to continue.</div></div>",
                       "<script type='text/plain' style='display:initial'>Subscribe to continue reading.</script>",
                       "<script type='text/plain' style='all:initial'>Subscribe to continue reading.</script>",
                       "<div dir='rtl'><div dir='ltr' style='display:flex;direction:inherit'><span>continue.</span>"
                       "<span>Subscribe to </span></div></div>",
                       "<div dir='rtl'><div style='float:inline-start'>continue.</div>"
                       "<div style='float:inline-start'>Subscribe to </div></div>",
                       # hidden text and the breaks of hidden elements take no place in the layout
                       "<div>Sub<span hidden>x</span>scribe to continue.</div>",
                       "<div>Sub<span style='display:none'>x</span>scribe to continue.</div>",
                       "<div>Sub<div hidden></div>scribe to continue.</div>",
                       "<div>Please sign <br hidden>in to continue.</div>",
                       # a value browsers drop leaves the one before it; displays as their one-word names
                       "<div style='display:flex;flex-direction:row-reverse;flex-direction:bogus'><span>continue.</span>"
                       "<span>Subscribe to </span></div>",
                       "<div style='float:right;float:bogus'>continue.</div><div style='float:right'>Subscribe to </div>",
                       "<div style='display:block flex;direction:rtl'><span>continue.</span><span>Subscribe to </span></div>",
                       # the old flexbox, wrapping in reverse, column flow
                       "<div style='display:-webkit-box;-webkit-box-direction:reverse'><span>continue.</span>"
                       "<span>Subscribe to </span></div>",
                       "<div style='display:-webkit-box'><span style='-webkit-box-ordinal-group:2'>continue.</span>"
                       "<span>Subscribe to </span></div>",
                       "<div style='display:flex;flex-wrap:wrap-reverse'><div style='width:100%'>continue.</div>"
                       "<div style='width:100%'>Subscribe to</div></div>",
                       "<div style='display:grid;grid-auto-flow:column;grid-template-rows:auto auto'><span>to</span>"
                       "<span>continue.</span><span>Subscribe</span><span>x</span></div>",
                       # hidden by default, shown by an inline display
                       "<datalist style='display:block'><option>Subscribe to continue.</option></datalist>",
                       "<ruby>x<rp style='display:inline'>Subscribe to continue.</rp><rt>y</rt></ruby>",
                       # tables show captions, headers and footers where they belong, right-to-left cells from the right
                       "<table><tfoot><tr><td>continue.</td></tr></tfoot><tbody><tr><td>Subscribe to</td></tr></tbody></table>",
                       "<table><tbody><tr><td>continue.</td></tr></tbody><thead><tr><td>Subscribe to</td></tr></thead></table>",
                       "<table><tr><td>continue.</td></tr><caption>Subscribe to</caption></table>",
                       "<table dir='rtl'><tr><td>continue.</td><td>Subscribe to</td></tr></table>",
                       "<table dir='rtl'><tr dir='ltr'><td>continue.</td><td>Subscribe to</td></tr></table>",
                       "<div dir='rtl'><div style='display:table-cell'>continue.</div>"
                       "<div style='display:table-cell'>Subscribe to</div></div>",
                       "<table align='right'><tr><td>continue.</td></tr></table>"
                       "<table align='right'><tr><td>Subscribe to</td></tr></table>",
                       # right-to-left text shows left-to-right words around right-to-left letters reordered
                       "<p dir='rtl'>continue.\u200fSubscribe to</p>", "<p dir='rtl'>continue. \u0628 Subscribe to</p>",
                       "<p style='direction:rtl'>continue.\u200fSubscribe to</p>",
                       "<p>\u2067continue.\u200fSubscribe to\u2069</p>", "<p><bdi>\u200fcontinue.\u200fSubscribe to</bdi></p>",
                       "<p dir='auto'>\u200fcontinue.\u200fSubscribe to</p>",
                       "<p dir='rtl'><span style='direction:ltr'>continue \u200fSubscribe to</span></p>",
                       "<p style='unicode-bidi:plaintext'>\u200fcontinue.\u200fSubscribe to</p>",
                       "<p style='unicode-bidi:plaintext;unicode-bidi:bogus'>\u200fcontinue.\u200fSubscribe to</p>",
                       "<div dir='rtl' style='column-count:2'><p>continue.</p><p>Subscribe to</p></div>",
                       # SVG text placed by coordinates; right-to-left math; prescripts before their base
                       "<svg width='600' height='40'><text x='300' y='20'>continue.</text>"
                       "<text x='0' y='20'>Subscribe to</text></svg>",
                       "<svg width='600' height='40'><text y='20'><tspan x='300'>continue.</tspan>"
                       "<tspan x='0'>Subscribe to</tspan></text></svg>",
                       "<div dir='rtl'><math><mi>continue.</mi><mi>Subscribe to</mi></math></div>",
                       "<math><mmultiscripts><mi>continue.</mi><mprescripts/><mi>Subscribe to</mi><none/>"
                       "</mmultiscripts></math>",
                       # text that may draw nothing: the display that wins, a zero font size
                       "<div>Sub<span style='display:block;display:none'>x</span>scribe to continue.</div>",
                       "<div>Sub<span style='font-size:0'>x</span>scribe to continue.</div>",
                       # reversed text: <bdo>, bidi overrides, override characters in annotations
                       "<bdo dir='rtl'>.eunitnoc ot ebircsbuS</bdo>",
                       "<span style='unicode-bidi:bidi-override;direction:rtl'>.eunitnoc ot ebircsbuS</span>",
                       "<ruby>x<rt>\u202e.eunitnoc ot ebircsbuS</rt></ruby>",
                       # isolates and inline boxes laid out whole sit in right-to-left text as one object each
                       "<p dir='rtl'><bdi>continue.</bdi> <bdi>Subscribe to</bdi></p>",
                       "<p dir='rtl'><span style='display:inline-block'>continue.</span> "
                       "<span style='display:inline-block'>Subscribe to</span></p>",
                       "<p dir='rtl'>continue. <bdi>Subscribe to</bdi></p>",
                       # table parts outside a table are ordered like a table's; displays are CSS, not raw text
                       "<div><span style='display:table-row'>continue.</span>"
                       "<span style='display:table-caption'>Subscribe to</span></div>",
                       "<div><span style='display:table-footer-group'>continue.</span>"
                       "<span style='display:table-row'>Subscribe to</span></div>",
                       "<div dir='rtl'><div style='DISPLAY:TABLE-CELL'>continue.</div>"
                       "<div style='display:table-cell'>Subscribe to</div></div>",
                       # each piece that may or may not draw is read kept and left out, in every combination
                       "<div>Sub<span style='font-size:0'>x</span>scribe <span style='bogus:1'> to continue.</span></div>",
                       "<div><span style='visibility:hidden'>x<span style='visibility:visible'>Sub</span>x</span>"
                       "scribe to continue.</div>",
                       # a notice a line break wraps onto two lines
                       "<div>Subscribe to<br>continue.</div>", "<div>Subscribe<br>to<br>continue.</div>",
                       "<pre>Subscribe\nto\ncontinue.</pre>",
                       # a notice split over paragraphs or list items reads as one
                       "<p>Subscribe to</p><p>continue.</p>", "<p>Subscribe</p><p>to</p><p>continue.</p>",
                       "<p>Please sign</p><p>in to continue.</p>", "<p>登录后</p><p>查看全文</p>",
                       "<p>Subscribe to</p><p hidden>xx</p><p>continue.</p>",
                       "<p>Subscribe to</p><p>continue reading this and all the other stories we publish every day for "
                       "our readers around the world, from every desk and every bureau we have.</p>",
                       # spacing a math value works out to; a border whose style draws nothing
                       "<div>Sub<span style='margin-right:calc(10px - 5px)'>scribe</span>to continue.</div>",
                       "<div>Sub<span style='border-left-width:10px;border-left-style:none'>scribe</span>"
                       " to continue.</div>",
                       # a piece that may not draw beside a line break; space set at one edge only
                       "<div>Sub<span style='font-size:0'>x</span>scribe to<br>continue.</div>",
                       "<div>Sub<span style='margin-right:10px'>scribe</span>to continue.</div>",
                       # text made by CSS: replaced content, hyphenation strings, custom ellipses
                       "<div style=\"content:'Subscribe to continue.'\">x</div>",
                       "<p style=\"hyphenate-character:'Subscribe to continue.';width:3em\">Super&shy;califragilistic</p>",
                       "<p style=\"white-space:nowrap;overflow:hidden;text-overflow:'Subscribe to continue.'\">"
                       + ARTICLE.replace(chr(10), " ") + "</p>",
                       "<div>" + ARTICLE.replace(chr(10), " ") * 3 + " Subscribe to<br>continue. "
                       + ARTICLE.replace(chr(10), " ") * 3 + "</div>",
                       # isolate controls; controls laid out whole whatever their display; SVG overrides and glyph
                       # positions; prose in SVG that is not returned as page text
                       "<p dir='rtl'>\u2066continue.\u2069 \u2066Subscribe to\u2069</p>",
                       "<p dir='rtl'><input style='display:inline' type='button' value='continue.'> "
                       "<input style='display:inline' type='button' value='Subscribe to'></p>",
                       "<svg><text style='unicode-bidi:bidi-override;direction:rtl'>.eunitnoc ot ebircsbuS</text></svg>",
                       "<svg><text x='220 210 200 190 180 170 160 150 140 130 120 110 100 90 80 70 60 50 40 30 20 10'>"
                       ".eunitnoc ot ebircsbuS</text></svg>",
                       "<svg width='800' height='600'><foreignObject width='800' height='600'><div>"
                       + ARTICLE.replace(chr(10), ' ') * 2 + "</div></foreignObject></svg>",
                       # HTML inside foreignObject is laid out as HTML; visibility a hidden SVG shows again
                       "<svg><foreignObject><bdo dir='rtl'>.eunitnoc ot ebircsbuS</bdo></foreignObject></svg>",
                       "<svg style='visibility:hidden'><text style='visibility:visible;unicode-bidi:bidi-override;"
                       "direction:rtl'>.eunitnoc ot ebircsbuS</text></svg>",
                       # table parts display:contents hands on; a caption side browsers drop
                       "<div><span style='display:table-row'>continue.</span><span style='display:contents'>"
                       "<span style='display:table-caption'>Subscribe to</span></span></div>",
                       "<table><tbody><tr><td>continue.</td></tr></tbody>"
                       "<caption style='caption-side:top;caption-side:bogus'>Subscribe to</caption></table>",
                       # a caption side display:contents hands on; caption text shown again inside a hidden caption
                       "<div><span style='display:contents;caption-side:bottom'><span style='display:table-caption'>"
                       "continue.</span></span><span style='display:table-row'>Subscribe to</span></div>",
                       "<table><tr><td>continue.</td></tr><caption style='visibility:hidden'>"
                       "<span style='visibility:visible'>Subscribe to</span></caption></table>",
                       # wording a control shows from its attributes; an SVG a CSS-wide display shows again
                       "<table><tr><td>continue.</td></tr><caption><input type='button' value='Subscribe to'>"
                       "</caption></table>",
                       "<svg style='display:none;display:initial'><text style='unicode-bidi:bidi-override;"
                       "direction:rtl'>.eunitnoc ot ebircsbuS</text></svg>",
                       "<svg><text x='0' y='30'>continue.</text><text x='0' y='10' style='display:none;"
                       "display:initial'>Subscribe to</text></svg>",
                       "<svg><text x='0' y='30'>continue.</text><text x='0' y='10' display='none' "
                       "style='display:inline'>Subscribe to</text></svg>",
                       "<svg><text x='0' y='30'>continue.</text><text x='0' y='10' style='display:none;all:initial'>"
                       "Subscribe to</text></svg>",
                       "<div style=\"content:'Subscribe to continue.';content:clip\">x</div>",
                       "<p style=\"hyphenate-character:'Subscribe to continue.';hyphenate-character:none;width:3em\">"
                       "Super&shy;califragilistic</p>",
                       "<p style=\"white-space:nowrap;overflow:hidden;text-overflow:'Subscribe to continue.';"
                       "text-overflow:auto\">" + ARTICLE.replace(chr(10), " ") + "</p>",
                       # characters that draw nothing, look-alike letters, accents
                       "<div>Sub\u034fscribe to continue.</div>", "<div>Sub\u180escribe to continue.</div>",
                       "<div>Sub\ufe0fscribe to continue.</div>", "<div>Sub\U000e0041scribe to continue.</div>",
                       "<div>Sub\u2062scribe to continue.</div>", "<div>Sub\u200escribe to continue.</div>",
                       "<div>Sub\u3164scribe to continue.</div>", "<div>Subscribe\u3164to\u3164continue.</div>",
                       "<div>Subscribe\u2800to\u2800continue.</div>", "<div>\u0405ubscribe to continue.</div>",
                       "<div>Subs\u0441ribe t\u043e \u0441\u043entinue.</div>", "<div>Subscribe t\u03bf continue.</div>",
                       "<div>Subscri\u0301be to continue.</div>"):
            page, receipt = self._page_receipt(
                f"<html><head><title>T</title></head><body>{notice}<main>{article}</main></body></html>")
            self.assertNotEqual(receipt["status"], "PASS", notice)
        for shown in ("<script type='text/plain' style='display:block'>Subscribe to continue.</script>",
                      "<style style='display:block'>Subscribe to continue.</style>"):
            page, receipt = self._page_receipt(f"<html><head style='display:block'><title>T</title>{shown}</head>"
                                               f"<body><main>{article}</main></body></html>")
            self.assertNotEqual(receipt["status"], "PASS", shown)  # a shown <head> shows what its children show
        page, receipt = self._page_receipt(
            "<html><head><title>T</title></head><body style='caption-side:bottom'><table><caption>continue.</caption>"
            f"<tr><td>Subscribe to</td></tr></table><main>{article}</main></body></html>")
        self.assertNotEqual(receipt["status"], "PASS")  # an inherited caption side puts the caption below
        page, receipt = self._page_receipt(
            f"<html><head><title>\u202eni ngiS\u202c</title></head><body><main>{article}</main></body></html>")
        self.assertNotEqual(receipt["status"], "PASS")  # a reversed title can read as a wall title
        for after in ("<p>Read the full</p><p>article here</p>", "<p>We value</p><p>your privacy</p>"):
            page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body><main>{article}{after}</main>"
                                               "</body></html>")
            self.assertNotEqual(receipt["status"], "PASS", after)  # split wording after the body, consent too
        page, receipt = self._page_receipt("<html><head><title>T</title></head><body><main><p>Hello <b>world</b>!</p>"
                                           f"{article}</main></body></html>")
        self.assertTrue(page["content"].startswith("Hello world!"))  # nothing added between inline runs
        page, receipt = self._page_receipt(
            "<html><head><title>T</title></head><body><div style='display:flex;gap:20px'><div>Subscribe</div>"
            f"<div>to continue.</div></div><main>{article}</main></body></html>")
        self.assertNotEqual(receipt["status"], "PASS")  # a gap between the boxes parts the words
        page, receipt = self._page_receipt(
            f"<html><head><title>{'A' * 1000} | Sign in</title></head><body><main>{article}</main></body></html>")
        self.assertEqual(receipt["reason_code"], "page_too_large_to_check")  # past what x-reader checks
        for root in ("height:100%", "block-size:100%"):
            page, receipt = self._page_receipt(
                f"<html style='{root}'><head><title>T</title></head><body style='height:100%'>"
                f"<div style='height:0%'>{article}</div><div>{article}</div></body></html>")
            self.assertNotEqual(receipt["status"], "PASS", root)  # a percentage of a set height is a set height
        long_title = "Brand " * 60 + "| Sign in"
        page, receipt = self._page_receipt(
            f"<html><head><title>{long_title}</title></head><body><main>{article}</main></body></html>")
        self.assertNotEqual(receipt["status"], "PASS")  # walls are looked for in the whole title, not the shown 200

    def test_text_box_values_and_option_labels_count_for_walls(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        for control in ("<input type='text' value='Please sign in to continue.'>",
                        "<input value='Please sign in to continue.'>",
                        "<select><option label='Please sign in to continue.'></option></select>"):
            page, receipt = self._page_receipt(
                f"<html><head><title>T</title></head><body>{control}<main>{article}</main></body></html>")
            self.assertNotEqual(receipt["status"], "PASS", control)
        page, receipt = self._page_receipt("<html><head><title>T</title></head><body><input type='password' "
                                           f"value='Please sign in to continue.'><main>{article}</main></body></html>")
        self.assertEqual(receipt["status"], "PASS")  # a password box shows dots, not the words

    def test_the_media_type_decides_how_the_body_is_read(self):
        from x_reader.evidence import classify_payload
        from x_reader.fetchers.jina import _Page, fetch_html_page

        def read(body, media_type):
            page_text = _Page(body)
            page_text.media_type = media_type
            with patch("x_reader.fetchers.jina._download_html", return_value=(page_text, "https://e.com/a")):
                page = fetch_html_page("https://e.com/a")
            return page, classify_payload({
                "source_type": "manual", "title": page["title"], "content": page["content"],
                "extra": {"fetch_method": "direct_html_pinned", "redirects_verified": True, "body_verified": True,
                          "embedded_media": page["embedded_media"], "page_signals": page["page_signals"]}})

        body = "<script>shown too</script><main>" + ARTICLE * 2 + "</main>"
        page, receipt = read(body, "text/plain")
        self.assertIn("<script>shown too</script>", page["content"])  # browsers show text/plain as written
        page, receipt = read("<Read more>\n" + ARTICLE * 2, "text/plain")
        self.assertEqual(receipt["status"], "PARTIAL")  # the teaser line is a line of words, not a tag
        page, receipt = read("<html><body><main>" + ARTICLE * 2 + "</main><p>unclosed", "application/xhtml+xml")
        self.assertEqual(receipt["reason_code"], "page_structure_unverified")
        # some browsers give each plain-text line the direction of its first letter
        page, receipt = read("\u200fcontinue.\u200fSubscribe to\n" + ARTICLE * 2, "text/plain")
        self.assertNotEqual(receipt["status"], "PASS")
        page, receipt = read("\u0645\u0631\u062d\u0628\u0627 Wikipedia\n" + ARTICLE * 2, "text/plain")
        self.assertEqual(receipt["status"], "PASS")
        page, receipt = read("\u202bcontinue. Subscribe to\u202c\n" + ARTICLE * 2, "text/plain")
        self.assertNotEqual(receipt["status"], "PASS")  # explicit direction controls reorder plain text too

    def test_button_labels_count_for_walls_not_as_page_text(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        page, receipt = self._page_receipt(
            f"<html><head><title>T</title></head><body><main><button>{article}</button></main></body></html>")
        self.assertNotIn("careful argument", page["content"])
        self.assertNotEqual(receipt["status"], "PASS")

    def test_style_escapes_are_read_as_browsers_read_them(self):
        from x_reader.fetchers.jina import _inline_declarations

        self.assertEqual(_inline_declarations("dis\\play:none"), [("display", "none", False)])
        # a value spelled with escapes outside a string is opaque (U+FFFD first): escapes
        # only spell names, so an escaped digit is no number and an escaped keyword is
        # judged as unknown; font names and keywords still read for the checks that look
        self.assertEqual(_inline_declarations("position:\\61 bsolute"), [("position", "\ufffdabsolute", False)])
        self.assertEqual(_inline_declarations("color:rgb(\\30,\\30,\\30)"), [("color", "\ufffdrgb(0,0,0)", False)])
        # an escape outside a string spells only identifier characters: one that yields
        # a delimiter, space, bracket or comma leaves a value no parser here accepts
        self.assertEqual(_inline_declarations("c\\;d:e"), [("c\ufffdd", "e", False)])
        self.assertEqual(_inline_declarations("color\\:black"), [])  # no unescaped ":", no declaration
        self.assertEqual(_inline_declarations("color:black\\!important"), [("color", "\ufffdblack\ufffdimportant", False)])
        self.assertEqual(_inline_declarations("color:red !\\69mportant"), [("color", "red", True)])
        self.assertEqual(_inline_declarations("color:rgb\\(0\\,0\\,0\\)"), [("color", "\ufffdrgb\ufffd0\ufffd0\ufffd0\ufffd", False)])
        self.assertEqual(_inline_declarations("font-family:'\\5FAE'"), [("font-family", "'\u5fae'", False)])
        for broken in ("a:b\\", "a:\\\nb", "a:b/*", "a:(b", "a:b)", "a:'b"):
            self.assertIsNone(_inline_declarations(broken), broken)
        page, receipt = self._page_receipt(
            "<html><head><title>T</title></head><body><main><p style='dis\\play:none'>Hidden words here.</p>"
            + "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines()) + "</main></body></html>")
        self.assertNotIn("Hidden words", page["content"])

    def test_native_hidden_elements(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        for body in (f"<main><details><summary>FAQ</summary><summary>{article}</summary></details><p>Short.</p></main>",
                     f"<main><div popover>{article}</div><p>Short.</p></main>",
                     f"<main><div popover=manual>{article}</div><p>Short.</p></main>"):
            page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body>{body}</body></html>")
            self.assertNotIn("careful argument", page["content"], body[:60])
            self.assertNotEqual(receipt["status"], "PASS", body[:60])

    def test_input_shapes_that_slow_or_swell_the_parser_are_refused(self):
        import time
        from x_reader.fetchers import jina

        cases = (
            "<p>" + "\x01" * (jina.MAX_INVALID_CODEPOINTS + 10) + "</p>",  # parse errors drained quadratically
            "<p>" + "\U0001fffe" * 100_000 + "</p>",  # noncharacters outside the BMP count too
            "".join(f"<i a{i}=1>" for i in range(1490)) + "</b>" * 20_000,  # end tags walk both stacks
            "<table>" + "<b>x</b>" * 30_000,  # foster parenting copies the child list
            "".join(f'<p style="color:red;{"margin:0 1px;" * 1500}">x</p>' for _ in range(250)),  # style volume
            "<main>" + '<span style="color:#123456;padding:1px 2px;font-family:a,b">w </span>' * 30_000 + "</main>",
            "<main><p>" + ARTICLE + "</p></main>" + "</b>" * 399_000,  # parse errors are counted, not kept
            "<main>" + ARTICLE + "</main><div hidden>" + "A" * 1_100_000 + "\U0001F600</div>",  # wall text budget
            "<div a=x\" " + " ".join(f"a{i}=1" for i in range(jina.MAX_TAG_ATTRIBUTES + 10)) + " \">x</div>",
            "<table>" + "x<b></b>" * 12000,  # foster-parented text copies
            "<html><head><title>" + "word " * 200_000 + "</title></head><body><main>"
            + "<p>x</p>" * 10 + "</main></body></html>",
            "<main>" + ("<p>" + "a" * 999 + "</p>") * 1100 + "</main>",  # text over the content cap
            # every way a long annotation's doubtful pieces may draw is charged before it is built
            "<main><ruby>字<rt>" + "读" * 250_000 + "<span style='font-size:1px'>x</span>" * 6 + "</rt></ruby></main>",
        )
        for html in cases:
            started = time.perf_counter()
            page = jina._extract_html_page(html)
            self.assertLess(time.perf_counter() - started, 8, html[:40])
            if "title" in html[:30]:
                self.assertLessEqual(len(page["title"]), 200)
            else:
                self.assertTrue(page["too_complex"], html[:40])
        started = time.perf_counter()
        page = jina._extract_html_page("<main>" + "<div dir=auto>" * 1400 + "1 " * 300_000 + "</div>" * 1400 + "</main>")
        self.assertLess(time.perf_counter() - started, 8)  # letters dir=auto looks through are budgeted
        self.assertTrue(page["layout_risk"] or page["too_complex"])
        deep = "<main>" + "<span style='display:contents'>" * 1000 + ARTICLE + "</span>" * 1000 + "</main>"
        page = jina._extract_html_page(deep)  # box trees are unwound without recursion, to a set depth
        self.assertTrue(page["layout_risk"] or page["too_complex"])

    def test_details_groups_keep_one_open(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        body = (f"<main><details name=faq open><summary>A</summary><p>Visible answer.</p></details>"
                f"<details name=faq open><summary>B</summary>{article}</details><p>Short.</p></main>")
        page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body>{body}</body></html>")
        self.assertIn("Visible answer", page["content"])
        self.assertNotIn("careful argument", page["content"])
        self.assertNotEqual(receipt["status"], "PASS")

    def test_meta_refresh_grammar(self):
        from x_reader.fetchers.jina import _parse_refresh

        self.assertEqual(_parse_refresh("0; url=https://elsewhere.example/"), "https://elsewhere.example/")
        self.assertEqual(_parse_refresh("0;https://elsewhere.example/"), "https://elsewhere.example/")
        self.assertEqual(_parse_refresh("3, URL='https://e.example/a'"), "https://e.example/a")
        self.assertEqual(_parse_refresh("5"), "")
        self.assertEqual(_parse_refresh("not-a-delay-url"), "")
        self.assertEqual(_parse_refresh(".; url=https://e.example/"), "")

    def test_pages_over_the_parse_budget_are_unknown(self):
        from x_reader.evidence import build_receipt
        from x_reader.fetchers.jina import ParseBudgetExceeded

        with patch("x_reader.fetchers.jina.fetch_html_page", side_effect=ParseBudgetExceeded("too much")), \
                patch("x_reader.reader.fetch_via_jina") as jina:
            content = self._read("https://e.com/a", hosted=True)
        jina.assert_not_called()
        receipt = build_receipt(content.to_dict() if hasattr(content, "to_dict") else {
            "source_type": "manual", "content": content.content, "title": content.title, "url": content.url,
            "extra": content.extra})
        self.assertEqual(receipt["status"], "UNKNOWN")
        self.assertEqual(receipt["reason_code"], "page_too_large_to_check")

    def test_templates_and_shadow_roots_cannot_vouch_for_text(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())
        for body in (f"<p hidden><template></p><div>{article}</div></template>",
                     f"<select><template></select><main>{article}</main></template>",
                     f"<div><template shadowrootmode=open><p>Shadow</p></template><main>{article}</main></div>",
                     f"<div><template shadowrootmode=banana><iframe src='https://p.example/v'></iframe></template><main>{article}</main></div>"):
            page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body>{body}</body></html>")
            self.assertNotEqual(receipt["status"], "PASS", body[:60])
        page, _receipt = self._page_receipt(
            f"<html><head><title>T</title></head><body><template shadowrootmode=banana><iframe src='https://p.example/v'></iframe></template><main>{article}</main></body></html>")
        self.assertEqual(page["embedded_media"], 0)

    def test_redirect_targets_use_the_canonical_hostname(self):
        from x_reader.fetchers import jina

        self.assertEqual(jina._next_hop("https://a.example/x", "https://fa\u00df.de/p"), "https://xn--fa-hia.de/p")
        self.assertEqual(jina._next_hop("https://a.example/x", "/y?z=1"), "https://a.example/y?z=1")
        with self.assertRaises(jina.BlockedDestination):
            jina._request_pinned("https://fa\u00df.de/", 5)

    def test_collapsed_controls_and_redirects(self):
        article = "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines())

        def read(body, head=""):
            return self._page_receipt(f"<html><head><title>T</title>{head}</head><body>{body}</body></html>")

        # a closed <details> shows only its summary; a closed <dialog> nothing
        page, receipt = read(f"<main>{article}<details><summary>FAQ</summary><p>Folded answer.</p></details></main>")
        self.assertNotIn("Folded answer", page["content"])
        self.assertEqual(receipt["status"], "PASS")
        page, _receipt = read(f"<main>{article}<details open><summary>FAQ</summary><p>Open answer.</p></details></main>")
        self.assertIn("Open answer", page["content"])
        _page, receipt = read(f"<main>{article}<dialog><p>Subscribe to continue reading.</p></dialog></main>")
        self.assertEqual(receipt["status"], "PARTIAL")
        # button labels and placeholders are wall wording; menus are not page text
        _page, receipt = read(f"<main>{article}<input type=submit value='Sign in to continue reading'></main>")
        self.assertNotEqual(receipt["status"], "PASS")
        page, _receipt = read(f"<main>{article}<select><option>Menu choice text</option></select></main>")
        self.assertNotIn("Menu choice", page["content"])
        # a meta refresh sends visitors elsewhere
        _page, receipt = read(f"<main>{article}</main>", head="<meta http-equiv='refresh' content='0; url=https://elsewhere.example/'>")
        self.assertEqual(receipt["reason_code"], "client_redirect")

    def test_markup_beyond_the_parse_budget_is_refused(self):
        from x_reader.fetchers import jina

        deep = "<div>" * (jina.MAX_PARSE_DEPTH + 10)
        self.assertTrue(jina._extract_html_page(deep)["too_complex"])
        many = "<br>" * (jina.MAX_PARSE_NODES + 10)
        self.assertTrue(jina._extract_html_page(many)["too_complex"])
        # a tag with thousands of attributes is refused before tokenizing (its duplicate check is quadratic)
        heavy = "<div " + " ".join(f"a{i}" for i in range(jina.MAX_TAG_ATTRIBUTES + 10)) + ">x</div>"
        self.assertTrue(jina._extract_html_page(heavy)["too_complex"])
        # clones of attribute-heavy formatting elements are counted
        cloned = "<p>" + "<b " + " ".join(f"a{i}=1" for i in range(300)) + "></p>" + "<p>x</p>" * 1000
        self.assertTrue(jina._extract_html_page(cloned)["too_complex"])
        # formatting elements a parser re-opens for every block would multiply the tree
        amplified = "<p>" + "".join(f"<b class=c{i}>" for i in range(60)) + "</p>" + "<p>x</p>" * 3000
        self.assertTrue(jina._extract_html_page(amplified)["too_complex"])
        # stray end tags under a deep stack cost scope checks
        scoped = "<span>" * 1400 + "</p>" * 100000
        self.assertTrue(jina._extract_html_page(scoped)["too_complex"])
        # unclosed paragraphs do not nest, and script text is not markup
        ok = "<p>x" * 5000 + "<script>" + "<div>" * 5000 + "</script>"
        self.assertFalse(jina._extract_html_page(ok)["too_complex"])
        with patch("x_reader.fetchers.jina._download_html", return_value=(deep, "https://e.com/a")):
            with self.assertRaises(ValueError):
                jina.fetch_html_page("https://e.com/a")

    def test_frames_in_svg_and_shadow_roots_count(self):
        main = "<main>" + "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines()) + "</main>"
        for extra, counted in (("<svg><foreignObject><iframe src='https://p.example/v'></iframe></foreignObject></svg>", 1),
                               ("<div><template shadowrootmode='open'><iframe src='https://p.example/v'></iframe></template></div>", 1),
                               ("<template><iframe src='https://p.example/v'></iframe></template>", 0),
                               ("<svg><title>Close</title><path d='M0 0'/></svg>", 0)):
            page, _receipt = self._page_receipt(f"<html><head><title>T</title></head><body>{main}{extra}</body></html>")
            self.assertEqual(page["embedded_media"], counted, extra)
            self.assertEqual(page["title"], "T", extra)

    def test_every_frame_counts_however_small(self):
        # ancestors (zoom, transform) and author CSS can enlarge or show any frame
        main = "<main>" + "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines()) + "</main>"
        for frame in ("<iframe width='1' height='1' src='https://t.example/p'>",
                      "<iframe style='display:none' src='https://t.example/p'>",
                      "<div style='zoom:400'><iframe width=1 height=1 src='https://t.example/p'></iframe></div>"):
            page, receipt = self._page_receipt(f"<html><head><title>T</title></head><body>{main}{frame}</iframe></body></html>")
            self.assertEqual(page["embedded_media"], 1, frame)
            self.assertNotEqual(receipt["status"], "PASS", frame)

    def test_aria_hidden_frames_are_still_visible_embeds(self):
        main = "<main>" + "".join(f"<p>{line}</p>" for line in (ARTICLE * 2).splitlines()) + "</main>"
        page, receipt = self._page_receipt(
            "<html><head><title>T</title></head><body>" + main
            + "<iframe aria-hidden='true' src='https://player.example.org/v/1'></iframe></body></html>")
        self.assertEqual(page["embedded_media"], 1)
        self.assertEqual(receipt["status"], "PARTIAL")

    def test_embeds_count_anywhere_on_the_page(self):
        main = "<main>" + "".join(f"<p>{line}</p>" for line in ARTICLE.splitlines()) + "</main>"
        page, receipt = self._page_receipt(
            "<html><head><title>T</title></head><body>" + main
            + "<aside><iframe src='https://player.example.org/v/1'></iframe></aside></body></html>")
        self.assertEqual(page["embedded_media"], 1)
        self.assertEqual(receipt["status"], "PARTIAL")


class WeChatArticleReadTest(unittest.TestCase):
    """The WeChat article read runs in a browser: skipped where Playwright or its Chromium is missing."""

    def test_the_article_is_the_page_the_browser_ended_on(self):
        from unittest.mock import AsyncMock
        from x_reader.evidence import build_receipt
        from x_reader.fetchers.wechat import fetch_wechat
        from x_reader.schema import from_wechat

        body = "读者会在这篇长文里看到一个仔细的论证。每一段都补充了背景、证据和清楚的结论。" * 30
        for final, page_text, complete, status in (
                ("https://evil.example/final", body, True, "UNKNOWN"),
                ("https://mp.weixin.qq.com/s/abc", "登录后查看全文\n" + body, True, "UNKNOWN"),
                ("https://mp.weixin.qq.com/s/abc", body + "\n阅读原文", False, "UNKNOWN"),  # page text cut short
                ("https://mp.weixin.qq.com/s/abc", body + "\n阅读原文", True, "PASS")):
            read = {"title": "文章", "content": body, "url": final, "author": "", "extraction": "container",
                    "embedded_media": 0, "visual_risks": [], "page_text": page_text, "page_text_complete": complete}
            with patch("x_reader.fetchers.jina.fetch_via_jina", side_effect=RuntimeError("blocked")), \
                    patch("x_reader.fetchers.browser.fetch_via_browser", AsyncMock(return_value=read)):
                article = asyncio.run(fetch_wechat("https://mp.weixin.qq.com/s/abc"))
            receipt = build_receipt(from_wechat(article).to_dict())
            self.assertEqual(receipt["status"], status, (final, page_text[:10]))

    def test_article_text_is_read_as_the_page_shows_it(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        from x_reader.fetchers.browser import _WECHAT_ARTICLE_JS

        prose = "<p>正文段落，用来占位的普通文字，读者可以正常看到它。</p>"
        cases = (
            # inline runs join and <br> parts lines; hidden text is left out, text shown again is kept
            ("<p>Please sign <span>in to continue.</span></p><p>Body text.</p>",
             "Please sign in to continue.\n\nBody text.", 0, []),
            ("<p>Subscribe to<br>continue.</p>", "Subscribe to\ncontinue.", 0, []),
            ("<p style='display:none'>gone</p><p style='visibility:hidden'>gone <span style='visibility:visible'>"
             "back</span></p>", "back", 0, []),
            # players, frames and WeChat's media components are counted, not read
            ("<p>Text</p><mpvoice>voice</mpvoice><iframe></iframe><mp-common-profile>Profile</mp-common-profile>"
             "<section><video><iframe></iframe></video></section><p>more</p>", "Text\n\nmore", 4, []),
            # images stay where they are, if shown; the page's own attributes are not the read's markers
            ("<p>before<img data-src='https://e.test/a.png'>after</p>",
             "before\n\n![image](https://e.test/a.png)\n\nafter", 0, []),
            ("<p>Sub<img hidden src='https://e.test/a.png'>scribe to continue.</p>", "Subscribe to continue.", 0, []),
            ("<p data-xr-skip>Please sign in to continue.</p>", "Please sign in to continue.", 0, []),
            # what innerText cannot vouch for
            ("<p style='opacity:0.1'>x</p>" + prose, None, 0, ["faint_text"]),
            ("<p style='color:#fff'>x</p>" + prose, None, 0, ["faint_text"]),
            ("<p style='font-size:0'>x</p>" + prose, None, 0, ["small_text"]),
            ("<p style='transform:scaleX(-1)'>x</p>" + prose, None, 0, ["transformed_text"]),
            ("<div style='height:20px;overflow:hidden'><p>a</p><p>b</p><p>c</p></div>" + prose, None, 0,
             ["clipped_text"]),
            ("<p style='margin-left:-9999px'>x</p>" + prose, None, 0, ["offscreen_text"]),
            ("<p dir='rtl'>continue. Subscribe to</p>" + prose, None, 0, ["reordered_text"]),
            ("<div style='display:flex;flex-direction:row-reverse'><span>a</span><span>b</span></div>" + prose, None,
             0, ["reordered_text"]),
            ("<p>\u202econtinue\u202c</p>" + prose, None, 0, ["reordered_text"]),
            (prose * 3 + "</div><div style='position:fixed;inset:0;background:#fff'>Sign in</div><div>", None, 0,
             ["covered_text"]),
            (prose * 3 + "</div><div style='position:fixed;left:0;right:0;bottom:0;height:40px'>Like</div><div>", None,
             0, []),
            # covers inside the article, or over a part of it further down the page
            ("<div style='position:relative'>" + prose * 2 + "<div style='position:absolute;inset:0;background:#fff'>"
             "</div></div>" + prose, None, 0, ["covered_text"]),
            (prose * 40 + "</div><div style='position:absolute;left:0;right:0;top:1500px;height:400px;background:#fff;"
             "pointer-events:none'></div><div>", None, 0, ["covered_text"]),
            # text its own box clips, letters filled white, filters, paint hit testing does not see
            ("<p style='height:0;overflow:hidden'>x</p>" + prose, None, 0, ["clipped_text"]),
            ("<p style='-webkit-text-fill-color:#fff'>x</p>" + prose, None, 0, ["faint_text"]),
            ("<p style='filter:blur(9px)'>x</p>" + prose, None, 0, ["filtered_text"]),
            (prose * 3 + "</div><div style='position:fixed;top:0;left:0;width:1px;height:1px;outline:3000px solid #fff'>"
             "</div><div>", None, 0, ["covered_text"]),
            (prose * 3 + "</div><div style='position:fixed;top:0;left:0;width:1px;height:1px;box-shadow:0 0 0 3000px #fff'>"
             "</div><div>", None, 0, ["covered_text"]),
        )
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:  # Chromium not downloaded
                self.skipTest(f"chromium is not available: {error}")
            try:
                page = browser.new_page()
                for body, content, embeds, risks in cases:
                    page.set_content(f"<html><head></head><body><div id='js_content'>{body}</div></body></html>")
                    before = page.evaluate("() => document.documentElement.outerHTML")
                    read = page.evaluate(_WECHAT_ARTICLE_JS)
                    if content is not None:
                        self.assertEqual(read["content"], content, body)
                    self.assertEqual((read["embeds"], read["risks"]), (embeds, risks), body)
                    self.assertIn(read["content"].split("\n")[0].replace("![image](https://e.test/a.png)", ""),
                                  read["page_text"], body)  # the whole page's text comes back too
                    self.assertEqual(page.evaluate("() => document.documentElement.outerHTML"), before, body)
                page.set_content("<html><head><style>.g::before{content:'Subscribe to continue.'}</style></head>"
                                 "<body><div id='js_content'><p class='g'>x</p></div></body></html>")
                self.assertEqual(page.evaluate(_WECHAT_ARTICLE_JS)["risks"], ["generated_text"])  # text made by CSS
                page.set_content("<html><head><style>.g::after{content:attr(data-note)}</style></head><body>"
                                 "<div id='js_content'><p class='g' data-note='Sign in to continue.'>x</p></div>"
                                 "</body></html>")
                self.assertEqual(page.evaluate(_WECHAT_ARTICLE_JS)["risks"], ["generated_text"])
                page.set_content("<html><head><style>.c{position:relative}.c::after{content:'';position:absolute;inset:0;"
                                 "background:#fff}</style></head><body><div id='js_content'><div class='c'>"
                                 + prose * 3 + "</div></div></body></html>")
                self.assertEqual(page.evaluate(_WECHAT_ARTICLE_JS)["risks"], ["covered_text"])  # a pseudo-element box
                page.set_content("<html><head></head><body><div id='js_content' hidden>" + prose + "</div></body></html>")
                self.assertIn("unrendered_article", page.evaluate(_WECHAT_ARTICLE_JS)["risks"])  # innerText of hidden
                page.set_content("<html><head></head><body><div id='js_content'>" + prose + "</div>"
                                 + "<p>Links here</p>" * 30000 + "</body></html>")
                self.assertFalse(page.evaluate(_WECHAT_ARTICLE_JS)["page_text_complete"])  # the page text cut short
            finally:
                browser.close()


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

    def test_youtube_error_pages_are_recognised(self):
        from x_reader.fetchers import youtube

        vid = "abcdefghijk"
        watch = f"https://www.youtube.com/watch?v={vid}"

        def state(title, content="Official video", source=watch, video=vid):
            return youtube._page_state(video, title, content, source)

        self.assertEqual(state("YouTube", "This video isn't available anymore\nVideo unavailable"), "unavailable")
        self.assertEqual(state("- YouTube", "Something"), "unidentified")
        self.assertEqual(state("Rick Astley - Never Gonna Give You Up"), "ok")
        self.assertEqual(state("YouTube Rewind 2018"), "ok")
        # a title discussing the error is still a video title; YouTube's own error titles are not
        self.assertEqual(state("How to fix 'Video unavailable' errors", "Video unavailable explained"), "ok")
        for title in ("Video unavailable - YouTube", "Sign in to confirm your age - YouTube", "Private video",
                      "Video unavailable. - YouTube", "This video isn't available - YouTube",
                      "This video isn\u2019t available anymore - YouTube",
                      "The uploader has not made this video available in your country - YouTube"):
            self.assertEqual(state(title), "unavailable", title)
        self.assertEqual(state("Sign in to YouTube - YouTube"), "unidentified")
        for title in ("Age-restricted video - YouTube", "This video is age-restricted - YouTube"):
            self.assertEqual(state(title), "unavailable", title)
        self.assertEqual(state("YouTube", "This video isn\u2019t available anymore"), "unavailable")
        self.assertEqual(state("Video unavailable errors explained - YouTube"), "ok")
        for body in ("Video unavailable errors explained in this tutorial.", "A guide to age-restricted video settings."):
            self.assertEqual(state("YouTube", body), "unidentified", body)
        for title in ("Before you continue to YouTube", "Sign in - YouTube", "Consent", ""):
            self.assertEqual(state(title, "Before you continue"), "unidentified", title)
        self.assertEqual(state("Bevor Sie zu YouTube weitergehen", "[Accept](https://consent.youtube.com/s)"),
                         "unidentified")
        # no single video, no URL Source, or Jina ended on another page
        self.assertEqual(state("Some channel", video=""), "unidentified")
        self.assertEqual(state("A video", source=""), "unidentified")
        self.assertEqual(state("A video", source="https://consent.youtube.com/m?continue=x"), "unidentified")
        self.assertEqual(state("A video", source="https://www.youtube.com/watch?v=zzzzzzzzzzz"), "unidentified")

    def test_jina_envelope_is_parsed(self):
        from x_reader.fetchers.jina import _parse_jina_text

        text = ("Title: YouTube\n\nURL Source: https://www.youtube.com/watch?v=abcdefghijk\n\n"
                "Warning: Target URL returned error 401: Unauthorized\n\nMarkdown Content:\nVideo unavailable\n")
        title, content, headers = _parse_jina_text(text)
        self.assertEqual(title, "YouTube")
        self.assertEqual(content, "Video unavailable")
        self.assertEqual(headers["url source"], ["https://www.youtube.com/watch?v=abcdefghijk"])
        self.assertEqual(_parse_jina_text("# Plain\n\nBody text")[:2], ("Plain", "Body text"))

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
            info.write_text(json.dumps({"duration": 600, "id": "abcdefghijk"}))
            self.assertEqual(youtube._read_info(str(info)), (600.0, "abcdefghijk"))
            info.write_text(json.dumps({"duration": "NaN"}))
            self.assertEqual(youtube._read_info(str(info)), (None, ""))
            info.write_text("[1, 2]")
            self.assertEqual(youtube._read_info(str(info)), (None, ""))

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
