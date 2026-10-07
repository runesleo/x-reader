# Evidence Receipt public-beta gate

The current Evidence Receipt code is a verified local/CI product candidate. This checklist is required before calling an internet-facing deployment a public beta.

## Already proven

- canonical PASS / PARTIAL / FAIL / UNKNOWN projection is shared with x-reader;
- hosted path shells out to the existing CLI rather than implementing a second reader;
- public URL validation remains in the request path;
- request child processes receive a minimal environment;
- saved browser sessions, Telegram credentials, Groq keys, output paths, and persistent inboxes are not inherited;
- each read uses a temporary HOME/inbox;
- response content preview is bounded;
- request concurrency is bounded in-process;
- Linux canary has exercised repository tests, a live generic page, a live mixed-media X URL, and CLI/HTTP receipt parity.

## Required before public internet exposure

### Abuse and network boundary

- Edge rate limit by client/IP or equivalent abuse identity.
- Global request budget in addition to the in-process semaphore.
- Deployment-level outbound network policy appropriate for a service that fetches user-supplied URLs.
- Confirm DNS rebinding / redirect handling remains inside the public-destination policy for every network hop.
- Maximum response/download/body budgets at the deployment layer.
- No generic proxy behavior: unsupported destinations must fail closed.

### Runtime isolation

- Run as an unprivileged service user.
- Read-only application filesystem where practical; writable temp only.
- No cloud metadata credentials or unrelated runtime secrets available to the process.
- Explicit CPU/memory/process/time limits for each instance.
- Verify temporary files are removed on normal and timeout paths.

### Observability without collecting source content

- Health and readiness checks.
- Aggregate counters for requests, status class, platform, latency, timeout, 429, and server errors.
- Do not log full fetched content.
- Avoid storing raw submitted URLs unless there is a specific disclosed need.
- Error logs must not serialize inherited environment/secrets.

### Product truth

- Page says beta, not production-grade or universal.
- Supported public surfaces are named exactly.
- PARTIAL/FAIL/UNKNOWN remain visible and shareable without being rewritten as success.
- Privacy/security text matches deployed behavior.
- Stable domain and support/issue path exist.
- A concise privacy notice explains request/log retention.

### Release verification

- Focused web/evidence tests pass.
- Full repository test suite passes from a clean checkout/worktree.
- Build/install smoke passes for the package containing `x_reader.web`.
- Preview deployment: `/healthz` passes.
- Preview deployment: live generic web read passes.
- Preview deployment: known mixed-media X remains PARTIAL unless its attached media was actually consumed.
- CLI and HTTP classifications match on the same canary source.
- Final diff reviewed; no credentials, local paths, or private canary data promoted.

## Explicitly not required for the first beta

- billing;
- accounts;
- persistent user history;
- team workspaces;
- private/authenticated source access;
- a second analytics stack;
- a separate product brand.

Those features add surface area before acquisition is proven.
