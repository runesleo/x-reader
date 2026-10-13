import unittest

from x_reader.evidence import build_receipt, classify_payload

ARTICLE = (
    "Readers of this long-form article will find a careful argument here.\n"
    "Each paragraph adds context, evidence and a clear conclusion for them.\n"
) * 4

X_VERIFIED = {
    "fetch_method": "fxtwitter", "media_status": "none", "structure_status": "verified",
    "quote_status": "none", "poll_status": "none", "article_status": "none", "thread_marker": False,
}


def _x(text="post text", **extra):
    return classify_payload({
        "source_type": "twitter", "content": text, "media_type": "text",
        "extra": {**X_VERIFIED, **extra},
    })


def _youtube(**extra):
    return classify_payload({
        "source_type": "youtube", "content": "spoken transcript", "media_type": "video",
        "extra": {"has_transcript": True, "transcript_source": "subtitles", "duration": 600,
                  "page_state": "ok", **extra},
    })


class EvidenceContractTest(unittest.TestCase):
    def test_x_with_attached_media_is_partial(self):
        result = _x(media_status="present", fetch_method="oembed")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["reason_code"], "attached_media_unread")
        self.assertEqual(result["components"]["post_text"], "PASS")
        self.assertEqual(result["components"]["attached_media"], "PARTIAL")

    def test_x_with_every_layer_checked_is_pass(self):
        result = _x()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["components"], {"post_text": "PASS"})

    def test_x_without_structure_proof_is_partial(self):
        # A legacy payload (or a failed structure probe) cannot rule out a quote, poll or article.
        result = classify_payload({
            "source_type": "twitter", "content": "post text", "media_type": "text",
            "extra": {"fetch_method": "fxtwitter", "media_status": "none"},
        })
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["reason_code"], "post_structure_unverified")

    def test_x_unknown_media_stays_partial(self):
        result = _x(media_status="unknown", fetch_method="oembed")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["components"]["attached_media"], "UNKNOWN")

    def test_x_thread_quote_and_article_layers_keep_it_partial(self):
        cases = (
            ({"thread_marker": True}, "thread_continuation_unread", "thread"),
            ({"quote_status": "media_unread"}, "quoted_post_incomplete", "quoted_post"),
            ({"quote_status": "incomplete"}, "quoted_post_incomplete", "quoted_post"),
            ({"article_status": "present"}, "article_body_unread", "article_body"),
        )
        for extra, reason, component in cases:
            result = _x(**extra)
            self.assertEqual(result["status"], "PARTIAL", extra)
            self.assertEqual(result["reason_code"], reason, extra)
            self.assertEqual(result["components"][component], "PARTIAL", extra)

    def test_x_quoted_post_and_poll_that_were_read_are_pass(self):
        result = _x(quote_status="read", poll_status="read")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["components"]["quoted_post"], "PASS")
        self.assertEqual(result["components"]["poll"], "PASS")

    def test_x_page_reader_text_is_not_a_verified_post(self):
        for method in ("jina", "x_article_jina", "playwright_page", ""):
            result = _x(text="Something went wrong. Try reloading. " * 3, fetch_method=method)
            self.assertEqual(result["status"], "UNKNOWN", method)
            self.assertEqual(result["reason_code"], "post_text_unverified", method)

    def test_youtube_video_is_never_pass_because_visuals_are_unread(self):
        result = _youtube(transcript_start_seconds=0.5, transcript_end_seconds=592,
                          transcript_covered_seconds=560, transcript_max_gap_seconds=4)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["reason_code"], "visual_media_unread")
        self.assertEqual(result["components"]["spoken_media"], "PASS")
        self.assertEqual(result["components"]["visual_media"], "PARTIAL")

    def test_youtube_sparse_or_short_subtitles_do_not_prove_speech(self):
        for extra in (
            # two cues at the very start and end: 585 s uncaptioned in between
            {"transcript_start_seconds": 1, "transcript_end_seconds": 598.25,
             "transcript_covered_seconds": 11.75, "transcript_max_gap_seconds": 585.5},
            # continuous captions that stop 90 s before the end
            {"transcript_start_seconds": 0, "transcript_end_seconds": 510,
             "transcript_covered_seconds": 510, "transcript_max_gap_seconds": 0},
            # a 45 s uncaptioned opening
            {"transcript_start_seconds": 45, "transcript_end_seconds": 598,
             "transcript_covered_seconds": 540, "transcript_max_gap_seconds": 2},
            # blips every 25 s: no long gap, but almost nothing captioned
            {"transcript_start_seconds": 0, "transcript_end_seconds": 599,
             "transcript_covered_seconds": 24, "transcript_max_gap_seconds": 24},
            # inconsistent: more covered than spanned
            {"transcript_start_seconds": 0, "transcript_end_seconds": 300,
             "transcript_covered_seconds": 590, "transcript_max_gap_seconds": 0},
            {},
            {"transcript_end_seconds": 590},
            {"transcript_start_seconds": 0, "transcript_end_seconds": 9000,
             "transcript_covered_seconds": 590, "transcript_max_gap_seconds": 0},
        ):
            result = _youtube(**extra)
            self.assertEqual(result["status"], "PARTIAL", extra)
            self.assertEqual(result["reason_code"], "transcript_coverage_unproven", extra)
            self.assertEqual(result["components"]["spoken_media"], "PARTIAL", extra)
        whisper = classify_payload({"source_type": "youtube", "content": "t", "media_type": "video",
                                    "extra": {"has_transcript": True, "transcript_source": "whisper",
                                              "duration": 600}})
        self.assertEqual(whisper["reason_code"], "transcript_coverage_unproven")

    def test_youtube_description_only_is_partial(self):
        result = classify_payload({
            "source_type": "youtube",
            "content": "page description",
            "media_type": "video",
            "extra": {"has_transcript": False, "page_state": "ok"},
        })
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["reason_code"], "spoken_media_unread")

    def test_youtube_error_pages_are_not_metadata_reads(self):
        for state, status, reason in (("unavailable", "FAIL", "source_unavailable"),
                                      ("unidentified", "UNKNOWN", "video_unidentified"),
                                      ("", "UNKNOWN", "video_unidentified")):
            result = classify_payload({"source_type": "youtube", "content": "Video unavailable", "media_type": "video",
                                       "extra": {"has_transcript": False, "page_state": state}})
            self.assertEqual((result["status"], result["reason_code"]), (status, reason), state)

    def test_wall_notices_are_anchored_to_short_lines(self):
        verified = {"redirects_verified": True, "embedded_media": 0, "page_signals": [], "body_verified": True}
        for title, body, status in (
            ("Troubleshooting authentication required errors", ARTICLE * 2, "PASS"),
            ("Research notes", ARTICLE * 2 + "\nContinue reading our latest research", "PASS"),
            ("Example", "Page not found (404)\n" + ARTICLE * 3, "UNKNOWN"),
            ("Example", "Page not found. Try searching our site.\n" + ARTICLE * 3, "UNKNOWN"),
            ("Example", ARTICLE * 2 + "\nPlease sign in to continue.", "FAIL"),
        ):
            result = classify_payload({"source_type": "manual", "title": title, "content": body, "extra": verified})
            self.assertEqual(result["status"], status, (title, body[:30]))

    def test_generic_pass_needs_the_shown_body_verified(self):
        result = classify_payload({"source_type": "manual", "title": "A", "content": ARTICLE * 2,
                                   "extra": {"redirects_verified": True, "embedded_media": 0, "page_signals": [],
                                             "body_verified": False}})
        self.assertEqual(result["reason_code"], "page_unverified")

    def test_generic_article_with_real_prose_is_pass(self):
        result = classify_payload({"source_type": "manual", "title": "Essay", "content": ARTICLE,
                                   "url": "https://blog.example.com/essay",
                                   "extra": {"redirects_verified": True, "embedded_media": 0, "page_signals": [], "body_verified": True}})
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["reason_code"], "text_body_read")

    def test_generic_pass_needs_the_page_read_by_x_reader_itself(self):
        for extra in ({}, {"redirects_verified": True}, {"redirects_verified": True, "embedded_media": None},
                      {"redirects_verified": False, "embedded_media": 0},
                      {"redirects_verified": True, "embedded_media": True}):
            result = classify_payload({"source_type": "manual", "title": "A", "content": ARTICLE, "extra": extra})
            self.assertEqual(result["status"], "UNKNOWN", extra)
            self.assertEqual(result["reason_code"], "page_unverified", extra)

    def test_generic_page_with_embedded_audio_or_video_is_partial(self):
        result = classify_payload({"source_type": "manual", "title": "A", "content": ARTICLE,
                                   "extra": {"redirects_verified": True, "embedded_media": 2}})
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["reason_code"], "embedded_media_unread")

    def test_generic_length_alone_does_not_pass(self):
        nav = "\n".join(["Home", "About us", "Pricing", "Blog", "Careers", "Contact sales"] * 20)
        links = "\n".join(f"[Item {i}](https://example.com/{'a' * 60}/{i})" for i in range(20))
        headlines = "\n".join(f"How one team rebuilt its whole data platform in week {i}" for i in range(20))
        for body in ("Home About Contact", "x" * 400, nav, links, headlines):
            result = classify_payload({"source_type": "manual", "title": "Example", "content": body})
            self.assertEqual(result["status"], "UNKNOWN", body[:30])
            self.assertEqual(result["reason_code"], "thin_generic_body", body[:30])

    def test_generic_paywall_teaser_is_partial(self):
        for body in (
            "Subscribe to continue reading this story.\n" + ARTICLE[:300],
            ARTICLE + "\nThis post is for paid subscribers. Upgrade to keep reading.",
            ARTICLE + "\n本文为会员专享内容，开通会员后阅读全文。",
            ARTICLE + "\n点击继续阅读全文",
            ARTICLE + "\nContinue reading",
        ):
            result = classify_payload({"source_type": "manual", "title": "Story", "content": body})
            self.assertEqual(result["status"], "PARTIAL", body[-40:])
            self.assertEqual(result["reason_code"], "paywall_or_teaser", body[-40:])

    def test_generic_consent_and_removed_pages_fail(self):
        consent = ("We value your privacy. We and our partners use cookies to store and access "
                   "information on your device. Accept all cookies or manage preferences below.")
        long_consent = consent + "\n" + (
            "Our vendors and our partners process data on the basis of legitimate interest, which you can "
            "object to. You can reject all or choose purposes one by one in the list that follows.\n") * 8
        removed = "Page not found\nThe page you were looking for has moved or never existed."
        for title, body, reason in (("Before you continue", consent, "challenge_or_error"),
                                    ("Example", removed, "source_unavailable")):
            result = classify_payload({"source_type": "manual", "title": title, "content": body})
            self.assertEqual(result["status"], "FAIL", reason)
            self.assertEqual(result["reason_code"], reason)
        verified = {"redirects_verified": True, "embedded_media": 0, "page_signals": [], "body_verified": True}
        for body in (long_consent, "We use cookies to give you the best experience.\n" + ARTICLE * 3):
            # one cookie phrase in long text: a consent wall and a bannered article look alike
            result = classify_payload({"source_type": "manual", "title": "News site", "content": body, "extra": verified})
            self.assertEqual(result["status"], "UNKNOWN", body[:40])
            self.assertEqual(result["reason_code"], "consent_text_present")

    def test_missing_page_openings_never_pass(self):
        verified = {"redirects_verified": True, "embedded_media": 0, "page_signals": [], "body_verified": True}
        soft_404 = "Page not found\n" + ARTICLE * 3  # HTTP 200 with plenty of help text
        result = classify_payload({"source_type": "manual", "title": "Example", "content": soft_404, "extra": verified})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason_code"], "possibly_missing_page")
        thin = classify_payload({"source_type": "manual", "title": "404 Not Found | Example",
                                 "content": "Sorry, we could not find that page.", "extra": verified})
        self.assertEqual(thin["status"], "FAIL")

    def test_missing_page_detection_is_anchored(self):
        verified = {"redirects_verified": True, "embedded_media": 0, "page_signals": [], "body_verified": True}
        guide = "How to fix Page Not Found errors\n" + ARTICLE * 2
        result = classify_payload({"source_type": "manual", "title": "How to fix Page Not Found errors",
                                   "content": guide, "extra": verified})
        self.assertEqual(result["status"], "PASS")
        oops = classify_payload({"source_type": "manual", "title": "Example", "content": "Oops! Page not found\n" + ARTICLE * 3,
                                 "extra": verified})
        self.assertEqual(oops["reason_code"], "possibly_missing_page")
        error = classify_payload({"source_type": "manual", "title": "Error 404 | Example",
                                  "content": "We looked everywhere for this page.", "extra": verified})
        self.assertEqual(error["status"], "FAIL")

    def test_missing_page_notices_must_be_exact(self):
        verified = {"redirects_verified": True, "embedded_media": 0, "page_signals": [], "body_verified": True}
        for title, body in (("How to fix Page Not Found errors", "How to fix Page Not Found errors\n" + ARTICLE),
                            ("404 Errors Explained: A Guide", ARTICLE)):
            result = classify_payload({"source_type": "manual", "title": title, "content": body, "extra": verified})
            self.assertEqual(result["status"], "PASS", title)
        branded = classify_payload({"source_type": "manual", "title": "Example News",
                                    "content": "Example News\nPage not found\n" + ARTICLE * 3, "extra": verified})
        self.assertEqual(branded["reason_code"], "possibly_missing_page")

    def test_page_wide_signals_are_honoured(self):
        for signal, status in (("paywall", "PARTIAL"), ("auth_required", "FAIL"), ("missing_page", "UNKNOWN"),
                               ("consent", "UNKNOWN")):
            result = classify_payload({"source_type": "manual", "title": "A", "content": ARTICLE * 2,
                                       "extra": {"redirects_verified": True, "embedded_media": 0,
                                                 "page_signals": [signal]}})
            self.assertEqual(result["status"], status, signal)

    def test_walls_mentioned_later_in_an_article_do_not_count(self):
        article = ARTICLE + "Readers who hit a page not found error should check the address first.\n" + ARTICLE
        result = classify_payload({"source_type": "manual", "title": "Fixing 404 errors", "content": article,
                                   "extra": {"redirects_verified": True, "embedded_media": 0, "page_signals": [], "body_verified": True}})
        self.assertEqual(result["status"], "PASS")

    def test_generic_unverified_redirect_and_media_pages_do_not_pass(self):
        unverified = classify_payload({"source_type": "manual", "title": "A", "content": ARTICLE,
                                       "extra": {"redirects_verified": False}})
        self.assertEqual(unverified["status"], "UNKNOWN")
        self.assertEqual(unverified["reason_code"], "page_unverified")
        media = classify_payload({"source_type": "manual", "title": "A", "content": ARTICLE,
                                  "url": "https://vimeo.com/123456",
                                  "extra": {"redirects_verified": True, "embedded_media": 0, "page_signals": [], "body_verified": True}})
        self.assertEqual(media["status"], "PARTIAL")
        self.assertEqual(media["reason_code"], "embedded_media_unread")

    def test_rss_summary_is_partial_and_full_body_is_pass(self):
        teaser = classify_payload({"source_type": "rss", "content": "Read more…", "extra": {"rss_content": "summary"}})
        self.assertEqual(teaser["status"], "PARTIAL")
        self.assertEqual(teaser["reason_code"], "feed_summary_only")
        missing_kind = classify_payload({"source_type": "rss", "content": ARTICLE})
        self.assertEqual(missing_kind["status"], "PARTIAL")
        full = classify_payload({"source_type": "rss", "content": f"<p>{ARTICLE}</p>", "extra": {"rss_content": "full"}})
        self.assertEqual(full["status"], "PASS")

    def test_rss_markup_and_read_more_do_not_count_as_body(self):
        image_teaser = f'<img src="https://cdn.example.com/{"a" * 400}.jpg">Teaser'
        read_more = f"<p>{ARTICLE}</p><p>Continue reading on the site…</p>"
        paid = f"<p>{ARTICLE}</p><p>Keep reading with a 7-day free trial. Subscribe to keep reading this post.</p>"
        for body in (image_teaser, read_more, paid):
            result = classify_payload({"source_type": "rss", "content": body, "extra": {"rss_content": "full"}})
            self.assertEqual(result["status"], "PARTIAL", body[-30:])
        unverified = classify_payload({"source_type": "rss", "content": f"<p>{ARTICLE}</p>",
                                       "extra": {"rss_content": "unverified"}})
        self.assertEqual(unverified["reason_code"], "feed_body_unverified")

    def test_wechat_notices_containers_and_image_posts(self):
        def wechat(content, **extra):
            return classify_payload({"source_type": "wechat", "title": "文章", "content": content, "extra": extra})

        self.assertEqual(wechat("此内容因违规无法查看")["status"], "FAIL")
        self.assertEqual(wechat("排查接口参数错误的三个步骤。\n" + ARTICLE * 3, extraction="container")["status"], "PASS")
        self.assertEqual(wechat(ARTICLE, extraction="page_body")["reason_code"], "article_container_missing")
        images = wechat("今日分享\n![image](https://mmbiz.qpic.cn/a.jpg)\n![image](https://mmbiz.qpic.cn/b.jpg)")
        self.assertEqual(images["status"], "PARTIAL")
        self.assertEqual(images["reason_code"], "attached_media_unread")
        self.assertEqual(wechat(ARTICLE, extraction="container")["status"], "PASS")

    def test_xhs_and_telegram_need_media_proof(self):
        for source in ("xhs", "telegram"):
            unknown = classify_payload({"source_type": source, "content": "caption"})
            self.assertEqual(unknown["status"], "PARTIAL", source)
            self.assertEqual(unknown["components"]["attached_media"], "UNKNOWN", source)
            present = classify_payload({"source_type": source, "content": "caption", "extra": {"media_status": "present"}})
            self.assertEqual(present["status"], "PARTIAL", source)
            none = classify_payload({"source_type": source, "content": "caption", "extra": {"media_status": "none"}})
            self.assertEqual(none["status"], "PASS", source)

    def test_login_shell_is_fail(self):
        result = classify_payload({
            "source_type": "manual",
            "title": "Sign in",
            "content": "Please sign in to continue. " + "x" * 100,
            "media_type": "text",
            "extra": {},
        })
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["reason_code"], "auth_required")

    def test_empty_generic_read_is_fail(self):
        result = classify_payload({"source_type": "manual", "title": "Example", "content": "  "})
        self.assertEqual(result["status"], "FAIL")

    def test_receipt_is_bounded_metadata_not_full_content(self):
        receipt = build_receipt({
            "source_type": "manual",
            "title": "Example",
            "content": ARTICLE,
            "url": "https://example.com",
            "fetched_at": "2026-10-07T00:00:00Z",
            "extra": {"fetch_method": "direct_html_pinned", "redirects_verified": True, "embedded_media": 0, "page_signals": [], "body_verified": True},
        })
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["content_chars"], len(ARTICLE))
        self.assertNotIn("content", receipt)
        self.assertEqual(receipt["fetch_method"], "direct_html_pinned")


if __name__ == "__main__":
    unittest.main()
