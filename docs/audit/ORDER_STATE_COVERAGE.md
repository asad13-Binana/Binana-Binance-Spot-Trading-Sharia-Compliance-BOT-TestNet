# Binance Order-State Coverage

## Covered offline

- Deterministic signal and command deduplication with pre-side-effect durable claims.
- Fixed OCO/OTOCO request construction, trailing-only construction, and filter prevalidation.
- Entry partial-fill accumulation and canceled-partial ownership state.
- Distinct entry versus protective-sell terminal semantics.
- `executionReport` and `ListStatus` deduplication and state advancement.
- Unknown accepted replacement outcome: no blind duplicate protection; reconciliation required.
- Restart behavior for `IN_PROGRESS` commands/signals: no automatic replay.
- Protection mode/protected quantity preservation during mirror/reconciliation.
- Future/stale universe and signal fail-closed behavior.

## Not proven without Binance Spot Testnet

- Actual OTO, OTOCO, OCO and trailing endpoint acceptance for the exact artifact.
- Fill during cancel/replace, accepted REST timeout, partial-fill commission effects, list-leg event ordering, user-stream disconnect/reconnect, and restart with open exchange lists.
- No production API key or real order was used.

**Readiness effect:** these are capital-safety paths, so protocol clean-pass count remains `0`.
