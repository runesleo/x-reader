# -*- coding: utf-8 -*-
"""
x-reader doctor — read-only environment self-check.

Usage:
    x-reader doctor                # human-readable report
    x-reader doctor --json         # machine-readable report for agents
    python3 -m x_reader.doctor     # same check, works even if a core library is missing

Shows what is ready, what is missing, and who has to fix it (agent or user).
Uses only the Python standard library, changes nothing, and makes no network
requests. Secret values are never printed; only whether they are set.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

MIN_PYTHON = (3, 10)
REPO_SPEC = "git+https://github.com/runesleo/x-reader.git"

CORE_LIBRARIES = (
    ("requests", "requests"),
    ("feedparser", "feedparser"),
    ("dotenv", "python-dotenv"),
    ("loguru", "loguru"),
    ("idna", "idna"),
)

# Where `npx skills add runesleo/x-reader --skill x-reader` puts the skill.
SKILL_DIRS = (
    ".agents/skills/x-reader",
    ".claude/skills/x-reader",
    ".codex/skills/x-reader",
    ".cursor/skills/x-reader",
)


@dataclass
class Check:
    id: str
    group: str  # core | optional | skill
    status: str  # OK | MISSING | BLOCKED | INFO
    label: str
    detail: str = ""
    fix_by: str = ""  # agent | user | ""
    next: str = ""


def _tilde(path: Path, home: Path) -> str:
    """Show paths under the home directory as ~/..., also through symlinks such as /tmp -> /private/tmp."""
    for candidate, base in ((path, home), (path.resolve(), home.resolve())):
        try:
            return "~/" + str(candidate.relative_to(base))
        except ValueError:
            continue
    return str(path)


def _python(home: Path) -> str:
    """The interpreter x-reader runs from, so install hints target the same environment."""
    exe = Path(sys.executable or "python3")
    shown = _tilde(exe, home)
    return f'"{exe}"' if " " in shown else shown


def _pip(home: Path, extra: str = "") -> str:
    target = f"x-reader[{extra}] @ {REPO_SPEC}" if extra else f"x-reader @ {REPO_SPEC}"
    return f'{_python(home)} -m pip install --upgrade "{target}"'


def _installed_version() -> Optional[str]:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python < 3.8
        return None
    try:
        return version("x-reader")
    except PackageNotFoundError:
        return None


def collect_checks(
    env: Optional[Mapping[str, str]] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    find_spec: Callable = importlib.util.find_spec,
    version_info: tuple = tuple(sys.version_info[:3]),
    platform: str = sys.platform,
    home: Optional[Path] = None,
    cwd: Optional[Path] = None,
    installed_version: Callable[[], Optional[str]] = _installed_version,
) -> list[Check]:
    environ: Mapping[str, str] = os.environ if env is None else env

    def is_set(name: str) -> bool:
        return bool(str(environ.get(name, "")).strip())
    home = Path.home() if home is None else home
    cwd = Path.cwd() if cwd is None else cwd
    checks: list[Check] = []

    def has_module(name: str) -> bool:
        try:
            return find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    # ── Core ─────────────────────────────────────────────
    py = ".".join(str(part) for part in version_info[:3])
    if tuple(version_info[:2]) >= MIN_PYTHON:
        checks.append(Check("python", "core", "OK", f"Python {py}"))
    else:
        checks.append(Check(
            "python", "core", "BLOCKED", f"Python {py}",
            detail="x-reader needs Python 3.10 or newer.",
            fix_by="user",
            next="Install Python 3.10+ from python.org or your package manager, then reinstall x-reader.",
        ))

    version = installed_version()
    if version:
        checks.append(Check("package", "core", "OK", f"x-reader {version}"))
    else:
        checks.append(Check(
            "package", "core", "INFO", "x-reader package metadata not found",
            detail="Running from a source checkout is fine; otherwise reinstall.",
            fix_by="agent", next=_pip(home),
        ))

    missing = [dist for module, dist in CORE_LIBRARIES if not has_module(module)]
    if missing:
        checks.append(Check(
            "core-libraries", "core", "MISSING", "Core libraries",
            detail="Missing: " + ", ".join(missing),
            fix_by="agent", next=_pip(home),
        ))
    else:
        checks.append(Check(
            "core-libraries", "core", "OK", "Core libraries",
            detail=", ".join(dist for _, dist in CORE_LIBRARIES),
        ))

    # ── Optional: only for the sources you need ──────────
    mac, linux = platform == "darwin", platform.startswith("linux")
    if which("yt-dlp"):
        checks.append(Check("yt-dlp", "optional", "OK", "yt-dlp", detail="YouTube / Bilibili subtitles and media"))
    else:
        checks.append(Check(
            "yt-dlp", "optional", "MISSING", "yt-dlp",
            detail="Needed for YouTube / Bilibili subtitles and media.",
            fix_by="agent",
            next="brew install yt-dlp" if mac
            else "pipx install yt-dlp  (or the distro's package manager)" if linux
            else "see https://github.com/yt-dlp/yt-dlp#installation",
        ))

    if which("ffmpeg"):
        checks.append(Check("ffmpeg", "optional", "OK", "ffmpeg", detail="Audio extraction for transcription"))
    else:
        checks.append(Check(
            "ffmpeg", "optional", "MISSING", "ffmpeg",
            detail="Needed to extract audio when a video has no subtitles.",
            fix_by="agent",
            next="brew install ffmpeg" if mac
            else "sudo apt install ffmpeg  (or the distro's package manager; ask the user before using sudo)" if linux
            else "see https://ffmpeg.org/download.html",
        ))

    if is_set("GROQ_API_KEY"):
        checks.append(Check("groq", "optional", "OK", "GROQ_API_KEY", detail="set (value not shown)"))
    else:
        checks.append(Check(
            "groq", "optional", "MISSING", "GROQ_API_KEY",
            detail="Only needed to transcribe videos that have no subtitles.",
            fix_by="user",
            next="Create a key at https://console.groq.com, then set GROQ_API_KEY in your shell or .env. Never paste the key into chat.",
        ))

    if has_module("playwright"):
        sessions = home / ".x-reader" / "sessions"
        saved = sorted(p.stem for p in sessions.glob("*.json")) if sessions.is_dir() else []
        checks.append(Check(
            "browser", "optional", "OK", "Browser fallback (Playwright)",
            detail="package found (browser binary not checked) · saved logins: " + (", ".join(saved) if saved else "none"),
        ))
    else:
        checks.append(Check(
            "browser", "optional", "MISSING", "Browser fallback (Playwright)",
            detail="For pages behind a login (WeChat, Xiaohongshu, some X posts). The user logs in once with `x-reader login <platform>`.",
            fix_by="agent", next=_pip(home, "browser") + f" && {_python(home)} -m playwright install chromium",
        ))

    tg_ready = has_module("telethon")
    tg_creds = is_set("TG_API_ID") and is_set("TG_API_HASH")
    if tg_ready and tg_creds:
        checks.append(Check("telegram", "optional", "OK", "Telegram", detail="telethon installed, credentials set (values not shown)"))
    else:
        gaps, steps, owners = [], [], []
        if not tg_ready:
            gaps.append("telethon not installed")
            steps.append(_pip(home, "telegram"))
            owners.append("agent")
        if not tg_creds:
            gaps.append("TG_API_ID / TG_API_HASH not set")
            steps.append("the user creates API credentials at https://my.telegram.org and sets TG_API_ID / TG_API_HASH in .env")
            owners.append("user")
        checks.append(Check(
            "telegram", "optional", "MISSING", "Telegram",
            detail="; ".join(gaps) + ".",
            fix_by=", ".join(owners),
            next="; then ".join(steps),
        ))

    if has_module("mcp"):
        checks.append(Check("mcp", "optional", "OK", "MCP server support"))
    else:
        checks.append(Check(
            "mcp", "optional", "MISSING", "MCP server support",
            detail="Only needed to run x-reader as an MCP server.",
            fix_by="agent", next=_pip(home, "mcp"),
        ))

    # ── Agent skill ──────────────────────────────────────
    found = []
    for base in (home, cwd):
        for rel in SKILL_DIRS:
            skill = base / rel / "SKILL.md"
            if skill.is_file():
                label = _tilde(skill, home)
                if label not in found:
                    found.append(label)
    if found:
        checks.append(Check("skill", "skill", "OK", "Agent skill", detail="found: " + ", ".join(found)))
    else:
        checks.append(Check(
            "skill", "skill", "INFO", "Agent skill not found",
            detail="Fine if you use the CLI or MCP directly.",
            fix_by="agent", next="npx skills add runesleo/x-reader --skill x-reader -g -y",
        ))

    return checks


def build_report(checks: list[Check]) -> dict:
    ready = not any(c.group == "core" and c.status in ("MISSING", "BLOCKED") for c in checks)
    return {
        "ok": ready,
        "ready": ready,
        "checks": [asdict(c) for c in checks],
        "next": 'x-reader "<url>" --json' if ready else "fix the core items above, then run x-reader doctor again",
    }


def format_report(checks: list[Check]) -> str:
    report = build_report(checks)
    lines = ["x-reader doctor — read-only, changes nothing", ""]
    sections = (
        ("core", "Core"),
        ("optional", "Optional (only for the sources you need)"),
        ("skill", "Agent skill"),
    )
    for group, title in sections:
        rows = [c for c in checks if c.group == group]
        if not rows:
            continue
        lines.append(title)
        for c in rows:
            line = f"  {c.status:<8} {c.label}"
            if c.detail:
                line += f" — {c.detail}"
            lines.append(line)
            if c.next and c.status != "OK":
                lines.append(f"           fix ({c.fix_by or 'agent'}): {c.next}")
        lines.append("")
    if report["ready"]:
        lines.append('Ready. Try: x-reader "https://example.com" --json')
    else:
        lines.append("Not ready: fix the core items above, then run `x-reader doctor` again.")
    return "\n".join(lines)


def run_doctor(json_output: bool = False, stream=None) -> int:
    """Print the report and return the exit code (0 = core ready)."""
    try:  # Match the CLI, which reads .env; optional so this stays stdlib-only.
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:  # a missing library or an unreadable .env must not break the report
        pass
    stream = sys.stdout if stream is None else stream
    checks = collect_checks()
    report = build_report(checks)
    if json_output:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        stream.write(format_report(checks) + "\n")
    return 0 if report["ready"] else 1


def main() -> None:
    sys.exit(run_doctor(json_output="--json" in sys.argv[1:]))


if __name__ == "__main__":
    main()
