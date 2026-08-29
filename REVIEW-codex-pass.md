# Codex Review — MCP compatibility fix

Date: 2026-08-30  
Scope: MCP dependency pin, stdio/SSE startup, regression coverage, CI, README, and changelog  
Source signal: [issue #23](https://github.com/runesleo/x-reader/issues/23)

## Blockers

None open.

## Resolved findings

- **P1 — SSE startup used unsupported `FastMCP.run()` arguments.** The server
  now assigns `host` and `port` through FastMCP settings before calling
  `run(transport="sse")`. A fresh MCP 1.29.1 environment successfully opened a
  loopback SSE listener.
- **P2 — the dependency-contract test required Python 3.11.** The test no
  longer imports `tomllib`, so it remains compatible with the project minimum
  of Python 3.10.
- **P2 — regression tests had no CI execution path.** A read-only GitHub
  Actions workflow now runs the suite on Python 3.10 and 3.11 for pushes to
  `main` and pull requests.

## OK

- Both `mcp` and `all` extras constrain the current FastMCP integration to
  `mcp>=1.0,<2`.
- The English and Chinese setup instructions match the packaging metadata and
  explain the MCP 1.x compatibility boundary.
- The public diff contains no private paths, credentials, internal task IDs, or
  environment-specific defaults.
- The change is limited to the clean-install failure, advertised SSE startup,
  regression coverage, and release documentation.

## Verification

- MCP 2.0 import failure independently reproduced.
- Fresh editable `x-reader[mcp]` install resolved MCP 1.29.1.
- 10/10 unit tests passed with the MCP extra installed.
- Real loopback SSE startup passed.
- Python compile check, workflow YAML parse, and `git diff --check` passed.

