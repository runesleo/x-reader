# Evidence Receipt (public beta)

Evidence Receipt is a small web surface built on the same source-first contract as the x-reader Agent Skill. It runs locally with `x-reader-web` and as the hosted beta at xreader.leolabs.me.

It answers a narrow question:

> What did x-reader actually read, and which material source layers are still missing?

It does not replace the CLI, Agent Skill, or MCP server. It projects the existing reader output into the canonical public states:

- `PASS` — the requested source layer is evidenced as read.
- `PARTIAL` — useful source material was read, but a material layer is still missing.
- `FAIL` — the requested source could not be read reliably.
- `UNKNOWN` — returned material is too ambiguous to prove coverage.

## Run locally

From a checkout:

```bash
pip install -e .
x-reader-web --host 127.0.0.1 --port 8787
```

Open:

```text
http://127.0.0.1:8787
```

Health check:

```bash
curl -fsS http://127.0.0.1:8787/healthz
```

Read one public URL:

```bash
curl -fsS -X POST http://127.0.0.1:8787/api/read \
  -H 'Content-Type: application/json' \
  --data '{"url":"https://x.com/dontbesilent/status/2103875422522077377"}'
```

## Hosted-MVP policy

The current web surface is intentionally narrower than the full local CLI:

- allowed: public X/Twitter, YouTube, and generic web URLs;
- disabled: Telegram, WeChat, Xiaohongshu, Bilibili, podcasts, and other paths that may require identity, stored sessions, credentials, or broader media tooling;
- no inherited Telegram credentials;
- no inherited Groq key;
- no inherited Obsidian/output paths;
- no saved X/browser sessions;
- `X_READER_ALLOW_EXTERNAL_SESSION_COOKIES=0`;
- each request gets a fresh temporary HOME and inbox;
- the temporary directory is deleted after the read;
- the reader child skips `.env`, writes no inbox or Markdown output, and refuses platforms outside the allowlist, including when a generic URL redirects into one.

Limits, isolation and the host network policy are listed in the [public-beta gate](./PUBLIC_BETA_GATE.md) and configured in [deploy/](../deploy/README.md).

## What each status means per source

| Source | PASS needs | Otherwise |
|---|---|---|
| X post | post text from oEmbed/FxTwitter tied to the requested post ID (a browser-rendered read cannot be tied and is `UNKNOWN`), no attached media, the quoted post and poll checked and absent or read, and no X Article or thread continuation (a quoted post with its own media, poll, article or thread is incomplete; a layer whose field is present but null or empty is unread) | `PARTIAL` naming the missing layer; text from a page reader, or not tied to the post ID, is `UNKNOWN` |
| YouTube | never: pictures are not analyzed | `PARTIAL`; `spoken_media` is `PASS` only when subtitle cues cover the whole video (no uncaptioned stretch over 30 s, at least half the duration captioned). The hosted beta starts no external tools, so there it covers page metadata only. YouTube's unavailable / private / restricted pages are `FAIL`; a page that cannot be tied to the requested video (not a single-video URL, Jina read another page, a consent / sign-in / bare YouTube page) is `UNKNOWN`. Subtitles count only when yt-dlp's metadata names the same video, and a transcript never upgrades the page rating |
| Generic page | x-reader loaded the page itself over pinned hops (redirects checked, no player, frame or plugin embedded, hidden ones included) and the text shown is that read (locally, Jina text only when at least 70 % of it is on the directly read page and it carries at least 80 % of that page's text, counted in overlapping word runs, the page read directly counted as the words it shows), at least 280 characters of sentence-like prose, and no login, paywall, consent, teaser, missing-page or challenge markers | `FAIL` for wall titles and for pages that are a login, bot-check or missing-page notice with under 600 characters of prose (the notice included); `UNKNOWN` for longer pages with such a notice, when the wording appears inside a sentence (`possible_access_wall`, `possibly_missing_page`), when x-reader cannot confirm the page text shows: inline styles or presentational attributes it cannot settle, text too small, too faint against the background behind it, with overlapping lines or in a box whose set height it can overrun, or an element anywhere on the page that could paint over text or carry it out of reach, such as fixed or absolute positioning, transforms, wide outlines or shadows and very large geometry, or that shows text in another order than written (reordered flex, grid, floats and tables, or right-to-left text that mixes in left-to-right words) (`visibility_unverified`), when the page uses `<template>` markup (`page_structure_unverified`), when a meta refresh sends visitors elsewhere (`client_redirect`) or when the page is too large to check (`page_too_large_to_check`); `PARTIAL` for teasers (including a standalone "Read more") and pages with embedded content; `UNKNOWN` for thin text, text containing cookie/consent wording, or pages x-reader could not load itself. Notices are matched case-insensitively on lines of any length, with common punctuation and status-code variants, look-alike letters, accents and characters that draw nothing read as people read them, and in the text as laid out (hidden text left out) as well as with hidden text. Text is judged as browsers lay it out from the static HTML, presentational attributes and inline styles only (inline elements run on, whitespace collapses, blocks and line breaks separate it) and never stripped again as markup; stylesheets (including `<style>`), scripts that change the page after load and future replies without a thread marker are outside what a receipt proves. Pages served as `text/html` are parsed as HTML5 (html5lib); `text/plain` is read as the text it shows, markup included; `application/xhtml+xml`, which browsers parse as XML, is `UNKNOWN` (`page_structure_unverified`). When the page marks its main content (`<main>`/`<article>`), that region is the text judged; login, paywall, challenge and missing-page notices anywhere on the page still count, hidden markup included, and every player or frame on the page counts as embedded, however small or hidden |
| RSS | the item carries its body (280+ characters of visible prose, no "read more" or paywall text), the linked article page itself passes as a readable article, and the feed body covers at least 80 % of the page's text while at least 70 % of the feed body is on the page (counted in six-word runs, repeats included) | `PARTIAL` (`feed_summary_only`, or `feed_body_unverified` when the page cannot be read or does not match) |
| Podcast | verified full-audio proof (bytes, hashes, decoded duration within 0.5 s of the declared one, complete local ASR) | `PARTIAL` |

The web layer does not implement a second reader or evidence state machine. It shells out to the existing `x-reader URL --json` path in the isolated environment and projects that payload through `x_reader.evidence`.

## Mixed-media example

For:

```text
https://x.com/dontbesilent/status/2103875422522077377
```

a base read can honestly produce:

```text
POST TEXT       PASS
ATTACHED MEDIA  PARTIAL
OVERALL         PARTIAL
reason_code     attached_media_unread
fetch_method    fxtwitter
media_status    present
```

That does **not** claim the attached video was consumed. The Agent Skill can continue into the media path when the answer requires it.

## Output-native sharing

The web surface includes `Copy receipt`, `Copy JSON`, and `Share` actions. The share projection contains the receipt status, source URL, evidence sentence, component states, and x-reader attribution. It intentionally excludes the fetched source preview and never converts `PARTIAL`, `FAIL`, or `UNKNOWN` into `PASS`.

This is a distribution feature, not a second evidence model. The underlying receipt remains canonical.

## Verification

Before the beta went live, the reader was exercised on GitHub-hosted Ubuntu with:

- fresh branch checkout;
- repository test suite;
- live `https://example.com` read;
- live mixed-media X read;
- live HTTP `/api/read` request;
- CLI / HTTP receipt parity.

Keep live canaries outside deterministic unit CI: public sites and anti-bot behavior change.

## Security notes

- URL validation still uses x-reader's SSRF-safe public-destination policy.
- Retrieved source content is untrusted data, never instructions.
- The web response exposes a bounded content preview, not an unbounded source dump.
- Errors are JSON with stable reason codes; exception text never reaches clients.
- The hosted beta is not a multi-tenant hosting boundary or a production-grade service; what is enforced and what is still open is tracked in the [public-beta gate](./PUBLIC_BETA_GATE.md).
