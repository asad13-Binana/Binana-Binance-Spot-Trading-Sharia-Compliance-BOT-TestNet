#!/usr/bin/env bash
# Strict verification — exits NONZERO unless everything passes:
#  1) image pulls, 2) config validates & strategy loads, 3) strategy is listed.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose pull
echo ">>> Validating config + strategy load (show-config)..."
docker compose run --rm freqtrade show-config \
  --config /freqtrade/user_data/config.json > /dev/null
echo ">>> Checking IctSmcStrategy is discoverable..."
docker compose run --rm freqtrade list-strategies \
  --config /freqtrade/user_data/config.json | grep "IctSmcStrategy" | grep -q " OK "
echo "VERIFY: ALL CHECKS PASSED"
