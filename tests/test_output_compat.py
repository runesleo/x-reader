"""Golden tests for the JSON shape downstream agents parse (CLI --json and MCP tools)."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from x_reader.schema import SourceType, UnifiedContent, from_twitter, from_youtube

# Top-level keys published by v0.3.x / v0.4.0. Adding keys is fine; removing or
# renaming one breaks agents that parse the payload.
PUBLISHED_KEYS = {
    "source_type", "source_name", "title", "content", "url", "id", "fetched_at",
    "media_type", "media_url", "score", "priority", "category", "tags", "processed",
    "digest_date", "title_cn", "content_cn", "extra",
}


class OutputCompatibilityTest(unittest.TestCase):
    def test_cli_json_keeps_every_published_key(self):
        item = UnifiedContent(source_type=SourceType.MANUAL, source_name="e", title="t", content="c", url="https://e.com")
        self.assertTrue(PUBLISHED_KEYS <= set(item.to_dict()))

    def test_youtube_and_twitter_extra_keep_existing_fields(self):
        yt = from_youtube({"title": "t", "description": "d", "url": "https://youtu.be/x", "has_transcript": True, "video_id": "x"})
        for key in ("duration", "view_count", "has_transcript", "video_id"):
            self.assertIn(key, yt.extra)
        tw = from_twitter({"text": "hi", "url": "https://x.com/a/status/1", "author": "a", "media_status": "present"})
        for key in ("likes", "retweets", "fetch_method", "media_status"):
            self.assertIn(key, tw.to_dict()["extra"])

    def test_all_extra_covers_every_optional_extra(self):
        try:
            import tomllib
        except ImportError:  # Python 3.10
            self.skipTest("tomllib needs Python 3.11+")
        from pathlib import Path

        extras = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())[
            "project"]["optional-dependencies"]
        everything = set(extras.pop("all"))
        for name, deps in extras.items():
            self.assertTrue(set(deps) <= everything, name)

    def test_mcp_read_url_returns_the_same_shape(self):
        try:
            from x_reader import mcp_server
        except ImportError:
            self.skipTest("mcp extra not installed")
        item = UnifiedContent(source_type=SourceType.MANUAL, source_name="e", title="t", content="c", url="https://e.com")
        with patch.object(mcp_server.reader, "read", AsyncMock(return_value=item)):
            payload = json.loads(asyncio.run(mcp_server.read_url("https://e.com")))
        self.assertTrue(PUBLISHED_KEYS <= set(payload))


if __name__ == "__main__":
    unittest.main()
