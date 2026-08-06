# Response to the 2026-07-23 independent deep audit

Every finding in `FINAL_INDEPENDENT_DEEP_AUDIT_VERDICT_2026-07-23.txt` (and the
earlier verdict that preceded it) was verified against the actual code before
any change. The independent auditor's overall stance was correct and is
adopted here: the packages are substantial and safe by design, but they are
NOT live-certified and the "5 clean passes" cannot be reached offline because
they require runtime evidence this environment cannot produce. Nothing below
changes the preserved core, the strategy signals, the Sharia logic, the order
logic, or live-trading capability.

## Code/packaging fixes applied (this round)

| Finding | Verdict | What changed |
|---|---|---|
| **CRITICAL** (first verdict): live container omits the strategy file, so `live_interlock` → `LIVE BLOCKED: evidence verification failed` | CONFIRMED real; live capability was broken in-container | **V102-REM-011**: `Dockerfile.services` now copies `freqtrade/user_data/strategies` (byte-identical, read-only) into the image so the live-evidence fingerprint check finds it. Testnet/sim were never affected. |
| **HIGH** (first verdict): corruption recovery could restore a lower stale `.bak` counter (fail-open) | CONFIRMED | **V102-REM-012**: corruption now ALWAYS fails closed; `.bak` is never used to lower the counter or clear quarantine. |
| **HIGH** (first verdict): missing/deleted budget file starts at zero | CONFIRMED | **V102-REM-013**: an install-identity marker distinguishes a true first install (zero is correct) from unexplained state loss (fail closed). Manual reset deletes both files, documented. |
| **HIGH** (first verdict): `/v1/key/info` bypasses the budget and is charged afterward | PARTLY — the bypass is required (it is the quarantine recovery path) | **V102-REM-014**: it still bypasses when quarantined (recovery), but a healthy budget already at its monthly cap now skips the probe entirely. Cost is still booked. |
| **HIGH** (first verdict): any integer can clear quarantine | Reviewed — mitigated by design | `reconcile_month` only receives values from a live, authoritative CMC `/v1/key/info` response for this exact key/account/month; malformed data returns `None` and never clears. CoinGecko has no such endpoint, so its quarantine clears only on month-rollover or documented manual reset. Documented in `docs/EXTERNAL_SIGNALS.md`. |
| **HIGH** (first verdict): no interprocess budget lock | Reviewed — single-writer invariant | Only the `universe` service constructs the external-signals budgets (one writer in the shipped 5-service topology). This invariant is now stated in `docs/EXTERNAL_SIGNALS.md`; running multiple universe replicas is unsupported. |
| **F-03**: FILE_REVIEW_LEDGER incomplete/stale (this is the "88 files" claim) | CONFIRMED — 83 files absent, 44 stale hashes, 2 phantom rows | **V102-REM-015**: both ledgers are now GENERATED from the tree (`scripts/build_audit_ledgers.py`) and CI-ENFORCED (`--check` in the gate + `tests/test_audit_ledgers.py`). They can never drift again. |
| **F-04**: FUNCTION_CALLBACK_LEDGER incomplete | CONFIRMED | Same generator now emits every AST-derived function/method with scope and context; CI-enforced. |
| **F-05**: 1m MACD "soft boost" not active in the entry path | CONFIRMED — and it is INTENTIONAL (source comment already says so) | Documentation-only (Option A): `docs/STRATEGY_NOTES.md` is now the authoritative behavior note; `tests/test_strategy_probe.py` pins that the 5m MACD is the active gate and the 1m MACD changes no entries. **The strategy was not changed** (you asked us not to). |
| **F-06**: strategy smoke probe outside the mandatory gate | CONFIRMED — and the FIRST fix was defective (see A-001) | **V102-REM-016 (corrected)**: `tests/test_strategy_probe.py` now forces the ACTIVE `macdhist_5m` gate, requires a non-vacuous fixture, proves the 1m `macdhist` changes nothing, and skips ONLY on a genuinely missing optional dependency. A dedicated CI job runs it inside the pinned Freqtrade 2026.6 image (TA-Lib present) and fails on any skip. |
| **F-07**: CI builds only the universe image, not the topology | CONFIRMED — and the FIRST fix was defective (see A-002) | **V102-REM-017 (corrected)**: the `integration-simulation` job asserts package-mode 0444, read-only Sharia mount, and health; the restart step now captures an immutable snapshot file plus its SHA-256 before the restart and requires the identical file, an unreduced state count, and health again afterwards. Full 5-service health (Freqtrade + Telegram) still needs credentials → testnet/Oracle gate. |
| **F-08**: images tag-pinned not digest-pinned | CONFIRMED (P1 hardening) | `docs/IMAGE_DIGEST_PINNING.md` + `scripts/resolve_image_digests.sh` document and tool the pinning; it needs a networked host and the operator's target architecture, so it is a documented pre-deployment step, not auto-applied. |
| **F-09**: VALIDATION_STATUS host metadata imprecise | CONFIRMED | `VALIDATION_STATUS.json` now records separate client/engine/POSIX-shell/systemd fields and the exact commands executed. |
| **F-10**: FINAL_FINDINGS missing F-03..F-09 | CONFIRMED | The canonical `docs/audit/ISSUE_LEDGER.csv` and the release `FINAL_FINDINGS.csv` now carry every deep-audit finding and its disposition. |

## Findings that remain OPEN (correctly — cannot be closed offline)

| Finding | Why it stays open |
|---|---|
| **F-01** live runtime certification | Needs Docker engine + full-topology run + fault drills. The GitHub `integration-simulation` job does part of this; the rest is the Oracle program. |
| **F-02** historical performance | Needs a real Freqtrade historical backtest, out-of-sample, lookahead/recursive analysis, and a timestamped universe/Sharia replay corpus. Not run; the docs already state prior backtests lost after fees. |
| Binance Spot Testnet lifecycle, Oracle 14-day soak | Need real testnet credentials and an Oracle VM. Blocked by design in this environment. |

## Second deep-audit round (A-001 … A-007)

A follow-up independent audit re-examined the fixes above and found that two of
them were themselves defective. That audit was correct. Dispositions:

| Finding | Verdict | What changed |
|---|---|---|
| **A-001 HIGH** — the strategy probe forced the INACTIVE 1-minute `macdhist`, not the active `macdhist_5m`; accepted a zero-signal fixture; converted any probe error into a SKIP; and was never run where TA-Lib exists | **CONFIRMED — my earlier "F-06 FIXED" was wrong.** With TA-Lib present the old assertion would have FAILED (the auditor measured 14/14/14 entries) | `freqtrade/tests/audit_probes.py` now also forces `macdhist_5m` (test-support file only; the strategy is untouched). `tests/test_strategy_probe.py` was rewritten: forced-negative 5m MACD must yield **zero** entries, the fixture must produce entries (non-vacuous), the 1m `macdhist` must change **nothing**, and the skip is narrowed to a missing optional import. A new `strategy-probe-container` CI job runs it inside `freqtradeorg/freqtrade:2026.6` and **fails if it skips**. |
| **A-002 HIGH** — the CI restart step hashed a filename list, never compared before/after, and `test -n "$before"` passed even with no state | **CONFIRMED — my earlier "F-07 FIXED" overstated it** | The step now selects an **immutable** `shared/universe/snapshots/universe_*.json`, records its SHA-256 and the state-file count, restarts, waits for health again, then requires the same file with an **identical hash** and a non-decreasing count. (`current_pairlist.json`/`status.json` are legitimately rewritten by the post-restart rescan, so they are the wrong target.) |
| **A-003 HIGH** — `verify_release.sh` ran `systemd-analyze verify` whenever the binary existed; on an unpacked CI/Linux checkout the units reference not-yet-installed paths and `docker.service`, so `set -e` aborted the whole gate | **CONFIRMED — this would have failed your GitHub Actions run** | The gate now captures the output and classifies it: genuine unit defects (unknown key/section, parse/invalid/syntax errors) still FAIL; problems fully explained by pre-deployment context are reported explicitly and tolerated; anything unexplained still fails. It never blanket-ignores a non-zero exit. |
| **A-004 MEDIUM** — no cross-process budget lock existed although an earlier narrative claimed one | **CONFIRMED (documentation was ahead of the code)** | `services/universe_service/external_signals/writer_lock.py` adds a real single-writer lease: `fcntl.flock(LOCK_EX\|LOCK_NB)` on POSIX (the Docker/Oracle deployment target and CI), held for the process lifetime. A second process that cannot take it disables enrichment (fail closed to Binance-only) and emits a CRITICAL audit event. Same-process re-entrancy is supported. On Windows dev hosts no OS lock is taken and the documented invariant applies — stated plainly rather than overclaimed. |
| **A-005 MEDIUM** — release documents carried stale counts and premature "FIXED" claims | **CONFIRMED** | All counts are regenerated from the built tree, and the F-06/F-07 rows above now record that their first fix was defective and what corrected it. |
| **A-006 MEDIUM** — digest/hash pinning still open | Unchanged (P1) | `docs/IMAGE_DIGEST_PINNING.md` + `scripts/resolve_image_digests.sh`; requires a networked host and the target architecture. |
| **A-007 HIGH** — profitability unproven | Unchanged | Remains an open external blocker; prior backtests lost after fees. |

## Honest status

```text
SOURCE-LEVEL OFFLINE GATE PASSED
CONTAINER/CI VALIDATION PENDING (GitHub Actions)
TESTNET/ORACLE RUNTIME VALIDATION INCOMPLETE
LIVE TRADING NOT CERTIFIED
```

The testnet package is a candidate for GitHub CI and Oracle **testnet-only**
deployment. The live package stays in simulation and keeps every signed-
evidence promotion gate. Do not enable real funds until F-01/F-02 and the
testnet/Oracle program are complete.
