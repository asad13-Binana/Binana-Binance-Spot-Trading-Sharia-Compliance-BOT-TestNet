#!/usr/bin/env bash
# Downloads 1m + 5m candles for the pairs in config.json (needed for backtesting).
set -euo pipefail
cd "$(dirname "$0")/.."
TIMERANGE="${1:-20250101-}"
PAIRS=$(python3 -c "import json;d=json.load(open('user_data/halal_list.json'));print(' '.join((a if '/' in a else a+'/USDT') for a in sorted(set(d.get('allowed',[]))-set(d.get('denied',[])))))")
docker compose run --rm -e FREQTRADE__PAIRLISTS='[{"method": "StaticPairList"}]' freqtrade download-data \
  --config /freqtrade/user_data/config.json \
  --timeframes 1m 5m --timerange "$TIMERANGE" --pairs $PAIRS
