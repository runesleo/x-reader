# GitHub PR Source Evidence (advisory)

This optional, read-only source-coverage report uses the EXISTING GitHub
Actions in runesleo/x-reader, and the canonical x_reader.evidence.build_receipt
without modifying that engine or repository branch rules.

## Opt in

Add this exact block to a GitHub PR description:

    <!-- x-reader:evidence -->
    - https://x.com/cursor_ai/status/2110075611633074295
    <!-- /x-reader:evidence -->

The pull_request event runs the job only when the opt-in marker is present.
The report appears in the GitHub Actions job summary, NOT as a PR comment.
No GitHub App, API key, secret, hosted service, queue, scheduler, or notification
provider is added. It is NOT a required check.

Only the explicit links in the PR description (up to three unique HTTPS URLs)
are assessed. Changed Markdown files, other sources in the diff, and factual
correctness are NOT verified. A PASS is an evidence-coverage claim only.
Malformed markers, too many URLs, userinfo credentials and insecure schemes
are rejected before any source read.

Security: the workflow uses pull_request (not pull_request_target), grants
contents:read only, and checkout does NOT persist GitHub credentials.
The existing x-reader subprocess uses a temporary HOME, with no account
session/cookie inheritance. Job output is bounded host labels and reason
codes; full URLs, query strings and fetched content are not echoed.
The PR author can change the check code in their own branch; therefore this
advisory MUST NOT be treated as trustworthy branch-protection evidence.

## Verification

Run: python -m unittest discover -s tests -p test_github_pr_source_evidence_advisory.py -v

A real opt-in PR must be tested on GitHub-hosted Actions before calling this
activated, and its actual job summary must be inspected. The presence of a
GitHub Action check alone is not proof of accurate source reading.
