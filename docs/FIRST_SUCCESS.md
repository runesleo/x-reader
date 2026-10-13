# x-reader first-success receipt

Dated verification: **2026-09-28**

This document records a real public-path verification of the canonical `x-reader` Agent Skill after PR #27 merged to `main`.

It is a dated canary, not a promise that third-party platforms will never change.

## Public install

Command:

```bash
npx skills add runesleo/x-reader --skill x-reader
```

Observed from a fresh project directory:

```text
Source: https://github.com/runesleo/x-reader.git
Repository cloned
Found 3 skills
Selected 1 skill: x-reader
Installation complete
Installed 1 skill
```

The installed skill contained the expected frontmatter:

```yaml
name: x-reader
description: Read source URLs from X/Twitter, articles, YouTube, Bilibili, podcasts, WeChat, Xiaohongshu, Telegram, RSS, and the open web.
```

## Mixed-media X benchmark

Public source:

```text
https://x.com/dontbesilent/status/2103875422522077377
```

### Post layer

`x-reader URL --json` returned the canonical status URL and post text through:

```json
{
  "source_type": "twitter",
  "source_name": "@dontbesilent",
  "extra": {
    "fetch_method": "oembed"
  }
}
```

Post text coverage: **PASS**

### Attached-media layer

Public media discovery with `yt-dlp` identified:

```text
extractor: twitter
duration: 178.259 seconds
container: mp4
native subtitles: zh
```

The MP4 was downloaded and independently measured by `ffprobe` at approximately **178.306 seconds**.

The native Chinese VTT track was downloaded and read from beginning to end.

Attached-video / spoken-content coverage: **PASS**

The important contract is component-level honesty: before the video/subtitle layer was consumed, the post could be read while the attached media remained `PARTIAL`.

## Negative benchmark

Nonexistent source tested:

```text
https://x.com/dontbesilent/status/1
```

Observed behavior:

- process exited with code `1`;
- stdout was machine-readable JSON;
- no search result or unrelated source was substituted as success.

Example shape:

```json
{
  "ok": false,
  "error": "...",
  "error_type": "RuntimeError"
}
```

Negative-source coverage: **FAIL**, as intended.

## Test gates

On the exact pre-merge feature head used for the final verification:

- GitHub Actions: PASS on the repository test workflow;
- real macOS checkout: **17/17 tests PASS**;
- current Skills CLI: canonical `x-reader` discovered;
- project-scoped Codex install: PASS;
- public owner/repo install from merged `main`: PASS.

## What this receipt proves

It proves the first-success path was real at the stated date:

1. a user can install the canonical Agent Skill from the public repo;
2. the source reader can retrieve the original X post;
3. attached media is treated as a separate evidence layer;
4. the benchmark video could be actually consumed via its native subtitle track;
5. a broken source fails closed instead of being silently replaced.

It does **not** claim every platform or every future URL will always be accessible. The product contract is to make incomplete coverage explicit.

## Clean-environment install via install.md (2026-10-13)

Goal: a user pastes one sentence into their agent and reaches a first real read within 15 minutes, on a machine without the author's paths, cookies, memory or saved sessions.

The skill came from public `main`; the CLI was installed from the release candidate that adds `x-reader doctor` (on public `main` the skill bootstrap is still pinned to an earlier verified commit).

| Environment | Starting point | Total time |
|---|---|---|
| macOS, empty temporary `HOME` | Python 3.14 and Node.js already installed | 244 s |
| Debian bookworm container (`node:24-bookworm-slim`) | no Python; user installs `python3`, `python3-venv`, `git` | 103 s |

Steps, following [install.md](../install.md) (skill from public `main` at `8a5bcda`; CLI from the release-candidate branch, as noted above):

```text
1 basics   python3 --version · git --version · npx --version
2 skill    npx skills add runesleo/x-reader --skill x-reader -g -y   (container: -a codex)
           → installed to ~/.agents/skills/x-reader
3 CLI      cache venv at ~/.cache/x-reader/venv
4 doctor   core OK; optional items MISSING with fix_by and next command; exit 0
5 read     x-reader <url> --json
```

First-success reads (both environments):

```text
https://x.com/dontbesilent/status/2103875422522077377
  source_type=twitter  fetch_method=oembed  media_status=present
  → post text READ; attached video PARTIAL until it is actually consumed

https://example.com
  fetch_method=direct_html_pinned  (Jina returned HTTP 401, safe fallback)
```

Found and fixed during the run: skill locations under a symlinked home (`/tmp` → `/private/tmp`) were listed twice; install hints now target the interpreter x-reader runs from.

### Public pinned path (2026-10-13, after merge)

The skill bootstrap now pins `8e94baa835d0f3d4f771924953b18873b862af96`. Running its bootstrap block unchanged installed `x-reader @ git+https://github.com/runesleo/x-reader.git@8e94baa…` into `~/.cache/x-reader/venv-8e94baa835d0`.

| Environment | Total time | doctor | X post | example.com |
|---|---|---|---|---|
| macOS, empty temporary `HOME` | 173 s | ready, exit 0 | `oembed`, `media_status=present` | `direct_html_pinned` |
| Debian bookworm container, after installing `python3`, `python3-venv`, `git` | 119 s | ready, exit 0 | `oembed`, `media_status=present` | `direct_html_pinned` |

### v0.4.0 pin (2026-10-13)

The skill bootstrap pins `20d4c2eded74d0c15844611e31265767f63d55af` (v0.4.0). Its bootstrap block, run unchanged on macOS with an empty `HOME`, installed `x-reader 0.4.0` from that public commit in 127 s; `doctor` ready (exit 0); X post `oembed` + `media_status=present`; example.com `direct_html_pinned`.
