# Hosted deployment

The files here are the configuration the public Evidence Receipt beta runs with
(hostnames replaced by placeholders). The reader code enforces the per-request
limits; these files add the service-wide and network-level ones.

| File | Installed as | What it enforces |
|---|---|---|
| `x-reader-evidence.service` | `/etc/systemd/system/` | Unprivileged user, read-only system, private `/tmp`, no capabilities; memory 512 MiB, CPU 80 % and 64 tasks for all requests together; 512 open files per process (each read lowers its own limit to 256) |
| `xreader-egress.nft` + `xreader-egress.service` | `/etc/nftables.d/`, `/etc/systemd/system/` | Sockets owned by the service user cannot reach loopback, private, CGNAT/tailnet, link-local/metadata, multicast or class E addresses; the reader's own address check refuses the other non-public ranges |
| `nginx-xreader.conf` | nginx site | TLS, 8 KiB bodies, 12 reads/minute and 2 concurrent reads per client IP, full request buffering, JSON error bodies |

Per request, in code (`x_reader/web_app.py`, `x_reader/utils/proc.py`):

- the reader runs as one child process with a minimal environment, a temporary HOME
  (deleted afterwards) and `X_READER_HOSTED=1`: no `.env`, no inbox or Markdown output,
  only the X / YouTube / generic readers, and no external tools (yt-dlp, ffmpeg, browsers);
- 75 s wall-clock deadline and 8 MiB output cap, both enforced while it runs; on exit,
  timeout or overflow its whole process group is killed;
- ceilings on that process: 768 MiB address space, 120 CPU-seconds, 64 MiB per written
  file, 256 open files, no core dumps; it is also the first OOM-kill candidate;
- at most 3 concurrent reads and 24 connection threads; a request not fully read
  within 20 s is cut off.

## Optional private aggregate usage (not enabled by this source change)

The existing hosted HTTP server can count product-page GETs and `/api/read`
responses in **daily aggregate buckets** when the service is explicitly
configured with an absolute `X_READER_USAGE_DB` path. Without the setting, no
metrics database is created and request behavior is unchanged. On an approved
production rollout, the existing `StateDirectory=xreader` is the appropriate
writable location: `Environment=X_READER_USAGE_DB=/var/lib/xreader/usage-aggregates.sqlite3`.
Do not enable or deploy this in a code-review-only task.

- Counts include page requests, HTTP errors including 429, PASS/PARTIAL/FAIL/UNKNOWN
  read outcomes, source category and coarse latency buckets. They are **not**
  unique users, installs or human-only visits; bots may be counted. The first
  observation starts when enabled; no historical backfill is inferred.
- Only the calendar day and enumerated aggregate categories/counts/latency sums
  are saved. Never save submitted URLs, query strings, IPs, user agents,
  referrers, content, cookies, account IDs or per-request records.
- The SQLite file must be private (0600) inside an existing writable directory.
  The active retention window is **90 UTC calendar dates**: today plus the
  preceding 89 dates. Ordinary writes prune older rows; in addition, the
  existing HTTP server's `serve_forever()` lifecycle attempts cleanup on
  startup and once per hour **even with no product requests**. There is no
  additional scheduler, process, public metrics endpoint or tracking cookie.
- **This is not an unconditional 90-day deletion deadline.** When the service
  is stopped, or metrics are disabled and the database remains on disk, no
  automatic cleanup runs. A busy/locked database can also skip a cleanup
  attempt; the running server retries on its next hourly pass. Before turning
  metrics off or decommissioning the service, an operator must explicitly
  prune or remove the existing private database and verify the result. Any
  separately retained backups need their own retention handling. Do not claim
  an unqualified maximum 90-day storage period without independently verified
  cleanup coverage while the service is offline.
- Failures of the optional writer and maintenance must not block source
  reading. A locked/busy database causes that metric observation to be
  skipped immediately (zero SQLite busy timeout), so totals are best-effort
  and may undercount under storage contention; do not label them exact visits
  or unique people.
- For an operator-only read after a separately authorized deployment:

  ```bash
  python3 -m x_reader.usage_metrics --db /var/lib/xreader/usage-aggregates.sqlite3 --days 14 --json
  ```

- To delete expired rows explicitly, including after disabling the telemetry
  environment variable, run (as the authorized database owner):

  ```bash
  python3 -m x_reader.usage_metrics --db /var/lib/xreader/usage-aggregates.sqlite3 --prune-expired --json
  ```

  This does not create a missing database. Check the exit status and
  `pruned_rows`, and inspect the stored date bounds rather than mistaking
  the filtered `--days` summary for proof of physical row deletion.
- **Production enablement gate:** approval and separate deployment are still
  required. Verify on the installed service that startup/idle cleanup works
  without traffic, document who prunes or erases the database after shutdown
  or opt-out, and account for backup copies before making any hard deletion
  promise. The current source change alone does not prove production is
  enabled, or that offline deletion is guaranteed.

The result is an aggregate histogram. p50/p95 can only be estimated to the
reported bucket; exact latency percentiles and unique visitor counts are
**not** available without additional instrumentation and review.

## Verify a deployment

```bash
curl -fsS https://<host>/healthz
systemctl show x-reader-evidence -p MemoryMax -p TasksMax -p CPUQuotaPerSecUSec -p LimitNOFILE
sudo nft list table inet xreader_egress
# from the service account, internal destinations must be refused:
sudo -u xreader curl -m 3 -sS http://169.254.169.254/ || echo "blocked as expected"
```
