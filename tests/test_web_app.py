import json
import subprocess
import unittest

from x_reader.web_app import HostedPolicyError, detect_platform, normalize_input_url, read_public_url


class WebAppPolicyTest(unittest.TestCase):
    def test_detects_supported_platforms(self):
        self.assertEqual(detect_platform("https://x.com/a/status/1"), "twitter")
        self.assertEqual(detect_platform("https://youtu.be/abcdefghijk"), "youtube")
        self.assertEqual(detect_platform("https://example.com/a"), "generic")

    def test_rejects_credential_bearing_url(self):
        with self.assertRaises(HostedPolicyError):
            normalize_input_url("https://user:pass@example.com/")

    def test_rejects_disabled_platform_before_network(self):
        called = False

        def validator(_url):
            nonlocal called
            called = True
            return _url

        with self.assertRaises(HostedPolicyError):
            read_public_url("https://t.me/example", validator=validator)
        self.assertFalse(called)

    def test_success_response_uses_canonical_receipt(self):
        payload = {
            "source_type": "twitter",
            "title": "hello",
            "content": "post text",
            "url": "https://x.com/a/status/1",
            "media_type": "text",
            "fetched_at": "2026-10-07T00:00:00Z",
            "extra": {"fetch_method": "oembed", "media_status": "present"},
        }

        def runner(_url):
            return subprocess.CompletedProcess([], 0, json.dumps(payload), "")

        data = read_public_url(
            "https://x.com/a/status/1",
            runner=runner,
            validator=lambda u: u,
        )
        self.assertTrue(data["ok"])
        self.assertEqual(data["receipt"]["status"], "PARTIAL")
        self.assertEqual(data["source"]["content_preview"], "post text")
        self.assertFalse(data["policy"]["saved_sessions"])
        self.assertFalse(data["policy"]["external_transcription_keys"])

    def test_failed_cli_is_valid_fail_receipt(self):
        def runner(_url):
            return subprocess.CompletedProcess(
                [],
                1,
                json.dumps(
                    {
                        "ok": False,
                        "error": "blocked",
                        "error_type": "ValueError",
                    }
                ),
                "",
            )

        data = read_public_url(
            "https://example.com",
            runner=runner,
            validator=lambda u: u,
        )
        self.assertTrue(data["ok"])
        self.assertEqual(data["receipt"]["status"], "FAIL")
        self.assertIn("valueerror", data["receipt"]["reason_code"])


if __name__ == "__main__":
    unittest.main()
