#!/bin/sh
# Runs as root only long enough to make the data directory writable by the
# unprivileged runtime user (named volumes are often created root-owned), then
# drops privileges before starting the server.
set -eu

DATA_DIR="$(dirname "${DATABASE_PATH:-/srv/data/transit.db}")"
mkdir -p "$DATA_DIR"
chown -R appuser:appuser "$DATA_DIR"

exec gosu appuser "$@"
