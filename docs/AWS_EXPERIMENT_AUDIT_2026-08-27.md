# AWS experiment audit disposition — 27 August 2026

This file records the disposition of the supplied AWS terminal transcript. It
is runtime evidence for an experimental AWS host, not Oracle deployment proof
and not an instruction to alter the protected bot.

## Confirmed facts

- Source was the exact TestNet `main` commit
  `e272d2c4d1b9b878e8a54959dd4b9b7cbbae1e55` and remained clean.
- The host was Ubuntu 24.04 x86_64 with 7.6 GiB RAM, 4 GiB swap and a 29 GiB
  root disk. This does not satisfy the repository's final four-bot Oracle
  target of at least 11,264 MiB physical RAM and 80 GiB free root storage.
- Docker/Compose and container image/import smoke checks passed.
- Only three of the six required Binana services were running: `universe`,
  `sharia-egress-proxy` and `sharia-screener`. Freqtrade, the execution sidecar
  and Telegram broker were absent. This was not a complete bot deployment.
- The GET-only authenticated preflight passed Binance TestNet,
  CoinMarketCap and Telegram. CoinGecko rejected the configured free Demo
  credential. No order, cancellation, transfer or withdrawal was attempted.
- Public Binance contract drift passed for 489 current tradeable Spot/USDT
  symbols.
- The isolated suite reported 662 passed, 6 skipped and three failures. Two
  audit-ledger failures came from copying an ignored host-only `.env.external`
  into the audit tree. The permission failure came from `cp -a` preserving
  group/world-writable host modes on all 308 copied files. These do not show a
  protected-core defect.

## Required corrective operation

1. Never copy a deployment checkout with `cp -a` for release verification.
   Audit a clean `git archive` or the exact GitHub release artifact so untracked
   `.env*` secrets cannot enter the tree and Git executable modes are retained.
2. Keep credentials only in `/etc/binana-testnet/.env`; never create
   `.env.external` inside a source, audit or artifact directory.
3. Use the root-owned artifact installer. It starts all six required services,
   checks every service/container health state and rolls back on any missing or
   unhealthy service.
4. Replace or activate the CoinGecko free Demo key and repeat the GET-only
   preflight. The repository uses the Demo endpoint and
   `x-cg-demo-api-key`; it does not require a paid Pro key.
5. Do not weaken the ledger or packaged-permission tests. Their failure was the
   intended warning that the audit input was not a clean release tree.

AWS remains an experiment-only host. Final deployment evidence must come from
the intended Oracle Ubuntu 24.04 ARM64 host and must still include the real
TestNet lifecycle, restart/reconciliation, backup/restore and soak gates.
