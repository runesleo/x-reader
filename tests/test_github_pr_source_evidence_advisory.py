"""Advisory GitHub PR source-coverage contract, with no platform calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from github_pr_source_evidence_advisory import (
    OPEN, CLOSE, source_urls, evaluate_pr, summary_markdown
)
from x_reader.evidence import build_receipt

URL = "https://x.com/example/status/123?token=not-to-echo"
BLOCK = OPEN + "\n- " + URL + "\n" + CLOSE


def event(body, sha="a"*40, action="opened"):
    return {"action": action, "pull_request": {
        "number": 42, "head": {"sha": sha}, "body": body
    }}


def source(url, media="none", method="oembed"):
    return {"source_type": "twitter", "url": url, "content": "Original X text",
            "extra": {"fetch_method": method, "post_id_verified": True,
                      "media_status": media, "structure_status": "verified",
                      "quote_status": "none", "article_status": "none",
                      "poll_status": "none", "thread_marker": False}}


class GitHubAdvisoryTests(unittest.TestCase):
    def test_unmarked_pr_skips_all_reads(self):
        out = evaluate_pr(event("A PR body"), lambda _: self.fail("must not fetch"))
        self.assertEqual(out["status"], "NOT_REQUESTED")
        self.assertFalse(out["github_write_performed"])

    def test_pass_scope_not_factual_truth(self):
        got = evaluate_pr(event(BLOCK),
                          lambda u: build_receipt(source(u), requested_url=u))
        self.assertEqual(got["status"], "PASS")
        self.assertFalse(got["factual_truth_verified"])
        self.assertFalse(got["changed_files_checked"])
        summary = summary_markdown(got)
        self.assertIn("Not a fact-check", summary)
        self.assertIn("No changed files were checked", summary)
        self.assertNotIn("not-to-echo", summary)
        self.assertNotIn("Original X text", summary)

    def test_media_unread_stays_partial(self):
        out = evaluate_pr(event(BLOCK),
                          lambda u: build_receipt(source(u, "present"), requested_url=u))
        self.assertEqual(out["status"], "PARTIAL")
        self.assertIn("attached_media_unread", summary_markdown(out))

    def test_unknown_method_cannot_pass(self):
        out = evaluate_pr(event(BLOCK),
                          lambda u: build_receipt(source(u, method="browser_page"), requested_url=u))
        self.assertEqual(out["status"], "UNKNOWN")

    def test_missing_or_malformed_markers_do_not_fetch(self):
        for body in (OPEN + "\n" + URL, CLOSE + "\n" + URL,
                     CLOSE + "\n" + OPEN + URL, OPEN + OPEN + URL + CLOSE,
                     OPEN + CLOSE):
            out = evaluate_pr(event(body), lambda _: self.fail("must not fetch"))
            self.assertEqual(out["status"], "UNKNOWN")
            self.assertEqual(out["source_count"], 0)

    def test_over_budget_and_unsafe_urls_do_not_fetch(self):
        for url in ("http://example.org", "https://user:pass@example.org/private"):
            out = evaluate_pr(event(OPEN + url + CLOSE), lambda _: self.fail("fetch"))
            self.assertEqual(out["status"], "UNKNOWN")
        too_many = "\n".join("https://example.org/" + str(i) for i in range(4))
        self.assertEqual(evaluate_pr(event(OPEN + too_many + CLOSE),
                                     lambda _: self.fail("fetch"))["reason"],
                         "SOURCE_BUDGET_EXCEEDED")

    def test_deduplicate_and_ignore_fenced_markdown(self):
        markdown = (OPEN + "\n" + URL + " " + URL + "\n" +
                    "https://example.org/safe\n" + CLOSE)
        self.assertEqual(len(source_urls(markdown)), 2)
        fenced = OPEN + "\n" + chr(96)*3 + "text\nhttps://evil.test/1\n" + chr(96)*3 + "\n" + URL + CLOSE
        self.assertEqual(source_urls(fenced), [URL])

    def test_reader_exception_and_unknown_status_not_success(self):
        def exception(_):
            raise RuntimeError("internal")
        self.assertEqual(evaluate_pr(event(BLOCK), exception)["status"], "FAIL")
        self.assertEqual(evaluate_pr(event(BLOCK),
                          lambda _: {"status":"INVENTED","reason_code":"source_complete"})["status"], "UNKNOWN")

    def test_event_validation_and_head_binding(self):
        with self.assertRaisesRegex(ValueError, "INVALID_PR_HEAD_SHA"):
            evaluate_pr(event(BLOCK, sha="bad"))
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_PR_EVENT_ACTION"):
            evaluate_pr(event(BLOCK, action="closed"))
        reader = lambda _: {"status":"PARTIAL","reason_code":"attached_media_unread"}
        a, b = evaluate_pr(event(BLOCK), reader), evaluate_pr(event(BLOCK), reader)
        self.assertEqual(a,b)
        self.assertNotEqual(a["head_sha"], evaluate_pr(event(BLOCK, sha="b"*40), reader)["head_sha"])

    def test_cli_job_summary_nonpublishing(self):
        with tempfile.TemporaryDirectory() as temp:
            ev = Path(temp)/"event.json"
            summ = Path(temp)/"job-summary.md"
            ev.write_text(json.dumps(event("not opted in")), encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
            p = subprocess.run([sys.executable, "-B",
                str(ROOT/"scripts/github_pr_source_evidence_advisory.py"),
                "--event", str(ev), "--summary", str(summ)],
                cwd=ROOT, capture_output=True, text=True, timeout=15, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("ADVISORY=true", p.stdout)
            self.assertIn("NOT_REQUESTED", summ.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
