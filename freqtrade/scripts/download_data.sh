#!/usr/bin/env bash
# Downloads 1m + 5m candles for the pairs in config.json (needed for backtesting).
set -euo pipefail
cd "$(dirname "$0")/.."
TIMERANGE="${1:-20250101-}"
PAIR_FILE=$(mktemp)
trap 'rm -f -- "$PAIR_FILE"' EXIT
python3 ../services/common/backtest_pairs.py \
  --compat-file "${SHARIA_COMPAT_FILE:-../shared/sharia/halal_coins.json}" \
  >"$PAIR_FILE"
mapfile -t PAIRS <"$PAIR_FILE"
(( ${#PAIRS[@]} > 0 )) || {
  echo 'DOWNLOAD PAIR GATE BLOCKED: pair resolver returned no pairs' >&2
  exit 2
}
docker compose run --rm -e FREQTRADE__PAIRLISTS='[{"method": "StaticPairList"}]' freqtrade download-data \
  --config /freqtrade/user_data/config.json \
  --timeframes 1m 5m --timerange "$TIMERANGE" --pairs "${PAIRS[@]}"
