# V8.1 merge and audit report

**Assessment date:** 15 July 2026  
**Release:** `V8.1-MERGED`  
**Default mode:** simulation  
**Production-live certification:** **NO**

## Executive verdict

This repaired package is a **simulation / controlled-testnet candidate** with readiness `BLOCKED — VALIDATION INCOMPLETE`, not a certified live-trading release. The two supplied bots are merged through explicit service boundaries rather than by splicing signal code into the execution engine.

- The supplied Binance V4.9.16 source is preserved byte-for-byte.
- The supplied Freqtrade V7 indicator, entry, and exit method ASTs are preserved.
- Freqtrade is permanently signal-only in this release; it returns `False` from `confirm_trade_entry`.
- The execution sidecar is the only component permitted to own Binance orders.
- Live mode is blocked by the original core interlock and a sidecar gate requiring the installed release hash, private environment hash, and persistent approval marker to match.

## Input evidence

| Input | SHA-256 |
|---|---|
| `binance_bot_V4.9.16_COMPLETE-1.zip` | `e89c4025ce423bcaea67be226dbc7243d929c2da7a7e5e3a44152ab570fef41a` |
| `ict_smc_freqtrade_DEPLOY_v7_MERGED-2.zip` | `e4812f3af69a1c55a633861fbbd243e1c612161770c5c7cad033842aff1a54d5` |
| preserved V4.9.16 Python core | `70b1d67cc0092b5b8db4a68b343cf893641bde1aae580e9ef51e2adec1062459` |

## Merge boundaries

### Preserved Binance core

`legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py` remains the supplied 9,387-line source. It continues to provide the scanner logic, broker wrapper, deterministic order IDs, query-before-retry behavior, OTOCO implementation, portfolio persistence, user-data stream, partial-fill handling, protection recovery, daily guards, and original menu/self-tests.

### Preserved Freqtrade strategy

The following V7 methods retain their original AST hashes:

- `populate_indicators_5m`
- `populate_indicators`
- `populate_entry_trend`
- `populate_exit_trend`

Only operational hooks were replaced: Sharia lookup, signal-file emission, startup notice, and the hard signal-only `confirm_trade_entry=False` gate.

### New V8.1 services

- `services/execution_sidecar` — single order owner, three protection modes, SQLite lifecycle/event state, reconciliation, commission-asset recording, fresh-signal and stop-out guards.
- `services/universe_service` — reproducible top-50 snapshots, active Spot/USDT filtering, BTC/BNB/stablecoin/leveraged-token exclusion, listing-age cache, volume/spread/filter checks, explicit Sharia gate.
- `services/telegram_broker` — one owner interface, original controls plus V8.1 controls, one-time confirmation tokens, duplicate command rejection, audit logging.

## Order and protection modes

| Mode | Entry structure | Post-entry structure |
|---|---|---|
| `FIXED_OCO` | OTOCO | take-profit `LIMIT_MAKER` + fixed `STOP_LOSS_LIMIT` |
| `TRAILING_ONLY` | OTO | pending trailing `STOP_LOSS_LIMIT` |
| `OCO_TRAILING` | OTOCO | take-profit `LIMIT_MAKER` + trailing `STOP_LOSS_LIMIT` contingent leg |

Every request uses symbol tick/step/notional/trailing filters from the preserved broker path. Deterministic client IDs and query-before-retry are retained.

## Persistent state

`execution_state.sqlite` stores:

- trade/pair and lifecycle state
- entry client/order ID
- order-list, take-profit, and stop IDs
- filled/protected quantity and average entry
- protection mode and trailing delta
- actual commission asset
- last exchange event and reconciliation status
- processed signal and command IDs
- raw `executionReport` and `ListStatus` events

## Universe and Sharia behavior

Filtering order:

`active Spot → USDT quote → exclude BTC/BNB → exclude stablecoins/leveraged tokens → require OCO/OTO/trailing support → explicit current HALAL → minimum listing age → positive gainer → volume → spread → price/lot/notional/trailing filters → rank strongest 50`

The explicit source of truth is `shared/sharia/sharia_status.json`. Only `HALAL` records with a valid future expiry pass. `UNKNOWN`, `DOUBTFUL`, `HARAM`, `STALE`, malformed, missing, or expired records block entry. A compatibility whitelist for the preserved core is generated automatically from the explicit file.

The included seed contains only the four symbols migrated from the supplied approved list. It does **not** fabricate approvals to fill 50 slots. Until more independently reviewed current `HALAL` records are added, the practical universe can be much smaller than 50.

## Re-entry controls

After a stop-loss event, the sidecar records the exchange event time and applies:

- a required newer closed-candle timestamp
- a new deterministic signal token
- current-universe and exact snapshot-hash match
- current explicit Sharia approval
- signal-age limit
- pair cooldown
- maximum stop-outs per pair/day
- global daily stop-out guard
- duplicate signal rejection

## Telegram controls

Original operational controls retained or represented:

- start/resume, stop/pause, status
- balance and profit
- last signal
- error/log view
- restart user stream
- settings
- self-test/backtest status
- trade size and maximum slots
- reload configuration
- emergency exit

Added controls:

- fixed OCO, trailing only, OCO + trailing
- convert protection
- break-even and profit-lock
- protection status and reconciliation
- universe and Sharia status
- deployment status

Owner authorization, one-time callback tokens, expiry, replay rejection, command IDs, and audit logs are enforced. Actions that enable trading or alter active protection require confirmation.

## Verification results

### V4.9.16 source suite

**33/33 passed.** Evidence: `docs/EVIDENCE_HISTORY.md` (raw logs retained
outside the release package per the packaging policy).

### V7 source audit probes

- interlock and Sharia fail-closed probes behaved as designed
- all security-relevant approval-hash fields changed the hash
- unsafe/missing backtest metrics were rejected
- 6,000-candle strategy smoke test completed
- 14 entries and 1,689 exits generated in the probe dataset
- indicator prefix differences were `0.0`

Evidence: `docs/V7_SOURCE_AUDIT_PROBES.json`.

### V8.1 tests

**65/65 passed** after the deep safety audit, including:

- byte preservation and strategy AST preservation
- all three request builders
- fixed-OCO restoration price safety
- explicit Sharia fail-closed/expiry behavior
- persistent signal/event deduplication and commission fields
- fresh-signal/stop-out guard
- callback expiry/replay rejection and exact sender/private-chat owner authorization
- no-network simulation process test proving a signal is consumed without an order submission
- forged pair/symbol and BNB signal rejection
- advanced lifecycle/mode/protected-quantity preservation during reconciliation
- audit/signal/universe/command-result retention bounds

Deployment/runtime hardening also verified in source:

- least-privilege per-service environment variables
- persistent Freqtrade database/log and preserved-core runtime paths
- startup reconciliation with entries remaining paused until owner confirmation
- pinned GitHub SSH known-hosts secret, safe one-root archive extraction, immutable image tags, and rollback
- Oracle memory gate that rejects the 1 GiB E2 micro target

Additional checks passed:

- recursive Python compilation
- high-confidence secret scan
- Sharia schema validation
- all JSON parsing
- shell syntax validation
- manifest verification
- Compose YAML structural validation

Docker was not available in the audit container, so the image build, native `docker compose config`, and extracted-artifact runtime checks are delegated to the hardened GitHub Actions workflow and must pass before Oracle deployment. The protocol clean-pass count is `0/3` because these external and capital-safety gates remain unavailable.


## Deep-audit corrections

The second-pass audit found and corrected 40 issues, including pre-side-effect durable claims for signals and commands, Telegram fail-closed pause ordering, strict finite input checks, protective-sell and partial-entry event semantics, list-status mapping, unknown accepted replacement handling, cross-process audit locking, future universe/command rejection, deterministic manifests, extracted-artifact CI parity, stale deployment-path neutralization, and installed-release hash binding.

Complete evidence is under `docs/audit/`. The issue ledger includes exact file/function ranges, reproduction, root cause, tests, core-strategy impact and remaining risk.

## Open risks and blockers

### CRITICAL — live stages not executed

No Binance Spot Testnet order was submitted, no production API key was used, no Oracle host was connected, and no extended Oracle resource soak was run. Therefore the package is not certified for live funds.

### HIGH — protection conversion cannot be atomic

Binance Spot does not provide an atomic order-list-to-order-list conversion. Converting an already protected position requires prevalidation, persisted intent, entry pause, cancel, replacement, and emergency re-protection on failure. A brief protection gap remains possible and is explicitly logged; it cannot honestly be represented as zero-risk.

### HIGH — Sharia coverage is intentionally incomplete

The code supports 50 ranked coins, but only explicit reviewed records pass. The seed dataset is not a comprehensive Sharia assessment and must be maintained by a qualified independent process.

### MEDIUM — commission asset cannot be forced to USDT per order

The bot never buys, requires, or depends on BNB. Binance determines the actual commission asset for each fill. The sidecar records the asset sent in the execution event. A buy fill can still deduct commission from the acquired base asset; this cannot be guaranteed away through the Spot order request.

### MEDIUM — dynamic universe historical replay

Standard Freqtrade backtesting cannot reconstruct the exact live rotating top-50 universe from current pairlist plugins. V8.1 saves timestamped snapshots and configuration hashes for later replay, but a complete historical snapshot corpus must first be accumulated.

### ENVIRONMENT — GitHub and Oracle not modified

No repository target or Oracle credentials were supplied, so no push, pull request, server installation, testnet trade, or live deployment was performed.

## Required external gates before live consideration

1. GitHub CI passes the full verification and Docker image build.
2. Binance Spot Testnet validates OTO, OTOCO, fixed OCO, OCO trailing, partial fills, timeouts, cancellations, user-stream events, and restart reconciliation.
3. Oracle runs Freqtrade dry-run + sidecar simulation with real market data and Telegram controls.
4. Resource, network, and restart soak is completed.
5. Emergency pause/exit and rollback are tested independently.
6. Three consecutive full clean audit passes are documented.
7. The exact release hash is approved in both preserved and sidecar live interlocks.

Until then, keep `EXECUTION_MODE=simulation` or controlled `testnet` only.
