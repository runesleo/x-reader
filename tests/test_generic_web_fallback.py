import asyncio
import sys
import types
import unittest
from unittest.mock import patch


class _DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


sys.modules.setdefault("loguru", types.SimpleNamespace(logger=_DummyLogger()))

from x_reader.fetchers import jina
from x_reader.reader import UniversalReader


class _Response:
    def __init__(self, *, status=200, headers=None, chunks=None):
        self.status = status
        self.headers = (
            {"Content-Type": "text/html; charset=utf-8"}
            if headers is None
            else headers
        )
        self._chunks = list(chunks or [])
        self.closed = False

    def read(self, _amount=None):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


class _Pool:
    def __init__(self, response=None):
        self.response = response
        self.closed = False

    def request(self, *_args, **_kwargs):
        return self.response

    def close(self):
        self.closed = True


class GenericWebFallbackTest(unittest.TestCase):
    def test_extract_html_text_excludes_title_and_scripts_from_body(self):
        title, content = jina._extract_html_text(
            "<html><head><title>Example</title><script>bad()</script></head>"
            "<body><main><h1>Hello</h1><p>Useful body text for readers.</p></main></body></html>"
        )
        self.assertEqual(title, "Example")
        self.assertNotIn("Example", content)
        self.assertIn("Hello", content)
        self.assertIn("Useful body text", content)
        self.assertNotIn("bad()", content)

    def test_pinned_https_connects_to_validated_ip_with_original_sni(self):
        response = _Response(chunks=[b"<html><body>ok</body></html>"])
        recorded = {}

        class RecordingPool(_Pool):
            def __init__(self, **kwargs):
                super().__init__(response=response)
                recorded["init"] = kwargs

            def request(self, method, target, **kwargs):
                recorded["request"] = (method, target, kwargs)
                return self.response

        with patch(
            "x_reader.utils.url_validator.resolve_safe_ips",
            return_value=["203.0.113.10"],
        ) as resolve, patch.object(jina, "HTTPSConnectionPool", RecordingPool):
            pool, got_response, ip = jina._request_pinned(
                "https://example.com/path?q=1",
                5,
            )

        resolve.assert_called_once_with("example.com", 443, timeout=5.0)
        self.assertEqual(ip, "203.0.113.10")
        self.assertEqual(recorded["init"]["host"], "203.0.113.10")
        self.assertEqual(recorded["init"]["server_hostname"], "example.com")
        self.assertEqual(recorded["init"]["assert_hostname"], "example.com")
        self.assertEqual(recorded["request"][0:2], ("GET", "/path?q=1"))
        self.assertEqual(recorded["request"][2]["headers"]["Host"], "example.com")
        self.assertIs(got_response, response)
        pool.close()

    def test_direct_html_extracts_public_page_and_closes_resources(self):
        html = (
            "<html><head><title>Example Domain</title></head><body>"
            "<h1>Example Domain</h1>"
            "<p>This domain is for use in illustrative examples in documents.</p>"
            "</body></html>"
        ).encode()
        response = _Response(chunks=[html])
        pool = _Pool(response)
        with patch.object(
            jina,
            "_request_pinned",
            return_value=(pool, response, "203.0.113.10"),
        ):
            data = jina.fetch_direct_html("https://example.com")

        self.assertEqual(data["fetch_method"], "direct_html_pinned")
        self.assertIn("illustrative examples", data["content"])
        self.assertTrue(response.closed)
        self.assertTrue(pool.closed)

    def test_redirect_is_revalidated_by_next_pinned_request(self):
        redirect = _Response(
            status=302,
            headers={
                "Location": "http://127.0.0.1/internal",
                "Content-Type": "text/html",
            },
        )
        first_pool = _Pool(redirect)
        calls = []

        def pinned(url, _timeout, headers=None):
            calls.append(url)
            if len(calls) == 1:
                return first_pool, redirect, "203.0.113.10"
            raise ValueError("Blocked private target")

        with patch.object(jina, "_request_pinned", side_effect=pinned):
            with self.assertRaisesRegex(ValueError, "Blocked private target"):
                jina.fetch_direct_html("https://example.com")

        self.assertEqual(
            calls,
            ["https://example.com", "http://127.0.0.1/internal"],
        )
        self.assertTrue(redirect.closed)
        self.assertTrue(first_pool.closed)

    def test_redirect_without_location_is_rejected(self):
        response = _Response(status=302, headers={"Content-Type": "text/html"})
        pool = _Pool(response)
        with patch.object(
            jina,
            "_request_pinned",
            return_value=(pool, response, "203.0.113.10"),
        ):
            with self.assertRaisesRegex(ValueError, "without Location"):
                jina.fetch_direct_html("https://example.com")

        self.assertTrue(response.closed)
        self.assertTrue(pool.closed)

    def test_missing_content_type_is_rejected(self):
        response = _Response(headers={}, chunks=[b"<html><body>Readable body text</body></html>"])
        pool = _Pool(response)
        with patch.object(
            jina,
            "_request_pinned",
            return_value=(pool, response, "203.0.113.10"),
        ):
            with self.assertRaisesRegex(ValueError, "Content-Type"):
                jina.fetch_direct_html("https://example.com")

        self.assertTrue(response.closed)
        self.assertTrue(pool.closed)

    def test_stream_body_limit_applies_without_content_length(self):
        response = _Response(
            headers={"Content-Type": "text/html"},
            chunks=[b"123456", b"78901"],
        )
        pool = _Pool(response)
        with patch.object(jina, "MAX_RESPONSE_BYTES", 10), patch.object(
            jina,
            "_request_pinned",
            return_value=(pool, response, "203.0.113.10"),
        ):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                jina.fetch_direct_html("https://example.com")

        self.assertTrue(response.closed)
        self.assertTrue(pool.closed)

    def test_total_deadline_stops_slow_stream_and_closes_resources(self):
        response = _Response(
            headers={"Content-Type": "text/html"},
            chunks=[b"first", b"second"],
        )
        pool = _Pool(response)
        with patch.object(
            jina,
            "_request_pinned",
            return_value=(pool, response, "203.0.113.10"),
        ), patch.object(
            jina.time,
            "monotonic",
            side_effect=[0.0, 0.0, 0.1, 1.0, 21.0],
        ):
            with self.assertRaisesRegex(TimeoutError, "total time"):
                jina.fetch_direct_html("https://example.com")

        self.assertTrue(response.closed)
        self.assertTrue(pool.closed)


    def test_request_pinned_closes_pool_when_connect_fails(self):
        recorded = {}

        class FailingPool(_Pool):
            def __init__(self, **kwargs):
                super().__init__()
                recorded["pool"] = self

            def request(self, *_args, **_kwargs):
                raise RuntimeError("connect failed")

        with patch(
            "x_reader.utils.url_validator.resolve_safe_ips",
            return_value=["8.8.8.8"],
        ), patch.object(jina, "HTTPSConnectionPool", FailingPool):
            with self.assertRaisesRegex(RuntimeError, "connect failed"):
                jina._request_pinned("https://example.com", 5)

        self.assertTrue(recorded["pool"].closed)

    def test_jina_uses_pinned_request_and_bounded_reader(self):
        body = b"# Example\n\nUseful Jina content body for the reader."
        response = _Response(
            headers={"Content-Type": "text/plain; charset=utf-8"},
            chunks=[body],
        )
        pool = _Pool(response)
        with patch.object(
            jina,
            "_request_pinned",
            return_value=(pool, response, "8.8.8.8"),
        ) as pinned:
            data = jina.fetch_via_jina("https://example.com")

        called_url = pinned.call_args.args[0]
        self.assertTrue(called_url.startswith("https://r.jina.ai/https://example.com"))
        self.assertEqual(data["fetch_method"], "jina_pinned")
        self.assertIn("Useful Jina content", data["content"])
        self.assertTrue(response.closed)
        self.assertTrue(pool.closed)

    def test_generic_reader_falls_back_when_jina_fails(self):
        with patch(
            "x_reader.reader.fetch_via_jina",
            side_effect=RuntimeError("jina down"),
        ), patch(
            "x_reader.fetchers.jina.fetch_direct_html",
            return_value={
                "title": "Fallback",
                "content": "A sufficiently long direct HTML body for the fallback contract.",
                "url": "https://example.com",
                "fetch_method": "direct_html_pinned",
            },
        ):
            content = asyncio.run(
                UniversalReader()._fetch("generic", "https://example.com")
            )

        self.assertEqual(content.title, "Fallback")
        self.assertIn("direct HTML body", content.content)


if __name__ == "__main__":
    unittest.main()
