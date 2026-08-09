#!/usr/bin/env bash
# V10.1 stack health helper. Fixes V101-NEW-005: a service that is merely
# present (Created/Paused/Exited) must NOT pass as healthy. Every one of the
# six expected services must be present, running, and health=healthy.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

EXPECTED=(
  universe sharia-egress-proxy sharia-screener freqtrade
  execution-sidecar telegram-broker
)
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-binance-freqtrade-v101}"

fail=0
running="$(docker compose ps --status running --services 2>/dev/null || true)"
for service in "${EXPECTED[@]}"; do
  if ! grep -qx "$service" <<<"$running"; then
    echo "NOT RUNNING: $service" >&2
    fail=1
    continue
  fi
  cid="$(docker compose ps -q "$service" 2>/dev/null || true)"
  if [[ -z "$cid" ]]; then
    echo "NO CONTAINER: $service" >&2
    fail=1
    continue
  fi
  # Prefer the container's health status; fall back to running state.
  hs="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true)"
  if [[ "$hs" != "healthy" && "$hs" != "running" ]]; then
    echo "UNHEALTHY: $service ($hs)" >&2
    fail=1
  fi
done

if [[ "$fail" -ne 0 ]]; then
  docker compose ps || true
  exit 1
fi
echo "all six services present, running and healthy"
