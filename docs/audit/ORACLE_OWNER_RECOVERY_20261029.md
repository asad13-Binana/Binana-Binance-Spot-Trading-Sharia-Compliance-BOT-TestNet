# Oracle owner recovery candidate — 2026-10-29

This source repair candidate is not deployed or exchange-certified. The Oracle
bundle uses Freqtrade 2026.9 at commit
`1f394eaebc2f46a83d26971388628707802f8602`, CCXT 4.5.87, and the pinned NFI X8
source through `BinanaNfiSpot`. The policy is five-minute signals and four nominal
250 USDT slots. The owner explicitly requested this profile for the new LIVE
package. Testnet strategy economics remain unchanged. LIVE activation remains
disabled by default.

## Repairs

- Persist entry claims and require authenticated protection and zero-fill proofs
  during recovery. Reject contradictory last-fill data before releasing a slot.
- Bind release authorization to the immutable package mode, account and exchange
  epoch, and validate the selected Spot endpoint before requests.
- Build Oracle artifacts from pinned public source. Use a provider-neutral policy
  for certificate issuance and renewal, a private renewal interpreter and bounded
  validation. Refuse implicit legacy startup in the old setup entrypoint.
- Preserve signed registry commands, durable Sharia/Telegram outcomes and shared
  Telegram retry handling. Accept explicitly selected all-eligible snapshots
  without padding or bypassing screening. Port only required support interfaces
  to LIVE.
- Pin pypdf 6.21.4, setuptools 84.1.0 and filelock 4.2.1. certifi 2026.10.19 and
  aiohttp 3.14.4 were already pinned. Enforce direct-pin/lock consistency and audit
  runtime, build, host and renewal dependencies.
- Require native AMD64 and ARM64 owner builds and offline regression runs in both
  PR workflows. Deployment triggers are unchanged; PR events cannot deploy.

The new Oracle artifact excludes the legacy order engine. Historical source and
captured build/rollback inputs are retained where dependencies still require them;
this is not a claim that every old repository file can safely be deleted.

## Evidence and limits

Offline release scripts passed for both packages. Issuance, renewal, installer and
rollback fixtures passed 18 tests per package. Current support images built and
passed their import/contract checks on AMD64 and emulated ARM64. Python dependency
audits on 1 November after the pypdf correction found no reported vulnerabilities in six distinct locked
closures. Source, fixture, image and GitHub CI results have different scopes;
review the checks on the actual PR commit before accepting the candidate.

The owner suite contains 260 required test identities and rejects missing,
duplicate or skipped tests. The zero-fill regression reproduced 12 failed
assertions before passing five tests per mode. A separate 38-test order run and
49-test recovery/adjacent-order run passed per mode. Counts overlap and must not be
added to claim coverage. Initial failures, timeouts and fixture corrections remain
in the local evidence ledger. Collection-time clock fixtures were corrected to
keep future timestamps in the future when a long suite reaches them.

Critical Ruff checks and Bandit high-severity/high-confidence checks passed. The
unchanged pinned vendor has 15 known F821 diagnostics in disabled position
adjustment paths/string annotations. The narrow lint exception applies only to
that vendor file; position adjustment stays disabled.

Fresh image OS scans still report 71 distinct Debian advisories: 164 package/finding
rows in support images and 166 in owner images, with no fixed versions listed in
those scans. This is a failed zero-vulnerability gate, not zero risk. Python audit
success does not remove OS findings. Per-advisory runtime reachability is untested.

Native Oracle installation, renewal across a real certificate expiry, reboot and
restore, sustained load, authenticated exchange acceptance and strategy performance
remain NOT TESTED. Local ARM emulation is not native Oracle acceptance. Historical
`VALIDATION_STATUS` files are not redated. This is a bounded review, not an
exhaustive semantic review or a guarantee of error-free trading. Publication does
not authorize merging, deployment or trading.

## Primary sources

- [Pinned Freqtrade source](https://github.com/freqtrade/freqtrade/tree/1f394eaebc2f46a83d26971388628707802f8602)
- [Binance Spot user data stream contract](https://github.com/binance/binance-spot-api-docs/blob/master/user-data-stream.md)
- [GitHub runner architectures](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [Oracle Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

On 1 November, a fresh dependency scan disclosed seven pypdf advisories affecting
the earlier 6.21.2 pin. The candidate advances to 6.21.4 using PyPI wheel/source
hashes. A real malformed-PDF regression reproduced the missing-trailer hang in
the previous package and passes after upgrade. The 77 focused PDF, Sharia retrieval
and dependency-consistency tests pass per package; the six updated lock audits
report no known vulnerabilities. [Upstream security release](https://github.com/py-pdf/pypdf/releases/tag/6.21.4).
