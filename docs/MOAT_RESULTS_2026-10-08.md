# x-reader Moat Test — 2026-10-08

## Status and honest scope

This is a **first-party baseline plus targeted repairs**, not a claim that x-reader beats native ChatGPT, Exa, Firecrawl, or Jina across the board. Those provider comparisons are not complete under identical execution environments. The results below are tied to the benchmark host, its optional dependencies, and the test URLs as of 2026-10-08.

The reference corpus contains 50 URLs (10 web, 10 public X, 5 each WeChat/Xiaohongshu/Telegram/Bilibili/podcast/auth-gated). The local benchmark runner uses text, media and auth-aware outcomes: `READ`, `PARTIAL_MEDIA`, `MEDIA_COMPLETE`, `FAIL`, `THIN`, `AUTH_SHELL`, `AUTH_UNVERIFIED`, `EXPECTED_BLOCK`, and `SKIP`. Two Xiaohongshu links known externally to be restricted are safety controls and excluded from the read-rate denominator. The five auth-gated controls are not scored as ordinary source successes.

## First full run — base install, before repairs

- 50 test cases; strict completeness **14/50 (28%)**; usable text **25/50 (50%)**; median latency ~3.04 seconds.
- Ordinary web: 10/10 page text.
- Public X: 10/10 post text, of which six included unread media; full-read 4/10.
- Podcasts: 5/5 episode/show-note text, 0/5 verified spoken-media completion.
- WeChat, Xiaohongshu: 0/5 each because base environment lacked Playwright.
- Telegram: 0/5 because base environment lacked Telethon.
- Bilibili: 0/5, web API returned HTTP 412.
- Authenticated/gated: 0/5 independently verified private-content reads. Two failed closed; others returned login/marketing shells without usable private evidence.

**Do not compare those original percentage denominators mechanically with later runs:** optional dependencies and two Xiaohongshu control classifications changed.

## Full dependency rerun — still before reader repairs

In a separate benchmark-only virtual environment with Playwright/Chromium/Telethon installed:
- WeChat: **4/5** substantive article reads, ~16–28 seconds each; one deleted article remained thin.
- Xiaohongshu: **0/5**; even with a pre-existing local XHS session, the browser sometimes redirected to `/404?error_code=300031`. Before repair, the reader could return a successful object with zero content. This was a correctness bug.
- Telegram: **0/5** because TG_API_ID/TG_API_HASH were absent from the benchmark environment, even after Telethon installation. Not evidence of an algorithmic fetch failure.

## Repairs and calibrated production-logic canaries

Changes are confined to an isolated development worktree/product branch; **no production deploy or main-branch merge**.

1. **Xiaohongshu fail-closed:** reject empty title/body shells, known platform security/unavailable pages, and a final `/404` URL, preserving actual share parameters. Unit tests cover Jina and browser layers. Two live previously restricted URLs now return explicit `FAIL`, not empty PASS.
2. **Bilibili HTTP 412 browser fallback:** on public API HTTP 412, use the existing URL-validated Playwright browser path, wait through navigation races with bounded extraction retries, require substantive video-page markers, then normalize metadata. The output sets `media_status=present`, `has_transcript=false`, and `fetch_method=bilibili_browser_fallback`. The canonical Evidence Receipt remains **PARTIAL**, not PASS.
3. **Corpus/metrics calibration:** Xiaohongshu now includes three externally indexed public share candidates with their original xsec_token query and two known restricted controls; source-class capability rates exclude blocked and auth-only controls. The platform's local session may still fail on externally readable URLs.

Real-network reruns on the benchmark environment:
- Bilibili: **4/5 PARTIAL_MEDIA**, one FAIL; usable page text 80%, **video-media completion 0/5**. Median ~22.9 seconds. Two independently verified accessible video pages returned 3,177 and 3,619 characters through the fallback in a focused canary. This is source *metadata/page text*, not video transcript.
- Xiaohongshu: **0/3 eligible public candidate reads**, **2/2 expected restricted controls rejected**; the platform access/session problem is still unresolved. The reader now reports it correctly.
- Automated repository suite: **118 tests OK**, including new platform and benchmark integrity regressions at the latest recorded test execution.

## Strategic implication

**Keep** the universal router, content schema, X post ingestion, authenticated-source boundary, source-aware fail-closed behavior, and mixed-media coverage contract.

**Prioritize next:**
1. Bilibili media completion (subtitles/transcript) and browser latency: metadata recovered but video's content remains unread;
2. Xiaohongshu session freshness and public-share access research; do not fake coverage and do not bypass access controls;
3. WeChat browser packaging/performance while preserving the 4/5 substantive-read result;
4. Telegram credentials/session as an explicit opt-in authenticated connector, not a keyless default reader;
5. cross-provider apples-to-apples evaluation only when comparable authenticated provider modes are available.

**Deprioritize** additional generic-page scraping tricks and paid consumer subscription for simple URL reading until an actual retained use case is validated.

## Reproduction

- Dataset and runner: `benchmarks/moat/corpus.jsonl`, `benchmarks/moat/run.py`
- Running the benchmark requires the intended optional dependencies; never install real user cookies into public CI.
- Repro examples:
  `PYTHONPATH=. python benchmarks/moat/run.py --providers x_reader --category bilibili --timeout 70 --workers 3`
  `PYTHONPATH=. python benchmarks/moat/run.py --providers x_reader --category xiaohongshu --timeout 60 --workers 3`
- Initial and post-fix raw JSONL results remain **private inside benchmark artifacts**, not included in source-control promotion.

## Follow-on podcast media-contract milestone — later 2026-10-08

A new isolated branch adds a dedicated `podcast` source type, audio media coverage, and explicit metadata-versus-preview Evidence Receipts.

- Five real Xiaoyuzhou episodes in default mode: **5/5 `PARTIAL_MEDIA`**, usable show-note text 5/5, full spoken-media 0/5; median default retrieval ~2.309 seconds.
- One public episode test: **12 seconds** of audio extracted and locally transcribed by faster-whisper tiny CPU, producing 91 machine-generated Chinese characters. The typed receipt is `PARTIAL / spoken_media_preview_only / preview_seconds=12 / transcript_coverage=preview`.
- Media access was subsequently tightened: validated allowlisted `media.xyzcdn.net` source, pinned-IP **HTTP 206** byte-range request capped at **2 MiB**, no redirects, local temporary M4A, and **local-file-only ffmpeg** to derive an ASR clip. The same public 12-second ASR canary passed again.
- Explicit user opt-in is required via `--media-preview-seconds 1..30`; no default audio download, full-episode ASR claim, Groq/OpenAI API call, or production deploy.
- The ASR transcript can have recognition errors/hallucinations and covers only the sample; the complete episode is still unread. This is incremental capability validation, **not proof of a commercial moat**.

**Benchmark runner verification:** The new explicit `--podcast-preview-seconds 12` flag was exercised on `pod-01` with `--workers 1 --limit 1` using local Whisper tiny CPU. One case returned `PARTIAL_MEDIA` in 11,783 ms; `full_read_rate=0.0`, `usable_text_rate=1.0`. The flag is disabled by default and refuses concurrent multi-case ASR previews. This is a time-limited audio sample, not full transcript coverage. Code/tests/README are in the product development branch; main, PyPI and stable NBG server remain unchanged.
