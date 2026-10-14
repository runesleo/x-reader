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

    def test_structured_media_status_present_none_unknown(self):
        self.assertEqual(
            twitter._media_status_from_fxtwitter(
                {"media": {"videos": [{"url": "https://video"}]}}
            ),
            "present",
        )
        # FxTwitter leaves "media" out when a post has none; a key that is there
        # but empty or null is a damaged layer, not proof of no media.
        for media in ({"videos": [], "photos": []}, None, [], {}):
            self.assertEqual(twitter._media_status_from_fxtwitter({"id": "1", "text": "t", "media": media}), "unknown")
        self.assertEqual(twitter._media_status_from_fxtwitter({"id": "1", "text": "t"}), "none")
        self.assertEqual(twitter._media_status_from_fxtwitter({}), "unknown")

    def test_from_twitter_keeps_fetch_and_media_metadata(self):
        content = from_twitter(
            {
                "text": "hello",
                "author": "@runesleo",
                "url": "https://x.com/runesleo/status/1",
                "fetch_method": "oembed",
                "author_url": "https://x.com/runesleo",
                "media_status": "present",
                "media_probe_method": "fxtwitter",
            }
        )
        self.assertEqual(content.extra["fetch_method"], "oembed")
        self.assertEqual(content.extra["author_url"], "https://x.com/runesleo")
        self.assertEqual(content.extra["media_status"], "present")
        self.assertEqual(content.extra["media_probe_method"], "fxtwitter")

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

    def test_fetch_twitter_oembed_uses_structured_media_probe(self):
        original_oembed = twitter._fetch_via_oembed
        original_fx = twitter._fetch_via_fxtwitter
        try:
            twitter._fetch_via_oembed = lambda _url: {
                "text": "这是一个完整的公开推文内容，长度足够，可以直接使用。",
                "post_id": "123",
                "author": "Leo",
                "title": "这是一个完整的公开推文内容",
                "media_status": "unknown",
            }
            twitter._fetch_via_fxtwitter = lambda _url, timeout=10: {
                "text": "same",
                "media_status": "none",
            }
            data = asyncio.run(
                twitter.fetch_twitter("https://x.com/runesleo/status/123?s=20")
            )
        finally:
            twitter._fetch_via_oembed = original_oembed
            twitter._fetch_via_fxtwitter = original_fx

        # the structured read's text is the post (oEmbed's may be cut short)
        self.assertEqual(data["fetch_method"], "fxtwitter")
        self.assertEqual(data["text"], "same")
        self.assertEqual(data["url"], "https://x.com/runesleo/status/123")
        self.assertEqual(data["media_status"], "none")
        self.assertEqual(data["media_probe_method"], "fxtwitter")

    def test_a_longer_structured_text_replaces_a_cut_oembed_text(self):
        from unittest.mock import patch

        prefix = "这是一个完整的公开推文内容，长度足够，可以直接使用。"
        with patch.object(twitter, "_fetch_via_oembed", return_value={
                "text": prefix, "post_id": "123", "author": "Leo", "title": "t", "media_status": "none"}), \
                patch.object(twitter, "_fetch_via_fxtwitter", return_value={
                    "text": prefix + "\nHere is the entire argument in this thread 🧵", "media_status": "none",
                    "structure_status": "verified", "quote_status": "none", "poll_status": "none",
                    "article_status": "none"}):
            data = asyncio.run(twitter.fetch_twitter("https://x.com/runesleo/status/123"))
        self.assertIn("entire argument", data["text"])
        self.assertTrue(data["thread_marker"])

    def test_fetch_twitter_keeps_oembed_media_presence_and_probes_structure(self):
        original_oembed = twitter._fetch_via_oembed
        original_fx = twitter._fetch_via_fxtwitter
        calls = []
        try:
            twitter._fetch_via_oembed = lambda _url: {
                "text": "完整正文 pic.twitter.com/abc123，长度也足够用于读取。",
                "post_id": "123",
                "author": "Leo",
                "title": "完整正文",
                "media_status": "present",
            }

            def probe(*_args, **_kwargs):
                calls.append(True)
                # A probe that disagrees about media must not erase oEmbed's evidence.
                return {"media_status": "none", "structure_status": "verified", "quote_status": "none",
                        "poll_status": "none", "article_status": "none"}

            twitter._fetch_via_fxtwitter = probe
            data = asyncio.run(
                twitter.fetch_twitter("https://x.com/runesleo/status/123")
            )
        finally:
            twitter._fetch_via_oembed = original_oembed
            twitter._fetch_via_fxtwitter = original_fx

        self.assertEqual(calls, [True])
        self.assertEqual(data["media_status"], "present")
        self.assertEqual(data["structure_status"], "verified")

    def test_polls_are_read_only_with_every_option(self):
        base = {"id": "1", "text": "post", "author": {"screen_name": "a"}}
        for poll in ({"choices": []}, {"choices": [None]}, {"choices": [{}]}, {"choices": [{"label": ""}]}, {},
                     {"choices": [{"label": "Yes"}, {"label": 3}]}):
            self.assertEqual(twitter._structure_from_fxtwitter(dict(base, poll=poll))["poll_status"], "unknown", poll)
        read = twitter._structure_from_fxtwitter(dict(base, poll={"choices": [{"label": "Yes"}, {"label": "No"}]}))
        self.assertEqual(read["poll_status"], "read")
        self.assertEqual(twitter._structure_from_fxtwitter(dict(base))["poll_status"], "none")

    def test_text_that_is_not_a_string_is_not_read(self):
        from unittest.mock import MagicMock, patch

        quoted = twitter._structure_from_fxtwitter({"id": "1", "text": "main", "author": {"screen_name": "a"},
                                                     "quote": {"id": "2", "text": [None], "author": {"screen_name": "b"}}})
        self.assertEqual(quoted["quote_status"], "unknown")
        self.assertNotIn("quote_text", quoted)
        for author in ([], "alice", True, None):
            damaged = twitter._structure_from_fxtwitter(
                {"id": "1", "text": "main", "author": {"screen_name": "a"},
                 "quote": {"id": "2", "text": "q", "author": author}})
            self.assertEqual(damaged["quote_status"], "unknown", author)
            # a reply whose author cannot be read may be a thread: the structure is unverified
            reply = twitter._structure_from_fxtwitter(
                {"id": "1", "text": "main", "author": author, "replying_to": "alice", "replying_to_status": "0"})
            self.assertEqual(reply["structure_status"], "unknown", author)
        for quote_author in ({"screen_name": ["alice"]}, {}, {"screen_name": " "}):
            damaged = twitter._structure_from_fxtwitter(
                {"id": "1", "text": "main", "author": {"screen_name": "a"},
                 "quote": {"id": "2", "text": "q", "author": quote_author}})
            self.assertEqual(damaged["quote_status"], "unknown", quote_author)
        missing = twitter._structure_from_fxtwitter({"id": "1", "text": "main", "author": {"screen_name": "a"},
                                                     "quote": {"id": "2", "text": "q"}})
        self.assertEqual(missing["quote_status"], "unknown")  # no author: no "[Quoted post by @]"
        for top in ({}, {"author": {}}, {"author": {"screen_name": ""}}):
            unread = twitter._structure_from_fxtwitter(dict({"id": "1", "text": "main"}, **top))
            self.assertEqual(unread["structure_status"], "unknown", top)
        odd_reply = twitter._structure_from_fxtwitter({"id": "1", "text": "main", "author": {"screen_name": "a"},
                                                       "replying_to_status": "not-a-status", "replying_to": "b"})
        self.assertEqual(odd_reply["structure_status"], "unknown")
        # X handles are [A-Za-z0-9_]{1,15}; post ids are ASCII numbers; polls have two to four options
        for screen in ("中", "a" * 16):
            self.assertEqual(twitter._structure_from_fxtwitter(
                {"id": "1", "text": "m", "author": {"screen_name": screen}})["structure_status"], "unknown", screen)
        post = {"id": "1", "text": "main", "author": {"screen_name": "a"}}
        for status in (10 ** 30, "١٢٣"):
            self.assertEqual(twitter._structure_from_fxtwitter(
                dict(post, replying_to="a", replying_to_status=status))["structure_status"], "unknown", status)
        broken_id = twitter._structure_from_fxtwitter(
            dict(post, quote={"id": {"broken": True}, "text": "q", "author": {"screen_name": "b"}}))
        self.assertEqual(broken_id["quote_status"], "unknown")
        one_option = twitter._structure_from_fxtwitter(dict(post, poll={"choices": [{"label": "Yes"}]}))
        self.assertEqual(one_option["poll_status"], "unknown")
        self.assertEqual(twitter._extract_author("https://x.com/i/status/123"), "")
        # one optional "@" before a replied-to handle; post ids in URLs are ASCII with no leading zero
        reply = {"id": "1", "text": "main", "author": {"screen_name": "a"}, "replying_to_status": "5"}
        self.assertEqual(twitter._structure_from_fxtwitter(dict(reply, replying_to="@@a"))["structure_status"],
                         "unknown")
        self.assertTrue(twitter._structure_from_fxtwitter(dict(reply, replying_to="@a"))["self_reply"])
        for url in ("https://x.com/a/status/01", "https://x.com/a/status/\u0661\u0662\u0663",
                    "https://x.com/a/status/1\u0662"):
            self.assertIsNone(twitter._extract_status_id(url), url)
        base = {"id": "1", "text": "main", "author": {"screen_name": "a"}}
        for replied, status in ((" ", "5"), ("a", []), ("a", False), ("a", ""), ("a", None)):
            reply = twitter._structure_from_fxtwitter(dict(base, replying_to=replied, replying_to_status=status))
            self.assertEqual(reply["structure_status"], "unknown", (replied, status))
        plain = twitter._structure_from_fxtwitter(dict(base, replying_to=None, replying_to_status=None))
        self.assertEqual(plain["structure_status"], "verified")  # FxTwitter's shape for a post that is not a reply
        response = MagicMock()
        response.json.return_value = {"tweet": {"id": "123", "text": [None], "author": {"screen_name": "a"}}}
        with patch.object(twitter.requests, "get", return_value=response):
            with self.assertRaises(ValueError):
                twitter._fetch_via_fxtwitter("https://x.com/runesleo/status/123")

    def test_layers_count_when_present_whatever_their_value(self):
        from x_reader.evidence import classify_payload
        from x_reader.schema import from_twitter

        base = {"id": "1", "text": "main post", "author": {"screen_name": "a"}}
        quoted = {"id": "2", "text": "quoted", "author": {"screen_name": "b"}}
        damaged = (dict(base, poll=None), dict(base, quote=None), dict(base, article=None), dict(base, article={}),
                   dict(base, media=None), dict(base, quote=dict(quoted, poll={})),
                   dict(base, quote=dict(quoted, poll=None)), dict(base, quote=dict(quoted, media=None)),
                   dict(base, quote=dict(quoted, article=None)))
        for tweet in damaged + (dict(base),):
            data = {"text": "main post", "author": "@a", "url": "https://x.com/a/status/1", "fetch_method": "fxtwitter",
                    "post_id_verified": True, "media_status": twitter._media_status_from_fxtwitter(tweet),
                    **twitter._structure_from_fxtwitter(tweet)}
            status = classify_payload(from_twitter(data).to_dict())["status"]
            self.assertEqual(status, "PASS" if tweet == dict(base) else "PARTIAL", tweet)

    def test_status_paths_must_match_whole(self):
        self.assertEqual(twitter._extract_status_id("https://x.com/alice/status/123"), "123")
        self.assertEqual(twitter._extract_status_id("https://twitter.com/alice/status/123/photo/1?s=20"), "123")
        self.assertEqual(twitter._extract_status_id("https://x.com/i/web/status/123"), "123")
        self.assertEqual(twitter._extract_status_id("https://twitter.com:443/alice/status/123"), "123")
        for url in ("https://x.com/alice/status/123garbage", "https://x.com/alice/status/123/extra/path",
                    "https://evilx.com/alice/status/123", "https://x.com/alice/status/"):
            self.assertIsNone(twitter._extract_status_id(url), url)
            self.assertFalse(twitter._is_tweet_url(url), url)

    def test_answers_for_another_post_are_not_used(self):
        from unittest.mock import MagicMock, patch

        oembed_other = {"text": "A full public post that is long enough to use directly.", "post_id": "999",
                        "author": "Leo", "title": "A full public post", "media_status": "unknown"}
        response = MagicMock()
        response.json.return_value = {"tweet": {"id": "999", "text": "other post", "author": {}}}
        with patch.object(twitter, "_fetch_via_oembed", return_value=oembed_other), \
                patch.object(twitter.requests, "get", return_value=response), \
                patch.object(twitter, "_fetch_article_via_jina", side_effect=RuntimeError("no")), \
                patch.object(twitter, "fetch_via_jina", side_effect=RuntimeError("no")), \
                patch.dict("os.environ", {"X_READER_HOSTED": "1"}):
            with self.assertRaises(RuntimeError):
                asyncio.run(twitter.fetch_twitter("https://x.com/runesleo/status/123"))
        with self.assertRaisesRegex(ValueError, "different post"):
            with patch.object(twitter.requests, "get", return_value=response):
                twitter._fetch_via_fxtwitter("https://x.com/runesleo/status/123")

    def test_fxtwitter_structure_appends_quote_and_poll_and_flags_articles(self):
        tweet = {
            "id": "1", "text": "My take 👇", "author": {"screen_name": "leo"},
            "quote": {"id": "2", "text": "Original claim", "author": {"screen_name": "alice"}},
            "poll": {"choices": [{"label": "Yes", "percentage": 60}, {"label": "No", "percentage": 40}]},
        }
        structure = twitter._structure_from_fxtwitter(tweet)
        self.assertEqual(structure["quote_status"], "read")
        self.assertEqual(structure["poll_status"], "read")
        self.assertEqual(structure["article_status"], "none")
        result = twitter._with_structure({"text": tweet["text"]}, structure)
        self.assertIn("[Quoted post by @alice]\nOriginal claim", result["text"])
        self.assertIn("- Yes (60%)", result["text"])
        with_media = twitter._structure_from_fxtwitter({**tweet, "quote": {
            "id": "2", "text": "chart", "author": {"screen_name": "alice"},
            "media": {"photos": [{"url": "https://pbs.twimg.com/x.jpg"}]}}})
        self.assertEqual(with_media["quote_status"], "media_unread")
        for layer in ({"article": {"title": "T"}}, {"poll": {"choices": []}}, {"quote": {"id": "9", "text": "q"}},
                      {"text": "1/ a thread inside the quote"}):
            nested = twitter._structure_from_fxtwitter({**tweet, "poll": None, "quote": {
                "id": "2", "text": "Original claim", "author": {"screen_name": "alice"}, **layer}})
            self.assertEqual(nested["quote_status"], "incomplete", layer)
        article = twitter._structure_from_fxtwitter({"id": "3", "text": "x.com/i/article/3", "author": {"screen_name": "a"},
                                                     "article": {"title": "T"}})
        self.assertEqual(article["article_status"], "present")
        self.assertEqual(twitter._structure_from_fxtwitter({})["structure_status"], "unknown")

    def test_replies_to_the_authors_own_post_continue_a_thread(self):
        post = {"id": "5", "text": "And here is the next step.", "author": {"screen_name": "Leo"},
                "replying_to": "leo", "replying_to_status": "4"}
        structure = twitter._structure_from_fxtwitter(post)
        self.assertTrue(twitter._with_structure({"text": post["text"]}, structure)["thread_marker"])
        reply_to_other = dict(post, replying_to="someone_else")
        structure = twitter._structure_from_fxtwitter(reply_to_other)
        self.assertFalse(twitter._with_structure({"text": post["text"]}, structure)["thread_marker"])
        quoted = twitter._structure_from_fxtwitter({"id": "6", "text": "Look", "author": {"screen_name": "bo"},
                                                     "quote": dict(post, id="5")})
        self.assertEqual(quoted["quote_status"], "incomplete")

    def test_text_only_fxtwitter_post_has_no_media(self):
        # FxTwitter omits "media" for posts without media.
        self.assertEqual(twitter._media_status_from_fxtwitter({"id": "20", "text": "just setting up"}), "none")
        self.assertEqual(twitter._media_status_from_fxtwitter({}), "unknown")

    def test_thread_markers(self):
        for text in ("1/ Here is why", "1/NoSpace thread start", "(1/5) A thread", "Last point (5/5)",
                     "Big news 🧵", "A thread 👇"):
            self.assertTrue(twitter._looks_like_thread(text), text)
        for text in ("Plain post with no thread", "Updated 10/13 numbers below", "50/50 odds"):
            self.assertFalse(twitter._looks_like_thread(text), text)

    def test_fetch_twitter_falls_back_after_thin_oembed(self):
        original_oembed = twitter._fetch_via_oembed
        original_fx = twitter._fetch_via_fxtwitter
        try:
            twitter._fetch_via_oembed = lambda _url: {
                "text": "https://t.co/abc123",
                "author": "Leo",
                "title": "",
                "media_status": "unknown",
            }
            twitter._fetch_via_fxtwitter = lambda _url, timeout=10: {
                "text": "FxTwitter 返回的完整正文",
                "author": "@runesleo",
                "title": "FxTwitter 返回的完整正文",
                "media_status": "none",
            }
            data = asyncio.run(
                twitter.fetch_twitter("https://x.com/runesleo/status/123")
            )
        finally:
            twitter._fetch_via_oembed = original_oembed
            twitter._fetch_via_fxtwitter = original_fx

        self.assertEqual(data["fetch_method"], "fxtwitter")
        self.assertEqual(data["text"], "FxTwitter 返回的完整正文")
        self.assertEqual(data["media_status"], "none")


if __name__ == "__main__":
    unittest.main()
