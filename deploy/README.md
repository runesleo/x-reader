# Hosted deployment

The files here are the configuration the public Evidence Receipt beta runs with
(hostnames replaced by placeholders). The reader code enforces the per-request
limits; these files add the service-wide and network-level ones.

| File | Installed as | What it enforces |
|---|---|---|
| `x-reader-evidence.service` | `/etc/systemd/system/` | Unprivileged user, read-only system, private `/tmp`, no capabilities; memory 512 MiB, CPU 80 %, 64 tasks and 512 open files for all requests together |
| `xreader-egress.nft` + `xreader-egress.service` | `/etc/nftables.d/`, `/etc/systemd/system/` | Sockets owned by the service user may reach public addresses only (loopback, private, CGNAT/tailnet, link-local/metadata, multicast and reserved ranges are rejected) |
| `nginx-xreader.conf` | nginx site | TLS, 8 KiB bodies, 12 reads/minute and 2 concurrent reads per client IP, full request buffering, JSON error bodies |

Per request, in code (`x_reader/web_app.py`, `x_reader/utils/proc.py`):

- the reader runs as one child process with a minimal environment, a temporary HOME
  (deleted afterwards) and `X_READER_HOSTED=1`: no `.env`, no inbox or Markdown output,
  only the X / YouTube / generic readers, and no external tools (yt-dlp, ffmpeg, browsers);
- 75 s wall-clock deadline and 8 MiB output cap, both enforced while it runs; on exit,
  timeout or overflow its whole process group is killed;
- ceilings on that process: 768 MiB address space, 120 CPU-seconds, 64 MiB per written
  file, 256 open files, no core dumps; it is also the first OOM-kill candidate;
- at most 4 concurrent reads and 24 connection threads; a request not fully read
  within 20 s is cut off.

## Verify a deployment

```bash
curl -fsS https://<host>/healthz
systemctl show x-reader-evidence -p MemoryMax -p TasksMax -p CPUQuotaPerSecUSec -p LimitNOFILE
sudo nft list table inet xreader_egress
# from the service account, internal destinations must be refused:
sudo -u xreader curl -m 3 -sS http://169.254.169.254/ || echo "blocked as expected"
```
