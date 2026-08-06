# External Validation Runbook — what MUST be done before live money

This build is an **OFFLINE-VERIFIED RELEASE CANDIDATE**. The steps below are NOT done and cannot be done on
the offline audit host. Complete them, in order, before considering any live promotion.

## 1. GitHub CI (matrix + audit + provenance)
Push to a connected repository; confirm the Python 3.10–3.13 matrix, `pip-audit --strict`, deterministic
artifact build, extracted-artifact retest, and Compose image build all pass. Pin base image digests; enable
branch protection + CODEOWNERS. Retain signed image/artifact provenance.

## 2. Enable screening API credit
Provide `SHARIA_OPENAI_API_KEY` + `SHARIA_MODEL` (billed separately from any ChatGPT/Claude subscription).
Run one complete manual `/scan BTC/USDT`; confirm a schema-valid V19.1 result is produced and signed. Until
then the screener fails closed to NO_TRADE_INFO and no coin is trade-eligible.

## 3. Binance Spot Testnet — full order lifecycle
Dedicated Testnet keys (Spot, no withdrawal). Exercise and capture raw REST/WebSocket evidence for: entry;
partial fill; OCO/OTO/OTOCO placement; trailing conversion; break-even/profit-lock; accepted-then-timeout
ambiguity; cancel/replace races; the **modern WebSocket API user-data stream** (`userDataStream.subscribe`;
this replaced the retired listen-key REST endpoints on 2026-02-20 and is unit-tested but NOT yet
integration-validated); restart adoption; and reconciliation. No filled quantity may end unprotected.

## 4. Oracle deployment + soak
Deploy to an A1/AMD Oracle Free Tier host. Run reboot, network-loss, disk-full, OOM, backup/restore, and
rollback drills. Confirm single-instance (flock) behavior. Soak ≥ 14 days with monitoring.

## 5. Exact-strategy backtest + dry-run
Accumulate a timestamped historical rotating top-50 + V19.1 result corpus. Run an exact-strategy Freqtrade
backtest (≥ 100 trades, realistic fees/slippage, out-of-sample/walk-forward). NOTE: every prior backtest of
this strategy lost after fees — a positive result must be treated with suspicion and re-verified for
look-ahead. Then dry-run 1–2 weeks on live data.

## 6. Live-evidence envelope + canary
Only after 1–5: generate and sign `LIVE_EVIDENCE.json` (binds release hash, controller hash, strategy
fingerprints, the exact backtest artifact, and Testnet/Oracle/clean-pass assertions). Set the live markers.
Start with a minimal-value canary on a dedicated sub-account, Spot-only key, withdrawals disabled,
IP-restricted.

## Verdict gate
Do NOT label the system "production-ready" or "live-certified" until every step above is genuinely complete
in a controlled, authorized environment.
