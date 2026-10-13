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
| X post | post text from oEmbed/FxTwitter (or a rendered post), no attached media, and the quoted post, poll, X Article and thread layers checked and absent or read (a quoted post with its own media, poll, article or thread is incomplete) | `PARTIAL` naming the missing layer; text from a page reader is `UNKNOWN` |
| YouTube | never: pictures are not analyzed | `PARTIAL`; `spoken_media` is `PASS` only when subtitle cues cover the whole video (no uncaptioned stretch over 30 s, at least half the duration captioned). The hosted beta starts no external tools, so there it covers page metadata only |
| Generic page | x-reader loaded the page itself over pinned hops (redirects checked, no embedded audio/video player), at least 280 characters of sentence-like prose, and no login, paywall, consent, teaser, removed or challenge markers | `FAIL` for walls and missing pages, `PARTIAL` for teasers and pages with audio/video, `UNKNOWN` for thin text or pages x-reader could not load itself |
| RSS | the item carries its body (280+ characters of visible prose, no "read more" or paywall text) and it covers at least 80 % of the linked article page's prose | `PARTIAL` (`feed_summary_only`, or `feed_body_unverified` when the article page cannot be read) |
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
fetch_method    oembed
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
