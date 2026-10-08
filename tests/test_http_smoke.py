import http.client
import json
import subprocess
import threading
import unittest
from unittest.mock import patch

from x_reader.web_app import EvidenceRequestHandler, ThreadingHTTPServer


FIXTURE = {
    "source_type": "twitter",
    "source_name": "@dontbesilent",
    "title": "fixture post",
    "content": "fixture post text",
    "url": "https://x.com/dontbesilent/status/2103875422522077377",
    "media_type": "text",
    "fetched_at": "2026-09-28T00:00:00Z",
    "extra": {
        "fetch_method": "oembed",
        "media_status": "present",
        "media_probe_method": "fxtwitter",
    },
}


class HttpFixtureSmokeTest(unittest.TestCase):
    def test_bilingual_product_and_app_routes(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), EvidenceRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path, marker in (
                ("/", "Turn a URL into source content your AI agent can actually use."),
                ("/en", "Universal content reader for AI agents"),
                ("/zh", "把一个链接，真正读成 AI 能用的内容。"),
                ("/app", "Paste a source. Check what was actually read."),
            ):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                conn.request("GET", path)
                response = conn.getresponse()
                body = response.read().decode("utf-8")
                self.assertEqual(response.status, 200, path)
                self.assertIn("text/html", response.getheader("Content-Type") or "")
                self.assertIn(marker, body, path)
                conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_root_accepts_tracking_query_string(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), EvidenceRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_port,
                timeout=3,
            )
            conn.request("GET", "/?utm_source=chatgpt.com")
            response = conn.getresponse()
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.getheader("Content-Type") or "")
            self.assertIn("<title>x-reader · Universal content reader for AI agents</title>", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_post_projects_fixture_into_partial_receipt(self):
        def fake_run_cli(_url):
            return subprocess.CompletedProcess([], 0, json.dumps(FIXTURE), "")

        with patch("x_reader.web_app.validate_public_url", side_effect=lambda u: u), patch(
            "x_reader.web_app.run_cli", side_effect=fake_run_cli
        ):
            server = ThreadingHTTPServer(("127.0.0.1", 0), EvidenceRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection(
                    "127.0.0.1",
                    server.server_port,
                    timeout=3,
                )
                body = json.dumps({"url": FIXTURE["url"]})
                conn.request(
                    "POST",
                    "/api/read",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["receipt"]["status"], "PARTIAL")
                self.assertEqual(payload["receipt"]["components"]["post_text"], "PASS")
                self.assertEqual(
                    payload["receipt"]["components"]["attached_media"],
                    "PARTIAL",
                )
                self.assertEqual(payload["receipt"]["fetch_method"], "oembed")
                self.assertEqual(payload["source"]["content_preview"], "fixture post text")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
