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

    def test_packaged_console_entrypoint_is_declared(self):
        config = (ROOT / "pyproject.toml").read_text()
        self.assertIn('x-reader-mcp = "x_reader.mcp_server:main"', config)

    @unittest.skipIf(importlib.util.find_spec("mcp") is None, "MCP extra not installed")
    def test_source_checkout_shim_reexports_packaged_server(self):
        import mcp_server
        import x_reader.mcp_server as packaged

        self.assertIs(mcp_server.mcp, packaged.mcp)
        self.assertIs(mcp_server.run_server, packaged.run_server)
        self.assertIs(mcp_server.main, packaged.main)

    @unittest.skipIf(importlib.util.find_spec("mcp") is None, "MCP extra not installed")
    def test_sse_uses_fastmcp_settings_instead_of_unsupported_run_kwargs(self):
        import x_reader.mcp_server as mcp_server

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

    @unittest.skipIf(importlib.util.find_spec("mcp") is None, "MCP extra not installed")
    def test_main_keeps_external_bind_fail_closed(self):
        import x_reader.mcp_server as mcp_server

        with patch.object(mcp_server, "run_server") as run:
            self.assertEqual(
                mcp_server.main(["--transport", "sse", "--host", "0.0.0.0", "--port", "8123"]),
                0,
            )

        run.assert_called_once_with("sse", "127.0.0.1", 8123)


if __name__ == "__main__":
    unittest.main()
