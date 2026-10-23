"""Offline synthetic cases; no network or credentials."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "check_reading_coverage.py"
TARGET = "1234567890123456789"


def fixture():
    kinds = [
        ("body", "text"),
        ("img-1", "image"),
        ("img-2", "image"),
        ("img-3", "image"),
        ("comments-root", "comments"),
        ("quotes-root", "quotes"),
        ("article", "external"),
    ]
    return {
        "target_id": TARGET,
        "inventory": {
            "complete": True,
            "evidence_ref": "synthetic:source-inventory",
            "items": [{"id": key, "kind": kind} for key, kind in kinds],
        },
        "observations": [
            {
                "id": key,
                "status": "read",
                "evidence_ref": f"synthetic:read:{key}",
                "pagination_exhausted": True,
                "pending_cursors": [],
                "pending_branches": [],
            }
            for key, kind in kinds
        ],
    }


class CoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = None
        if MODULE.exists():
            spec = importlib.util.spec_from_file_location("reading_coverage", MODULE)
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)

    def check(self, data):
        self.assertIsNotNone(self.module, "check_reading_coverage.py is missing")
        return self.module.validate_receipt(data, TARGET)

    def observation(self, data, key):
        return next(item for item in data["observations"] if item["id"] == key)

    def reject(self, data, code):
        result = self.check(data)
        self.assertFalse(result["coverage_complete"])
        self.assertFalse(result["reading_complete"])
        self.assertIn(code, [item["code"] for item in result["blockers"]])

    def test_complete_evidence_passes(self):
        result = self.check(fixture())
        self.assertTrue(result["reading_complete"])
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["blockers"], [])

    def test_one_of_three_images_not_read_rejects(self):
        data = fixture()
        self.observation(data, "img-3")["status"] = "pending"
        self.reject(data, "ITEM_NOT_READ")

    def test_entire_image_observation_omitted_rejects(self):
        data = fixture()
        data["observations"] = [x for x in data["observations"] if x["id"] != "img-3"]
        self.reject(data, "MISSING_OBSERVATION")

    def test_pending_comment_branch_rejects(self):
        data = fixture()
        self.observation(data, "comments-root")["pending_branches"] = ["reply-42"]
        self.reject(data, "PENDING_BRANCHES")

    def test_pending_comment_cursor_rejects(self):
        data = fixture()
        self.observation(data, "comments-root")["pending_cursors"] = ["next-page"]
        self.reject(data, "PENDING_CURSORS")

    def test_empty_cursor_alone_does_not_prove_completion(self):
        data = fixture()
        self.observation(data, "comments-root")["pagination_exhausted"] = False
        self.reject(data, "PAGINATION_NOT_EXHAUSTED")

    def test_external_summary_only_rejects(self):
        data = fixture()
        self.observation(data, "article")["status"] = "summary_only"
        self.reject(data, "ITEM_NOT_READ")

    def test_inaccessible_media_does_not_count_as_read(self):
        data = fixture()
        self.observation(data, "img-1")["status"] = "inaccessible"
        self.reject(data, "ITEM_NOT_READ")

    def test_read_claim_without_evidence_rejects(self):
        data = fixture()
        self.observation(data, "img-1")["evidence_ref"] = "  "
        self.reject(data, "MISSING_READ_EVIDENCE")

    def test_tool_success_is_not_a_read_receipt(self):
        self.reject({"status": "success", "exit_code": 0}, "TARGET_MISMATCH")

    def test_unenumerated_source_inventory_rejects(self):
        data = fixture()
        data["inventory"]["complete"] = False
        self.reject(data, "INVENTORY_INCOMPLETE")

    def test_missing_inventory_evidence_rejects(self):
        data = fixture()
        data["inventory"].pop("evidence_ref")
        self.reject(data, "MISSING_INVENTORY_EVIDENCE")

    def test_cannot_omit_comment_or_quote_check(self):
        for kind in ("text", "comments", "quotes"):
            with self.subTest(kind=kind):
                data = fixture()
                data["inventory"]["items"] = [
                    x for x in data["inventory"]["items"] if x["kind"] != kind
                ]
                self.reject(data, "REQUIRED_CATEGORY_MISSING")

    def test_duplicate_inventory_item_rejects(self):
        data = fixture()
        data["inventory"]["items"].append(copy.deepcopy(data["inventory"]["items"][0]))
        self.reject(data, "DUPLICATE_ITEM")

    def test_duplicate_observation_rejects(self):
        data = fixture()
        data["observations"].append(copy.deepcopy(data["observations"][0]))
        self.reject(data, "DUPLICATE_OBSERVATION")

    def test_uninventoried_observation_rejects(self):
        data = fixture()
        item = copy.deepcopy(data["observations"][0])
        item["id"] = "unlisted"
        data["observations"].append(item)
        self.reject(data, "UNINVENTORIED_OBSERVATION")

    def test_other_target_receipt_rejects(self):
        data = fixture()
        data["target_id"] = "other-target"
        self.reject(data, "TARGET_MISMATCH")

    def test_legacy_post_id_field_still_matches(self):
        data = fixture()
        data.pop("target_id")
        data["post_id"] = TARGET
        self.assertTrue(self.check(data)["coverage_complete"])

    def test_non_boolean_truthy_values_do_not_pass(self):
        data = fixture()
        data["inventory"]["complete"] = "true"
        self.reject(data, "INVENTORY_INCOMPLETE")

    def test_empty_and_malformed_inputs_fail_closed(self):
        for value in (None, [], "text", {}, {"target_id": TARGET, "inventory": []}):
            with self.subTest(value=value):
                self.assertFalse(self.check(value)["coverage_complete"])

    def test_bad_nested_types_fail_closed(self):
        for area in ("inventory", "observations"):
            for value in (None, [], "x", 1, True):
                with self.subTest(area=area, value=value):
                    data = fixture()
                    data[area] = value
                    self.assertFalse(self.check(data)["coverage_complete"])

    def test_no_media_case_needs_positive_inventory_evidence(self):
        data = fixture()
        data["inventory"]["items"] = [
            x for x in data["inventory"]["items"] if x["kind"] != "image"
        ]
        ids = {x["id"] for x in data["inventory"]["items"]}
        data["observations"] = [x for x in data["observations"] if x["id"] in ids]
        self.assertTrue(self.check(data)["coverage_complete"])

    def test_cli_does_not_accept_invalid_json_as_empty(self):
        self.assertTrue(MODULE.exists())
        result = subprocess.run(
            [sys.executable, str(MODULE), "--target-id", TARGET],
            input="{invalid",
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        self.assertEqual(result.returncode, 2)
        output = json.loads(result.stdout)
        self.assertFalse(output["coverage_complete"])
        self.assertEqual(output["blockers"][0]["code"], "INVALID_JSON")

    def test_cli_complete_receipt_returns_json_without_actions(self):
        self.assertTrue(MODULE.exists())
        result = subprocess.run(
            [sys.executable, str(MODULE), "--target-id", TARGET],
            input=json.dumps(fixture()),
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertTrue(output["coverage_complete"])
        self.assertNotIn("command", output)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
