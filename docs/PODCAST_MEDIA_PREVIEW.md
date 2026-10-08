# Podcast media-preview contract (experimental, local CLI only)

## What this adds

x-reader now treats a public podcast episode page as a podcast **audio source**, not as an ordinary website whose show notes can claim full-source coverage.

| Mode | Example | Outcome |
|---|---|---|
| Default | `x-reader https://www.xiaoyuzhoufm.com/episode/... --json` | Public title/show notes only; `PARTIAL`, audio unread |
| Explicit preview | `x-reader https://www.xiaoyuzhoufm.com/episode/... --media-preview-seconds 12 --json` | Show notes plus first 12 seconds of machine-generated spoken audio; **still `PARTIAL`** |
| Full audio | Not implemented in this iteration | Never claim full coverage or full transcript |

With the explicit preview flag (integer **1–30 seconds**), x-reader may install/download local optional model weights, locate the approved public audio CDN, fetch only the **first 2 MiB maximum** with a pinned-IP HTTP Range request (HTTP 206 required, redirects rejected, audio Content-Type checked), decode the downloaded temporary file with **network-disabled ffmpeg** (file protocol only), run local faster-whisper (tiny CPU), and delete temporary audio. It never automatically uses user cookies, a Groq/OpenAI key, or a paid endpoint. The model output is machine-generated and may contain recognition errors or hallucinations, so it is not a substitute for listening to the complete episode.

Optional dependencies:

``bash
# Experimental product branch; not yet merged into main or published on PyPI.
git clone https://github.com/runesleo/x-reader.git
cd x-reader
git switch product/evidence-receipt-mvp-20261007
python -m pip install -e '.[media]'
# Also install ffmpeg and yt-dlp in PATH; model weights may be downloaded
# only when you explicitly request --media-preview-seconds.
```

The audio preview currently supports only public **Xiaoyuzhou** episodes with audio streams from approved `media.xyzcdn.net`; other podcasts stay metadata-only and return a clear unsupported-preview marker rather than falsely claiming coverage. ffmpeg is never given a remote URL: it reads a local temporary audio file after pinned-IP validation and bounded download. Only the original public episode URL is stored in the result; the extracted CDN location is ephemeral and never committed.

## Evidence

`source_type=podcast`, `media_type=audio`, and `extra.transcript_coverage=none|preview` now travel through `UnifiedContent` and `build_receipt`. `has_transcript` stays **false** for a sampled audio clip. Receipt reason codes:

- `spoken_media_unread`: readable show notes, no audio.
- `spoken_media_preview_only`: a measured first-N-seconds audio preview was transcribed, remaining episode unread.
- `spoken_media_read`: reserved for independently verified complete media coverage; **not produced by the current preview path**.

Do not count metadata-only or preview-only podcast reads as `MEDIA_COMPLETE` in Moat Benchmark.

## Limitations and next gates

- This does **not** automatically transcribe 47–145-minute podcast episodes, Bilibili video audio, YouTube videos behind login walls, or arbitrary private media.
- Per-request local model inference may use CPU and download model weights on first opt-in. No background continuous media processing or new billing.
- The CDN must support HTTP 206 byte ranges and an audio Content-Type; unsupported streams fail the optional preview rather than relaxing network safety.
- Full-source completion needs audio-duration accounting, chunk boundaries, deduplication, transcript provenance, and an end-to-end test covering the entire episode.
- Hosted public web beta is intentionally unchanged; no audio preview is enabled on the server.

## Reproducible benchmark (single opt-in case only)

```bash
# Run from this checkout with the media extra, ffmpeg and yt-dlp installed.
PYTHONPATH=. python benchmarks/moat/run.py \
  --providers x_reader --category podcast --limit 1 --workers 1 \
  --timeout 120 --podcast-preview-seconds 12
```

The benchmark refuses multi-case/parallel ASR preview execution by default. A 2026-10-08 real run on one public Xiaoyuzhou episode completed in **11.783 seconds**, yielding `PARTIAL_MEDIA`, `preview_seconds=12`, and `full_read_rate=0.0`. A five-case metadata-only run returned 5/5 `PARTIAL_MEDIA`, 0/5 full-media completion. No developer model API or browser login key was used. The unredacted media sample, local Whisper model cache and binary files are **not** committed.
