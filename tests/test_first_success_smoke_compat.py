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


class FirstSuccessSmokeCompatibilityTest(unittest.TestCase):
    def test_twitter_media_present_legacy_projection(self):
        coverage, evidence, components = smoke.classify({
            "source_type": "twitter",
            "content": "post text",
            "media_type": "text",
            "extra": {"fetch_method": "oembed", "media_status": "present"},
        })
        self.assertEqual(coverage, "PARTIAL")
        self.assertIn("attached media", evidence)
        self.assertEqual(components, {"post_text": "READ", "attached_media": "PARTIAL"})

    def test_twitter_media_none_legacy_projection(self):
        coverage, _evidence, components = smoke.classify({
            "source_type": "twitter",
            "content": "post text",
            "media_type": "text",
            "extra": {"fetch_method": "oembed", "media_status": "none"},
        })
        self.assertEqual(coverage, "READ")
        self.assertEqual(components, {"post_text": "READ"})

    def test_twitter_unknown_media_drops_unknown_legacy_component(self):
        coverage, _evidence, components = smoke.classify({
            "source_type": "twitter",
            "content": "post text",
            "media_type": "text",
            "extra": {"fetch_method": "oembed", "media_status": "unknown"},
        })
        self.assertEqual(coverage, "PARTIAL")
        self.assertEqual(components, {"post_text": "READ"})

    def test_generic_auth_shell_still_auth_required(self):
        coverage, _evidence, _components = smoke.classify({
            "source_type": "manual",
            "title": "Sign in",
            "content": "Please sign in to continue. " + "x" * 200,
            "extra": {},
        })
        self.assertEqual(coverage, "AUTH_REQUIRED")


if __name__ == "__main__":
    unittest.main()
