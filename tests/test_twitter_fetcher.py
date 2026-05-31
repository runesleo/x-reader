import unittest
import asyncio
import json
import os
import sys
import tempfile
import types


class _DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


sys.modules.setdefault("loguru", types.SimpleNamespace(logger=_DummyLogger()))

from x_reader.fetchers import twitter
from x_reader.schema import from_twitter


class TwitterFetcherHelpersTest(unittest.TestCase):
    def test_normalize_x_url(self):
        self.assertEqual(
            twitter._normalize_x_url(
                "https://twitter.com/runesleo/status/123?s=20&t=tracking"
            ),
            "https://x.com/runesleo/status/123",
        )
        self.assertEqual(
            twitter._normalize_x_url(
                "https://mobile.twitter.com/runesleo/status/123#fragment"
            ),
            "https://x.com/runesleo/status/123",
        )
        self.assertEqual(
            twitter._normalize_x_url("x.com/runesleo/status/123?s=20"),
            "https://x.com/runesleo/status/123",
        )

    def test_extract_status_id_common_shapes(self):
        self.assertEqual(
            twitter._extract_status_id("https://x.com/runesleo/status/123"), "123"
        )
        self.assertEqual(
            twitter._extract_status_id("https://x.com/i/web/status/456"), "456"
        )
        self.assertEqual(
            twitter._extract_status_id("https://x.com/i/article/789"), "789"
        )
        self.assertEqual(
            twitter._extract_status_id("https://x.com/runesleo/article/101"), "101"
        )

    def test_strip_tweet_html_removes_embed_chrome(self):
        html = (
            '<blockquote><p lang="zh">第一行<br>第二行 '
            '<a href="https://t.co/abc">pic.twitter.com/abc</a></p>'
            '&mdash; Leo (@runesleo) <a href="https://x.com/runesleo/status/1">'
            "May 31, 2026</a></blockquote>"
        )
        self.assertEqual(
            twitter._strip_tweet_html(html),
            "第一行\n第二行 pic.twitter.com/abc",
        )

    def test_detect_thin_oembed_text(self):
        self.assertTrue(twitter._is_thin_oembed_text("https://t.co/abc123"))
        self.assertTrue(twitter._is_thin_oembed_text("短句"))
        self.assertTrue(twitter._is_thin_oembed_text("这是一段被截断的内容…"))
        self.assertFalse(
            twitter._is_thin_oembed_text(
                "这是一个完整的公开推文内容，长度足够，也没有只剩短链。"
            )
        )

    def test_from_twitter_keeps_fetch_metadata(self):
        content = from_twitter(
            {
                "text": "hello",
                "author": "@runesleo",
                "url": "https://x.com/runesleo/status/1",
                "fetch_method": "oembed",
                "author_url": "https://x.com/runesleo",
            }
        )
        self.assertEqual(content.extra["fetch_method"], "oembed")
        self.assertEqual(content.extra["author_url"], "https://x.com/runesleo")

    def test_session_cookies_require_explicit_external_opt_in(self):
        from x_reader.fetchers import browser

        original_get_session_path = browser.get_session_path
        original_env = os.environ.get("X_READER_ALLOW_EXTERNAL_SESSION_COOKIES")
        try:
            with tempfile.NamedTemporaryFile("w", delete=False) as handle:
                json.dump(
                    {
                        "cookies": [
                            {
                                "name": "auth_token",
                                "value": "test-cookie-value",
                                "domain": ".x.com",
                            }
                        ]
                    },
                    handle,
                )
                session_path = handle.name

            browser.get_session_path = lambda _platform: session_path
            os.environ.pop("X_READER_ALLOW_EXTERNAL_SESSION_COOKIES", None)
            self.assertIsNone(twitter._session_cookie_header())

            os.environ["X_READER_ALLOW_EXTERNAL_SESSION_COOKIES"] = "1"
            self.assertEqual(
                twitter._session_cookie_header(), "auth_token=test-cookie-value"
            )
        finally:
            browser.get_session_path = original_get_session_path
            if original_env is None:
                os.environ.pop("X_READER_ALLOW_EXTERNAL_SESSION_COOKIES", None)
            else:
                os.environ["X_READER_ALLOW_EXTERNAL_SESSION_COOKIES"] = original_env
            if "session_path" in locals():
                os.unlink(session_path)

    def test_fetch_twitter_uses_oembed_when_content_is_complete(self):
        original = twitter._fetch_via_oembed
        try:
            twitter._fetch_via_oembed = lambda _url: {
                "text": "这是一个完整的公开推文内容，长度足够，可以直接使用。",
                "author": "Leo",
                "title": "这是一个完整的公开推文内容",
            }
            data = asyncio.run(
                twitter.fetch_twitter("https://x.com/runesleo/status/123?s=20")
            )
        finally:
            twitter._fetch_via_oembed = original

        self.assertEqual(data["fetch_method"], "oembed")
        self.assertEqual(data["url"], "https://x.com/runesleo/status/123")

    def test_fetch_twitter_falls_back_after_thin_oembed(self):
        original_oembed = twitter._fetch_via_oembed
        original_fx = twitter._fetch_via_fxtwitter
        try:
            twitter._fetch_via_oembed = lambda _url: {
                "text": "https://t.co/abc123",
                "author": "Leo",
                "title": "",
            }
            twitter._fetch_via_fxtwitter = lambda _url: {
                "text": "FxTwitter 返回的完整正文",
                "author": "@runesleo",
                "title": "FxTwitter 返回的完整正文",
            }
            data = asyncio.run(
                twitter.fetch_twitter("https://x.com/runesleo/status/123")
            )
        finally:
            twitter._fetch_via_oembed = original_oembed
            twitter._fetch_via_fxtwitter = original_fx

        self.assertEqual(data["fetch_method"], "fxtwitter")
        self.assertEqual(data["text"], "FxTwitter 返回的完整正文")


if __name__ == "__main__":
    unittest.main()
