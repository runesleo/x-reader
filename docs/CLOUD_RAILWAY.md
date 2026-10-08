# x-reader Cloud · Railway + GitHub OAuth (isolated preview)

This Cloud service reuses the branch's existing public-only reader and canonical Evidence Receipt. It adds an owner-only OAuth Streamable HTTP MCP. It is not an X API proxy, not the public free-web beta, and does not use browser sessions or X account cookies.

## Deploy prerequisites

1. Install/connect the official Railway ChatGPT plugin. Confirm it can actually list or create Railway projects. Create exactly one `x-reader-cloud` Railway service from branch `feat/cloud-railway-oauth-20261008`, using `Dockerfile.cloud` and `railway.toml`. Do not point production `main` at this branch until verification.
2. Attach exactly one persistent Railway Volume at **`/data`** BEFORE service start; one instance only. Startup fails closed without the mount. The container starts as root only to provision `/data/oauth`, then drops to the unprivileged `xreader` service account.
3. Generate one HTTPS Railway domain in Networking. Record the **actual** generated origin, including https:// but no path, as `PUBLIC_URL`.
4. In GitHub Developer Settings create a **GitHub OAuth App**. Homepage URL = actual `PUBLIC_URL`; authorization callback URL = `PUBLIC_URL/auth/callback`. Avoid broad scopes or granting repository write.
5. Populate all variables below in the Railway Variables **secure configuration**, not in Git, shell history, logs, or chat.
6. Deploy and verify `GET /healthz` returns 200 with `service=x-reader-cloud`; `POST /mcp` without Bearer token returns **401**, never tool data. Validate `/.well-known/oauth-authorization-server` discovery.
7. Authenticate an MCP client as the explicitly allowed GitHub login and verify `tools/list` only exposes `read_url`. Repeat as a non-owner and confirm no tool access.
8. Real live X canary: with owner OAuth run `read_url(url="https://x.com/dontbesilent/status/2103875422522077377")`; evidence should indicate post text PASS and attached media PARTIAL unless fetched and consumed; never upgrade unread media to PASS. Compare with local CLI and capture a timestamped JSON receipt. Also test `https://example.com` as general-web baseline. Record exact HTTP response and OAuth login. Restart service and confirm OAuth client registration persists on Volume.

## Railway variables

| Variable | Type | Value or requirement |
| --- | --- | --- |
| `PUBLIC_URL` | config | Actual Railway https origin (never guess the domain) |
| `GITHUB_OAUTH_CLIENT_ID` | config | GitHub OAuth App Client ID |
| `GITHUB_OAUTH_CLIENT_SECRET` | **secret** | GitHub OAuth App Client Secret |
| `X_READER_ALLOWED_GITHUB_LOGIN` | config | `runesleo` for owner-only preview |
| `X_READER_JWT_SIGNING_KEY` | **secret** | New random 48+ character string, persistent across redeploys |
| `X_READER_STORAGE_FERNET_KEY` | **secret** | `Fernet.generate_key().decode()`, persistent; rotating invalidates saved OAuth state |
| `PORT` | managed | Set/injected by Railway; service binds `0.0.0.0:$PORT` |

Keep one replica: the rate budget and the encrypted Volume storage are single-process. Retain Volume and signing/encryption secrets across redeploys. Do not use the local `x-reader-mcp --transport sse --allow-external` entrypoint as a public deployment.

## Endpoints

- `GET /healthz`: unprotected, no secrets, readiness only.
- `POST /mcp`: OAuth-protected MCP Streamable HTTP.
- `GET /.well-known/oauth-authorization-server`: OAuth discovery.
- `GET /auth/callback`: GitHub OAuth callback after user authorization.

## Explicit non-goals and blockers

- No X account OAuth, posting, DMs, private searches, or saved session cookies. GitHub OAuth authenticates the MCP **caller**, not the X account.
- No public anonymous `/api/read` until edge request limits, network egress hardening, and the Evidence Receipt beta gate are independently complete.
- Railway Volume, project, HTTPS domain, OAuth app credentials and live remote X results must all be verified in Railway and a real remote MCP client. GitHub CI alone does **not** prove production deployment.
