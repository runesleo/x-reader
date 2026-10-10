# x-reader Moat Benchmark

This benchmark is intentionally adversarial. It asks whether x-reader owns source-access / ingestion capabilities that remain useful even as frontier models gain stronger browsing and computer-use abilities.

## Corpus

`benchmarks/moat/corpus.jsonl` contains 50 real URLs:

| Class | Count | Why it matters |
|---|---:|---|
| ordinary web | 10 | commodity baseline; x-reader should not spend moat R&D here |
| public X | 10 | post text, threads, media-awareness and X-specific fallbacks |
| WeChat | 5 | anti-bot / JS-rendered Chinese source class |
| Xiaohongshu | 5 | 3 externally indexed share URLs + 2 known restricted controls, preserving public xsec_token query |
| Telegram | 5 | channel/post semantics and credential-vs-public boundaries |
| Bilibili | 5 | video metadata, subtitles / audio completion |
| podcasts | 5 | page metadata vs spoken-content completion |
| auth-gated | 5 | whether a provider honestly blocks instead of returning a login shell |

The corpus should stay real and dated. Replace dead public URLs rather than weakening the class.

## Outcomes

Transport success is not enough.

- `READ`: substantive source text; when media is required, text-only retrieval is not full completion.\n- `PARTIAL_MEDIA`: page text/description read but material audio/video is not proven consumed.\n- `MEDIA_COMPLETE`: transcript/subtitles or other material media evidence has been retrieved.
- `THIN`: technically returned, but too little content to support normal source use.
- `BLOCK_SHELL`: returned a login/security/unavailable shell rather than source content.
- `FAIL`: source could not be retrieved.
- `EXPECTED_BLOCK`: an auth-gated or known restricted control fails closed.\n- `AUTH_SHELL` / `AUTH_UNVERIFIED`: a private source yielded an auth page or unverified public/marketing content.\n- `CONTROL_READ_REVIEW`: a previously restricted control became readable and needs recuration.
- `SKIP`: provider unavailable because a required credential is absent.

The JSONL result also records latency, content size, fetch method, media status and x-reader evidence status when available. `full_read_rate` and `usable_text_rate` use only eligible public cases as the denominator; the five auth-gated cases and two known restricted XHS controls are separately counted as safety/access tests. Do not treat `PARTIAL_MEDIA` as `MEDIA_COMPLETE`.\n\n**Baseline caveats (2026-10-08):** From the base package, WeChat/XHS failed before the optional Playwright runtime was installed, Telegram needed Telethon and API credentials, and Bilibili's API returned 412. A full-dependency retest recovered 4/5 WeChat text pages. Two externally verified Bilibili video pages were subsequently recovered through the browser fallback as `PARTIAL_MEDIA` (~3,177 and ~3,619 characters), not full video transcription. Other Bilibili URLs are still unverified or failing. Local XHS session still encounters platform error `300031` on some externally readable links. Never publish the old 28% full-read headline after corpus/eligibility changes without a new complete run.

## Providers

### x_reader

Runs the repository CLI in an isolated temporary inbox:

```bash
python benchmarks/moat/run.py --providers x_reader
```

### Jina Reader

Uses the current public Reader endpoint with `Accept: application/json`.

```bash
python benchmarks/moat/run.py --providers jina
```

Do not infer availability from marketing/docs. On 2026-10-08 the same benchmark host received HTTP 401 without a Jina key, despite the public Reader page describing a no-key tier. That is recorded as provider availability, not automatically as an x-reader moat win.

### Firecrawl

Optional and fail-closed:

```bash
FIRECRAWL_API_KEY=... python benchmarks/moat/run.py --providers firecrawl
```

Without the key, cases are `SKIP`. Never register accounts, purchase credits or expose a key just to make the benchmark green.

### Exa / native-model baselines

These should be run as connector/model-assisted adjudication against the same corpus, then imported or compared to the raw JSONL. They are deliberately not faked through an undocumented HTTP endpoint.

### Short podcast full-source proof (explicit, single case only)

The development runner supports `--podcast-full-short` but **only** with `--providers x_reader --workers 1` and a single eligible podcast row in a custom corpus. It calls the canonical Evidence Receipt; `MEDIA_COMPLETE` is awarded only when the hash/bytes/duration/coverage fields are independently self-consistent and the receipt is `PASS`. A forged `has_transcript=true`, incomplete bytes or partially decoded audio cannot earn `MEDIA_COMPLETE`.

A 13.12-second publicly accessible Xiaoyuzhou episode (ID `6a14e9dd3209346094186445`) completed in 25.660 seconds on the isolated benchmark host (97,989 complete bytes, full decoded 13.12s, ASR 4 speech segments). **This proves a short-file mechanism, not a long-podcast moat or cross-provider win.**

### Long-audio provenance and resume benchmark (experimental)

For a **single, public, eligible Xiaoyuzhou episode**, use the development branch and `--podcast-full-long --workers 1 --timeout 180` with a single-row corpus. The reference 50-URL corpus remains unchanged; opt-in ASR must never silently run on all sources. By default, the harness keeps audio/transcripts in a temporary private cache; only setting `X_READER_LONG_CACHE_DIR` explicitly enables checkpoint reuse across benchmark processes. The canonical Evidence Receipt must validate byte/chunk hashes, uninterrupted PCM time intervals, segment transcript hashes, and bounded Whisper timestamp adjustments before a case can be classified `MEDIA_COMPLETE`.

A 277.9-second public episode returned **MEDIA_COMPLETE** from the standard CLI/benchmark in **15,704 ms** on 2026-10-10 when resuming existing verified source/ASR checkpoints. The 5/5 chunks and 5/5 ASR checkpoints were independently reused in an additional direct reader call. This is one successful public case, **not** a general long-form accuracy or commercial moat result. See [dated evidence](./LONG_AUDIO_RESUME_2026-10-10.md).

## Recommended sequence

First establish x-reader's own 50-case baseline:

```bash
PYTHONPATH=. python benchmarks/moat/run.py --providers x_reader --workers 4 --timeout 45
```

Then run available external baselines. For fast iteration by source class:

```bash
PYTHONPATH=. python benchmarks/moat/run.py --providers x_reader,jina --category x_public --category wechat
```

## Decision rule

Do not call a class a moat because x-reader simply returns more characters.

Promote a source class only when repeated runs show a durable advantage in at least one of:

1. original-source success where generic readers regularly fail;
2. authenticated/local-session access with a safe boundary;
3. media completion (subtitle/transcript/audio/video) rather than page metadata only;
4. deterministic structured output useful to agents;
5. explicit provenance / coverage that prevents silent false completion;
6. monitoring/change-detection workflows that remove repeated manual reads.

If ordinary web is tied with commodity readers, stop investing there and route to the cheapest reliable provider.
