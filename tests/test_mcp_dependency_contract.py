import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class McpDependencyContractTest(unittest.TestCase):
    def test_every_mcp_extra_stays_on_the_fastmcp_compatible_major(self):
        config = (ROOT / "pyproject.toml").read_text()

        self.assertEqual(
            config.count('"mcp[cli]>=1.0,<2"'),
            2,
            "both the mcp and all extras must cap the FastMCP dependency",
        )
        self.assertNotIn('"mcp[cli]>=1.0"', config)

    @unittest.skipIf(importlib.util.find_spec("mcp") is None, "MCP extra not installed")
    def test_sse_uses_fastmcp_settings_instead_of_unsupported_run_kwargs(self):
        import mcp_server

        class FakeMcp:
            def __init__(self):
                self.settings = SimpleNamespace(host="127.0.0.1", port=8000)
                self.calls = []

            def run(self, **kwargs):
                self.calls.append(kwargs)

        fake = FakeMcp()
        with patch.object(mcp_server, "mcp", fake):
            mcp_server.run_server("sse", "127.0.0.2", 8123)

        self.assertEqual((fake.settings.host, fake.settings.port), ("127.0.0.2", 8123))
        self.assertEqual(fake.calls, [{"transport": "sse"}])


if __name__ == "__main__":
    unittest.main()
