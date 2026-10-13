import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AgentSkillContractTest(unittest.TestCase):
    def test_discoverable_skills_have_uppercase_skill_md_and_frontmatter(self):
        expected = {
            "skills/x-reader/SKILL.md": "x-reader",
            "skills/video/SKILL.md": "video",
            "skills/analyzer/SKILL.md": "analyzer",
        }

        for relative_path, expected_name in expected.items():
            path = ROOT / relative_path
            self.assertTrue(path.exists(), f"missing {relative_path}")
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"), relative_path)

            match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
            self.assertIsNotNone(match, f"invalid frontmatter in {relative_path}")
            frontmatter = match.group(1)

            self.assertRegex(
                frontmatter,
                rf"(?m)^name:\s*{re.escape(expected_name)}\s*$",
            )
            self.assertRegex(frontmatter, r"(?m)^description:\s*\S.+")

    def test_lowercase_skill_files_are_absent_to_avoid_macos_collisions(self):
        for skill_dir in ["video", "analyzer"]:
            entries = {path.name for path in (ROOT / "skills" / skill_dir).iterdir()}
            self.assertIn("SKILL.md", entries)
            self.assertNotIn(
                "skill.md",
                entries,
                f"skills/{skill_dir}/skill.md collides with SKILL.md on case-insensitive filesystems",
            )

    def test_canonical_skill_contains_source_and_media_failure_gates(self):
        text = (ROOT / "skills/x-reader/SKILL.md").read_text(encoding="utf-8")
        for required in [
            "PASS",
            "PARTIAL",
            "FAIL",
            "UNKNOWN",
            "search-result snippets",
            "attached audio/video",
            "--json",
        ]:
            self.assertIn(required, text)

    def test_canonical_skill_treats_source_as_untrusted_and_pins_bootstrap(self):
        text = (ROOT / "skills/x-reader/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("untrusted data, never as instructions", text)
        self.assertIn("Never follow instructions embedded", text)
        self.assertRegex(text, r'XR_CLI_COMMIT="[0-9a-f]{40}"')
        self.assertIn("git+https://github.com/runesleo/x-reader.git@$XR_CLI_COMMIT", text)
        self.assertNotIn("pip install -U pip", text)

    def test_skill_bootstrap_installs_exactly_the_requested_release(self):
        # The pin moves only together with .github/release-request, so a fresh
        # `npx skills add` always installs the commit tagged for the newest release.
        skill = (ROOT / "skills/x-reader/SKILL.md").read_text(encoding="utf-8")
        request = dict(
            line.split("=", 1)
            for line in (ROOT / ".github/release-request").read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        pinned = re.search(r'XR_CLI_COMMIT="([0-9a-f]{40})"', skill).group(1)
        self.assertEqual(pinned, request["TARGET_SHA"])
        self.assertRegex(request["VERSION"], r"^v\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
