# Architecture — Binance Spot / Freqtrade V10.1 + Sharia V19.1 (consolidated)

Default runtime mode is **simulation**. The system cannot place a live order without an explicit,
evidence-gated live promotion. This document describes the consolidated build produced by the
two-stage forensic audit.

## Five services (one Compose project: `binance-freqtrade-v101`)

1. **universe** — every `UNIVERSE_REFRESH_SECONDS`, ranks all Binance Spot USDT pairs by 24h gain, keeps
   the top 50, applies market-eligibility + the V19.1 status gate, and publishes an atomic, hash-stamped
   `current_pairlist.json` snapshot. Mounts the canonical Sharia directory **read-only**.

2. **sharia-screener** — the fifth, independent service (Oracle-suitable). It verifies the immutable
   V19.1 controller's exact SHA-256 at startup and is the **only** writer of the canonical Sharia
   directory. It runs a durable, restart-safe SQLite queue with priority `signal > manual > bulk > idle`,
   screens each pair through an external AI model with hosted web search, strictly validates the result
   against the V19.1 output schema (5-gate HARAM proof, 15-word quote, ticker identity, GREEN proof
   checks), writes an HMAC-signed result envelope + report + status projection, and idle-scans the current
   top-50 universe. Every failure (missing key, quota, malformed, timeout, wrong asset) **fails closed to
   NO_TRADE_INFO**. It never holds Binance trading credentials and can never place an order.

3. **freqtrade** — runs the four protected ICT/SMC signal methods (byte-identical, fingerprinted). It is
   **signal-only**: `confirm_trade_entry` returns `False`, so it never owns Binance orders. On each loop it
   emits HMAC-signed signal envelopes for halal, top-gainer pairs and writes a durable signal-seam
   heartbeat. Mounts strategy/config and the Sharia directory read-only.

4. **execution-sidecar** — the **sole Binance order owner**. It verifies signed signals, performs a fresh
   V19.1 re-screen of the exact pair before any order (fail-closed on timeout), runs the durable
   claim → submit → protection → event → reconcile lifecycle, owns OCO/OTO/OTOCO placement with complete
   pre-cancel filter validation, structured emergency exit, endpoint-complete startup reconciliation,
   SQLite integrity + verified backups, and the signed live-evidence gate. In simulation it drives a
   deterministic exchange simulator through the same lifecycle (with injectable timeout/reject/partial
   faults). In testnet/live it drives the preserved V4.9.16 core, whose user-data transport is swapped to
   the modern WebSocket API subscription (listen-key REST was retired 2026-02-20).

5. **telegram-broker** — the owner-only remote control. Signs every command and Sharia scan request,
   verifies signed results, persists its update offset across restarts, and exposes status/pause/resume/
   emergency/protection controls plus V19.1 `scan-one`, `scan-all`, `/scan BASE/USDT`, direct-pair typing,
   and report retrieval. Never writes canonical Sharia data.

## Trust boundaries (V101-NEW-001)

Every inter-service message is an HMAC-SHA-256 envelope bound to producer, purpose, nonce, timestamps,
expiry, and the installed release hash. Each producer holds only the key(s) it needs. The canonical Sharia
directory is writable by the screener alone; all other services mount it read-only. A compromised sibling
container therefore cannot forge a command, a signal, or a Sharia verdict, and cross-release replay is
rejected. Durable idempotency claims neutralise replay of a *valid* captured message.

## Order-ownership invariant

Exactly one component (the execution sidecar) submits Binance orders. Freqtrade is permanently signal-only.
No filled quantity is left unprotected: partial fills, cancel-after-partial, and ambiguous replacements are
reconciliation-only and never trigger blind re-protection or a second protective sell.

## Sharia flow (master protocol section 8)

`top-50 gainers → V19.1 status gate → strategy signal → SIGNED signal → fresh V19.1 re-screen (fail-closed)
→ risk/claim → order`. Only `GREEN` / `GREEN_AVOID_OPTIONAL` are trade-eligible; every other verdict,
including a missing, expired, malformed, or mismatched result, blocks the trade.

## Preserved core

`legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py` (sha256 `70b1d67c…2459`) is byte-identical and runs its
own 33/33 self-tests. The four strategy methods are pinned by interpreter-independent source + token
fingerprints. The V19.1 controller (`07106bb8…ba78`) is immutable and integrity-checked at build, install,
and service startup.
