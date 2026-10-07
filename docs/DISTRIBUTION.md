# x-reader distribution plan

This document defines the product/distribution loop for the Evidence Receipt surface. It is a launch contract, not a claim that the hosted beta is already public.

## Product form

x-reader should stay one product with four surfaces:

1. **Open-source core** — trust, forks, issues, contributions, and durable discovery.
2. **Free web utility** — zero-install first success: paste a public URL and get an Evidence Receipt.
3. **Agent integration** — Agent Skill / CLI / MCP for users who want the check inside an existing workflow.
4. **Paid layer later** — hosted API, higher limits, history/team workflow, or managed integrations only after independent demand is observed.

Do not create a separate product, brand, reader, or evidence state machine for each surface. The canonical PASS / PARTIAL / FAIL / UNKNOWN contract remains shared.

## Primary activation event

The product's first-success event is:

> A user who does not need to know Leo pastes a URL and gets a truthful receipt that changes what they are willing to claim from that source.

The web page must make that possible without signup.

## Distribution surfaces

### 1. GitHub → web utility

GitHub is the existing trust surface. README copy should explain the problem in one screen, show the one-line Agent Skill install, and point to the web utility once a stable public URL exists.

Measure: README → web visits and completed reads. Do not invent attribution when it is unavailable.

### 2. Search-intent pages

After a stable domain exists, create small pages that answer concrete jobs rather than generic “AI research” keywords:

- read an X/Twitter post for an AI agent
- verify whether an X post has unread attached media
- X/Twitter URL to machine-readable JSON
- evidence-aware URL reader
- Twitter/X MCP server for agents
- source verification for AI research
- YouTube transcript evidence vs page metadata

Each page should resolve to the same Evidence Receipt product, not a new mini-tool.

### 3. Agent ecosystems

Package and list the existing integration where users already configure agents:

- Agent Skills directories
- MCP directories/registries that accept the current server shape
- developer/agent tool lists where source-verification is a real use case

The listing claim is narrow: x-reader reads supported sources and exposes evidence coverage. Do not claim universal browsing or complete media coverage.

### 4. Output-native sharing

Every web result has three actions:

- Copy receipt
- Copy JSON
- Share

Share text includes the actual status, source URL, evidence sentence, component states, and an x-reader attribution footer. It never upgrades PARTIAL/FAIL/UNKNOWN into PASS.

This is the built-in referral loop: useful output can travel without requiring Leo to post it.

### 5. Content demos

Content should demonstrate failure correction, not advertise features. Default pattern:

1. show the confident-but-wrong agent behavior;
2. paste the same source into x-reader;
3. show the missing layer (for example attached video = PARTIAL);
4. show the corrected claim boundary;
5. link to the free utility / repo.

The already-built Evidence Receipt video is the primary launch asset. Do not create another launch video unless this one fails review.

## What to measure first

Before introducing a paywall, prove independent acquisition.

Minimum evidence to record:

- completed public reads per day;
- acquisition source at an aggregate/privacy-safe level;
- PASS/PARTIAL/FAIL/UNKNOWN mix;
- platform mix;
- p50/p95 request latency;
- 429/timeout/fetch-failure rate;
- share/copy action counts if privacy-safe instrumentation is added;
- GitHub stars/forks/issues are supporting signals, not revenue proof.

The key milestone is not MRR. It is the first verified user who arrives through GitHub/search/agent ecosystem rather than a Leo-owned post and completes a read.

## Monetization gate

Do not add billing merely because the product can accept payments.

Open a paid experiment only after at least one of these demand signals appears:

- repeated API/automation requests from independent users;
- hosted usage high enough that limits/latency/history matter;
- teams ask for shared history, keys, quotas, or support;
- users explicitly ask for a managed version rather than self-hosting.

Likely paid surfaces: API quota, higher limits, durable history, team keys, managed MCP/agent integration. Keep the open-source reader and evidence contract useful on their own.

## Launch order

1. Finish local product surface and tests.
2. Pass the public-beta safety gate in `docs/PUBLIC_BETA_GATE.md`.
3. Push/review the product branch.
4. Create a non-production preview and run the same live X + HTTP parity canary.
5. After explicit approval, expose the stable public beta URL.
6. Update README/distribution listings to the stable URL.
7. Publish one demonstration asset, then let product/GitHub/search carry the long tail.

Public push, deployment, account changes, billing, and social publishing remain separate gated actions.
