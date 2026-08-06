# Strategy Notes — IctSmcStrategy (authoritative behavior)

This note is the authoritative description of the ACTIVE entry/exit behavior
of `freqtrade/user_data/strategies/IctSmcStrategy.py`. It exists to resolve
deep-audit finding F-05 (a 1-minute MACD "soft boost" that some descriptions
implied was active). **The strategy source is not changed by this note** — it
already carries an inline comment stating the same thing. Where any older
prose (e.g. marketing-style summaries) disagreed, this note governs.

## Timeframes

- Base timeframe: `1m`.
- Informative timeframe: `5m` (merged via the `@informative("5m")` decorator).

## Active entry conditions (all must hold)

- 5m EMA stack bullish: `EMA9 > EMA21 > EMA50`, close above 5m EMA50.
- **5m MACD histogram > 0 — HARD GATE** (`macdhist_5m > 0`, MACD 12/26/9 on 5m).
- 1m price above rolling VWAP.
- 1m EMA9/21 3-bar pullback and reclaim; close above 1m EMA9; EMA9 rising.
- RSI(14) > 50 and rising.
- RVOL ≥ 1.5; ADX > 20; volume > 0.
- Pair is currently V19.1 `GREEN` (explicit Sharia gate, fail-closed).
- Freqtrade emits the signal only; `confirm_trade_entry` always returns
  `False`, so the execution sidecar owns every real order.

## The 1-minute MACD (5/13/6) — REFERENCE ONLY, by design

The strategy computes a fast 1-minute MACD (fastperiod=5, slowperiod=13,
signalperiod=6) and stores `macdhist`. It is labelled a "soft confirmation /
boost" in the source, **but it is intentionally NOT an entry condition**: the
`(dataframe["macdhist"] > 0)` line is deliberately commented out in
`populate_entry_trend`, and the emitted signal payload does not carry the
1-minute MACD histogram. The downstream execution sidecar therefore never
re-scores it.

Consequences you can rely on:

- Only the **5m** MACD histogram gates entries. Forcing it negative blocks
  every entry; forcing it positive permits entries (pinned by
  `tests/test_strategy_probe.py::test_1m_macd_is_a_hard_gate_in_the_entry_path`).
- The 1m MACD is available for analysis/telemetry but changes no trade
  selection.

### If you ever want the 1m MACD to actually influence entries

That is a **core-strategy change** and must be an explicit, approved
decision — not an incidental edit. The safe way to do it (option B from the
audit) is a deterministic, testable soft-score that adds to a rank without
becoming a hidden hard gate, with its own regression tests and a refreshed
strategy fingerprint. Do not enable it silently before a deployment. As
shipped, the reference-only behavior above is the approved design.

## Active exit conditions

- Structural exit: close < VWAP and 5m MACD histogram < 0.
- Minimal-ROI schedule, hard stop-loss, and trailing stop (see
  `docs/audit/CORE_STRATEGY_FINGERPRINT.json`).
- The execution sidecar owns Binance-side exits/protection (OCO/OTO/trailing).

## Preservation

The four signal-producing methods (`populate_indicators_5m`,
`populate_indicators`, `populate_entry_trend`, `populate_exit_trend`) are
fingerprint-pinned in `RELEASE_MANIFEST.json`. Any change to them changes the
fingerprints and fails the release gate — that is the intended guardrail
against silent strategy drift.
