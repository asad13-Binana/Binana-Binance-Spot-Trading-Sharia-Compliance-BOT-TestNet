# Core Strategy and Behavior Fingerprint

**Generated:** 2026-07-15

- Strategy: `IctSmcStrategy`; interface `3 (implicit Freqtrade 2026.6 default; class does not override INTERFACE_VERSION)`
- Timeframes: base `1m`, informative `5m`; startup candles `210`
- Trading: Binance Spot only; no shorting; Freqtrade is permanently signal-only; execution sidecar is the sole order owner.
- Legacy core SHA-256: `70b1d67cc0092b5b8db4a68b343cf893641bde1aae580e9ef51e2adec1062459`
- Core strategy changed: **NO**

## Entry fingerprint
- 5m EMA9>EMA21>EMA50
- close>5m EMA50
- 5m MACD histogram>0
- close>rolling VWAP
- 3-bar EMA9/21 pullback
- close>1m EMA9
- EMA9 rising
- RSI>50 and rising
- RVOL>=1.5
- ADX>20
- volume>0
- explicit current HALAL
- Freqtrade emits signal only; confirm_trade_entry always false

## Exit and protection fingerprint
- structural exit: close<VWAP and 5m MACD histogram<0
- minimal ROI schedule
- hard stoploss
- trailing stop
- execution sidecar owns Binance exits/protection

## Change comparison
All changed files were classified as intended safety corrections, compatibility/deployment corrections, test-only changes, or documentation/evidence changes. No signal-producing strategy AST changed.

## V10.1 fingerprint method update (2026-07-16)

The AST-dump hashes above are superseded. V10.1 pins the same four methods
with interpreter-independent canonical fingerprints (source segment + logical
token stream; `services/common/strategy_fingerprint.py`). Both forms were
verified equal between this release's strategy file and the original supplied
Claude strategy — the methods are byte-identical. Enforced by
`tests/test_v81.py`, `tests/test_v101_consolidation.py`,
`scripts/build_manifest.py`, and `scripts/verify_manifest.py` on
Python 3.10–3.13.
