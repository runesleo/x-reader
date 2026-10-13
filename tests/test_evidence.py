import unittest

from x_reader.evidence import build_receipt, classify_payload


class EvidenceContractTest(unittest.TestCase):
    def test_x_with_attached_media_is_partial(self):
        result = classify_payload({
            "source_type": "twitter",
            "content": "post text",
            "media_type": "text",
            "extra": {"fetch_method": "oembed", "media_status": "present"},
        })
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["components"]["post_text"], "PASS")
        self.assertEqual(result["components"]["attached_media"], "PARTIAL")

    def test_x_without_media_is_pass(self):
        result = classify_payload({
            "source_type": "twitter",
            "content": "post text",
            "media_type": "text",
            "extra": {"fetch_method": "fxtwitter", "media_status": "none"},
        })
        self.assertEqual(result["status"], "PASS")

    def test_x_unknown_media_stays_partial(self):
        result = classify_payload({
            "source_type": "twitter",
            "content": "post text",
            "media_type": "text",
            "extra": {"fetch_method": "oembed", "media_status": "unknown"},
        })
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["components"]["attached_media"], "UNKNOWN")

    def test_youtube_subtitles_covering_the_video_are_pass(self):
        result = classify_payload({
            "source_type": "youtube",
            "content": "spoken transcript",
            "media_type": "video",
            "extra": {"has_transcript": True, "transcript_source": "subtitles",
                      "duration": 600, "transcript_end_seconds": 590},
        })
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["components"]["spoken_media"], "PASS")
        self.assertIn("visual content was not analyzed", result["evidence"])

    def test_youtube_transcript_without_coverage_proof_is_partial(self):
        for extra in (
            {"has_transcript": True},
            {"has_transcript": True, "transcript_source": "subtitles", "duration": 600, "transcript_end_seconds": 120},
            {"has_transcript": True, "transcript_source": "subtitles", "duration": float("nan"), "transcript_end_seconds": 590},
            {"has_transcript": True, "transcript_source": "subtitles", "duration": 600, "transcript_end_seconds": 9000},
            {"has_transcript": True, "transcript_source": "whisper", "duration": 600},
        ):
            result = classify_payload({"source_type": "youtube", "content": "t", "media_type": "video", "extra": extra})
            self.assertEqual(result["status"], "PARTIAL", extra)
            self.assertEqual(result["reason_code"], "transcript_coverage_unproven", extra)

    def test_short_generic_body_is_not_pass(self):
        for body in ("Home About Contact", "x" * 279):
            result = classify_payload({"source_type": "manual", "title": "Example", "content": body})
            self.assertEqual(result["status"], "UNKNOWN", body[:20])

    def test_rss_summary_is_partial_and_full_body_is_pass(self):
        teaser = classify_payload({"source_type": "rss", "content": "Read more…", "extra": {"rss_content": "summary"}})
        self.assertEqual(teaser["status"], "PARTIAL")
        self.assertEqual(teaser["reason_code"], "feed_summary_only")
        missing_kind = classify_payload({"source_type": "rss", "content": "y" * 500})
        self.assertEqual(missing_kind["status"], "PARTIAL")
        full = classify_payload({"source_type": "rss", "content": "y" * 500, "extra": {"rss_content": "full"}})
        self.assertEqual(full["status"], "PASS")

    def test_xhs_and_telegram_need_media_proof(self):
        for source in ("xhs", "telegram"):
            unknown = classify_payload({"source_type": source, "content": "caption"})
            self.assertEqual(unknown["status"], "PARTIAL", source)
            self.assertEqual(unknown["components"]["attached_media"], "UNKNOWN", source)
            present = classify_payload({"source_type": source, "content": "caption", "extra": {"media_status": "present"}})
            self.assertEqual(present["status"], "PARTIAL", source)
            none = classify_payload({"source_type": source, "content": "caption", "extra": {"media_status": "none"}})
            self.assertEqual(none["status"], "PASS", source)

    def test_youtube_description_only_is_partial(self):
        result = classify_payload({
            "source_type": "youtube",
            "content": "page description",
            "media_type": "video",
            "extra": {"has_transcript": False},
        })
        self.assertEqual(result["status"], "PARTIAL")

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

    def test_receipt_is_bounded_metadata_not_full_content(self):
        receipt = build_receipt({
            "source_type": "manual",
            "title": "Example",
            "content": "x" * 400,
            "url": "https://example.com",
            "fetched_at": "2026-10-07T00:00:00Z",
            "extra": {"fetch_method": "direct_html_pinned"},
        })
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["content_chars"], 400)
        self.assertNotIn("content", receipt)
        self.assertEqual(receipt["fetch_method"], "direct_html_pinned")


if __name__ == "__main__":
    unittest.main()
