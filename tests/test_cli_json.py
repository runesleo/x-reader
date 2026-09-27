import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

from x_reader.cli import cmd_fetch, main
from x_reader.schema import SourceType, UnifiedContent


class CliJsonOutputTest(unittest.TestCase):
    def setUp(self):
        self.url = "https://x.com/runes_leo/status/123"
        self.item = UnifiedContent(
            source_type=SourceType.TWITTER,
            source_name="@runes_leo",
            title="full source title",
            content="full source body that must not be truncated",
            url=self.url,
            extra={"fetch_method": "oembed"},
        )

    @patch("x_reader.cli.UnifiedInbox")
    @patch("x_reader.cli.UniversalReader")
    def test_single_url_json_contains_complete_payload(self, reader_cls, _inbox_cls):
        reader = reader_cls.return_value
        reader.read = AsyncMock(return_value=self.item)

        output = io.StringIO()
        with redirect_stdout(output):
            cmd_fetch([self.url], json_output=True)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["url"], self.url)
        self.assertEqual(payload["content"], self.item.content)
        self.assertEqual(payload["extra"]["fetch_method"], "oembed")

    @patch("x_reader.cli.UnifiedInbox")
    @patch("x_reader.cli.UniversalReader")
    def test_batch_json_is_an_array_of_complete_payloads(self, reader_cls, _inbox_cls):
        reader = reader_cls.return_value
        second = UnifiedContent(
            source_type=SourceType.MANUAL,
            source_name="example.org",
            title="second",
            content="second body",
            url="https://example.org/a",
        )
        reader.read_batch = AsyncMock(return_value=[self.item, second])

        output = io.StringIO()
        with redirect_stdout(output):
            cmd_fetch([self.url, second.url], json_output=True)

        payload = json.loads(output.getvalue())
        self.assertEqual([row["url"] for row in payload], [self.url, second.url])
        self.assertEqual(payload[0]["content"], self.item.content)
        self.assertEqual(payload[1]["content"], second.content)

    @patch("x_reader.cli.UnifiedInbox")
    @patch("x_reader.cli.UniversalReader")
    def test_json_error_is_machine_readable_and_nonzero(self, reader_cls, _inbox_cls):
        reader = reader_cls.return_value
        reader.read = AsyncMock(side_effect=ValueError("source unavailable"))

        output = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(output):
            cmd_fetch([self.url], json_output=True)

        self.assertEqual(raised.exception.code, 1)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "source unavailable")
        self.assertEqual(payload["error_type"], "ValueError")

    @patch("x_reader.cli.cmd_fetch")
    def test_json_flag_can_precede_url(self, fetch):
        with patch.object(sys, "argv", ["x-reader", "--json", self.url]):
            main()

        fetch.assert_called_once_with([self.url], json_output=True)


if __name__ == "__main__":
    unittest.main()
