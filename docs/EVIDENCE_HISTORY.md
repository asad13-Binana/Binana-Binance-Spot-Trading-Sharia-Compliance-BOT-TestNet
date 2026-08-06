# Historical verification evidence — summaries

Per the packaging policy adopted in the 2026-07-23 remediation (independent
audit ISSUE 10), raw historical verification logs are no longer shipped
inside release packages. Release packages carry only current release
validation metadata (`VALIDATION_STATUS.json`, `RELEASE_MANIFEST.json`);
full raw logs live outside the public package — in prior release archives
retained by the owner and, going forward, as GitHub Actions run artifacts.

Summaries of the retired logs:

## V4.9.16 legacy self-test (`V4.9.16_SELFTEST.log`, retired)

The preserved all-in-one core's deterministic self-test suite: **33/33
passed** on the offline verification host. The same suite still runs live
on every release verification (`deploy/verify_release.sh`) and in CI, so
the current result is always reproducible from the package itself.

## V8.1 final verification (`V8.1_FINAL_VERIFICATION.log`, retired)

Recorded the V8.1-MERGED-AUDITED offline gate: full unit suite, secret
scan, manifest verification. Historical context only — V8.1 remained
BLOCKED for release because of the interpreter-dependent strategy-hash
test later fixed in V10.1 (see `docs/audit/CHANGELOG_AUDIT_FIXES.md`).

## V10.1 final verification (`V10.1_FINAL_VERIFICATION.log`, retired)

Recorded the V10.1-CONSOLIDATED offline gate on 2026-07-19: consolidated
core suite, monitoring suite, 33/33 legacy self-tests, secret scan, Sharia
schema and controller integrity, manifest verification. Superseded by the
V10.2 verification results in `VALIDATION_STATUS.json`.

Current-release verification is always reproducible offline:

```bash
pip install -r requirements-dev.txt
bash deploy/verify_release.sh
```
