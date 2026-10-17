# Freqtrade 2026.9 and admission repair

This change prepares the AWS Spot Testnet owner for Freqtrade 2026.9 and repairs admission state transitions. Deployment and exchange acceptance are separate gates; a source merge is not evidence that AWS has been upgraded. LIVE runtime activation is outside this change.

## Evidence for the missing trades

The read-only account-scoped sample ending 2026-10-15 21:56 UTC contained five distinct strategy candidates over 24 hours. Four failed the existing bullish-flow rule (taker-buy ratio at least 0.52 and positive CVD); the remaining FOGO candidate failed confluence with 0/7 votes. There were no accepted organic entries. At 22:06 UTC, authenticated RPC reported the owner RUNNING with all 120 active pairs analyzed, zero stale analyzed frames and zero analysis errors. A running process and an analyzed candle do not imply an eligible entry.

The recorded flow ratios were GTC 0.240912, OPEN 0.488910, and OSMO 0.372608 and 0.329858; all four CVD values were negative. There is no retained evidence that any of these candidates subsequently became eligible before expiry. Some raw strategy tags are also excluded by the existing no-rebuy policy. This repair does not bypass that policy, change signals, or manufacture a trade.

## Changes and regression evidence

The operator explicitly approved rechecking an unsubmitted weak-flow candidate within its original candle window. Previously REJECTED_FLOW was final for that candle. A candidate may now be checked again after five seconds, only while its original closed five-minute signal is still current. Every market, freshness, confluence and capacity gate runs again. Confluence rejection remains final for that closed candle. The 52% flow threshold, positive CVD requirement, 5/7 confluence threshold, 2/3 volume and momentum thresholds, 4 slots and 1000 USDT allocation are unchanged.

Regression tests also reproduced a separate capacity defect: an existing PAIR_NOT_READY intent could bypass the checks applied to a new intent, reserving a fifth 250 USDT position. Reservation now checks slots, total allocation, incidents, unknown obligations, signal expiry and exact expected intent state in one SQLite write transaction. A concurrent weak-flow rejection cannot overwrite an already reserved or submitted entry. Any order identity, fill, trade binding or protection history prevents readmission.

The 17 new admission tests cover recovery, restart, cooldown, expiry before and after a slow read or lock, frame identity, order/fill history, duplicate reservation, allocation and slot limits, concurrent changes, terminal/unknown states and bounded journal events. Before the repair the initial 11 regressions failed (eight assertions and three unexpected admission exceptions). After the repair all 17 pass. An independent source review added three of the race/state regressions and found no remaining actionable issue in this change.

The complete clean 2026.9 candidate image passes 244 offline tests with zero skips under a read-only filesystem and no network. The dependency-free owner subset passes 112 tests. Exchange lifecycle and soak results must be recorded against the actual AWS image before activation; local offline results do not replace those gates.

## Upstream compatibility and preserved strategy

Official release: <https://github.com/freqtrade/freqtrade/releases/tag/2026.9>

- Upstream commit: `1f394eaebc2f46a83d26971388628707802f8602`.
- Upstream source archive SHA256: `a453638c8ad59f8655036eaabd0858cb432331e70caa259976299e5c59108c89`.
- Root legacy-package upstream image index: `sha256:989aff5636d8f0213ed44fe334c3314a194a51c0c755d16866960247aafd2eae`.
- The clean owner materializer retains 376 allowlisted upstream files. Seven existing owner overlays were ported by a conflict-free three-way merge; reversing that merge reproduced every previous overlay after newline normalization.
- Five added upstream compatibility tests plus 50 existing focused tests pass against the actual 2026.9 framework. The hash-locked owner dependencies add upstream-required `cloudpickle==3.1.2`; CCXT remains 4.5.87.
- The NFI vendor file is unchanged: SHA256 `fcf9f622a3ad797bd143561d1017f3930220f22c793905af929ff1d33bbde686`.
- The root package's IctSmc strategy is also unchanged: SHA256 `9f6bafc78c8cd0d9b9cbde615ddce89e304ab09738584b88d05bfdf92ff4e830`. Its six probe assertions pass with both the standard probe and the real 2026.9 StrategyResolver. Spot mode, simulation default, disabled native entry and no-short policy are preserved.

The deployed owner uses BinanaNfiSpot; the root legacy package uses IctSmc. They must not be described as the same running strategy. The AWS sidecar is a market-data/advisory service; Freqtrade remains the sole order owner.

## Remaining limits

The change permits a recovered flow condition to be considered; it cannot guarantee a qualifying signal, fill or return. Recent public-market trade receipt can exceed the owner's five-second freshness budget on quiet symbols even when the broader advisory status says fresh. The owner's tighter check is preserved. The 120-pair active set is a qualified exchange/trading universe, not the entire halal registry. No expansion or threshold relaxation is included.

An AWS upgrade requires fresh exact-image offline, one-slot and four-slot soak, lifecycle, restore and rollback evidence; a signed release certificate; and updated renewal and source-publication records. Restore comparison must immediately follow its matching lifecycle run, before another probe changes exchange balances. Keep the current qualified image and database backup until those gates pass. No old-image receipt certifies the new image.

## October 18 verification and storage recovery

The clean 2026.9 ARM64 image also passed all 244 tests with zero skips under Docker Desktop emulation. The actual AWS candidate, image `sha256:960d789f0c9cbf986d00f37860c9189acb3da11c8fd0b5b9f105c0ecdcbf53ef`, passed ten consecutive full 244-test offline runs with no network and zero skips. It was built from source commit `5fcdc7e1548978613e2f4b75e70bc9f9d5edf673`; these results still do not claim an activated deployment or organic trades.

On resumption, the previous owner was paused by PERSISTENCE_DISK_CRITICAL. Its server had two nominal 4 GiB swap files and over 5 GB of available RAM. The redundant swap area's pages were safely moved back to RAM, the original fstab was backed up, and only the duplicate `/swapfile-oracle-trading-bots` entry/file was retired. The primary 4 GiB swap remains. Free disk space rose from approximately 4.28 GB to 8.57 GB; release blockers cleared and the previous owner was verified RUNNING before acceptance testing began. No trading threshold was relaxed to clear the disk guard.

Separately, a regression reproduced an installer defect: a nominal 4 GiB swap area reports 4,194,300 KiB after its 4 KiB header, which the old code rounded down to 4095 MiB and incorrectly treated as below the 4096 MiB target. Both setup scripts now compare exact KiB with the actual page-sized header allowance. Five tests cover target sizes, header sizes and insufficient/adequate capacity; four boundary cases failed before the change, and all 15 related installer/capacity tests pass afterward. This proves the source defect, not which historical command created the duplicate on AWS.

The GitHub matrix failed after its full offline package gate passed because monitoring dependency MCP 1.28.1 acquired advisory [GHSA-84m7-p3x7-pcfv](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-84m7-p3x7-pcfv). Both source packages now pin the compatible fixed 1.30.0 release and its official wheel/sdist hashes. The exact failing audit was reproduced, then the updated lock audit, fresh hash-locked dependency installation and pip consistency check passed. Added behavioral coverage checks finite session retention/capacity, all twelve monitor tools and bounded report dispatch. This dependency is separate from the owner image's 94-package closure.

The full October 18 Linux package verification passed: 707 unittest cases (one optional TA-Lib class skipped in this minimal package environment), 10 API tests, 63 monitoring tests and all 33 built-in self-tests. The separately executed pinned Freqtrade strategy probe covers that optional strategy stack. Release manifests, exact audit ledgers, secret checks, file modes, JSON, static service/timer checks and YAML structural checks passed. This Linux scratch container has no Docker socket or systemd-analyze, so its static checks do not claim a complete Oracle host installation.
