# Audit Fix Change Log

**Assessment date:** 2026-07-15

The preserved legacy core and the four signal-producing Freqtrade methods were not changed.

## Changed files
- `.dockerignore` — MODIFIED
- `.github/workflows/ci.yml` — MODIFIED
- `Dockerfile.services` — MODIFIED
- `README.md` — MODIFIED
- `RELEASE_MANIFEST.json` — MODIFIED
- `RELEASE_SHA256.txt` — MODIFIED
- `VALIDATION_STATUS.json` — MODIFIED
- `deploy/install_artifact.sh` — MODIFIED
- `deploy/oracle_setup.sh` — MODIFIED
- `deploy/verify_release.sh` — MODIFIED
- `docker-compose.yml` — MODIFIED
- `docs/GITHUB_ORACLE_DEPLOYMENT.md` — MODIFIED
- `docs/MERGE_AND_AUDIT_REPORT.md` — MODIFIED
- `docs/V8.1_FINAL_VERIFICATION.log` — MODIFIED
- `docs/audit/BLOCKER_REPORT.md` — ADDED
- `docs/audit/CHANGELOG_AUDIT_FIXES.md` — ADDED
- `docs/audit/CORE_STRATEGY_FINGERPRINT.json` — ADDED
- `docs/audit/CORE_STRATEGY_FINGERPRINT.md` — ADDED
- `docs/audit/FILE_REVIEW_LEDGER.csv` — ADDED
- `docs/audit/FINAL_AUDIT_REPORT.md` — ADDED
- `docs/audit/FUNCTION_CALLBACK_LEDGER.csv` — ADDED
- `docs/audit/ISSUE_LEDGER.csv` — ADDED
- `docs/audit/LIFECYCLE_TRACE.md` — ADDED
- `docs/audit/OFFICIAL_SOURCE_COVERAGE_MATRIX.csv` — ADDED
- `docs/audit/ORACLE_COMPATIBILITY.md` — ADDED
- `docs/audit/ORDER_STATE_COVERAGE.md` — ADDED
- `docs/audit/REMAINING_LIMITATIONS.md` — ADDED
- `docs/audit/TELEGRAM_COVERAGE.md` — ADDED
- `docs/audit/TEST_COMMANDS.md` — ADDED
- `docs/audit/TEST_EVIDENCE_LEDGER.csv` — ADDED
- `freqtrade/README.md` — MODIFIED
- `freqtrade/deploy/freqtrade-docker.service` — MODIFIED
- `freqtrade/deploy/freqtrade-update.service` — MODIFIED
- `freqtrade/deploy/freqtrade-update.timer` — MODIFIED
- `freqtrade/deploy/oracle_setup.sh` — MODIFIED
- `freqtrade/deploy/update.sh` — MODIFIED
- `freqtrade/docker-compose.yml` — MODIFIED
- `freqtrade/scripts/start.sh` — MODIFIED
- `freqtrade/scripts/stop.sh` — MODIFIED
- `requirements.services.txt` — MODIFIED
- `scripts/build_manifest.py` — MODIFIED
- `scripts/verify_manifest.py` — MODIFIED
- `services/common/atomic.py` — MODIFIED
- `services/common/audit.py` — MODIFIED
- `services/execution_sidecar/core_adapter.py` — MODIFIED
- `services/execution_sidecar/main.py` — MODIFIED
- `services/execution_sidecar/order_manager.py` — MODIFIED
- `services/execution_sidecar/protection_modes.py` — MODIFIED
- `services/execution_sidecar/risk_checks.py` — MODIFIED
- `services/execution_sidecar/state_store.py` — MODIFIED
- `services/telegram_broker/bot.py` — MODIFIED
- `services/universe_service/scanner.py` — MODIFIED
- `services/universe_service/snapshot_store.py` — MODIFIED
- `tests/secret_scan.py` — MODIFIED
- `tests/test_v81.py` — MODIFIED
- `tests/test_v81_safety_regressions.py` — ADDED

## Change classes
- Capital-safety and idempotency corrections
- Binance state-machine compatibility corrections
- Telegram authorization/outcome/fail-closed corrections
- Persistence, logging, snapshot and clock-skew reliability corrections
- Oracle/Docker/GitHub deployment and deterministic packaging corrections
- Regression tests and audit evidence

**Core strategy change:** `FALSE`

---

# V10.1 consolidation changes (2026-07-16)

Per `BINANCE_BOT_FINAL_VERDICT_IMP.md` (final independent verdict): create
V10.1 from the audited V8.1 safety baseline, repair its two release blockers,
and import only reviewed V10 operational pieces.

## V101-001 — interpreter-dependent strategy hash test (release blocker)
- **Was:** `tests/test_v81.py::test_signal_strategy_methods_are_unchanged`
  hashed `ast.dump(node)` of the four protected signal methods
  (`tests/test_v81.py:26-38`, `scripts/build_manifest.py:17-22` in V8.1).
  Python 3.12 adds AST fields, so the unchanged source produced a different
  hash and the mandatory suite failed (64/65) on the exact interpreter chosen
  by `Dockerfile.services` and CI.
- **Now:** `services/common/strategy_fingerprint.py` computes canonical
  source-segment and logical-token fingerprints that never leave text/token
  space. Baselines were regenerated from the byte-identical method text and
  are enforced in `tests/test_v81.py`, `scripts/build_manifest.py`, and
  `scripts/verify_manifest.py` (which now also recomputes the fingerprints
  from the installed strategy file). CI verifies on Python 3.10/3.11/3.12/3.13.
  The strategy file was not edited; its four signal methods hash identical to
  the original reviewed strategy (source and token forms).

## V101-002 — vulnerable dependency pin (release blocker)
- **Was:** `requirements.services.txt:1` pinned `requests==2.32.3` with two
  published advisories (fixed versions 2.32.4 and 2.33.0).
- **Now:** `requests==2.34.2` (the version the independent V10 dependency
  audit reported clean on 2026-07-15). The complete offline suite is re-run
  against the new pin, `tests/test_v101_consolidation.py` blocks any future
  pin below 2.32.4, and `pip-audit` is a blocking CI step.

## V101-003 — reviewed V10 donor imports (no execution-path changes)
- Imported after review against this ledger: `Makefile`,
  `scripts/run_legacy_selftests.sh` (bounded legacy self-test),
  `scripts/healthcheck.sh` (adapted to the V8.1 service set),
  `docs/LIVE_PROMOTION_CHECKLIST.md`.
- **Explicitly NOT imported:** V10 `services/execution_bridge/bridge.py`
  (records signals after submission — replay risk on ambiguous crash) and its
  trailing-conversion path (blind re-protection after accepted-but-unobserved
  timeout), per final-verdict findings 1 and 2. The V8.1 sidecar remains the
  sole execution owner.

## V101-004 — release identity and honesty updates
- `RELEASE_MANIFEST.json` release id `V10.1-CONSOLIDATED` (lineage recorded);
  `VALIDATION_STATUS.json` rewritten with current results and the still-open
  external blockers; README gains a V10.1 status header. Deployment infra
  identifiers (`/opt/binance-freqtrade-v81`, image tag
  `binance-freqtrade-v81-services`) are intentionally unchanged so existing
  Oracle installs keep working; the CI artifact is named
  `binance-freqtrade-v101-<sha>.tar.gz`.


## V10.2-EXT-001 (2026-07-22) — external signals + read-only-mount fix

The preserved legacy core and the four signal-producing Freqtrade methods
were again not changed (manifest preservation hashes verified).

### Changed files
- `services/universe_service/scanner.py` — MODIFIED (enrichment hooks; V102-FIX-001)
- `docker-compose.yml` — MODIFIED (universe env vars only; topology unchanged)
- `.env.example` — MODIFIED (external-signal configuration block)
- `README.md` — MODIFIED
- `AUDIT_AND_RELEASE_NOTES_2026-07-19.md` — MODIFIED (addendum)
- `VALIDATION_STATUS.json` — MODIFIED
- `services/universe_service/external_signals/__init__.py` — ADDED
- `services/universe_service/external_signals/budget.py` — ADDED
- `services/universe_service/external_signals/breaker.py` — ADDED
- `services/universe_service/external_signals/httpguard.py` — ADDED
- `services/universe_service/external_signals/coingecko_client.py` — ADDED
- `services/universe_service/external_signals/cmc_client.py` — ADDED
- `services/universe_service/external_signals/enrichment.py` — ADDED
- `docs/EXTERNAL_SIGNALS.md` — ADDED
- `tests/test_external_signals.py` — ADDED (37 tests)
- `docs/audit/ISSUE_LEDGER.csv` — MODIFIED (V102-FIX-001 row)
