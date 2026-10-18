"""Privacy and correctness acceptance for hosted opt-in aggregate metrics."""

import json
import http.client
from datetime import date, datetime, timedelta, timezone
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from x_reader import usage_metrics, web_app


class UsageAggregatesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "usage.sqlite3"
        self.env = patch.dict(os.environ, {"X_READER_USAGE_DB": str(self.db)})
        self.env.start()
        self.addCleanup(self.env.stop)
        usage_metrics._WARNED_STORAGE = False

    @staticmethod
    def _clock(day: date):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        return patch.object(usage_metrics, "datetime", FrozenDatetime)

    def _seed_days(self, *days: date) -> None:
        usage_metrics.record_response(method="GET", request_target="/app", http_status=200)
        with sqlite3.connect(self.db) as conn:
            for day in days:
                conn.execute(usage_metrics._UPSERT, (
                    day.isoformat(), "app", "200", "NA", "NA",
                    "none", "lt_500ms", 1,
                ))

    def _stored_days(self) -> set[str]:
        with sqlite3.connect(self.db) as conn:
            return {row[0] for row in conn.execute("SELECT DISTINCT day FROM daily_usage")}

    def test_exact_ninety_calendar_dates_not_ninety_prior_dates(self):
        today = date(2026, 10, 18)
        oldest = today - timedelta(days=89)
        expired = today - timedelta(days=90)
        older = today - timedelta(days=91)
        with self._clock(today):
            self._seed_days(oldest, expired, older)
            usage_metrics.record_response(method="GET", request_target="/app", http_status=200)
            summary_days = {row["day"] for row in usage_metrics.read_summary(self.db, days=90)["rows"]}
        expected = {today.isoformat(), oldest.isoformat()}
        self.assertEqual(self._stored_days(), expected)
        self.assertEqual(summary_days, expected)

    def test_next_utc_day_advances_retention_boundary(self):
        today = date(2026, 10, 18)
        oldest = today - timedelta(days=89)
        with self._clock(today):
            self._seed_days(oldest)
            self.assertIn(oldest.isoformat(), self._stored_days())
        with self._clock(today + timedelta(days=1)):
            usage_metrics.record_response(method="GET", request_target="/app", http_status=200)
        self.assertNotIn(oldest.isoformat(), self._stored_days())
        self.assertIn((today + timedelta(days=1)).isoformat(), self._stored_days())

    def test_idle_http_server_prunes_without_new_requests(self):
        today = date(2026, 10, 18)
        expired = today - timedelta(days=90)
        oldest = today - timedelta(days=89)
        with self._clock(today):
            self._seed_days(expired, oldest)
            cleaned = threading.Event()
            real_prune = web_app.prune_expired

            def signal_prune(path):
                removed = real_prune(path)
                cleaned.set()
                return removed

            server = web_app.BoundedThreadingHTTPServer(
                ("127.0.0.1", 0), web_app.EvidenceRequestHandler, max_threads=4,
            )
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
            )
            try:
                with patch.object(web_app, "prune_expired", side_effect=signal_prune):
                    thread.start()
                    self.assertTrue(cleaned.wait(2), "idle serve_forever never ran retention")
            finally:
                if thread.is_alive():
                    server.shutdown()
                    thread.join(2)
                server.server_close()
        self.assertEqual(
            self._stored_days(), {today.isoformat(), oldest.isoformat()},
        )

    def test_operator_prune_does_not_create_missing_database(self):
        missing = Path(self.temp.name) / "missing.sqlite3"
        self.assertEqual(usage_metrics.prune_expired(missing), 0)
        self.assertFalse(missing.exists())

    def test_operator_prune_does_not_recreate_database_removed_during_open(self):
        self._seed_days(date(2000, 1, 1))
        original_connect = sqlite3.connect

        def remove_just_before_open(*args, **kwargs):
            self.db.unlink()
            return original_connect(*args, **kwargs)

        with patch.object(usage_metrics.sqlite3, "connect", side_effect=remove_just_before_open):
            with self.assertRaises(sqlite3.OperationalError):
                usage_metrics.prune_expired(self.db)
        self.assertFalse(self.db.exists())

    def test_operator_prune_works_with_telemetry_disabled(self):
        self._seed_days(date(2000, 1, 1))
        with patch.dict(os.environ, {"X_READER_USAGE_DB": ""}):
            proc = subprocess.run(
                [sys.executable, "-m", "x_reader.usage_metrics", "--db",
                 str(self.db), "--prune-expired", "--json"],
                capture_output=True, text=True, timeout=8, check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["pruned_rows"], 1)
        self.assertNotIn(date(2000, 1, 1).isoformat(), self._stored_days())

    def test_idle_cleanup_is_nonblocking_if_database_locked(self):
        today = date(2026, 10, 18)
        expired = today - timedelta(days=90)
        with self._clock(today):
            self._seed_days(expired)
            server = web_app.BoundedThreadingHTTPServer(
                ("127.0.0.1", 0), web_app.EvidenceRequestHandler,
            )
            locker = sqlite3.connect(self.db)
            try:
                locker.execute("BEGIN EXCLUSIVE")
                start = time.monotonic()
                server.service_actions()
                self.assertLess(time.monotonic() - start, 0.2)
            finally:
                locker.rollback()
                locker.close()
            self.assertIn(expired.isoformat(), self._stored_days())
            server._next_usage_prune_at = 0.0
            server.service_actions()
            server.server_close()
        self.assertNotIn(expired.isoformat(), self._stored_days())

    def test_only_aggregate_fields_and_private_file(self):
        secret = "private-user-submitted-token-123"
        for _ in range(2):
            usage_metrics.record_response(
                method="POST", request_target=f"/api/read?url=https://private.test/{secret}",
                http_status=200,
                receipt={"status": "PARTIAL", "source_type": "twitter",
                         "reason_code": "attached_media_unread"},
                elapsed_ms=2310,
            )
        usage_metrics.record_response(
            method="GET", request_target=f"/app?utm={secret}", http_status=200, elapsed_ms=9,
        )
        result = usage_metrics.read_summary(self.db)
        self.assertTrue(result["configured"])
        self.assertEqual(sum(row["count"] for row in result["rows"]), 3)
        self.assertEqual(
            next(row["count"] for row in result["rows"] if row["route"] == "read"), 2,
        )
        self.assertEqual(os.stat(self.db).st_mode & 0o777, 0o600)
        self.assertNotIn(secret.encode(), self.db.read_bytes())
        self.assertNotIn(b"client_ip", self.db.read_bytes())
        self.assertNotIn(b"user_agent", self.db.read_bytes())

    def test_opt_in_is_required(self):
        with patch.dict(os.environ, {"X_READER_USAGE_DB": ""}):
            usage_metrics.record_response(method="GET", request_target="/app", http_status=200)
        self.assertFalse(self.db.exists())

    def test_uninitialized_private_database_is_empty_not_an_error(self):
        fd = os.open(self.db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        result = usage_metrics.read_summary(self.db)
        self.assertTrue(result["configured"])
        self.assertEqual(result["rows"], [])

    def test_existing_connections_are_closed(self):
        opened = []
        closed = []
        original_connect = sqlite3.connect

        class TrackedConnection(sqlite3.Connection):
            def close(self):
                closed.append(id(self))
                return super().close()

        def traced_connect(*args, **kwargs):
            conn = original_connect(*args, factory=TrackedConnection, **kwargs)
            opened.append(id(conn))
            return conn

        with patch.object(usage_metrics.sqlite3, "connect", side_effect=traced_connect):
            usage_metrics.record_response(method="GET", request_target="/app", http_status=200)
            self.assertEqual(len(usage_metrics.read_summary(self.db)["rows"]), 1)
        self.assertEqual(opened, closed)

    def test_locked_database_does_not_block_http_response(self):
        usage_metrics.record_response(method="GET", request_target="/app", http_status=200)
        locker = sqlite3.connect(self.db)
        try:
            locker.execute("BEGIN EXCLUSIVE")
            start = time.monotonic()
            usage_metrics.record_response(method="GET", request_target="/app", http_status=200)
            self.assertLess(time.monotonic() - start, 0.2)
        finally:
            locker.rollback()
            locker.close()

    def test_special_character_database_path_uses_exact_file(self):
        special = Path(self.temp.name) / "usage#private?metrics.sqlite3"
        with patch.dict(os.environ, {"X_READER_USAGE_DB": str(special)}):
            usage_metrics.record_response(method="GET", request_target="/app", http_status=200)
            result = usage_metrics.read_summary(special)
        self.assertEqual(sum(row["count"] for row in result["rows"]), 1)
        self.assertEqual(sorted(p.name for p in Path(self.temp.name).iterdir()), [special.name])

    def test_locked_metrics_never_occupy_reader_slots(self):
        # Four sequential readers must keep returning 200 even while a separate
        # SQLite connection prevents telemetry persistence entirely.
        usage_metrics.record_response(method="GET", request_target="/app", http_status=200)
        locker = sqlite3.connect(self.db)
        server = web_app.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), web_app.EvidenceRequestHandler, max_threads=4,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        result = {"ok": True, "receipt": {"status": "PASS", "source_type": "manual"}}
        statuses = []
        try:
            locker.execute("BEGIN EXCLUSIVE")
            with patch.object(web_app, "read_public_url", return_value=result):
                started = time.monotonic()
                for _ in range(4):
                    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    try:
                        client.request("POST", "/api/read", body=b'{"url":"https://example.com"}',
                                       headers={"Content-Type": "application/json"})
                        response = client.getresponse()
                        statuses.append(response.status)
                        self.assertEqual(json.loads(response.read())["receipt"]["status"], "PASS")
                    finally:
                        client.close()
                self.assertLess(time.monotonic() - started, 1.0)
        finally:
            locker.rollback()
            locker.close()
        self.assertEqual(statuses, [200] * 4)

    def test_relative_summary_cli_reads_exact_private_file(self):
        usage_metrics.record_response(method="GET", request_target="/app", http_status=200)
        proc = subprocess.run(
            [sys.executable, "-m", "x_reader.usage_metrics", "--db", "usage.sqlite3", "--json"],
            cwd=self.temp.name, capture_output=True, text=True, timeout=8, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["configured"])
        self.assertEqual(sum(row["count"] for row in result["rows"]), 1)
        self.assertEqual(sorted(p.name for p in Path(self.temp.name).iterdir()), [self.db.name])

    def test_nonproduct_requests_do_not_create_db(self):
        for method, target in (("GET", "/healthz"), ("GET", "/robots.txt"),
                               ("POST", "/random"), ("GET", "/invalid?url=secret")):
            usage_metrics.record_response(method=method, request_target=target, http_status=200)
        self.assertFalse(self.db.exists())

    def test_timeout_and_429_are_grouped_without_url(self):
        usage_metrics.record_response(
            method="POST", request_target="/api/read", http_status=200,
            receipt={"status": "FAIL", "source_type": "manual", "reason_code": "timeout"},
            elapsed_ms=20000,
        )
        usage_metrics.record_response(method="POST", request_target="/api/read",
                                      http_status=429, elapsed_ms=0)
        records = usage_metrics.read_summary(self.db)["rows"]
        self.assertEqual({row["http_code"] for row in records}, {"200", "429"})
        self.assertTrue(any(row["error_class"] == "timeout" for row in records))
        self.assertTrue(any(row["latency_bucket"] == "gte_15s" for row in records))

    def test_storage_error_is_fail_open(self):
        bad = str(Path(self.temp.name) / "missing" / "usage.sqlite3")
        with patch.dict(os.environ, {"X_READER_USAGE_DB": bad}):
            usage_metrics.record_response(
                method="GET", request_target="/app", http_status=200,
            )
        self.assertFalse(Path(bad).exists())

    def test_bad_permission_refused(self):
        self.db.write_text("not a database", encoding="utf-8")
        os.chmod(self.db, 0o644)
        usage_metrics.record_response(method="GET", request_target="/app", http_status=200)
        self.assertEqual(self.db.read_text(), "not a database")
        with self.assertRaises(ValueError):
            usage_metrics.read_summary(self.db)

    def test_parallel_upserts_are_at_most_once_and_best_effort(self):
        failures = []
        def write():
            try:
                usage_metrics.record_response(
                    method="POST", request_target="/api/read", http_status=200,
                    receipt={"status": "PASS", "source_type": "manual"}, elapsed_ms=100,
                )
            except BaseException as exc:
                failures.append(exc)
        threads = [threading.Thread(target=write) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(failures)
        total = sum(x["count"] for x in usage_metrics.read_summary(self.db)["rows"])
        # Busy SQLite storage is deliberately skipped rather than blocking HTTP.
        # Under zero-busy-timeout contention even all 12 can be skipped.
        self.assertGreaterEqual(total, 0)
        self.assertLessEqual(total, 12)
        # But an uncontended follow-up must be persisted exactly once.
        usage_metrics.record_response(
            method="POST", request_target="/api/read", http_status=200,
            receipt={"status": "PASS", "source_type": "manual"}, elapsed_ms=100,
        )
        self.assertEqual(
            sum(x["count"] for x in usage_metrics.read_summary(self.db)["rows"]), total + 1,
        )

    def test_http_handler_counts_page_and_one_success_not_health(self):
        server = web_app.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), web_app.EvidenceRequestHandler, max_threads=4,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        root = f"http://127.0.0.1:{server.server_port}"
        mock_response = {
            "ok": True,
            "receipt": {"status": "PASS", "source_type": "manual"},
            "source": None,
        }
        with patch.object(web_app, "read_public_url", return_value=mock_response):
            with urlopen(root + "/app", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                resp.read()
            with urlopen(root + "/healthz", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                resp.read()
            req = Request(root + "/api/read", method="POST",
                          data=json.dumps({"url": "https://example.com"}).encode(),
                          headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=5) as resp:
                self.assertEqual(json.load(resp)["receipt"]["status"], "PASS")
        grouped = usage_metrics.read_summary(self.db)["rows"]
        self.assertEqual({x["route"] for x in grouped}, {"app", "read"})
        self.assertEqual(sum(x["count"] for x in grouped), 2)


if __name__ == "__main__":
    unittest.main()
