# Evidence Receipt (experimental)

Evidence Receipt is a small local web surface built on the same source-first contract as the x-reader Agent Skill.

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
- the temporary directory is deleted after the read.

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

## Verification

The product branch was independently exercised on GitHub-hosted Ubuntu with:

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
- The MVP is local-first and is not a multi-tenant hosting boundary.
- If this becomes an internet-facing service, add deployment-specific request limits, isolation, observability, abuse controls, and an explicit hosting threat model before calling it production-ready.
