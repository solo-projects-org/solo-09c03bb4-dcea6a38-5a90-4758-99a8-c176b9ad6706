#!/usr/bin/env bash
# Restart durability check: publish, restart the API container, query again.
# Requires the stack to be up: docker compose up -d
set -euo pipefail

API="${API:-http://127.0.0.1:8000}"
SOURCE="${SOURCE:-http://127.0.0.1:8001}"

echo "== switch source to v1 and publish"
curl -fsS -X POST "$SOURCE/scenario" -H 'content-type: application/json' -d '{"name":"v1"}' >/dev/null
curl -fsS -X POST "$API/api/v1/collect" | python3 -m json.tool

echo "== captured current version before restart"
BEFORE="$(curl -fsS "$API/api/v1/current-version")"
echo "$BEFORE"

echo "== restarting api container"
docker compose restart api >/dev/null
for _ in $(seq 1 30); do
  if curl -fsS "$API/health" >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "== current version after restart"
AFTER="$(curl -fsS "$API/api/v1/current-version")"
echo "$AFTER"

if [ "$BEFORE" != "$AFTER" ]; then
  echo "FAIL: current version changed across restart" >&2
  exit 1
fi

echo "== query after restart"
curl -fsS "$API/api/v1/announcements?stop_id=STOP-001&at=2026-11-17T09:00:00Z" | python3 -m json.tool
echo "PASS: published data survived restart"
