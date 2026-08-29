import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class McpDependencyContractTest(unittest.TestCase):
    def test_every_mcp_extra_stays_on_the_fastmcp_compatible_major(self):
        config = tomllib.loads((ROOT / "pyproject.toml").read_text())
        extras = config["project"]["optional-dependencies"]

        self.assertIn("mcp[cli]>=1.0,<2", extras["mcp"])
        self.assertIn("mcp[cli]>=1.0,<2", extras["all"])
        self.assertFalse(
            any(dep == "mcp[cli]>=1.0" for deps in extras.values() for dep in deps),
            "an uncapped MCP extra would resolve to 2.x and crash mcp_server.py",
        )


if __name__ == "__main__":
    unittest.main()
