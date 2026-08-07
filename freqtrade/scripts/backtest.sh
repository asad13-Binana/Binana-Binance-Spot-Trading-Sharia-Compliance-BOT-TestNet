#!/usr/bin/env bash
# THE ENFORCED GATE. Runs the backtest with protections + fees, then PARSES the
# result and FAILS (nonzero exit) unless minimum acceptance criteria are met:
#   profit_factor > 1.15  AND  trades >= 100  AND  profit_total > 0
#   AND max_drawdown_account < 20% (missing drawdown = fail-closed)
# Passing here is NECESSARY, not sufficient: re-run on a later out-of-sample
# timerange you did not tune on, then dry-run for 1-2 weeks before any live step.
set -euo pipefail
cd "$(dirname "$0")/.."
TIMERANGE="${1:-20250101-}"
PAIRS=$(python3 -c "import json;d=json.load(open('user_data/halal_list.json'));print(' '.join((a if '/' in a else a+'/USDT') for a in sorted(set(d.get('allowed',[]))-set(d.get('denied',[])))))")
docker compose run --rm -e FREQTRADE__PAIRLISTS='[{"method": "StaticPairList"}]' freqtrade backtesting \
  --config /freqtrade/user_data/config.json \
  --strategy IctSmcStrategy \
  --timeframe 1m --timerange "$TIMERANGE" --fee 0.001 --pairs $PAIRS \
  --enable-protections --export trades

python3 - << 'PYGATE'
import json, sys, zipfile, pathlib
res_dir = pathlib.Path("user_data/backtest_results")
try:
    latest = json.loads((res_dir / ".last_result.json").read_text())["latest_backtest"]
    p = res_dir / latest
    if p.suffix == ".zip":
        with zipfile.ZipFile(p) as z:
            name = [n for n in z.namelist() if n.endswith(".json") and "config" not in n][0]
            data = json.loads(z.read(name))
    else:
        data = json.loads(p.read_text())
    stats = data["strategy"]["IctSmcStrategy"]
    pf = stats.get("profit_factor")
    trades = stats.get("total_trades", 0)
    profit = stats.get("profit_total", 0)
    dd = stats.get("max_drawdown_account", None)
    print("\n================ BACKTEST GATE ================")
    print(f"  trades         : {trades}")
    print(f"  profit_total   : {profit:.4%}" if isinstance(profit,(int,float)) else f"  profit_total   : {profit}")
    print(f"  profit_factor  : {pf}")
    print(f"  max_dd_account : {dd}")
    dd_ok = isinstance(dd,(int,float)) and dd < 0.20
    if dd is None:
        print("  NOTE: max_drawdown_account MISSING -> fail-closed")
    ok = (pf is not None and pf > 1.15) and trades >= 100 and (profit or 0) > 0 and dd_ok
    print(f"  VERDICT        : {'PASS — proceed to OUT-OF-SAMPLE retest, then dry-run' if ok else 'FAIL — DO NOT GO LIVE'}")
    print("===============================================\n")
    sys.exit(0 if ok else 1)
except SystemExit:
    raise
except Exception as e:
    print(f"\nBACKTEST GATE: could not parse results ({e}) — FAIL-CLOSED.\n")
    sys.exit(1)
PYGATE
