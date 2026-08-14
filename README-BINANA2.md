# Binana 2.0 — fresh rebuild foundation

This directory is the new modular-monolith implementation path. It is intentionally
separate from the existing Freqtrade/microservice stack while Testnet parity is
being established.

## Safety posture

- Binance Spot only; native REST adapter, no CCXT on the critical order path.
- Fresh databases start with a durable global entry pause.
- Order intent is persisted before the network submission begins.
- A timeout/5xx/-1007 during an execution-sensitive request becomes
  `ENTRY_UNKNOWN`; the same client order ID is reconciled instead of blindly
  submitting another order.
- V19.1 is local and fail-closed. Only current `GREEN` or
  `GREEN_AVOID_OPTIONAL` records in schema version 2 are tradeable. Missing,
  corrupt, stale, duplicate, conflicting or unknown records are no-trade.
- SQLite/WAL is the authoritative safety-state store. No JSON sidecar is used
  for pauses, halts, recovery intents or order lifecycle state.
- `ENTRIES_ENABLED=false` is the default and cannot silently clear a durable
  SQLite pause.

## Strategy parity

`binana2/strategy/v1_original.py` ports the current protected signal shape:
5m EMA9>EMA21>EMA50 + positive MACD(12,26,9) histogram, with the existing 1m
VWAP pullback/reclaim, RSI>50 and rising, RVOL>=1.5, ADX>20 and EMA9-rising
conditions. The 1m MACD(5,13,6) remains reference-only, as in the current bot.

This port is not declared behaviourally certified until golden-vector parity is
run against the existing Freqtrade/TA-Lib implementation on identical closed
candles. Entries therefore remain disabled during this foundation stage.

## Run tests

```bash
PYTHONPATH=. pytest -q binana2/tests
```

## Run locally

```bash
cp .env.binana2.example .env.binana2
set -a; . ./.env.binana2; set +a
python -m binana2.app.main
```

## Certification gates before live money

1. Golden-vector strategy parity with the protected current strategy.
2. Full in-process relocation/parity of the V19.1 controller/screener pipeline.
3. Binance Spot Testnet authenticated lifecycle suite, including partial fills,
   ambiguous submissions, cancel/fill races, OCO/OTO/OTOCO and restart recovery.
4. User Data Stream reconnect/replay/out-of-order tests.
5. Protection lifecycle and protection-gap instrumentation/certification.
6. Telegram command bus/idempotency implementation and tests.
7. Oracle VM reboot/network/DNS/disk/clock fault programme and soak.
8. Tiny-capital live certification before normal live sizing.

This is an engineering implementation, not a fatwa or independent Sharia ruling.
