# Independent monitoring re-audit disposition

The independent ChatGPT/Sol re-audit was materially legitimate. The earlier
integration report overstated readiness. The original monitoring archive had
two competing APIs, inconsistent authorization, inadequate redaction, unsafe
environment coupling, wrong topology/ports and execution sources, ineffective
enable/IP controls, unbounded log reads, a global pre-auth limiter, incomplete
systemd wiring, unrestricted MCP URL configuration, and CI/manifest gaps.

This package contains one canonical read-only FastAPI implementation. It uses
constant-time Bearer authorization, source-network enforcement, mandatory
auth audit records with request IDs, per-client post-auth rate limiting,
recursive secret redaction, bounded log reads, strict request bounds, correct
execution-state/P&L/signal data separation, correct v101 paths and testnet port,
sanitized root-owned container snapshots without a Docker-socket mount,
mode-gated systemd units, loopback-only MCP/Telegram clients, pinned monitoring
dependencies, and release-manifest coverage.

Offline tests do not replace Docker, systemd, Oracle, Binance Testnet, Telegram,
or GitHub Actions runtime evidence. See VALIDATION_STATUS.json and
docs/EXTERNAL_VALIDATION_RUNBOOK.md for the remaining blockers.


# V10.2-EXT-001 addendum (2026-07-22) — external signal enrichment

Scope of this release increment, applied identically to the live and testnet
packages; the byte-preserved legacy core and the reviewed strategy signal
methods are unchanged (manifest preservation hashes still match).

## Added

- services/universe_service/external_signals/ — CoinGecko + CoinMarketCap
  clients for the ACTIVE universe service (the legacy implementations stay
  dormant inside the preserved core). Advisory-only: annotations plus an
  optional, default-off market-cap floor. Fail-open on any provider problem.
- Free-tier budgets preset 4% under the documented quotas verified
  2026-07-22 (CoinGecko Demo 100/min + 10,000/month -> 96 + 9,600;
  CMC Basic 50/min + 15,000 credits/month -> 48 + 14,400), with per-day
  spreading (month/31), restart-surviving counters on the shared volume,
  request-vs-credit accounting, documented-quota clamping, Retry-After-aware
  circuit breakers, and reserve-before-send accounting.
- ENABLE_CMC_TRENDING is now environment-controlled (the old hardcoded
  False remains only inside the dormant legacy file).
- New env vars wired through .env.example and docker-compose.yml (universe
  service only; the 5-service topology is unchanged). Docs:
  docs/EXTERNAL_SIGNALS.md. Tests: tests/test_external_signals.py
  (37 offline tests; full suite 172 + 49 monitoring).

## Fixed

- V102-FIX-001: scan_once() unconditionally wrote the legacy halal
  projection into /app/shared/sharia, which production mounts READ-ONLY for
  the universe container — every scan cycle would have failed on Oracle and
  the stack could never become healthy. The write is now best-effort
  (the sharia-screener, which mounts the directory read-write, owns that
  projection); a regression test pins scan survival on EROFS.


# V10.2 REMEDIATION addendum (2026-07-23) — independent-audit fixes

Applied identically to both packages in response to the 2026-07-23
independent ChatGPT audit (FINAL_INDEPENDENT_AUDIT_REPORT_2026-07-23.md and
BINANCE_BOT_V10_2_REMEDIATION_PLAN_FOR_CLAUDE_CODE.md). The preserved
legacy core, strategy signal fingerprints, ranking, order logic, risk
logic, Telegram trading commands, and live-promotion evidence gates are
UNCHANGED (manifest preservation hashes verified before and after).

- V102-REM-001 (CRITICAL, audit ISSUE 1): runtime package-mode interlock.
  The execution sidecar now reads the read-only RELEASE_MODE file baked
  into the service image (never an env var) and refuses forbidden
  execution modes BEFORE any authenticated code path. The testnet package
  is structurally incapable of live execution; the live package still
  defaults to simulation and keeps every existing promotion gate.
- V102-REM-002 (HIGH, ISSUE 2): budget overrides now clamp to HARD safety
  ceilings 4% under the documented free tiers (96/9600 CoinGecko,
  48/14400 CMC) instead of the full provider quotas. Status output shows
  configured vs effective limits.
- V102-REM-003 (HIGH, ISSUE 3): CMC accounting is credit-true — actual
  status.credit_count booked when above the estimate, conservative
  estimate kept otherwise, and interval-limited /v1/key/info
  reconciliation that only ever raises the local ledger (except as the
  sanctioned quarantine recovery).
- V102-REM-004 (HIGH, ISSUE 4): Docker image and CI install from the
  resolved requirements.services.lock with pip check; pip-audit runs
  against the exact locks.
- V102-REM-005 (MEDIUM, ISSUE 5): per-minute request window and circuit
  breaker cool-downs (429 Retry-After, auth) persist across restarts.
- V102-REM-006 (MEDIUM, ISSUE 6): corrupted quota ledgers fail CLOSED —
  quarantine + backup recovery + provider blocked until reconciliation or
  documented manual reset. Usage can never reset to zero.
- V102-REM-007 (MEDIUM, ISSUE 7): stable provider ids on every row,
  ambiguous ticker symbols excluded, and the market-cap floor fires only
  on cross-verified below-floor agreement from BOTH providers.
- V102-REM-008 (MEDIUM, ISSUE 8): corrected quota arithmetic in all docs
  (~46.5% CoinGecko / ~10.3% CMC of safety budgets at defaults) and
  removed absolute no-ban guarantees.
- V102-REM-009 (LOW/MEDIUM, ISSUE 9): release labels come from the new
  RELEASE_VERSION metadata file (manifest, verification banner, CI names);
  the v101 operational namespace is retained for upgrade compatibility.
- V102-REM-010 (LOW, ISSUE 10): historical raw logs removed from the
  package and summarized in docs/EVIDENCE_HISTORY.md.
- Audit ISSUE 11 (validation gap): local Docker Stage A evidence added
  where executable (image build, compose config, in-container package-mode
  guard, universe container run with read-only Sharia mount, restart
  persistence). Binance Spot Testnet lifecycle, Oracle soak, and real
  historical backtests remain OPEN external blockers. Status wording:
  OFFLINE RELEASE GATE PASSED / TESTNET-ORACLE RUNTIME VALIDATION
  INCOMPLETE / LIVE TRADING NOT CERTIFIED.
