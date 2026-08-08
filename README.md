# Binance Spot / Freqtrade V10.2 + Sharia V19.1

Status: **offline release gate passed; testnet/Oracle runtime validation
incomplete; live trading not certified.** `RELEASE_MODE` identifies the
testnet or live-capable package — and is enforced at runtime: the testnet
package is structurally incapable of live execution. The live package
defaults to simulation and cannot place a live order without its signed
evidence gate. AI Sharia screening is research, not a fatwa.

The release version lives in `RELEASE_VERSION`. The `v101` operational
namespace (server paths such as `/etc/binance-freqtrade-v101`, image and
compose names) is deliberately retained for upgrade compatibility.

## Disclaimer and risk warning

This software can place orders on a cryptocurrency exchange. Trading carries
substantial risk, including total loss of funds.

  * Not financial, investment, tax or legal advice.
  * No profitability claim. The strategy has **not** been validated as
    profitable after fees and slippage.
  * The authors accept no liability for any financial loss arising from use
    of this software.
  * You are solely responsible for your API keys, capital, risk limits and
    legal/tax compliance.
  * Sharia screening output is automated research assistance, **not a fatwa**.

Use the Binance Spot Testnet first. Deploy real capital only after your own
independent validation, and only with money you can afford to lose entirely.

See `LICENSE` for the full disclaimer.

## Architecture

Five Docker services provide the top-50 USDT universe, immutable V19.1 Sharia
screening, signal-only Freqtrade, the single order-owning execution sidecar,
and owner-only Telegram control. A separate `botmon` systemd service observes
authoritative execution state without trading credentials or Docker-socket
access. See `ARCHITECTURE.md`.

The universe scan can optionally enrich its output with free-tier CoinGecko
and CoinMarketCap data (trending flags, market caps, an optional
cross-verified market-cap floor). The feature is advisory-only, disabled by
default, and hard-capped 4% under the documented free quotas — overrides
clamp to the 96% ceiling, throttle state survives restarts, corrupt quota
ledgers fail closed, and any provider problem degrades to Binance-only
scanning. See `docs/EXTERNAL_SIGNALS.md`.

## Verification

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
bash deploy/verify_release.sh
```

The release gate runs the core unittest suite, the monitoring pytest suite,
33 legacy self-tests, source/controller integrity, secret scanning, systemd
validation, structured-file checks, and non-mutating exact-manifest
verification. GitHub CI additionally builds the images and verifies a freshly
extracted deterministic artifact.

## Safety invariants

- Testnet must be deployed first; live requires matching release markers and a
  signed live-evidence envelope.
- Freqtrade is signal-only; the execution sidecar is the only Binance order
  owner.
- Only V19.1 `GREEN` / `GREEN_AVOID_OPTIONAL` results are trade-eligible.
- Inter-service messages are HMAC-authenticated and release-bound.
- BNB and BTC are excluded as bases; no BNB fee dependency.
- Trading secrets exist only in Oracle's mode-600 private env, never in Git.

## Documentation

Start with `docs/ORACLE_DEPLOYMENT_GUIDE.md`,
`docs/GITHUB_ORACLE_DEPLOYMENT.md`,
`docs/GITHUB_RELEASE_AND_ROLLBACK_GUIDE.md`,
`docs/SECURITY_AND_SECRETS_GUIDE.md`,
`docs/TELEGRAM_BOT_SETUP.md`,
`docs/CODEX_RELEASE_NOTES_2026-08-08.md`,
`docs/EXTERNAL_VALIDATION_RUNBOOK.md`,
`docs/OFFICIAL_DEPLOYMENT_REFERENCES.md`, and `monitoring/README.md`.

The strategy has not proven an edge: prior backtests lost after fees. Testnet,
backtest gates, Oracle soak, and independent re-audit remain mandatory before
live promotion.
