import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import subprocess
import hashlib
from x_reader.schema import from_podcast

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

    def test_full_long_benchmark_is_explicit_and_isolated(self):
        row = {
            "id": "long", "category": "podcast",
            "url": "https://www.xiaoyuzhoufm.com/episode/6aab4896051af796b9e966c5",
            "media_expected": True, "min_chars": 100,
        }
        false_claim = from_podcast({
            "url": row["url"], "title": "Public long show",
            "description": "Show notes " * 30,
            "full_transcript": "Unverified transcription",
            "transcript_coverage": "full", "has_transcript": True,
            "coverage_basis": "verified_chunk_manifest_and_contiguous_pcm_asr",
        }).to_dict()
        false_claim["extra"].update({
            "chunk_count": 5, "segment_count": 5,
            "reused_source_chunks": 5, "reused_asr_segments": 2,
            "asr_boundary_adjustments": 1, "max_boundary_overrun_seconds": 1.36,
        })
        done = subprocess.CompletedProcess([], 0, json.dumps(false_claim), "")
        with patch.dict("os.environ", {
            "OBSIDIAN_VAULT": "/sensitive/home/notes",
        }, clear=False):
            with patch("subprocess.run", return_value=done) as run:
                result = moat.x_reader_provider(
                    row, timeout=180, podcast_full_long=True
                )
        command = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertIn("--media-full-long", command)
        self.assertNotIn("OBSIDIAN_VAULT", env)
        self.assertIn("X_READER_LONG_CACHE_DIR", env)
        self.assertIn("xr-moat-", env["X_READER_LONG_CACHE_DIR"])
        self.assertFalse(result["media_complete"])
        self.assertEqual(result["evidence_status"], "PARTIAL")
        self.assertEqual(result["reused_source_chunks"], 5)
        self.assertEqual(result["reused_asr_segments"], 2)
        self.assertEqual(result["asr_boundary_adjustments"], 1)
        self.assertAlmostEqual(result["max_boundary_overrun_seconds"], 1.36)
        self.assertNotEqual(moat.classify(row, result), "MEDIA_COMPLETE")

    def test_full_short_podcast_benchmark_requires_verified_receipt(self):
        row = {"id": "short-full", "category": "podcast",
               "url": "https://www.xiaoyuzhoufm.com/episode/6a14e9dd3209346094186445",
               "min_chars": 200, "media_expected": True}
        transcript = "这是完整的十三秒公开音频转录。"
        good = from_podcast({
            "url": row["url"], "title": "Short public episode",
            "description": "Public show notes " * 10,
            "full_transcript": transcript,
            "has_transcript": True, "transcript_coverage": "full",
            "coverage_basis": "complete_encoded_bytes_and_full_decoded_pcm",
            "transcription_method": "local_whisper_tiny_cpu",
            "coverage_intervals": [{"start_seconds": 0.0, "end_seconds": 13.12}],
            "media_duration_seconds": 13.12,
            "decoded_duration_seconds": 13.12,
            "processed_seconds": 13.12,
            "coverage_ratio": 1.0, "audio_bytes": 97989,
            "media_sha256": "a" * 64,
            "media_url_sha256": "b" * 64,
            "transcript_sha256": hashlib.sha256(transcript.encode()).hexdigest(),
            "asr_segments": 2, "verified_complete_bytes": True,
        }).to_dict()
        done = subprocess.CompletedProcess([], 0, json.dumps(good), "")
        with patch.dict("os.environ", {"OBSIDIAN_VAULT": "/private/vault"}):
            with patch("subprocess.run", return_value=done) as run:
                result = moat.x_reader_provider(
                    row, timeout=120, podcast_full_short=True
                )
        command = run.call_args.args[0]
        self.assertIn("--media-full-short", command)
        self.assertEqual(run.call_args.kwargs["env"]["OUTPUT_DIR"].startswith("/"), True)
        self.assertNotIn("OBSIDIAN_VAULT", run.call_args.kwargs["env"])
        self.assertTrue(result["media_complete"])
        self.assertEqual(result["evidence_status"], "PASS")
        self.assertEqual(moat.classify(row, result), "MEDIA_COMPLETE")

        bad = json.loads(json.dumps(good))
        bad["extra"]["transcript_sha256"] = "0" * 64
        with patch("subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(bad), "")):
            result = moat.x_reader_provider(row, timeout=120, podcast_full_short=True)
        self.assertFalse(result["media_complete"])
        self.assertEqual(result["evidence_status"], "PARTIAL")
        self.assertNotEqual(moat.classify(row, result), "MEDIA_COMPLETE")

    def test_podcast_preview_benchmark_is_explicit_and_still_partial(self):
        row = next(r for r in self.rows if r["category"] == "podcast")
        payload = {
            "source_type": "podcast", "title": "Episode",
            "content": "Episode show notes " * 25,
            "extra": {
                "media_status": "present", "has_transcript": False,
                "transcript_coverage": "preview", "preview_seconds": 12,
                "preview_transcript_chars": 60,
            },
        }
        done = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with patch("subprocess.run", return_value=done) as run:
            result = moat.x_reader_provider(row, timeout=90, podcast_preview_seconds=12)
        cmd = run.call_args.args[0]
        self.assertIn("--media-preview-seconds", cmd)
        self.assertEqual(cmd[cmd.index("--media-preview-seconds") + 1], "12")
        self.assertEqual(result["preview_seconds"], 12)
        self.assertFalse(result["media_complete"])
        self.assertEqual(moat.classify(row, result), "PARTIAL_MEDIA")

    def test_normal_benchmark_does_not_call_media_preview(self):
        row = next(r for r in self.rows if r["category"] == "podcast")
        payload = {
            "source_type": "podcast", "title": "Episode",
            "content": "Episode show notes " * 25,
            "extra": {"media_status": "present", "has_transcript": False},
        }
        done = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with patch("subprocess.run", return_value=done) as run:
            moat.x_reader_provider(row, timeout=30)
        self.assertNotIn("--media-preview-seconds", run.call_args.args[0])

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
