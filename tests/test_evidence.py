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

    def test_youtube_transcript_is_pass(self):
        result = classify_payload({
            "source_type": "youtube",
            "content": "spoken transcript",
            "media_type": "video",
            "extra": {"has_transcript": True},
        })
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["components"]["spoken_media"], "PASS")

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
            "content": "x" * 100,
            "url": "https://example.com",
            "fetched_at": "2026-10-07T00:00:00Z",
            "extra": {"fetch_method": "direct_html_pinned"},
        })
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["content_chars"], 100)
        self.assertNotIn("content", receipt)
        self.assertEqual(receipt["fetch_method"], "direct_html_pinned")


if __name__ == "__main__":
    unittest.main()
