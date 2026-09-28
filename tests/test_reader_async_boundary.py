import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


class _DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


sys.modules.setdefault("loguru", types.SimpleNamespace(logger=_DummyLogger()))

from x_reader.reader import UniversalReader


class ReaderAsyncBoundaryTest(unittest.TestCase):
    def test_generic_blocking_fetches_are_offloaded_from_event_loop(self):
        reader = UniversalReader()
        calls = []

        async def fake_to_thread(func, *args, **kwargs):
            calls.append(getattr(func, "__name__", str(func)))
            if getattr(func, "__name__", "") == "fetch_via_jina":
                raise RuntimeError("jina down")
            if getattr(func, "__name__", "") == "fetch_direct_html":
                return {
                    "title": "Fallback",
                    "content": "A sufficiently long fallback document body for async routing.",
                    "url": "https://example.com",
                    "fetch_method": "direct_html_pinned",
                }
            return func(*args, **kwargs)

        with patch("x_reader.reader.asyncio.to_thread", side_effect=fake_to_thread):
            content = asyncio.run(reader._fetch("generic", "https://example.com"))

        self.assertIn("fetch_via_jina", calls)
        self.assertIn("fetch_direct_html", calls)
        self.assertEqual(content.extra["fetch_method"], "direct_html_pinned")


if __name__ == "__main__":
    unittest.main()
