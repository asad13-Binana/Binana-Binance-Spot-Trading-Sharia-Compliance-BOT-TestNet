# Archived Freqtrade V7 component — signal engine only

This directory is retained to preserve the supplied V7 strategy, audit probes, and offline backtest helpers. It is **not** a standalone deployment of V8.1.

In the merged release:

- `IctSmcStrategy` emits closed-candle signal files.
- `confirm_trade_entry()` always returns `False`; Freqtrade must not own Binance orders.
- `services/execution_sidecar` is the sole execution owner.
- The root `docker-compose.yml`, root `.env.example`, root `deploy/`, and `docs/GITHUB_ORACLE_DEPLOYMENT.md` are the only supported runtime/deployment path.
- The files under `freqtrade/deploy/` and `freqtrade/scripts/start.sh` are intentionally fail-closed compatibility stubs. They must not be installed or enabled.
- `freqtrade/docker-compose.yml` is restricted to the `offline-audit` profile for config validation, data download, and backtesting only.

Do not copy private credentials into this directory. Do not run this subtree as an independent bot.
