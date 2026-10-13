# Changelog

**Repository:** All notable changes are derived from `git log` and linked when a GitHub `origin` is configured.

This project: **x-reader**.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) for structure; the **History** section lists commits in reverse chronological order (newest first).

## [Unreleased]

## [0.5.0] - 2026-10-14

### Added

- Hosted Evidence Receipt web reader (`x-reader-web`, public beta at xreader.leolabs.me): public X, YouTube and generic web URLs, with a PASS / PARTIAL / FAIL / UNKNOWN receipt for each read. Deployment configuration is in `deploy/`.
- Podcast episodes (Xiaoyuzhou, Apple Podcasts): show notes by default, plus opt-in local ASR in the CLI: `--media-preview-seconds N` (≤30 s sample, always `PARTIAL`), `--media-full-short` (≤120 s / ≤2 MiB, verified full transcript) and `--media-full-long` (≤90 min / ≤64 MiB, resumable, verified chunks). Needs `x-reader[media]`, ffmpeg and ffprobe.
- X posts: the quoted post and poll options are appended to the post text when they were read; extra fields `structure_status`, `quote_status`, `poll_status`, `article_status` and `thread_marker` record what was checked.
- A generic URL that redirects into X, YouTube, a podcast or another supported platform is read by that platform's reader.
- Generic reads load the page directly over pinned hops first; the extra fields `redirects_verified` and `embedded_media` record whether that worked and how many players, frames and plugins the page embeds.
- `x-reader doctor` checks ffprobe and local transcription (faster-whisper).
- Bilibili, Xiaohongshu and browser-fallback fixes from the Evidence Receipt work.

### Changed

- **Podcast URLs now return `source_type=podcast` and `media_type=audio`** (before: read as generic pages, `manual` / `text`). Consumers that branch on `source_type` should handle `podcast`.
- URL routing is shared by the CLI and the hosted reader and matches hosts exactly: `dropbox.com` is no longer treated as X, a `/feedback` path is no longer a feed; fxtwitter / vxtwitter / fixupx links go to the X reader.
- Evidence receipts are stricter:
  - YouTube videos are never `PASS`, because pictures are not analyzed; the `spoken_media` component is `PASS` only when subtitle cues cover the whole video (no uncaptioned stretch over 30 s).
  - X posts are `PASS` only when media, quoted post, poll, X Article and thread layers were checked and are absent or read (a quoted post with its own layers is incomplete); text from a page reader (Jina, a page without the post element) is `UNKNOWN`.
  - Generic pages are `PASS` only when x-reader loaded the page itself, found no visible player, frame or plugin, and read 280+ characters of sentence-like prose without login, paywall, consent, teaser, missing-page or challenge markers; pages with embedded content and teasers are `PARTIAL`; thin text, pages that open like a missing-page notice and pages x-reader could not load are `UNKNOWN`.
  - RSS items count as full only with 280+ characters of visible prose, no "read more" or paywall text, a linked article page that passes on its own, and most of that page's text in the feed body (`feed_body_unverified` otherwise); WeChat removal/verification notices `FAIL`, image-carried posts are `PARTIAL`.
  - Full-audio proofs allow 0.5 s between declared and decoded duration (was 1 %), and `coverage_ratio` is the measured processed/declared ratio.
- Podcast previews report the seconds actually decoded (`preview_seconds`) next to `requested_preview_seconds`.
- Telegram message URLs (`t.me/<channel>/<id>`, `t.me/s/...`) read that exact message instead of the channel's latest post.
- FxTwitter posts without a `media` field count as having no media (before: unknown).
- RSS feeds served as `text/plain`, `application/octet-stream` or without a Content-Type are accepted when the body is a feed.
- Pinned connections try IPv4 addresses first and move on to the next checked address when a connection fails, within the request's time budget and at most three addresses (hosts with IPv6 addresses but no IPv6 route could not read generic pages).
- With `--json`, usage errors print `{"ok": false, "error_type": "UsageError", ...}` and exit 2; an unknown command exits 2.
- The `all` extra includes `media` (faster-whisper).
- Moat benchmark: every x-reader row is scored from its Evidence Receipt; new outcome `UNVERIFIED`; restricted controls that time out stay out of the denominator; benchmark runs cannot write to an Obsidian vault configured in `.env`.

### Security

- yt-dlp only receives a rebuilt `https://www.youtube.com/watch?v=<id>` URL with its generic extractor disabled, so consent or redirect URLs cannot send it to other hosts.
- RSS feeds are downloaded with the pinned, per-hop validated fetcher and parsed from bytes, so feed reads can no longer be redirected to private addresses.
- External tools run with a deadline and an output budget enforced while they run; on exit, timeout or overflow their whole process group is killed.
- Hosted reads: one shared router plus a child-side platform allowlist that also covers redirects; generic text comes only from the service's own pinned fetch (no Jina Reader); the child skips `.env`, writes no inbox or Markdown, inherits no proxies, starts no external tools (no yt-dlp, ffmpeg or browser), and runs under address-space / CPU / file-size / open-file ceilings; CLI output is size-capped.
- Hosted server: absolute 20 s request-read deadline watched by one thread, connection cap that survives thread-start failures, JSON-only POSTs, JSON for every error including unparseable request targets, a fixed set of public reason codes, control characters refused in URLs, no interpreter version in headers, stricter CSP.
- The manual preview workflow no longer opens a public tunnel; it runs the live canary against the server on the CI runner.
- yt-dlp calls skip playlists, cap audio downloads at 25 MB and bound subtitle / metadata files; short-audio decoding stops at the PCM budget and long-audio decoding has an idle watchdog.

## [0.4.0] - 2026-10-13

### Added

- `x-reader doctor [--json]`: read-only self-check that lists core and optional capabilities as `OK` / `MISSING` / `BLOCKED` / `INFO`, says who fixes each item (agent or user) and the next command, never prints secret values, and exits non-zero only when a core item is not ready. Also runnable as `python3 -m x_reader.doctor`.
- `install.md`: step-by-step setup written for AI agents, so a user can paste one sentence into Claude Code, Codex or Cursor and have the agent install, self-check and complete a first read.
- README (EN / 简体中文): one-step "paste this into your agent" setup, plus links to the product site and the hosted web reader.

### Changed

- The Agent Skill bootstrap pins a verified commit that includes `doctor`, and names its cache venv after the pinned commit, so a new pin installs a fresh environment instead of reusing an older CLI.
- `doctor` repair hints reinstall the running release tag instead of moving `main`.

### Fixed

- `x-reader doctor` runs on an install with missing dependencies; other commands explain what is missing and exit non-zero.
- README: WeChat uses a headless browser fallback for public articles (saved login sessions are not used yet); Xiaoyuzhou / Apple Podcasts links are read as generic web pages; the transcription model is `whisper-large-v3`.
- `x_reader.__version__` matches the package version.

## [0.3.1] - 2026-09-29

### Added

- First-success smoke coverage for generic public web and mixed-media X, plus a reproducible broken-URL issue template and benchmark notes.

### Changed

- Preserve structured X/Twitter media evidence through `media_status=present|none|unknown`, so post text can be `READ` while attached media remains `PARTIAL`.
- Offload blocking generic fetch work from the async event loop and keep machine-readable source receipts explicit about the fetch path used.

### Security

- Pin generic Jina/direct connections to validated public IPs while preserving original Host/SNI/certificate hostname verification.
- Revalidate redirect targets, fail closed on private/special/ambiguous destinations, and bound DNS workers, response sizes, fetch deadlines, and resource cleanup.
- Support TUN fake-IP DNS safely by resolving synthetic `198.18.0.0/15` answers through pinned trusted DoH instead of trusting the synthetic destination.
- Reject ambiguous legacy numeric host forms and tighten IDNA/public-unicast validation.

### Fixed

- Recover generic public pages through a safe direct HTML fallback when Jina returns an error such as HTTP 401.
- Prevent login/error/challenge shells and unknown attached-media states from being overstated as fully read source content.

## [0.3.0] - 2026-09-28

### Added

- Canonical `x-reader` Agent Skill with explicit `PASS / PARTIAL / FAIL / UNKNOWN` source-coverage receipts.
- Complete machine-readable CLI output through `x-reader <url> --json`.
- Dated first-success verification covering public Agent Skill installation, original X post retrieval, attached-video subtitle evidence, and fail-closed broken URLs.

### Changed

- Make `x-reader` the recommended agent-facing entry point while preserving the narrower `video` and `analyzer` skills.
- Use uppercase `SKILL.md` consistently so skill discovery works on case-insensitive macOS filesystems.
- Rework English and Chinese README surfaces around the source-first contract and correct Claude Code MCP setup.

### Security

- Treat all fetched web/social/media content as untrusted data and explicitly forbid following instructions embedded in source content.
- Pin the Agent Skill's automatic CLI bootstrap to the verified immutable first-success commit instead of a moving GitHub branch.
- Keep local cookies and credentials outside source content by default.

### Fixed

- Keep the optional MCP dependency on the FastMCP-compatible 1.x line so fresh `x-reader[mcp]` installs do not fail before the protocol handshake; configure SSE through FastMCP settings and run the regression suite on Python 3.10/3.11.

## History (full git log)

- 2026-04-26 — chore: add root config files ([`1e9473d`](https://github.com/runesleo/x-reader/commit/1e9473d54c2d0e0be9f631435d987fc4e24985bc))
- 2026-04-26 — docs: add examples ([`bda2da3`](https://github.com/runesleo/x-reader/commit/bda2da3dc2f1ef197fe658648e2e2d63bafc99fc))
- 2026-04-26 — chore: add SECURITY.md and CODE_OF_CONDUCT.md ([`60c551d`](https://github.com/runesleo/x-reader/commit/60c551d46be757eec15a93698a1792fbf8dd0d17))
- 2026-04-26 — docs: add share card and wechat dom probe ([`90de17c`](https://github.com/runesleo/x-reader/commit/90de17c342eee2fbbf4358507d4265909cb1df82))
- 2026-04-22 — docs(readme): sync Author section to unified SOP ([`f7ac0fe`](https://github.com/runesleo/x-reader/commit/f7ac0fe08ad9104ddbd12f907fce9b702f17900f))
- 2026-03-25 — fix: fallback to bundled Chromium when Chrome is unavailable (#13) ([`dd624e5`](https://github.com/runesleo/x-reader/commit/dd624e54035681d07de6eef80092afdab14c858e))
- 2026-03-04 — docs: add Star History chart to README ([`fdd582c`](https://github.com/runesleo/x-reader/commit/fdd582ccd39c70375d0dba176faeea2e59159ba9))
- 2026-02-27 — 安全修复: SSRF防护、XXE防护、路径遍历防护及MCP服务器加固 (#6) ([`d3760a5`](https://github.com/runesleo/x-reader/commit/d3760a55d7422acd8f1a54843331a5a4adfcbcfd))
- 2026-02-26 — fix: add FxTwitter API as Tier 1 to fix long tweet truncation (#11) ([`8619afc`](https://github.com/runesleo/x-reader/commit/8619afce8ffb6996c99443f3a1f4c311d668cd66))
- 2026-02-25 — chore(github): add automated maintenance workflows ([`d1bc6fc`](https://github.com/runesleo/x-reader/commit/d1bc6fc53bc2123fd6882ff2209cddd216f1a233))
- 2026-02-25 — fix(wechat): bypass anti-hotlinking and improve DOM extraction ([`d82d222`](https://github.com/runesleo/x-reader/commit/d82d222996bc7bb2ec4158e5ee021d17fc4137a8))
- 2026-02-24 — fix(twitter): treat t.co-only oEmbed responses as thin and fallback (#5) ([`084387b`](https://github.com/runesleo/x-reader/commit/084387baf0dca932df31ffdc1af0bf8ea4c585c7))
- 2026-02-24 — fix(twitter): use system Chrome channel for login and Playwright fetch (#4) ([`47e3ed9`](https://github.com/runesleo/x-reader/commit/47e3ed9969a34eed9a93f67cfbdbccd6ac6d9f35))
- 2026-02-24 — security: harden SSRF, MCP binding, and TG session handling ([`733c913`](https://github.com/runesleo/x-reader/commit/733c9136a48ec2f1616e66f129435f5e2e3dd687))
- 2026-02-23 — feat: XHS extraction improvements — dedicated selectors, session guards, headless login ([`36c1ae5`](https://github.com/runesleo/x-reader/commit/36c1ae5092c20eb3edf1ce6feec79e79a730c593))
- 2026-02-23 — docs: add Author section with leolabs.me link ([`a6d9ad4`](https://github.com/runesleo/x-reader/commit/a6d9ad439f580408a4615a9db898b27421b0d3c7))
- 2026-02-23 — feat: close tweet-claim gaps — Twitter 3-tier, YouTube Whisper, Obsidian path ([`573e545`](https://github.com/runesleo/x-reader/commit/573e5458851f661d96328b27a33f469f74d99129))
- 2026-02-23 — feat: add Playwright browser fallback for XHS and WeChat ([`1da8ab4`](https://github.com/runesleo/x-reader/commit/1da8ab431d99002b7fd767e250a8c9c3352e43bf))
- 2026-02-23 — fix: address Codex review — Telegram dedupe, CLI bare domains, README accuracy ([`85807cd`](https://github.com/runesleo/x-reader/commit/85807cd07403a9b092472cf348ce0eeb5dc3f5ef))
- 2026-02-22 — fix: pyproject.toml TOML table ordering — dependencies was under [project.urls] ([`0ad756b`](https://github.com/runesleo/x-reader/commit/0ad756b01c06916950e3a8d7fe44d956be3535a6))
- 2026-02-22 — feat: add MCP server, Claude Code skills, YouTube transcripts, and polish for v0.1.0 ([`27cde68`](https://github.com/runesleo/x-reader/commit/27cde68096387d8fab1207aeb815d63467d486a1))
- 2026-02-22 — fix: generic fallback SourceType enum + inbox JSON error handling ([`3c817a2`](https://github.com/runesleo/x-reader/commit/3c817a220e1021ee2e4196501468ebc2fedd4bbd))
- 2026-02-22 — feat: x-reader v0.1.0 — universal content reader for 7+ platforms ([`8d569a9`](https://github.com/runesleo/x-reader/commit/8d569a935d4b8cb1a5a544205db2f0a9cefe2432))
