import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "first_success_smoke",
    ROOT / "scripts" / "first_success_smoke.py",
)
smoke = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(smoke)


class FirstSuccessSmokeTest(unittest.TestCase):
    def test_twitter_media_present_is_partial_with_component_receipt(self):
        coverage, evidence, components = smoke.classify(
            {
                "source_type": "twitter",
                "content": "post text",
                "media_type": "text",
                "extra": {
                    "fetch_method": "oembed",
                    "media_status": "present",
                },
            }
        )
        self.assertEqual(coverage, "PARTIAL")
        self.assertIn("attached media present", evidence)
        self.assertEqual(
            components,
            {"post_text": "READ", "attached_media": "PARTIAL"},
        )

    def test_twitter_media_none_is_read(self):
        coverage, evidence, components = smoke.classify(
            {
                "source_type": "twitter",
                "content": "plain public post text",
                "media_type": "text",
                "extra": {
                    "fetch_method": "oembed",
                    "media_status": "none",
                },
            }
        )
        self.assertEqual(coverage, "READ")
        self.assertIn("no media reported", evidence)
        self.assertEqual(components, {"post_text": "READ"})

    def test_twitter_media_unknown_is_conservatively_partial(self):
        coverage, evidence, components = smoke.classify(
            {
                "source_type": "twitter",
                "content": "plain public post text",
                "media_type": "text",
                "extra": {
                    "fetch_method": "oembed",
                    "media_status": "unknown",
                },
            }
        )
        self.assertEqual(coverage, "PARTIAL")
        self.assertIn("could not be verified", evidence)
        self.assertEqual(components, {"post_text": "READ"})

    def test_generic_login_shell_is_auth_required_even_when_long(self):
        coverage, _evidence, _components = smoke.classify(
            {
                "source_type": "manual",
                "title": "Sign in",
                "content": "Please sign in to continue. " + ("x" * 200),
                "extra": {"fetch_method": "direct_html_pinned"},
            }
        )
        self.assertEqual(coverage, "AUTH_REQUIRED")

    def test_generic_error_shell_is_failed_even_when_long(self):
        coverage, _evidence, _components = smoke.classify(
            {
                "source_type": "manual",
                "title": "Access Denied",
                "content": "Request blocked. " + ("x" * 200),
                "extra": {"fetch_method": "direct_html_pinned"},
            }
        )
        self.assertEqual(coverage, "FAILED")

    def test_generic_normal_body_is_read(self):
        coverage, _evidence, _components = smoke.classify(
            {
                "source_type": "manual",
                "title": "Example Domain",
                "content": "This is a real public document body with enough content to read.",
                "extra": {"fetch_method": "direct_html_pinned"},
            }
        )
        self.assertEqual(coverage, "READ")

    def test_auth_detection_requires_explicit_login_language(self):
        self.assertTrue(smoke.looks_auth_required("Try: x-reader login twitter"))
        self.assertTrue(smoke.looks_auth_required("This page is a login wall"))
        self.assertFalse(smoke.looks_auth_required("cookie parser crashed"))
        self.assertFalse(smoke.looks_auth_required("session transport timed out"))
        self.assertFalse(smoke.looks_auth_required("Playwright is not installed"))


if __name__ == "__main__":
    unittest.main()
