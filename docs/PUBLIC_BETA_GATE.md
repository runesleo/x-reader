# Evidence Receipt public-beta gate

Status (2026-10-13): the hosted surface runs as a **public beta** for public X, YouTube and generic web URLs. It is not a production-grade or multi-tenant service. This page records what was verified for the beta, where the evidence is, and what is still open.

## Verified for the beta

### Reader path

- The canonical PASS / PARTIAL / FAIL / UNKNOWN projection is shared with x-reader (`x_reader/evidence.py`); the web layer shells out to the CLI instead of implementing a second reader.
- The hosted policy and the CLI reader share one URL router (`x_reader/routing.py`); the child process refuses platforms outside the hosted allowlist, including when a generic URL redirects into one (`tests/test_hosted_hardening.py`).
- Every network hop the server opens itself is re-resolved, checked against private/special addresses and pinned to the checked IP; RSS is downloaded this way before parsing; yt-dlp only ever receives a rebuilt `https://www.youtube.com/watch?v=<id>` URL with the generic extractor disabled.

### Isolation

- Request child processes get a minimal environment and a temporary HOME/inbox that is deleted afterwards; they skip `.env`, never write inbox or Markdown output, and inherit no saved sessions, Telegram credentials, Groq keys, output paths or proxies.
- Runs as an unprivileged service user with a read-only system, private `/tmp` and no capabilities ([deploy/x-reader-evidence.service](../deploy/x-reader-evidence.service)).
- Outbound connections from the service account to loopback, private, CGNAT/tailnet, link-local/metadata, multicast and reserved addresses are rejected at the host ([deploy/xreader-egress.nft](../deploy/xreader-egress.nft)).

### Limits

- Per request: 75 s deadline and 8 MiB output cap enforced while the reader runs; its whole process group is killed on exit, timeout or overflow; 120 CPU-seconds, 64 MiB per written file and 256 open files per process; reader processes are the first OOM-kill candidates.
- Per service: 512 MiB memory, 80 % CPU, 64 tasks, 512 open files; at most 4 concurrent reads and 32 connection threads; requests not fully read within 20 s are cut off.
- At the edge: 12 reads/minute and 2 concurrent reads per client IP, 8 KiB bodies, full request buffering ([deploy/nginx-xreader.conf](../deploy/nginx-xreader.conf)).
- Responses: bounded content preview, JSON for every error, stable reason codes only (no exception text), no interpreter version in headers, strict CSP.

### Product truth

- Pages say beta; supported surfaces are named exactly; PARTIAL / FAIL / UNKNOWN stay visible and shareable without being rewritten as success.
- Fetched content is not logged; failures log only an exception type, not the URL.

### Release verification

- Repository test suite passes on Linux and macOS (CI: Python 3.10 and 3.11).
- Live canaries on the deployed host: `/healthz`, a generic page, and a mixed-media X post that stays PARTIAL; CLI and HTTP receipts match.

## Still open

- Aggregate counters (requests, status class, platform, latency, timeouts, 429s, server errors) without collecting content.
- A short privacy notice on the site explaining request and log retention.
- A global daily request budget in addition to per-IP limits.
- Per-request memory and process ceilings (today they are per process and per service, not per request group).

## Explicitly not required for the beta

- billing;
- accounts;
- persistent user history;
- team workspaces;
- private/authenticated source access;
- a second analytics stack;
- a separate product brand.

Those features add surface area before acquisition is proven.
