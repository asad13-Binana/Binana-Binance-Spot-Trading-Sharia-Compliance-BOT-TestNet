> **HISTORICAL DOCUMENT (V8.1) — SUPERSEDED.** This file is retained as an
> audit-trail record of an earlier release. It does NOT describe the current
> V10.2-EXT-001 package. For current status see `VALIDATION_STATUS.json`,
> `docs/DEEP_AUDIT_RESPONSE.md`, and `docs/audit/ISSUE_LEDGER.csv`.

# Final Deep Audit Report — Binance Spot / Freqtrade V8.1

**Assessment date:** 2026-07-15  
**Readiness classification:** `BLOCKED — VALIDATION INCOMPLETE`  
**Default operating mode:** simulation  
**Production-live certified:** **NO**  
**Protocol clean-pass count:** `0 / 3`

## Executive verdict

The supplied package contained multiple confirmed capital-safety, state-machine, authorization/outcome, deployment and reproducibility defects despite its original passing offline suite. The current audited tree fixes those defects without changing the byte-preserved V4.9.16 core or the four approved signal-producing Freqtrade method ASTs.

The repaired package is suitable for continued **offline simulation and controlled external testnet validation**, not production funds. Capital-safety behavior that depends on actual Binance order/event timing and Oracle runtime behavior remains objectively blocked.

## Highest-severity defects fixed

1. Live activation was not cryptographically bound to the installed artifact. It now requires equality of the installed manifest hash, private environment hash and persistent approval marker.
2. Entry signals and dangerous Telegram commands were claimed only after side effects. They are now durably claimed `IN_PROGRESS` before exchange/control actions and never auto-replayed after uncertain restart.
3. Rejected protective sell events and canceled partial entries could corrupt open-position lifecycle state. Entry and protection terminal semantics are now distinct and reconciliation is forced.
4. Accepted replacement plus local persistence failure could trigger blind duplicate reprotection. The bot now latches uncertainty and refuses blind resubmission.
5. Telegram pause called a fallible Freqtrade API before disarming the sidecar. The order is now fail-closed.
6. Stale nested deployment paths could start an obsolete competing architecture. They are disabled archival stubs.
7. Release manifests and archives were non-deterministic and the extracted artifact was not retested. Packaging and CI now enforce deterministic, safe extraction and extracted-artifact parity checks.

## Verification status

- Merged offline tests: **65/65 passed** after the latest code changes.
- Preserved V4.9.16 self-tests: **33/33 passed**.
- Legacy core SHA-256: `70b1d67cc0092b5b8db4a68b343cf893641bde1aae580e9ef51e2adec1062459`.
- Core strategy changed: **FALSE**.
- No live order, production key, withdrawal capability or real-money credential was used.

## Evidence package

- `ISSUE_LEDGER.csv` — 43 corrected findings, one documented static-analysis false positive, and six external blockers.
- `FILE_REVIEW_LEDGER.csv` — complete regular-file inventory, hashes, dependencies, state and coverage.
- `FUNCTION_CALLBACK_LEDGER.csv` — Python and shell function/callback inventory with line ranges and safety attributes.
- `OFFICIAL_SOURCE_COVERAGE_MATRIX.csv` — Binance, Freqtrade, Oracle, Docker and GitHub source mapping.
- `TEST_EVIDENCE_LEDGER.csv` — commands, tree hashes, results and logs.
- `CORE_STRATEGY_FINGERPRINT.*` — baseline and preservation declaration.
- `TELEGRAM_COVERAGE.md`, `ORDER_STATE_COVERAGE.md`, `ORACLE_COMPATIBILITY.md`, `LIFECYCLE_TRACE.md`.

## Final stopping condition

Condition B applies. External Testnet, Docker, Oracle, GitHub Actions, dependency-CVE and historical-universe gates cannot be completed in this environment. Claiming three clean passes or live readiness would be inaccurate.
