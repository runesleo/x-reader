import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "benchmarks" / "moat" / "corpus.jsonl"
RUNNER = ROOT / "benchmarks" / "moat" / "run.py"

spec = importlib.util.spec_from_file_location("moat_run", RUNNER)
moat = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(moat)


class MoatBenchmarkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = moat.load_corpus(CORPUS)

    def test_corpus_has_exactly_50_unique_real_urls(self):
        self.assertEqual(len(self.rows), 50)
        self.assertEqual(len({r["id"] for r in self.rows}), 50)
        self.assertEqual(len({r["url"] for r in self.rows}), 50)
        self.assertTrue(all(r["url"].startswith("https://") for r in self.rows))

    def test_corpus_has_expected_source_class_shape(self):
        counts = {}
        for row in self.rows:
            counts[row["category"]] = counts.get(row["category"], 0) + 1
        self.assertEqual(counts, {
            "web": 10,
            "x_public": 10,
            "wechat": 5,
            "xiaohongshu": 5,
            "telegram": 5,
            "bilibili": 5,
            "podcast": 5,
            "auth_gated": 5,
        })


    def test_xhs_corpus_has_three_candidate_posts_and_two_restricted_controls(self):
        notes = [r for r in self.rows if r["category"] == "xiaohongshu"]
        self.assertEqual(sum(bool(r.get("expected_block")) for r in notes), 2)
        self.assertEqual(sum(not r.get("expected_block") for r in notes), 3)
        self.assertTrue(all("xsec_token=" in r["url"] for r in notes))

    def test_restricted_controls_excluded_from_capability_denominator(self):
        cases = [
            {"provider": "x_reader", "category": "xiaohongshu", "outcome": "EXPECTED_BLOCK",
             "requires_auth": False, "expected_block": True, "latency_ms": 10},
            {"provider": "x_reader", "category": "xiaohongshu", "outcome": "READ",
             "requires_auth": False, "expected_block": False, "latency_ms": 20},
            {"provider": "x_reader", "category": "xiaohongshu", "outcome": "FAIL",
             "requires_auth": False, "expected_block": False, "latency_ms": 20},
        ]
        summary = moat.summarize(cases)["providers"]["x_reader"]
        self.assertEqual(summary["eligible_n"], 2)
        self.assertEqual(summary["categories"]["xiaohongshu"]["eligible_n"], 2)
        self.assertEqual(summary["categories"]["xiaohongshu"]["full_read_rate"], 0.5)

    def test_expected_block_does_not_count_as_read(self):
        row = {"requires_auth": False, "expected_block": True, "min_chars": 20}
        self.assertEqual(moat.classify(row, {"ok": False, "content": ""}), "EXPECTED_BLOCK")
        self.assertEqual(moat.classify(row, {"ok": True, "content": "安全限制 Account abnormal"}), "EXPECTED_BLOCK")
        self.assertEqual(moat.classify(row, {"ok": True, "content": "substantive note source content"}), "CONTROL_READ_REVIEW")

    def test_block_shell_is_not_scored_as_read(self):
        row = {"requires_auth": False, "min_chars": 20}
        result = {"ok": True, "content": "Security restriction. Account abnormal. Switch account and retry."}
        self.assertEqual(moat.classify(row, result), "BLOCK_SHELL")

    def test_normal_page_navigation_sign_in_is_not_block_shell(self):
        row = {"requires_auth": False, "min_chars": 20}
        result = {"ok": True, "content": "Sign in | Create account\n" + ("substantive source text " * 30)}
        self.assertEqual(moat.classify(row, result), "READ")

    def test_auth_login_page_is_not_scored_as_read(self):
        row = {"requires_auth": True, "min_chars": 20}
        result = {
            "ok": True,
            "canonical_url": "https://github.com/login?return_to=%2Fsettings%2Fprofile",
            "title": "Sign in to GitHub · GitHub",
            "content": "Sign in to GitHub Username Password",
        }
        self.assertEqual(moat.classify(row, result), "AUTH_SHELL")

    def test_auth_success_without_proof_is_unverified(self):
        row = {"requires_auth": True, "min_chars": 20}
        result = {"ok": True, "canonical_url": "https://example.com/private", "content": "some returned body"}
        self.assertEqual(moat.classify(row, result), "AUTH_UNVERIFIED")

    def test_media_expected_text_is_partial_media(self):
        row = {"requires_auth": False, "min_chars": 20, "media_expected": True}
        result = {"ok": True, "content": "substantive show notes without transcript"}
        self.assertEqual(moat.classify(row, result), "PARTIAL_MEDIA")

    def test_media_complete_is_full_completion(self):
        row = {"requires_auth": False, "min_chars": 20, "media_expected": True}
        result = {"ok": True, "content": "actual transcript body", "media_complete": True}
        self.assertEqual(moat.classify(row, result), "MEDIA_COMPLETE")

    def test_auth_failure_is_expected_block(self):
        row = {"requires_auth": True, "min_chars": 20}
        self.assertEqual(moat.classify(row, {"ok": False, "content": ""}), "EXPECTED_BLOCK")

    def test_thin_success_is_not_read(self):
        row = {"requires_auth": False, "min_chars": 200}
        self.assertEqual(moat.classify(row, {"ok": True, "content": "short"}), "THIN")

    def test_timeout_shape_can_still_be_summarized(self):
        item = {
            "provider": "x_reader", "id": "x-01", "category": "x_public",
            "url": "https://x.com/example/status/1", "outcome": "FAIL",
            "ok": False, "error": "timeout",
        }
        s = moat.summarize([item])
        self.assertEqual(s["providers"]["x_reader"]["categories"]["x_public"]["outcomes"]["FAIL"], 1)

    def test_summary_is_category_aware(self):
        results = [
            {"provider": "x_reader", "category": "web", "outcome": "READ", "latency_ms": 10},
            {"provider": "x_reader", "category": "web", "outcome": "FAIL", "latency_ms": 20},
        ]
        s = moat.summarize(results)
        self.assertEqual(s["providers"]["x_reader"]["categories"]["web"]["full_read_rate"], 0.5)
        self.assertEqual(s["providers"]["x_reader"]["categories"]["web"]["usable_text_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
