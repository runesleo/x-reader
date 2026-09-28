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
