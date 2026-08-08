# Codex revision 2026-08-08A release notes

This is the Binance Spot Testnet package for revision `2026-08-08A`; its
internal release identifier is `V10.2-CODEX-2026-08-08A`. The immutable
package interlock continues to make live execution structurally unavailable.

## Runtime and release repairs

- The Sharia screener now receives `EXECUTION_MODE` from Compose and applies
  the same immutable package/execution matrix as the execution sidecar.
- Only `simulation` and `testnet` screening modes are permitted in this
  package; `EXECUTION_MODE=live` remains blocked before screening or exchange
  activity can begin.
- Regression coverage now exercises every permitted and forbidden package /
  execution combination, verifies the Compose propagation, and prevents
  drift between `RELEASE_VERSION`, `RELEASE_MODE`, validation metadata and the
  generated release manifest.
- Release and validation metadata have been advanced together to remove the
  stale revision mismatch.

## Protected behaviour

- `IctSmcStrategy.py` was not modified. Its required SHA-256 remains
  `9f6bafc78c8cd0d9b9cbde615ddce89e304ab09738584b88d05bfdf92ff4e830`.
- The preserved all-in-one legacy core was not modified. Its required
  SHA-256 remains
  `70b1d67cc0092b5b8db4a68b343cf893641bde1aae580e9ef51e2adec1062459`.
- No credential, deployment secret, validation result or trading-performance
  claim has been invented or embedded.

## Evidence and remaining gates

Exact local results for this revision are recorded in
`docs/audit/TEST_EVIDENCE_LEDGER.csv`; the file set and hashes are bound by
`RELEASE_MANIFEST.json` and `RELEASE_SHA256.txt`.

The local full release gate passed 353 of 357 core tests with four documented
skips, 50 monitoring tests and 33 of 33 preserved legacy self-tests. Secret,
controller-integrity, audit-ledger, JSON, YAML and service-unit checks passed.
One Starlette/httpx deprecation warning remains disclosed. Docker was not
available on the local host, so container execution remains a GitHub gate.

This source package is ready only for the remaining controlled validation
path in `VALIDATION_STATUS.json`. This exact revision still requires
successful GitHub container CI, protected-branch/environment controls,
immutable container digests, hash-pinned Python distributions, authenticated
Binance Spot Testnet lifecycle evidence, Oracle soak and rollback evidence,
reconciliation evidence and performance review before any live promotion is
considered.
