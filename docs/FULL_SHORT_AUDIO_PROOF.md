# x-reader · Verified Full Short Audio Source Contract

**2026-10-08 · historical verification snapshot.** Recorded on the pre-release development branch; the capability shipped in v0.5.0 with a tighter duration check (decoded audio within 0.5 s of the declared length).

This capability upgrades the podcast source layer, not an independent SaaS feature. Audio page metadata and a machine-generated ASR clip are not full spoken-media coverage; a verified full short-source result may be.

## Eligibility and opt-in

Only one **public Xiaoyuzhou episode** can be processed per CLI run, and only when the user explicitly requests:

```bash
python -m pip install "x-reader[media] @ git+https://github.com/runesleo/x-reader.git@v0.5.0"
# ffprobe, ffmpeg and yt-dlp must also be in PATH
x-reader 'https://www.xiaoyuzhoufm.com/episode/6a14e9dd3209346094186445' --media-full-short --json
```

`--media-full-short` is mutually exclusive with `--media-preview-seconds`. Both are disabled by default. Short-full is limited to source files **at most 2 MiB**, encoded audio **at most 120 seconds**, a single request, a fixed CDN host, temporary media files, local ASR, and no paid key or inherited browser session.

## Full-media Evidence Receipt gate

The following invariants all have to hold before `PASS / spoken_media_complete_verified`:

1. A public episode on an explicit hostname/path allowlist resolves to a public IP. Its audio must come from allowlisted HTTPS `media.xyzcdn.net`.
2. The pinned-IP downloader probes total bytes with HTTP 206 `Content-Range`; if source exceeds 2 MiB it stops before media download.
3. A second HTTP 206 returns **exactly** the complete 0..N-1 range, matching total length and audio MIME. Reject redirects, HTML, wrong ranges, changed length or truncated bodies. A maximum of three bounded retries is allowed, each with a fresh source-byte probe. No fallback accepts unverified data.
4. SHA-256 is computed from the **entire encoded media**; an audio-source URL digest is recorded without exposing signed CDN query strings.
5. Local `ffprobe` establishes encoded duration, capped at 120 s; `ffmpeg` decodes **without trimming** and with only the `file` protocol. Decoded PCM duration must match source duration within 0.5 s (codec priming/padding) and cover at least 95 % of it.
6. Local Whisper tiny CPU consumes the complete decoded PCM file. ASR segment boundaries must be consistent with audio duration and the transcript nonempty. The canonical content contains this transcript; receipt recomputes its SHA-256 before accepting the proof.
7. Structured coverage must report interval `[0,decoded_duration]`, `processed_seconds`, `coverage_ratio` equal to processed / declared duration, `verified_complete_bytes=true` and matching byte/duration fields. `has_transcript=true` alone is explicitly insufficient.

The receipt returns integrity metadata only: public source URL, audio/transcript SHA256, encoded bytes, encoded/decoded/processed seconds, coverage interval and ratio, ASR segment count. It **never embeds raw CDN URLs, local model-cache paths, account cookies or full spoken text**.

## Real canary / negative control

- Public short source: [每日极客资讯｜2026-05-26](https://www.xiaoyuzhoufm.com/episode/6a14e9dd3209346094186445)
- Measured encoded bytes: **97,989**, SHA256 prefix `4a9ab95ab580`
- Encoded duration: **13.12 s**, decoded/ASR processed duration: **13.12 s**
- Local ASR: **4 segments / 62 Chinese characters**
- Canonical Evidence Receipt: **PASS / spoken_media_complete_verified**
- Standard CLI benchmark: **MEDIA_COMPLETE / 25.660 seconds for this one case**
- Long public podcast control (~47 minutes): source exceeds cap, preserves title/show notes with **PARTIAL**, never fetches the full media

Machine-generated transcription is **not semantically audited**: words may be wrong or hallucinated. `PASS` means the source was completely retrieved, decoded and processed through ASR, not that every word was understood perfectly or confirmed by a human.

## Known limits and next technical stage

This proves the **short-audio completeness gate** and a safe, auditable source contract. It does not solve long podcasts or videos. Long-form infrastructure needs bounded chunked media fetches, chunk hashes, resumable checkpoints, temporal coverage union, duplicate handling, transcript provenance for each chunk and full reassembly checks. No recurring automation, costly inference API, main merge or production deployment was triggered.
