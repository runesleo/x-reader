# Video & Podcast Digest Skill

> Send a video/podcast link → get full transcript + structured summary

## Supported Platforms

| Platform | Type | Subtitles | Whisper Transcription |
|----------|------|-----------|----------------------|
| YouTube | Video | ✅ | ✅ |
| Bilibili | Video | ✅ | ✅ |
| X/Twitter | Video | ❌ | ✅ |
| Xiaoyuzhou (小宇宙) | Podcast | ❌ | ✅ |
| Apple Podcasts | Podcast | ❌ | ✅ |
| Direct links (mp3/mp4/m3u8) | Any | ❌ | ✅ |

## Trigger

Auto-triggered when a media URL is detected:
- YouTube: `youtube.com`, `youtu.be`
- Bilibili: `bilibili.com`, `b23.tv`
- X/Twitter: `x.com`, `twitter.com` (tweets with video)
- Xiaoyuzhou: `xiaoyuzhoufm.com`
- Apple Podcasts: `podcasts.apple.com`
- Direct: `.mp3`, `.mp4`, `.m3u8`, `.m4a`, `.webm`

## Optional X/Twitter Source Context

For X/Twitter videos, use this skill for transcription and digest output. When
the user provides a TweetClaw/OpenClaw source packet, treat it as extra source
evidence before Step 3. Useful packet contents include tweet search results,
reply search results, user lookup details, follower exports, monitor snapshots,
webhook events, and media URLs.

Do not publish posts, send replies, send direct messages, or handle X account
credentials in this skill. Keep those actions inside the approved
TweetClaw/OpenClaw workflow and only cite the resulting source packet in the
digest.

## Pipeline

### Step 0: Detect Media Type

| URL Pattern | Type | Pipeline |
|-------------|------|----------|
| `xiaoyuzhoufm.com/episode/` | Podcast | → Step 1b (Xiaoyuzhou) |
| `podcasts.apple.com` | Podcast | → Step 1c (Apple) |
| `bilibili.com`, `b23.tv` | Video | → Step 1d (Bilibili API) |
| `.mp3`, `.m4a` direct link | Audio | → Step 2b (direct download) |
| Other | Video | → Step 1a (subtitle extraction) |

### Step 1a: Video — Extract Subtitles

```bash
# Clean up temp files
rm -f /tmp/media_sub*.vtt /tmp/media_audio.mp3 /tmp/media_transcript*.json /tmp/media_segment_*.mp3 2>/dev/null || true

# YouTube (prefer English, fallback Chinese)
yt-dlp --skip-download --write-auto-sub --sub-lang "en,zh-Hans" -o "/tmp/media_sub" "VIDEO_URL"

# Bilibili
yt-dlp --skip-download --write-auto-sub --sub-lang "zh-Hans,zh" -o "/tmp/media_sub" "VIDEO_URL"
```

Check for subtitles:
```bash
ls /tmp/media_sub*.vtt 2>/dev/null
```
- **Has subtitles** → Read VTT content, skip to Step 3
- **No subtitles** → Step 2a (download audio)

### Step 1b: Xiaoyuzhou (小宇宙) — Extract Audio URL

```bash
# Extract CDN direct link from __NEXT_DATA__
# Xiaoyuzhou is a Next.js SPA, but initial HTML contains audio URL in __NEXT_DATA__
AUDIO_URL=$(curl -sL -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36" \
  "EPISODE_URL" \
  | grep -oE 'https://media\.xyzcdn\.net/[^"]+\.(m4a|mp3)' \
  | head -1)

echo "Audio URL: $AUDIO_URL"

# Download audio
curl -L -o /tmp/media_audio.mp3 "$AUDIO_URL"
```

> If curl extraction is empty (rare), fallback: use Puppeteer/browser to get rendered page and extract.

→ Step 2b (check size & transcribe)

### Step 1c: Apple Podcasts — via yt-dlp

```bash
yt-dlp -f "ba[ext=m4a]/ba/b" --extract-audio --audio-format mp3 --audio-quality 5 \
  -o "/tmp/media_audio.%(ext)s" "APPLE_PODCAST_URL"
```

→ Step 2b (check size & transcribe)

### Step 1d: Bilibili — API Direct Audio Stream

yt-dlp returns 412 for Bilibili even with cookies. Use Bilibili's API instead:

```bash
# 1. Extract BV number from URL
BV="BV1xxxxx"  # Replace with actual BV number

# 2. Get video info (title, duration, CID)
curl -s "https://api.bilibili.com/x/web-interface/view?bvid=$BV" \
  -H "User-Agent: Mozilla/5.0" -H "Referer: https://www.bilibili.com/" \
  | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; print(f\"Title: {d['title']}\nDuration: {d['duration']}s\nCID: {d['cid']}\")"

# 3. Get audio stream URL
CID=<CID from previous step>
AUDIO_URL=$(curl -s "https://api.bilibili.com/x/player/playurl?bvid=$BV&cid=$CID&fnval=16&qn=64" \
  -H "User-Agent: Mozilla/5.0" -H "Referer: https://www.bilibili.com/" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['dash']['audio'][0]['baseUrl'])")

# 4. Download audio (Referer header required, otherwise 403)
curl -L -o /tmp/media_audio.m4s \
  -H "User-Agent: Mozilla/5.0" -H "Referer: https://www.bilibili.com/" "$AUDIO_URL"

# 5. Convert to mp3
ffmpeg -y -i /tmp/media_audio.m4s -acodec libmp3lame -q:a 5 /tmp/media_audio.mp3
```

→ Step 2b (check size & transcribe)

### Step 2a: Video — Download Audio (when no subtitles)

```bash
# YouTube may need --cookies-from-browser chrome to bypass bot detection
yt-dlp --cookies-from-browser chrome -f "ba[ext=m4a]/ba/b" --extract-audio --audio-format mp3 --audio-quality 5 \
  -o "/tmp/media_audio.%(ext)s" "VIDEO_URL"
```

### Step 2b: Check Audio Size & Segment

```bash
FILE_SIZE=$(stat -f%z /tmp/media_audio.* 2>/dev/null || stat -c%s /tmp/media_audio.* 2>/dev/null)
echo "File size: $FILE_SIZE bytes"
```

- **≤ 25MB (25000000)** → Step 2c (transcribe directly)
- **> 25MB** → Split first, then transcribe each segment

**Splitting large audio (>25MB)**:
```bash
# Get total duration
DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 /tmp/media_audio.* | head -1)

# Split into 10-minute segments (keeps each under 25MB)
SEGMENT_SEC=600
SEGMENTS=$(python3 -c "import math; print(math.ceil(float('$DURATION')/$SEGMENT_SEC))")

# Cut segments
for i in $(seq 0 $((SEGMENTS-1))); do
  START=$((i * SEGMENT_SEC))
  ffmpeg -y -i /tmp/media_audio.* -ss $START -t $SEGMENT_SEC -acodec libmp3lame -q:a 5 \
    "/tmp/media_segment_${i}.mp3" 2>/dev/null
done
```

→ Call Step 2c for each segment **sequentially** (parallel triggers Groq 524 timeout), concatenate results

### Step 2c: Whisper Transcription

**Prerequisite**: `GROQ_API_KEY` environment variable

```bash
# Check API key
if [ -z "$GROQ_API_KEY" ]; then
  echo "❌ GROQ_API_KEY not set. Get one at: https://console.groq.com/keys"
  exit 1
fi

# Transcribe single file (replace AUDIO_FILE with actual path)
curl -s -X POST "https://api.groq.com/openai/v1/audio/transcriptions" \
  -H "Authorization: Bearer $GROQ_API_KEY" \
  -H "Content-Type: multipart/form-data" \
  -F "file=@AUDIO_FILE" \
  -F "model=whisper-large-v3-turbo" \
  -F "response_format=verbose_json" \
  -F "language=zh" \
  > /tmp/media_transcript.json

# Extract plain text
python3 -c "import json; print(json.load(open('/tmp/media_transcript.json'))['text'])"
```

**Whisper model options**:
| Model | Speed | Accuracy | Use Case |
|-------|-------|----------|----------|
| `whisper-large-v3-turbo` | 10x realtime | High | Default choice |
| `whisper-large-v3` | 5x realtime | Highest | Professional/noisy content |

**Language parameter**:
- Chinese: `language=zh`
- English: `language=en`
- Auto-detect: omit language parameter

### Step 3: Structured Summary

Choose output format based on media type:

**Video (≤20 min)**:
1. **Overview** (1-2 sentences)
2. **Key Points** (3-5 bullet points)
3. **Notable Quotes** (if any)
4. **Action Items** (if applicable)

**Podcast (>20 min)**:
1. **Overview** (2-3 sentences: who discussed what)
2. **Chapter Summary** (segmented by topic, 2-3 sentences each)
3. **Key Points** (5-8 bullet points)
4. **Notable Quotes**
5. **Action Items** (if applicable)

## Output Format

### Video

```
## 📺 Video Digest

**Title**: [Video Title]
**Duration**: [x minutes]
**Language**: [Chinese/English]

### Overview
[1-2 sentence summary]

### Key Points
1. [Point 1]
2. [Point 2]
...

### Notable Quotes
> "xxx" — [timestamp]

### Action Items
- [if applicable]
```

### Podcast

```
## 🎙️ Podcast Digest

**Show**: [Podcast Name]
**Episode**: [Episode Title]
**Duration**: [x minutes]
**Guests**: [if any]

### Overview
[2-3 sentences: who discussed what, core conclusions]

### Chapter Summary
#### 1. [Topic] (~xx:xx-xx:xx)
[2-3 sentences of core content]

#### 2. [Topic] (~xx:xx-xx:xx)
[2-3 sentences of core content]
...

### Key Points
1. [Point 1]
2. [Point 2]
...

### Notable Quotes
> "xxx"

### Action Items
- [if applicable]
```

## Error Handling

| Situation | Action |
|-----------|--------|
| No subtitles + no GROQ_API_KEY | Prompt user to set API key |
| No subtitles + has API key | Auto Whisper transcription |
| Xiaoyuzhou curl extraction empty | Use Puppeteer/browser to get rendered HTML |
| Audio >25MB | ffmpeg segment (10min/segment), transcribe sequentially |
| Podcast >2 hours | Warn user about duration, confirm before proceeding |
| Groq 524 timeout | Do NOT parallelize — transcribe sequentially, sleep 5-8s between segments |
| Groq 429 rate limit | 7200s/hour limit, wait for retry-after header, then retry |
| yt-dlp Bilibili 412 | Use Bilibili API instead (Step 1d) |
| yt-dlp YouTube bot detection | Add `--cookies-from-browser chrome` |
| Network timeout | Retry once |
| Spotify links | Inform user: not supported (DRM protected) |

## Groq Whisper Limits

- Max 25MB per request
- Free tier: 7200 seconds of audio/hour (rolling window), ~20 hours/day
- Supported formats: mp3, mp4, mpeg, mpga, m4a, wav, webm

## Dependencies

- `yt-dlp`: video download + subtitle extraction
- `ffmpeg`: audio conversion + segmentation
- `curl`: Xiaoyuzhou audio download, Bilibili API
- `GROQ_API_KEY`: Whisper transcription API (free at https://console.groq.com/keys)
