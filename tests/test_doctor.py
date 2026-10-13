import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from x_reader.doctor import build_report, collect_checks, format_report, run_doctor


def _spec_for(missing=()):
    def find_spec(name):
        return None if name in missing else object()
    return find_spec


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.cwd = Path(self.tmp.name) / "project"
        self.home.mkdir()
        self.cwd.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def checks(self, **overrides):
        kwargs = dict(
            env={},
            which=lambda name: None,
            find_spec=_spec_for(),
            version_info=(3, 11, 9),
            platform="linux",
            home=self.home,
            cwd=self.cwd,
            installed_version=lambda: "0.3.1",
        )
        kwargs.update(overrides)
        return {c.id: c for c in collect_checks(**kwargs)}

    def test_core_ready_even_when_all_optional_tools_are_missing(self):
        checks = self.checks(find_spec=_spec_for(missing=("playwright", "telethon", "mcp")))
        self.assertEqual(checks["python"].status, "OK")
        self.assertEqual(checks["core-libraries"].status, "OK")
        for optional in ("yt-dlp", "ffmpeg", "groq", "browser", "telegram", "mcp"):
            self.assertEqual(checks[optional].status, "MISSING", optional)
        self.assertTrue(build_report(list(checks.values()))["ready"])

    def test_html5lib_is_a_core_library(self):
        checks = self.checks(find_spec=_spec_for(missing=("html5lib",)))
        self.assertEqual(checks["core-libraries"].status, "MISSING")
        self.assertIn("html5lib", checks["core-libraries"].detail)
        self.assertFalse(build_report(list(checks.values()))["ready"])

    def test_missing_core_library_is_not_ready_and_names_the_package(self):
        checks = self.checks(find_spec=_spec_for(missing=("dotenv",)))
        self.assertEqual(checks["core-libraries"].status, "MISSING")
        self.assertIn("python-dotenv", checks["core-libraries"].detail)
        self.assertEqual(checks["core-libraries"].fix_by, "agent")
        self.assertFalse(build_report(list(checks.values()))["ready"])

    def test_old_python_is_blocked_for_the_user(self):
        checks = self.checks(version_info=(3, 9, 18))
        self.assertEqual(checks["python"].status, "BLOCKED")
        self.assertEqual(checks["python"].fix_by, "user")
        self.assertFalse(build_report(list(checks.values()))["ready"])

    def test_secret_values_are_never_printed(self):
        secret = "gsk_super_secret_value"
        checks = list(self.checks(env={"GROQ_API_KEY": secret, "TG_API_ID": "123", "TG_API_HASH": "hash-secret"}).values())
        rendered = format_report(checks) + json.dumps(build_report(checks))
        self.assertNotIn(secret, rendered)
        self.assertNotIn("hash-secret", rendered)
        self.assertIn("value not shown", rendered)

    def test_repair_hints_pin_the_running_release(self):
        checks = self.checks(find_spec=_spec_for(missing=("requests", "mcp")))
        self.assertIn("x-reader.git@v0.3.1", checks["core-libraries"].next)
        self.assertIn("x-reader.git@v0.3.1", checks["mcp"].next)
        unknown = self.checks(find_spec=_spec_for(missing=("requests",)), installed_version=lambda: None)
        self.assertNotIn("@v", unknown["core-libraries"].next)

    def test_missing_groq_key_is_a_user_step(self):
        checks = self.checks()
        self.assertEqual(checks["groq"].fix_by, "user")
        self.assertIn("Never paste the key into chat", checks["groq"].next)

    def test_platform_specific_install_hint(self):
        self.assertEqual(self.checks(platform="darwin")["yt-dlp"].next, "brew install yt-dlp")
        self.assertIn("pipx install yt-dlp", self.checks(platform="linux")["yt-dlp"].next)
        self.assertIn("ask the user before using sudo", self.checks(platform="linux")["ffmpeg"].next)
        self.assertIn("ffmpeg.org", self.checks(platform="win32")["ffmpeg"].next)

    def test_whitespace_only_credentials_count_as_missing(self):
        checks = self.checks(env={"GROQ_API_KEY": "   ", "TG_API_ID": " ", "TG_API_HASH": "x"})
        self.assertEqual(checks["groq"].status, "MISSING")
        self.assertEqual(checks["telegram"].status, "MISSING")

    def test_unreadable_env_file_does_not_break_the_report(self):
        output = io.StringIO()
        with patch("dotenv.load_dotenv", side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "bad")):
            code = run_doctor(json_output=True, stream=output)
        self.assertIn(code, (0, 1))
        json.loads(output.getvalue())

    def test_telegram_lists_every_missing_step_once(self):
        both = self.checks(find_spec=_spec_for(missing=("telethon",)))["telegram"]
        self.assertEqual(both.fix_by, "agent, user")
        self.assertIn("; then ", both.next)
        only_package = self.checks(
            find_spec=_spec_for(missing=("telethon",)), env={"TG_API_ID": "1", "TG_API_HASH": "2"}
        )["telegram"]
        self.assertEqual(only_package.fix_by, "agent")
        self.assertFalse(only_package.next.endswith("; then "))

    def test_skill_found_in_universal_agents_dir_and_shown_with_tilde(self):
        skill = self.home / ".agents" / "skills" / "x-reader"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: x-reader\n---\n")
        check = self.checks()["skill"]
        self.assertEqual(check.status, "OK")
        self.assertIn("~/.agents/skills/x-reader/SKILL.md", check.detail)
        self.assertNotIn(str(self.home), check.detail)

    def test_skill_in_home_is_listed_once_when_cwd_is_home(self):
        skill = self.home / ".agents" / "skills" / "x-reader"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: x-reader\n---\n")
        check = self.checks(cwd=self.home.resolve())["skill"]
        self.assertEqual(check.detail, "found: ~/.agents/skills/x-reader/SKILL.md")

    def test_skill_missing_is_info_not_failure(self):
        checks = self.checks()
        self.assertEqual(checks["skill"].status, "INFO")
        self.assertTrue(build_report(list(checks.values()))["ready"])

    def test_json_output_is_strict_json_and_exit_code_tracks_core(self):
        output = io.StringIO()
        code = run_doctor(json_output=True, stream=output)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0 if payload["ready"] else 1)
        self.assertIn("checks", payload)
        self.assertTrue(all({"id", "status", "fix_by", "next"} <= set(c) for c in payload["checks"]))

    @patch("x_reader.doctor.run_doctor", return_value=0)
    def test_cli_dispatches_doctor_with_json_flag(self, run):
        from x_reader.cli import main

        with patch.object(sys, "argv", ["x-reader", "doctor", "--json"]):
            with self.assertRaises(SystemExit) as raised, redirect_stdout(io.StringIO()):
                main()
        self.assertEqual(raised.exception.code, 0)
        run.assert_called_once_with(json_output=True)


class CliImportGuardTest(unittest.TestCase):
    def test_other_commands_explain_a_missing_dependency(self):
        from x_reader import cli

        output = io.StringIO()
        with patch.object(cli, "_IMPORT_ERROR", ModuleNotFoundError("No module named 'loguru'")), \
                patch.object(sys, "argv", ["x-reader", "https://example.com", "--json"]):
            with self.assertRaises(SystemExit) as raised, redirect_stdout(output):
                cli.main()
        self.assertEqual(raised.exception.code, 1)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("x-reader doctor", payload["error"])

    @patch("x_reader.doctor.run_doctor", return_value=1)
    def test_doctor_still_runs_when_a_dependency_is_missing(self, run):
        from x_reader import cli

        with patch.object(cli, "_IMPORT_ERROR", ModuleNotFoundError("No module named 'loguru'")), \
                patch.object(sys, "argv", ["x-reader", "doctor"]):
            with self.assertRaises(SystemExit) as raised:
                cli.main()
        self.assertEqual(raised.exception.code, 1)
        run.assert_called_once_with(json_output=False)


if __name__ == "__main__":
    unittest.main()
