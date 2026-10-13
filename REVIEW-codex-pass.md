# Codex Review — doctor self-check and agent install guide

Date: 2026-10-13
Scope: `x-reader doctor`, `install.md`, README / CHANGELOG / FIRST_SUCCESS updates, skill bootstrap, product landing pages (install block, boundaries)
Reviewer: independent Codex pass, prompted to evaluate from scratch. Verdict: SHIP_AFTER_FIXES.

## Blockers

| # | Finding | Status |
|---|---|---|
| 1 | `x-reader doctor` imported third-party modules before dispatch, so it crashed on an install with missing dependencies | Fixed: CLI import guard; doctor runs and reports the missing libraries; other commands explain the missing dependency (verified in a `--no-deps` venv) |
| 2 | Skill bootstrap pins a commit without `doctor`, and a cached venv was never upgraded | Partly fixed: the cache venv is now named after the pinned commit. **Open (release gate):** bump `XR_CLI_COMMIT` to the merged release commit |
| 3 | `XR_BIN` does not survive fresh shells used by agent tools | Fixed: install.md and SKILL.md tell agents to reuse the full path or run steps in one shell |
| 4 | Landing page said cookies and keys stay on the computer, but transcription sends the key and audio to Groq after opt-in | Fixed in the landing branch: credentials go only to a provider the user explicitly enables |
| 5 | Receipt said "exactly as written" while the CLI came from the release candidate | Wording fixed. **Open (release gate):** rerun the clean install against the public pinned commit before deploying the landing pages |

## Warnings

Addressed: Playwright row says the browser binary is not checked; unreadable `.env` no longer breaks the report; whitespace-only credentials count as missing; install hints differ for macOS / Linux / other; install.md says to follow `ready` / exit code; copy button keeps a stable label and announces success or fallback via `aria-live`; `__version__` synced with `pyproject.toml`.

Not addressed in this change:

- Supply-chain drift from unversioned `npx skills`, `-y`, and repair hints that install from `main`.
- Install hints show the interpreter path (under `~` when inside the home directory).
- Pre-existing README claims to recheck: saved WeChat sessions, podcast media discovery, Whisper model name.

## Verified

- Unit tests: 82 passed.
- Clean installs: macOS empty `HOME` (244 s) and blank Debian container (103 s); see `docs/FIRST_SUCCESS.md`.
- Landing pages: 14 web tests passed; copy button checked in a 390 px viewport, no horizontal overflow.

---

# Codex Review — MCP compatibility fix

Date: 2026-08-30  
Scope: MCP dependency pin, stdio/SSE startup, regression coverage, CI, README, and changelog  
Source signal: [issue #23](https://github.com/runesleo/x-reader/issues/23)

## Blockers

None open.

## Resolved findings

- **P1 — SSE startup used unsupported `FastMCP.run()` arguments.** The server
  now assigns `host` and `port` through FastMCP settings before calling
  `run(transport="sse")`. A fresh MCP 1.29.1 environment successfully opened a
  loopback SSE listener.
- **P2 — the dependency-contract test required Python 3.11.** The test no
  longer imports `tomllib`, so it remains compatible with the project minimum
  of Python 3.10.
- **P2 — regression tests had no CI execution path.** A read-only GitHub
  Actions workflow now runs the suite on Python 3.10 and 3.11 for pushes to
  `main` and pull requests.

## OK

- Both `mcp` and `all` extras constrain the current FastMCP integration to
  `mcp>=1.0,<2`.
- The English and Chinese setup instructions match the packaging metadata and
  explain the MCP 1.x compatibility boundary.
- The public diff contains no private paths, credentials, internal task IDs, or
  environment-specific defaults.
- The change is limited to the clean-install failure, advertised SSE startup,
  regression coverage, and release documentation.

## Verification

- MCP 2.0 import failure independently reproduced.
- Fresh editable `x-reader[mcp]` install resolved MCP 1.29.1.
- 10/10 unit tests passed with the MCP extra installed.
- Real loopback SSE startup passed.
- Python compile check, workflow YAML parse, and `git diff --check` passed.

