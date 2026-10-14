import unittest

from x_reader.evidence import build_receipt, classify_payload

VERIFIED = {"redirects_verified": True, "embedded_media": 0, "page_signals": [], "body_verified": True}

ARTICLE = (
    "Readers of this long-form article will find a careful argument here.\n"
    "Each paragraph adds context, evidence and a clear conclusion for them.\n"
) * 4

X_VERIFIED = {
    "fetch_method": "fxtwitter", "post_id_verified": True, "media_status": "none", "structure_status": "verified",
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
                  "page_state": "ok", "video_id": "abcdefghijk", "transcript_video_id": "abcdefghijk", **extra},
    })


class EvidenceContractTest(unittest.TestCase):
    def test_x_with_attached_media_is_partial(self):
        result = _x(media_status="present", fetch_method="oembed")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["reason_code"], "attached_media_unread")
        self.assertEqual(result["components"]["post_text"], "PASS")
        self.assertEqual(result["components"]["attached_media"], "PARTIAL")

    def test_x_text_must_be_tied_to_the_requested_post(self):
        for verified in (None, False):
            extra = dict(X_VERIFIED)
            if verified is None:
                extra.pop("post_id_verified")
            else:
                extra["post_id_verified"] = verified
            result = classify_payload({"source_type": "twitter", "content": "post text", "media_type": "text",
                                       "extra": extra})
            self.assertEqual(result["reason_code"], "post_identity_unverified", verified)

    def test_x_with_every_layer_checked_is_pass(self):
        result = _x()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["components"], {"post_text": "PASS"})

    def test_x_without_structure_proof_is_partial(self):
        # A legacy payload (or a failed structure probe) cannot rule out a quote, poll or article.
        result = classify_payload({
            "source_type": "twitter", "content": "post text", "media_type": "text",
            "extra": {"fetch_method": "fxtwitter", "post_id_verified": True, "media_status": "none"},
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
                                              "duration": 600, "video_id": "abcdefghijk", "page_state": "ok"}})
        self.assertEqual(whisper["reason_code"], "transcript_coverage_unproven")

    def test_youtube_transcript_and_page_are_rated_on_their_own(self):
        full = {"transcript_start_seconds": 0.5, "transcript_end_seconds": 592,
                "transcript_covered_seconds": 560, "transcript_max_gap_seconds": 4}
        # subtitles yt-dlp did not attribute to this video prove nothing about its speech
        for other in ("", "zzzzzzzzzzz"):
            result = _youtube(transcript_video_id=other, **full)
            self.assertEqual(result["reason_code"], "transcript_coverage_unproven", other)
            self.assertEqual(result["components"]["spoken_media"], "PARTIAL", other)
        # a transcript never upgrades a page that is unavailable or not the video's
        for state, metadata in (("unavailable", "FAIL"), ("unidentified", "UNKNOWN"), ("", "UNKNOWN")):
            result = _youtube(page_state=state, **full)
            self.assertEqual(result["status"], "PARTIAL", state)
            self.assertEqual(result["components"]["page_metadata"], metadata, state)
            self.assertEqual(result["components"]["spoken_media"], "PASS", state)
        # ...and only subtitles attributed to this video can change the overall verdict
        for state, status in (("unavailable", "FAIL"), ("unidentified", "UNKNOWN")):
            for other in ("", "zzzzzzzzzzz"):
                result = _youtube(page_state=state, transcript_video_id=other, **full)
                self.assertEqual(result["status"], status, (state, other))
        # no single video: nothing is tied to a video, transcript flag or not
        for extra in ({"has_transcript": True, "page_state": "ok", "video_id": ""},
                      {"has_transcript": False, "page_state": "unidentified", "video_id": ""}):
            result = classify_payload({"source_type": "youtube", "content": "page", "media_type": "video",
                                       "extra": extra})
            self.assertEqual(result["status"], "UNKNOWN", extra)

    def test_youtube_description_only_is_partial(self):
        result = classify_payload({
            "source_type": "youtube",
            "content": "page description",
            "media_type": "video",
            "extra": {"has_transcript": False, "page_state": "ok", "video_id": "abcdefghijk"},
        })
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["reason_code"], "spoken_media_unread")

    def test_youtube_error_pages_are_not_metadata_reads(self):
        for state, status, reason in (("unavailable", "FAIL", "source_unavailable"),
                                      ("unidentified", "UNKNOWN", "video_unidentified"),
                                      ("", "UNKNOWN", "video_unidentified")):
            result = classify_payload({"source_type": "youtube", "content": "Video unavailable", "media_type": "video",
                                       "extra": {"has_transcript": False, "page_state": state,
                                                 "video_id": "abcdefghijk"}})
            self.assertEqual((result["status"], result["reason_code"]), (status, reason), state)

    def test_wall_notices_open_a_line_of_any_length(self):
        long_prompt = "Please sign in to continue. " + "Lorem ipsum dolor sit amet, consectetur adipiscing. " * 10
        for title, body, status in (
            ("Troubleshooting authentication required errors", ARTICLE * 2, "PASS"),
            ("Authentication required errors: causes and fixes", ARTICLE * 2, "PASS"),
            ("Request blocked errors explained", "Request blocked errors follow a firewall match.\n" + ARTICLE * 2,
             "PASS"),
            ("403. What It Means for Your API", ARTICLE * 2, "PASS"),
            ("404. A History of the Web", ARTICLE * 2, "PASS"),
            ("Research notes", ARTICLE * 2 + "\nContinue reading our latest research", "PASS"),
            ("Blog", ARTICLE * 2 + "\nYou must be logged in to post a comment.", "PASS"),
            ("Blog", ARTICLE * 2 + "\nSign in to view your account", "PASS"),
            ("Blog", ARTICLE * 2 + "\nAdvertisement - Continue Reading Below\n" + ARTICLE, "PASS"),
            ("Essay", ARTICLE * 2 + "\nSmith, J. Microservices in practice. Example Press. (registration required).", "PASS"),
            ("Essay", ARTICLE * 2 + "\nPlease wait...\n" + ARTICLE, "PASS"),
            ("Essay", ARTICLE * 2 + "\nDoe, A. Archive notes. Example Times. (login required).", "PASS"),
            ("Example", "Page not found (404)\n" + ARTICLE * 3, "UNKNOWN"),
            ("Example", "Page not found. Try searching our site.\n" + ARTICLE * 3, "UNKNOWN"),
            ("Example", long_prompt, "FAIL"),
            ("Just a moment... | Example", ARTICLE * 2, "FAIL"),
            ("Attention Required! | Example", ARTICLE * 2, "FAIL"),
            # beside substantial prose a notice may be a wall or a quote: neither pass nor fail
            ("Example", ARTICLE * 2 + "\nPlease sign in to continue.", "UNKNOWN"),
            ("Example", ARTICLE * 2 + "\nAccess denied.", "UNKNOWN"),
            # prompts inside a sentence are hints
            ("Blog", ARTICLE * 2 + "\nTo read this story, please sign in.", "UNKNOWN"),
            ("Blog", ARTICLE * 2 + "\nThis content is for members. Login required.", "UNKNOWN"),
            ("Blog", ARTICLE * 2 + "\nIf you see this, verify you are human to proceed.", "UNKNOWN"),
        ):
            result = classify_payload({"source_type": "manual", "title": title, "content": body, "extra": VERIFIED})
            self.assertEqual(result["status"], status, (title, body[-40:]))

    def test_login_walls_in_any_dash_or_wording(self):
        for opening in ("Please sign\u2011in to continue.", "Please sign\u2010in to continue.",
                        "Please sign\u00adin to continue.", "Please sign\u2013in to continue.",
                        "Authentication required to continue.", "Login is required to continue."):
            thin = classify_payload({"source_type": "manual", "title": "Example", "content": opening, "extra": VERIFIED})
            self.assertEqual(thin["status"], "FAIL", opening)
            long = classify_payload({"source_type": "manual", "title": "Example",
                                     "content": opening + "\n" + ARTICLE * 2, "extra": VERIFIED})
            self.assertEqual(long["status"], "UNKNOWN", opening)
        for title in ("Sign in to Example", "401 Authorization Required"):
            result = classify_payload({"source_type": "manual", "title": title, "content": "Use your account.",
                                       "extra": VERIFIED})
            self.assertEqual(result["status"], "FAIL", title)
        for opening in ("Please sign in before continuing.", "You need to sign in before continuing.",
                        "Registration required.", "(Authentication required to continue.)", "[One more step.]",
                        "(Access denied.)", "Please wait while we check your browser.",
                        "Register to read this article.", "Create an account to read this article.",
                        "Sign in now to continue.", "Please authenticate to continue."):
            thin = classify_payload({"source_type": "manual", "title": "Example", "content": opening, "extra": VERIFIED})
            self.assertEqual(thin["status"], "FAIL", opening)
            long = classify_payload({"source_type": "manual", "title": "Example",
                                     "content": ARTICLE * 2 + "\n" + opening, "extra": VERIFIED})
            self.assertEqual(long["status"], "UNKNOWN", opening)
        # a citation's access note is not a wall
        note = classify_payload({"source_type": "manual", "title": "Essay",
                                 "content": ARTICLE * 2 + "\n(subscription required).", "extra": VERIFIED})
        self.assertEqual(note["status"], "PASS")
        # prompts to comment or to open one's own account are not walls
        for line in ("Please sign in before commenting.", "Please sign in or register to comment.",
                     "Please sign in to your account.", "Please sign in to continue commenting.",
                     "Sign in to access the comments."):
            result = classify_payload({"source_type": "manual", "title": "Blog", "content": ARTICLE * 2 + "\n" + line,
                                       "extra": VERIFIED})
            self.assertEqual(result["status"], "PASS", line)

    def test_standalone_read_more_is_a_teaser(self):
        for cta in ("Read more →", "READ MORE", "Read more…", "Read full story"):
            result = classify_payload({"source_type": "manual", "title": "Story", "content": ARTICLE + "\n" + cta,
                                       "extra": VERIFIED})
            self.assertEqual(result["reason_code"], "paywall_or_teaser", cta)
        # "Read more:" introducing a related link inside an article is not a teaser
        related = classify_payload({"source_type": "manual", "title": "Story",
                                    "content": ARTICLE + "\nRead more: how the index was built\n" + ARTICLE,
                                    "extra": VERIFIED})
        self.assertEqual(related["status"], "PASS")

    def test_pages_too_large_to_check_do_not_pass(self):
        from x_reader import evidence

        huge = "x. " * (evidence.MAX_WALL_SCAN_CHARS // 3 + 10)
        self.assertEqual(evidence.gate_signals("T", huge), ["scan_budget_exceeded"])
        # NFKC turns each U+FDFA into 18 characters: short lines, but far over the normalized budget
        expanding = ("\ufdfa" * 50_000 + "\n") * 5
        self.assertEqual(evidence.gate_signals("T", expanding), ["scan_budget_exceeded"])
        result = classify_payload({"source_type": "manual", "title": "T", "content": ARTICLE,
                                   "extra": dict(VERIFIED, page_signals=["scan_budget_exceeded"])})
        self.assertEqual(result["reason_code"], "page_too_large_to_check")

    def test_wall_wording_is_matched_as_people_read_it(self):
        from x_reader import evidence

        for notice in ("\u0405ubscribe to continue reading.", "Subscri\u0301be to continue reading.",
                       "Sub\u034fscribe to continue reading.", "Sub\U000e0041scribe to continue reading.",
                       "Sub\u3164scribe to continue reading.", "Subscribe\u3164to\u3164continue reading.",
                       "Subscribe\u2800to\u2800continue reading.", "\U0001d412ubscribe to continue reading.",
                       "\ua731\u1d1c\u0299\ua731\u1d04\u0280\u026a\u0299\u1d07 to continue reading.",
                       "\U0001f182\U0001f184\U0001f171scribe to continue reading."):
            self.assertIn("paywall", evidence.gate_signals("T", notice), notice)
        self.assertEqual(evidence._normalize("Caf\u00e9 \u0421\u043e\u0440\u0443"), "cafe copy")
        self.assertEqual(evidence._normalize("\u767b\u5f55\u540e\u9605\u8bfb\u5168\u6587"),
                         "\u767b\u5f55\u540e\u9605\u8bfb\u5168\u6587")  # Chinese stays as written

    def test_known_missing_page_wording_never_passes(self):
        for opening in ("Page not found (Error 404)", "Page not found [ERROR 404]", "Page not found (HTTP 404)",
                        "Page not found, try searching our site.", "[ERROR 404]", "(HTTP 404)", "404 - Page not found",
                        "Page not found. " + "We looked everywhere for the page you wanted, but it has wandered off.",
                        "We're sorry, but the page you requested could not be found.",
                        "Error: This page does not exist", "The page you are looking for might have been removed.",
                        "PAGE NOT FOUND", "Page Not Found: /old-slug", "Page unavailable.", "Content not available.",
                        "This page is gone."):
            result = classify_payload({"source_type": "manual", "title": "Example", "content": opening + "\n" + ARTICLE * 3,
                                       "extra": VERIFIED})
            self.assertEqual(result["reason_code"], "possibly_missing_page", opening)

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
                                   "extra": dict(VERIFIED, embedded_media=2)})
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["reason_code"], "embedded_media_unread")
        # no component may pass before the shown body is verified
        unverified = classify_payload({"source_type": "manual", "title": "A", "content": ARTICLE,
                                       "extra": dict(VERIFIED, embedded_media=1, body_verified=False)})
        self.assertEqual(unverified["status"], "UNKNOWN")
        self.assertEqual(unverified["components"], {"text_body": "UNKNOWN"})
        # a thin menu beside a player is no text body that passes
        thin = classify_payload({"source_type": "manual", "title": "A", "content": "Home About Contact Blog Shop",
                                 "extra": dict(VERIFIED, embedded_media=1)})
        self.assertEqual(thin["components"], {"text_body": "UNKNOWN", "media": "PARTIAL"})

    def test_generic_length_alone_does_not_pass(self):
        nav = "\n".join(["Home", "About us", "Pricing", "Blog", "Careers", "Contact sales"] * 20)
        links = "\n".join(f"[Item {i}](https://example.com/{'a' * 60}/{i})" for i in range(20))
        headlines = "\n".join(f"How one team rebuilt its whole data platform in week {i}" for i in range(20))
        for body in ("Home About Contact", "x" * 400, nav, links, headlines):
            result = classify_payload({"source_type": "manual", "title": "Example", "content": body, "extra": VERIFIED})
            self.assertEqual(result["status"], "UNKNOWN", body[:30])
            self.assertEqual(result["reason_code"], "thin_generic_body", body[:30])

    def test_generic_paywall_teaser_is_partial(self):
        for body in (
            "Subscribe to continue reading this story.\n" + ARTICLE[:300],
            ARTICLE + "\nThis post is for paid subscribers. Upgrade to keep reading.",
            ARTICLE + "\n本文为会员专享内容，开通会员后阅读全文。",
            ARTICLE + "\n点击继续阅读全文",
            ARTICLE + "\nContinue reading",
            ARTICLE + "\nContinue reading this article",
            ARTICLE + "\nRead the rest of this article",
            ARTICLE + "\nContinue reading on the publisher website →",
            ARTICLE + "\nThe market fell 3% on Monday. Continue reading →",
            ARTICLE + "\nKEEP READING…",
        ):
            result = classify_payload({"source_type": "manual", "title": "Story", "content": body, "extra": VERIFIED})
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
        for signal, status in (("paywall", "PARTIAL"), ("auth_title", "FAIL"), ("challenge_title", "FAIL"),
                               ("auth_required", "UNKNOWN"), ("challenge_or_error", "UNKNOWN"),
                               ("missing_page", "UNKNOWN"), ("consent", "UNKNOWN"), ("auth_hint", "UNKNOWN"),
                               ("challenge_hint", "UNKNOWN"), ("missing_hint", "UNKNOWN")):
            result = classify_payload({"source_type": "manual", "title": "A", "content": ARTICLE * 2,
                                       "extra": dict(VERIFIED, page_signals=[signal])})
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
            if extra.get("extraction") == "container":  # a browser read that ended on the article, nothing in doubt
                extra = {"final_url": "https://mp.weixin.qq.com/s/abc", "visual_risks": [], "page_signals": [], **extra}
            return classify_payload({"source_type": "wechat", "title": "文章", "content": content, "extra": extra,
                                     "url": "https://mp.weixin.qq.com/s/abc"})

        self.assertEqual(wechat("此内容因违规无法查看")["status"], "FAIL")
        self.assertEqual(wechat("排查接口参数错误的三个步骤。\n" + ARTICLE * 3, extraction="container",
                                embedded_media=0)["status"], "PASS")
        self.assertEqual(wechat(ARTICLE, extraction="page_body")["reason_code"], "article_container_missing")
        images = wechat("今日分享\n![image](https://mmbiz.qpic.cn/a.jpg)\n![image](https://mmbiz.qpic.cn/b.jpg)")
        self.assertEqual(images["status"], "PARTIAL")
        self.assertEqual(images["reason_code"], "attached_media_unread")
        self.assertEqual(wechat(ARTICLE, extraction="container", embedded_media=0)["status"], "PASS")
        # players and frames: counted ones are unread, uncounted ones may be there
        self.assertEqual(wechat(ARTICLE, extraction="container", embedded_media=1)["reason_code"],
                         "embedded_media_unread")
        self.assertEqual(wechat(ARTICLE, extraction="container")["reason_code"], "media_presence_unknown")
        self.assertEqual(wechat(ARTICLE, extraction="reader")["status"], "PARTIAL")
        # walls count as on generic pages
        long_body = ARTICLE * 3
        self.assertEqual(classify_payload({"source_type": "wechat", "title": "Sign in to continue",
                                           "content": "Please sign in to continue.\n" + long_body,
                                           "extra": {"extraction": "container", "embedded_media": 0}})["status"],
                         "FAIL")
        self.assertEqual(wechat("Please sign in to continue.\n" + long_body, extraction="container",
                                embedded_media=0)["status"], "UNKNOWN")
        self.assertEqual(wechat("Subscribe to continue reading.\n" + long_body, extraction="container",
                                embedded_media=0)["status"], "PARTIAL")
        # a notice wrapped onto two lines of one paragraph, joined by a space or not
        self.assertEqual(wechat("Please sign\nin to continue.\n\n" + long_body, extraction="container",
                                embedded_media=0)["status"], "UNKNOWN")
        self.assertNotEqual(wechat("登录后\n查看全文\n\n" + long_body, extraction="container",
                                   embedded_media=0)["status"], "PASS")
        # the browser read is plain text: wording in angle brackets is words, not markup
        self.assertNotEqual(wechat("<Please sign in to continue>\n" + long_body, extraction="container",
                                   embedded_media=0)["status"], "PASS")
        # the page the browser ended on must be the article
        for final in ("https://evil.example/final", "", "http://mp.weixin.qq.com/s/abc",
                      "https://mp.weixin.qq.com/mp/profile_ext", "https://mp.weixin.qq.com/s/other",
                      "https://mp.weixin.qq.com/safe/abc"):
            self.assertEqual(wechat(ARTICLE, extraction="container", embedded_media=0, final_url=final)["reason_code"],
                             "article_identity_unverified", final)
        # what the browser read cannot vouch for, or did not check
        self.assertEqual(wechat(ARTICLE, extraction="container", embedded_media=0,
                                visual_risks=["faint_text"])["reason_code"], "visibility_unverified")
        self.assertEqual(classify_payload({"source_type": "wechat", "title": "文章", "content": ARTICLE, "extra": {
            "extraction": "container", "embedded_media": 0, "final_url": "https://mp.weixin.qq.com/s/abc"},
            "url": "https://mp.weixin.qq.com/s/abc"})["reason_code"], "visibility_unverified")
        long_form = "https://mp.weixin.qq.com/s?__biz=MzA&mid=22&idx=1&sn=ab"
        for final, code in ((long_form + "&chksm=1&scene=21#rd", "text_body_read"),
                            (long_form.replace("idx=1", "idx=2"), "article_identity_unverified")):
            self.assertEqual(classify_payload({"source_type": "wechat", "title": "文章", "content": ARTICLE, "url": long_form,
                                               "extra": {"extraction": "container", "embedded_media": 0,
                                                         "final_url": final, "visual_risks": [],
                                                         "page_signals": []}})["reason_code"], code)
        # walls around the article must have been looked for; ports other than HTTPS's are another page
        self.assertEqual(wechat(ARTICLE, extraction="container", embedded_media=0, page_signals=None)["reason_code"],
                         "visibility_unverified")
        self.assertEqual(wechat(ARTICLE, extraction="container", embedded_media=0,
                                final_url="https://mp.weixin.qq.com:444/s/abc")["reason_code"],
                         "article_identity_unverified")
        self.assertEqual(wechat(ARTICLE, extraction="container", embedded_media=0,
                                page_signals=["scan_budget_exceeded"])["reason_code"], "page_too_large_to_check")
        # a browser read's words in angle brackets are prose, not markup
        self.assertEqual(wechat("<" + ARTICLE.replace(chr(10), " ") * 2 + ">", extraction="container",
                                embedded_media=0)["status"], "PASS")
        # walls drawn around the article count; links around it are not teasers
        from x_reader.evidence import page_wall_signals
        self.assertNotEqual(wechat(ARTICLE, extraction="container", embedded_media=0,
                                   page_signals=page_wall_signals("文章", "登录后查看全文\n" + ARTICLE))["status"], "PASS")
        self.assertNotEqual(wechat(ARTICLE, extraction="container", embedded_media=0,
                                   page_signals=page_wall_signals("文章", "Please sign in to continue.\n" + ARTICLE))[
            "status"], "PASS")
        self.assertNotEqual(wechat(ARTICLE, extraction="container", embedded_media=0,
                                   page_signals=page_wall_signals("文章", "完成验证后即可继续访问"))["status"], "PASS")
        self.assertEqual(wechat(ARTICLE, extraction="container", embedded_media=0,
                                page_signals=page_wall_signals("文章", ARTICLE + "\n阅读原文\n赞 分享 推荐 写留言"))[
            "status"], "PASS")

    def test_wechat_notice_titles_fail_and_notice_lines_beside_text_are_unknown(self):
        def wechat(title, content):
            return classify_payload({"source_type": "wechat", "title": title, "content": content,
                                     "url": "https://mp.weixin.qq.com/s/abc",
                                     "extra": {"extraction": "container", "embedded_media": 0, "visual_risks": [],
                                               "final_url": "https://mp.weixin.qq.com/s/abc", "page_signals": []}})

        long_text = "读者会在这篇长文里看到一个仔细的论证。每一段都补充了背景、证据和清楚的结论。" * 30
        self.assertEqual(wechat("该内容已被发布者删除", "帮助说明。" + long_text)["status"], "FAIL")
        self.assertEqual(wechat("参数错误", long_text)["status"], "FAIL")
        self.assertEqual(wechat("参数错误排查指南", long_text)["status"], "PASS")
        self.assertEqual(wechat("文章", "该内容已被发布者删除\n" + long_text)["status"], "UNKNOWN")
        self.assertEqual(wechat("文章", "参数错误\n" + long_text)["status"], "UNKNOWN")
        self.assertEqual(wechat("文章", long_text + "\n该内容已被发布者删除")["status"], "UNKNOWN")  # any line
        self.assertEqual(wechat("文章", long_text + "\n参数错误")["status"], "UNKNOWN")
        short = "排查参数错误需要先检查请求格式，再核对签名与时间戳，最后看服务端日志里的具体报错。" * 4
        self.assertNotEqual(wechat("文章", short)["status"], "FAIL")  # wording in a sentence is not the notice

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
