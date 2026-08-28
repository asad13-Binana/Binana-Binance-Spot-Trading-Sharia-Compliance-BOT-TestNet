# External Market Signals — CoinGecko + CoinMarketCap (V10.2-EXT-001)

The active universe service can enrich its top-50 scan with free-tier
CoinGecko and CoinMarketCap data. This document is the authoritative
reference for what the feature does, the exact rate budgets, and the
fail-safe rules. The byte-preserved legacy core
(`legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py`) is untouched — its own
CoinGecko/CMC code remains dormant; the active implementation lives in
`services/universe_service/external_signals/`.

Remediated 2026-07-23 per the independent audit: hard 96% quota ceilings,
credit-true CMC accounting with provider reconciliation, restart-surviving
throttle state, fail-closed corrupt-ledger handling, and identity-verified
market-cap filtering (V102-REM-002/003/005/006/007/008).

## What it does (and deliberately does not do)

| Does | Does not |
|---|---|
| Annotate each universe row with `external_signals` metadata: CoinGecko market cap, cap rank, trending flag and stable `coingecko_id`; CMC rank, market-wide 24h momentum rank, trending flag and stable `cmc_id` | Never reorders the Binance-derived ranking |
| Optionally reject coins below `EXTERNAL_MIN_MARKET_CAP_USD` — but ONLY when BOTH providers independently and unambiguously report a known below-floor cap (see identity policy) | Never rejects on unknown, single-provider, ambiguous, conflicting, or stale data; never adds coins to the universe |
| Publish provider health, configured vs effective budgets, breaker state, and cache age to `shared/universe/external_signals.json` | Never blocks or fails a scan: any provider problem degrades to Binance-only scanning |

Both providers ship **disabled** (`ENABLE_COINGECKO_SIGNALS=false`,
`ENABLE_CMC_TRENDING=false`), so default behavior is identical to the
previous release. CMC enablement is environment-controlled — the old
hardcoded `ENABLE_CMC_TRENDING = False` limitation applies only to the
dormant legacy file.

## Free-tier quotas and the permanent 4% reserve

Documented free-tier quotas, verified 2026-07-22 against the official pages:

| Provider | Plan | Documented quota | HARD safety ceiling (4% under) |
|---|---|---|---|
| CoinGecko (`docs.coingecko.com`, `coingecko.com/en/api/pricing`) | Demo (free) | 100 calls/min, 10,000 call credits/month | **96 calls/min, 9,600 credits/month** |
| CoinGecko keyless | public, IP-shared | undocumented | **5 calls/min** (hard clamp) |
| CoinMarketCap (`coinmarketcap.com/api/pricing`) | Basic (free) | 50 requests/min, 15,000 call credits/month | **48 req/min, 14,400 credits/month** |

The ceilings are HARD (audit ISSUE 2): environment overrides may lower a
budget but anything above a ceiling is clamped **to the ceiling, not to the
provider quota**, so no configuration path can reach 100% of the free tier.
Zero or negative values are startup errors. The status file reports both
the configured and the effective values.

Because advisory universe enrichment and automatic Sharia source discovery
run in separate processes, their default budgets are partitioned and checked
as one plan-wide startup contract. CoinGecko uses 84/4,800 for advisory
enrichment plus 12/4,800 for discovery. CMC uses 42/11,400 plus 6/3,000. The
Sharia service refuses startup if enabled consumers collectively exceed the
96/9,600 or 48/14,400 safety ceiling. API keys are never included in this
diagnostic output.

Additional protections:

- **Daily spreading** — each process partition is spread over a worst-case
  31-day month (CoinGecko 154/day + 154/day; CMC 367/day + 96/day), so one
  process cannot burn the shared month of credits in one day.
- **Restart-surviving throttle state** (audit ISSUE 5) — daily/monthly
  counters, the per-minute request window, and circuit-breaker cool-downs
  (including 429 `Retry-After` and auth cool-downs) all persist in
  `shared/universe/external/` and are reloaded on startup. A crash-looping
  container cannot burst past the per-minute cap or erase a cool-down.
- **Fail-closed corruption handling** (audit ISSUE 6) — an unreadable or
  structurally invalid budget ledger is quarantined
  (`*.corrupt-<timestamp>`), a last-known-good `.bak` is tried, and if no
  valid state exists the month is treated as FULLY SPENT (provider blocked)
  until CMC `/v1/key/info` reconciliation recovers the true figure or the
  operator performs the documented manual reset. Corruption can reduce
  availability; it can never increase API usage allowance.
- **Credit-true CMC accounting** (audit ISSUE 3) — credits are reserved
  before each request (both providers count failed requests toward quota,
  preserved V4.9.1 Codex L4-01 finding); the response's actual
  `status.credit_count` is booked when it exceeds the estimate; missing or
  malformed credit metadata keeps the conservative estimate; and the local
  ledger reconciles with the provider's authoritative `/v1/key/info` figure
  at most once per `CMC_RECONCILE_SECONDS`. Reconciliation only ever raises
  the local count — except as the sanctioned recovery from quarantine.

### Expected usage at defaults

With `EXTERNAL_SIGNALS_REFRESH_SECONDS=1800` (30 min), worst-case scheduled
usage is approximately:

- CoinGecko: 3 calls per refresh (2 market pages + trending) × 48 refreshes
  × 31 days = **4,464 calls/month ≈ 93%** of the advisory process's 4,800
  partition (and 46.5% of the plan-wide 9,600 safety ceiling).
- CMC: 1 credit per refresh × 48 × 31 = 1,488, plus ~124 reconciliation
  credits = **≈1,612 credits/month ≈ 14.1%** of the advisory process's 11,400
  partition (and about 11.2% of the plan-wide 14,400 safety ceiling).

These figures are before failures, retries, manual actions, or
provider-side billing changes. The configured limits materially reduce
quota-exhaustion and throttling risk but cannot guarantee provider
availability or prevent a provider from changing limits, billing, abuse
detection, or account policy.

## Identity policy (audit ISSUE 7)

Ticker symbols are not globally unique, so provider data is handled with
explicit identity rules:

- Every row carries the provider's stable id (`coingecko_id` string,
  `cmc_id` integer).
- A symbol that maps to more than one distinct provider id within the
  fetched data is **ambiguous**: it is excluded from annotations, trending
  flags, and filtering entirely (counts appear in the status file).
- The market-cap floor is **cross-verified**: it may reject a pair only
  when CoinGecko AND CMC are both enabled, both have fresh unambiguous
  rows with stable ids for the base asset, and BOTH independently report a
  known cap below the floor. A conflict (one provider at/above the floor)
  means no rejection. Symbol-only, single-provider data can therefore
  never hard-reject a tradeable pair.
- Enrichment annotations remain advisory metadata matched by symbol (with
  the stable ids included so downstream consumers can verify); they are
  never a buy trigger and never affect ranking.

## Endpoints used

- CoinGecko: `GET /api/v3/coins/markets` (2 pages × 250, cap + rank +
  24h change + id), `GET /api/v3/search/trending`. Both are Demo-plan
  endpoints. Authentication: `x-cg-demo-api-key` header when a key is set.
- CoinMarketCap: `GET /v1/cryptocurrency/listings/latest?sort=percent_change_24h`
  and the zero-data `GET /v1/key/info` usage endpoint. Both are in the free
  Basic plan. The legacy `/v1/cryptocurrency/trending/latest` endpoint is
  **not** Basic-plan and is deliberately not called — "trending" is derived
  as the top `CMC_TRENDING_LIMIT` rows of the momentum-sorted listing.
  Authentication: `X-CMC_PRO_API_KEY` header.

## Fail-safe behavior

| Event | Response |
|---|---|
| HTTP 429 | Honors `Retry-After` (capped at 900s), opens the provider's circuit breaker for that long; the cool-down persists across restarts |
| HTTP 401/402/403 | Key/plan configuration problem: provider disabled for 6 hours (persisted), warning logged |
| Timeouts, 5xx, bad JSON | 3 consecutive failures open the breaker for `EXTERNAL_BREAKER_COOLDOWN_SECONDS` (default 900s) |
| Budget exhausted | Calls skipped until the window/day/month rolls over; scan continues Binance-only |
| Fetch failed but cache exists | Previous data keeps serving up to 4× the refresh interval, then the provider drops out |
| Corrupt budget/breaker state | Fail closed: quarantine + backup recovery + provider blocked until reconciliation or manual reset |
| Any unexpected exception | Caught, logged, audited (`external_signals_refresh_failed`); the scan is never interrupted |

## Operations

- Status file: `shared/universe/external_signals.json` — enablement,
  configured vs effective budgets, breaker state, cache ages, ambiguous
  symbol counts, last CMC reconciliation.
- Throttle/ledger state: `shared/universe/external/` —
  `coingecko_budget.json`, `cmc_budget.json` (+ `.bak` backups and
  `.corrupt-<timestamp>` quarantines), `coingecko_breaker.json`,
  `cmc_breaker.json`, `cmc_reconcile.json`, `cache.json`.
- Audit events: `external_signals_refresh_failed` (WARNING),
  `external_budget_state_quarantined` (WARNING),
  `external_budget_state_recovered` (WARNING),
  `external_budget_state_reconciled` (INFO) in `shared/audit/events.jsonl`.
- **Manual reset after quarantine** (deliberate operator action only):
  stop the universe service, remove the provider's `*_budget.json`,
  `*_budget.json.bak`, `*_budget.json.install`, and `*.corrupt-*` files under
  `shared/universe/external/`, then restart. A fresh ledger starts at zero,
  so only do this when the provider dashboard confirms real usage is low.
  Removing the budget json WITHOUT its `.install` marker does not reset — the
  service treats a vanished ledger as unexplained state loss and fails closed
  (V102-REM-013). You must delete both to reset.
- Universe snapshots record the feature configuration under
  `configuration.external_signals`, so the configuration hash changes when
  (and only when) the operator changes the feature settings.

### Concurrency: enforced single-writer lease (A-004)

The budget/breaker/cache state under `shared/universe/external/` is written by
exactly ONE process: the `universe` service. No other container constructs the
external-signals clients (pinned by a test in `tests/test_external_signals.py`).

That invariant is now **enforced**, not just documented:

- On startup, when either provider is enabled, the enrichment layer takes a
  single-writer lease on `shared/universe/external/.writer.lock`.
- On **POSIX** — the Docker/Oracle deployment target and CI — this is a real
  `fcntl.flock(LOCK_EX | LOCK_NB)` advisory lock held for the lifetime of the
  process. A second process cannot acquire it.
- A process that fails to acquire the lease **disables enrichment for itself**
  (fail closed → Binance-only scanning), logs CRITICAL, and writes an
  `external_signals_writer_conflict` audit event. It never writes the ledger.
- Re-entrant within one process, so a re-scan or test never self-deadlocks.
- On **Windows** development hosts no OS lock is taken (production does not run
  there); the lease file still records the holder and the documented invariant
  applies. `external_signals.json` reports `writer_lock_backend` so you can see
  which mode is active.

Running more than one `universe` replica against the same shared volume remains
UNSUPPORTED: the second replica will simply refuse to spend quota. If you ever
need horizontal scaling here, replace the lease with a shared counter first.

## Env reference

| Variable | Default | Meaning |
|---|---|---|
| `ENABLE_COINGECKO_SIGNALS` | `false` | Master switch for CoinGecko enrichment |
| `ENABLE_CMC_TRENDING` | `false` | Master switch for CMC enrichment (requires key) |
| `COINGECKO_API_KEY` | empty | Free Demo key; keyless is clamped to 5 calls/min |
| `COINMARKETCAP_API_KEY` / `CMC_API_KEY` | empty | Free Basic key (both names accepted) |
| `EXTERNAL_SIGNALS_REFRESH_SECONDS` | `1800` | Provider cache TTL, 300–86400 |
| `EXTERNAL_MIN_MARKET_CAP_USD` | `0` (off) | Cross-verified market-cap floor (needs BOTH providers) |
| `CMC_TRENDING_LIMIT` | `20` | Top-N momentum rows flagged as trending (1–100) |
| `COINGECKO_PER_MINUTE_LIMIT` | `84` | Advisory partition; aggregate hard ceiling remains 96 |
| `COINGECKO_MONTHLY_LIMIT` | `4800` | Advisory partition; aggregate hard ceiling remains 9,600 |
| `CMC_PER_MINUTE_LIMIT` | `42` | Advisory partition; aggregate hard ceiling remains 48 |
| `CMC_MONTHLY_LIMIT` | `11400` | Advisory partition; aggregate hard ceiling remains 14,400 |
| `EXTERNAL_BREAKER_COOLDOWN_SECONDS` | `900` | Breaker cool-down, 60–86400 |
| `CMC_RECONCILE_SECONDS` | `21600` | `/v1/key/info` reconciliation interval, 3600–604800 |

If CoinGecko or CoinMarketCap change their free-tier quotas, update the
`*_FREE_*` constants in
`services/universe_service/external_signals/enrichment.py` (single source of
truth — the 96% safety ceilings derive from them) and re-derive the
documented numbers here and in `.env.example`.
