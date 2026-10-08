#!/bin/sh
set -eu
umask 077

# Fail closed if Railway Volume /data was not attached.
if ! mountpoint -q /data; then
  echo "ERROR: Railway persistent Volume is required at /data" >&2
  exit 78
fi
# Volume mount points are root-owned. Provision the private OAuth directory,
# then immediately drop privileges for the actual network process.
install -d -m 700 -o xreader -g xreader /data/oauth
install -d -m 700 -o xreader -g xreader /data/.local/share
chown xreader:xreader /data
exec gosu xreader python -m x_reader.cloud_mcp
