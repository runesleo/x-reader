# x-reader · Long-audio chunk resume and complete coverage

**2026-10-10 · experimental product branch** — This report describes the *existing* x-reader source-ingestion layer. The public hosted web service and `main` are unchanged.

## Real source

- Public Xiaoyuzhou episode: [SpaceX 筹备星舰第 14 次试飞、AI陷创意争议与帕台农错觉之谜](https://www.xiaoyuzhoufm.com/episode/6aab4896051af796b9e966c5)
- Exact encoded bytes from HTTP 206/strong ETag: **4,483,364**; **5 chunks** of at most 1,048,576 bytes, each SHA-256 verified.
- Source audio duration: **277.90 seconds**; fully decoded and processed PCM: **277.92 seconds**.
- Audio SHA-256 prefix: `7cf6e7678f89` (source identity is version-bound to hashed episode URL, CDN URL and strong ETag).
- Full-audio ASR: **5 contiguous PCM windows**, **59 recognized utterance segments**, **4,333 transcript characters**, `coverage_ratio=1.0`.
- Canonical Evidence Receipt: **PASS / spoken_media_complete_verified**, with internal source/chunk/PCM/ASR integrity fields, no raw audio URL or transcript in the receipt. Transcription accuracy has **not** been manually audited.

## Real-world sequence

1. **Deliberate interruption:** fetched and hashed only the first **two 1MiB chunks**, then interrupted. The persisted 0700/0600 checkpoint manifest contained only hashes and byte ranges, not the original CDN URL.
2. **First resume (2026-10-08):** all five source chunks were complete and verified, two ASR checkpoint records were written, but processing aborted on a Whisper final segment timestamp beyond a physical 60-second audio window. The system correctly withheld `PASS`.
3. **Failure reproduction (2026-10-10):** segment index 2 had real 60-second PCM `[120,180]`; Whisper emitted the last utterance timestamp `57.36–61.36` relative to that 60s chunk. This was a **1.36-second model timestamp overshoot**, not absent decoded PCM.
4. **Guarded correction:** accept **at most one near-end timestamp overrun** per audio interval, capped at the lesser of **2 seconds** or **3.5% of the window** (with a small minimum tolerance), and record `asr_boundary_adjustments=1` and `max_boundary_overrun_seconds=1.36`. Utterances starting after the PCM ends, major overruns or duplicate overruns still fail closed. The audio samples and source hashes are never stretched or invented.
5. **Complete resume:** five of five verified source chunks reused, two of five ASR records reused and three remaining audio intervals transcribed. Canonical Evidence Receipt **PASS**, with complete source and temporal evidence; elapsed **15.4 seconds** for that resumed call.
6. **Repeat run:** **5/5 source chunks + 5/5 ASR checkpoints reused**, same SHA256 prefix and **PASS**, in **13.42 seconds**.
7. **Standard CLI/Moat Benchmark:** one public episode processed through `--podcast-full-long` with one worker and explicitly supplied private resume directory, classified **1/1 MEDIA_COMPLETE**, **15,704 ms**, and zero consumer account/API keys.

## Safety, privacy and negative controls

- Public Xiaoyuzhou episode URLs only. Audio must come from allowlisted HTTPS `media.xyzcdn.net`; pinned public-IP connections use HTTP 206 with strong ETag, bounded byte ranges, no redirects and strict MIME checks.
- **64 MiB max compressed audio**, **90-minute max decoded duration**, 1MiB download chunks, max 90 ASR windows, local ffmpeg/Whisper CPU. No default or concurrent full-long processing.
- Oversized multi-hour public podcast rejected at the CDN `Content-Range` header stage **before downloading its audio**.
- Corrupted cached chunks are downloaded afresh; missing/changed ETag or changed source length cannot silently reuse another version; corrupted ASR checkpoints are recomputed.
- Missing PCM interval, transcript segment, full-source hash or exact time coverage prevents a complete-source receipt. Negative tests confirm forged `has_transcript=true` or missing manifests remain `PARTIAL`.
- Cached byte chunks and local ASR transcript text remain on disk to enable later resumes. Directories are private (0700) and files 0600, but the cache **is persistent**, not encrypted by x-reader, and does not auto-expire. Users can select a separate private `X_READER_LONG_CACHE_DIR`; do not run this mode against private/credentialed sources.
- Model word recognition may be incorrect or hallucinated. **100% means all PCM source audio was processed**, not that every word is accurate.

## Reproduce in the existing product branch

```bash
git clone https://github.com/runesleo/x-reader.git
cd x-reader
git switch product/evidence-receipt-mvp-20261007
python -m pip install -e '.[media]'
# ffmpeg, ffprobe and yt-dlp must be in PATH
x-reader 'https://www.xiaoyuzhoufm.com/episode/6aab4896051af796b9e966c5' --media-full-long --json
```

This is an **experimental development-branch** capability, not a change to the public hosted x-reader web beta. The benchmark source case was deliberately kept separate from the canonical 50-URL corpus. Private caches, binary audio and machine transcript artifacts are **not** committed.

## What remains unproven

- Hours-long reliability/CPU/disk-cost, rescoring after model upgrades, cross-source/transcript semantic accuracy, YouTube/Bilibili video decoding, and private-source auth boundaries.
- No comparison to Jina, Firecrawl, Exa or frontier model native browsing on equivalent media/credential conditions was completed.
