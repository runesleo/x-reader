# Evidence Receipt public-beta gate

Status (v0.5.0): the hosted surface runs as a **public beta** for public X, YouTube and generic web URLs. It is not a production-grade or multi-tenant service. This page records what was verified for the beta, where the evidence is, and what is still open.

## Verified for the beta

### Reader path

- The canonical PASS / PARTIAL / FAIL / UNKNOWN projection is shared with x-reader (`x_reader/evidence.py`); the web layer shells out to the CLI instead of implementing a second reader.
- The hosted policy and the CLI reader share one URL router (`x_reader/routing.py`); the child process refuses platforms outside the hosted allowlist, including when a generic URL redirects into one (`tests/test_hosted_hardening.py`).
- Connections to the submitted site and every redirect hop are re-resolved, checked against private/special addresses and pinned to a checked IP. Fixed third-party APIs (X oEmbed, FxTwitter, Jina Reader) are called over standard HTTPS.
- Hosted generic reads return only the text the service fetched over those pinned hops, so the receipt describes what was checked; hosted generic reads never use Jina Reader, which follows redirects on its own side.
- A hosted read is a single process: no yt-dlp, ffmpeg or browser is started, so YouTube receipts there cover page metadata only.

### Isolation

- Request child processes get a minimal environment and a temporary HOME/inbox that is deleted afterwards; they skip `.env`, never write inbox or Markdown output, and inherit no saved sessions, Telegram credentials, Groq keys, output paths or proxies.
- Runs as an unprivileged service user with a read-only system, private `/tmp` and no capabilities ([deploy/x-reader-evidence.service](../deploy/x-reader-evidence.service)).
- Outbound connections from the service account to loopback, private, CGNAT/tailnet, link-local/metadata, multicast and class E (240.0.0.0/4) addresses are rejected at the host ([deploy/xreader-egress.nft](../deploy/xreader-egress.nft)); the reader's own address check refuses every other non-public range (documentation, benchmarking, NAT64, 6to4 and the rest) before connecting.

### Limits

- Per request (one process): pages over 2.5 MiB are not read directly (a read peaks near 35 times the page's size), 768 MiB address space, 120 CPU-seconds, 64 MiB per written file, 256 open files, 75 s deadline and 8 MiB output cap enforced while it runs; its process group is killed on exit, timeout or overflow; reader processes are the first OOM-kill candidates; hosted reads write no files besides the temporary HOME.
- Per service: 512 MiB memory, 80 % CPU, 64 tasks; 512 open files per process (each read process lowers its own limit to 256); at most 3 concurrent reads and 24 connection threads; requests not fully read within 20 s are cut off.
- At the edge: 12 reads/minute and 2 concurrent reads per client IP, 8 KiB bodies, full request buffering, JSON error bodies ([deploy/nginx-xreader.conf](../deploy/nginx-xreader.conf)).
- Responses: bounded content preview, JSON for every error, stable reason codes only (no exception text), no interpreter version in headers, strict CSP.

### Product truth

- Pages say beta; supported surfaces are named exactly; PARTIAL / FAIL / UNKNOWN stay visible and shareable without being rewritten as success.
- Fetched content is not logged; failures log only an exception type, not the URL.

### Release verification

- Repository test suite passes on Linux and macOS (CI: Python 3.10 and 3.11).
- Live canaries on the deployed host: `/healthz`, a generic article that passes, a thin page that does not, and a mixed-media X post that stays PARTIAL. The manual "Evidence Receipt Live Canary" workflow runs the same checks on a CI runner without exposing the server.

## Still open

- Aggregate counters (requests, status class, platform, latency, timeouts, 429s, server errors) without collecting content.
- A short privacy notice on the site explaining request and log retention.
- A global daily request budget in addition to per-IP limits.
- Per-request cgroups, needed before the hosted surface may start external tools (yt-dlp, ffmpeg, browsers).

## Explicitly not required for the beta

- billing;
- accounts;
- persistent user history;
- team workspaces;
- private/authenticated source access;
- a second analytics stack;
- a separate product brand.

Those features add surface area before acquisition is proven.
