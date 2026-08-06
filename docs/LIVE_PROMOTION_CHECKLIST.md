# Live promotion checklist (V10.1)

Adapted from the V10 donor package and aligned with the final verdict
(`BINANCE_BOT_FINAL_VERDICT_IMP.md`). Live trading stays blocked until every
item has recorded evidence. Any code, dependency, configuration, or manifest
change resets all promotion evidence.

## Stage 0 — pristine offline verification (repeat 3x)
- [ ] `deploy/verify_release.sh` passes from a pristine artifact extraction
      (all tests, legacy 33/33, secret scan, Sharia schema, manifest).
- [ ] GitHub Actions green on Python 3.10, 3.11, 3.12, and 3.13.
- [ ] `pip-audit -r requirements.services.txt --strict` clean.
- [ ] Three consecutive identical clean passes recorded with artifact hash.

## Stage A — offline fault-injection evidence
- [ ] Crash before/after send; timeout-after-accept; duplicate callbacks;
      user-stream disconnect; restart with open OCO; stale universe/Sharia;
      same-candle re-entry; cancel-and-replace failure; Telegram replay.
      (Covered by tests/; record the run output.)

## Stage B — Binance Spot Testnet (sidecar)
- [ ] Entry order, OCO, OTOCO, trailing contingent leg accepted and filled.
- [ ] Partial fill, cancellation, rejected leg, and accepted-timeout handled.
- [ ] executionReport + listStatus consumed; restart reconciliation adopts
      open orders; no duplicate order after ambiguity.
- [ ] Save request IDs, client IDs, user-stream events, and final balances.

## Stage C — Oracle dry-run soak
- [ ] Exact checksum-addressed artifact installed via install_artifact.sh.
- [ ] Freqtrade dry-run + sidecar simulation on real market data >= 14 days.
- [ ] Restart recovery, backup, pause/reconcile, upgrade, failed-upgrade
      rollback, Telegram access control each demonstrated once.
- [ ] Resource usage stable within Always-Free limits (no swap thrashing).

## Stage D — controlled live consideration
- [ ] Every open position protected; restarts preserve order ownership.
- [ ] Unknown order status produces no duplicate order (verified on testnet).
- [ ] Three consecutive full audit passes on the exact live artifact.
- [ ] Binance key: dedicated sub-account, Spot-only, withdrawals disabled,
      IP-restricted to the Oracle host; BNB fee discount OFF.
- [ ] `EXECUTION_MODE=live` requires SIDECAR_RELEASE_HASH + SIDECAR_LIVE_OK
      to match the installed RELEASE_SHA256.txt; AUTO_CONFIRM must be false.
- [ ] Start with one symbol, minimal stake, and an independently tested
      emergency pause and exit path.
