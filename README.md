# BINANA Binance Spot Testnet

Latest update (October 8, 2026): **AWS Binance Spot Testnet trading is resumed.** The verified owner is running with no open recovery incident and no Resume blockers. Acceptance includes protected entry/exit, repeated restarts, flat database restore, ten runs of the 164-test owner suite, one/four-position ten-minute soaks and an actual deployment/rollback rehearsal. The manual registry contains **375 owner-listed symbols**; research loads the exact supplied Sharia v19.3 controller.

The active AWS deployment uses Freqtrade 2026.8/NFI as its sole order owner, with a monitor-only sidecar. The root package below still represents the older deployment layout; it must not be used to overwrite AWS until runtime integration is complete. Recovered owner source is in `runtime_owner/`.

See [current status and recent changes](docs/recovery/RECOVERY_STATUS_20261007.md) and [the additive registry record](docs/recovery/REGISTRY_ADDITIONS_20260930.json). Historical deployment and validation notes below do not certify the current candidate.

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

## Packaged architecture (legacy deployment)

Six Docker services provide the top-50 USDT universe, governed Sharia egress,
manual registry projection and v19.3 research screening, signal-only Freqtrade, the single
order-owning execution sidecar, and owner-only Telegram control. A separate
`botmon` systemd service observes
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
# Install each hash-locked runtime closure separately from unhashed test tools.
pip install --require-hashes -r requirements.services.lock
pip install --require-hashes -r monitoring/requirements-monitoring.lock
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
- On AWS, Freqtrade/NFI owns orders and the execution sidecar only monitors.
  The legacy root Compose package has a different topology and is not the AWS installer.
- Research decisions require the supplied v19.3 controller and signed owner approval; the operational manual registry is a separate gate.
- Inter-service messages are HMAC-authenticated and release-bound.
- BTC, BNB, XRP, SOL and ETH are excluded as execution bases by the recovered owner; manual registry membership does not override execution exclusions. There is no BNB fee dependency.
- AWS trading secrets remain in private server configuration, never in Git.

## Documentation

Start with `docs/ORACLE_DEPLOYMENT_GUIDE.md`,
`docs/GITHUB_ORACLE_DEPLOYMENT.md`,
`docs/GITHUB_RELEASE_AND_ROLLBACK_GUIDE.md`,
`docs/SECURITY_AND_SECRETS_GUIDE.md`,
`docs/SHARIA_LOCAL_SCREENING.md`,
`docs/TELEGRAM_BOT_SETUP.md`,
`docs/CODEX_RELEASE_NOTES_2026-08-09.md`,
`docs/CODEX_RELEASE_NOTES_2026-08-08.md`,
`docs/EXTERNAL_VALIDATION_RUNBOOK.md`,
`docs/OFFICIAL_DEPLOYMENT_REFERENCES.md`, and `monitoring/README.md`.

The strategy has not proven an edge: prior backtests lost after fees. Testnet,
backtest gates, Oracle soak, and independent re-audit remain mandatory before
live promotion.
