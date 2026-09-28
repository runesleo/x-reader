# First-success benchmark

This benchmark measures one thing: **did x-reader actually retrieve the requested source layer?**

It is a live canary, not a deterministic CI suite. Public websites, login walls, and anti-bot behavior change.

## Coverage states

| State | Meaning |
|---|---|
| `READ` | Requested textual body/post was retrieved |
| `PARTIAL` | Some source layer was retrieved, but not the whole requested source |
| `AUTH_REQUIRED` | A local login/session is the remaining blocker |
| `FAILED` | Supported retrieval paths did not return usable content |

## Run

```bash
python scripts/first_success_smoke.py \
  https://example.com \
  https://x.com/dontbesilent/status/2103875422522077377
```

The smoke emits JSON receipts. Do not turn `PARTIAL` into `READ` in documentation or demos.

## Verified local snapshot — 2026-09-27

The same two-URL canary above was run from a fresh editable install:

| URL | Overall | Components / evidence |
|---|---|---|
| `https://example.com` | `READ` | substantial public web body retrieved |
| `https://x.com/dontbesilent/status/2103875422522077377` | `PARTIAL` | post text=`READ` via oEmbed; attached media=`PARTIAL` because the base CLI did not retrieve/transcribe it |

This is a dated external canary, not a permanent availability guarantee; the X post is third-party content and may disappear or change availability.

## Target matrix

| Class | Minimum acceptance |
|---|---|
| Public web/article | `READ` |
| Public X status | post text must be `READ`; overall source is `READ` only when media_status=`none`, otherwise conservatively `PARTIAL` |
| YouTube/Bilibili | `PARTIAL` is honest for base CLI until subtitles/transcript are retrieved |
| X post + attached video | text and media must be reported separately |
| WeChat/XHS | `READ` when body is extracted; otherwise `AUTH_REQUIRED` or `FAILED` |
| Podcast/direct audio | `PARTIAL` until spoken content is transcribed |

A broken public URL should become either a regression fixture after a fix or a documented limitation.
