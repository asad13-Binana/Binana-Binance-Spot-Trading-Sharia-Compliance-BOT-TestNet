# AWS recovery status — 7 October 2026

The Testnet research service now runs the supplied v19.3 controller. The trading owner remains r17, paused with zero open trades and one recovery incident. This update does not certify trading readiness.

## Applied and verified

- The 375-symbol operational registry is unchanged: the prior 305 were preserved and only the 70 missing symbols were added from the owner's latest 177-symbol list. Review dates and independent market/exclusion gates remain in force.
- Testnet PR #30 and LIVE PR #25 were merged by the owner. Both merged-main CI runs passed. PR #28 was closed as superseded after its useful fixes were preserved in #30.
- Only the AWS research container was upgraded from merged Testnet commit 6611df5792451f4c7c21092640d93c79bb9274dc. Its healthy process loaded controller SHA256 418e7280f0b6a5f4cd9ba3887b8be3099f5fcc4b18bfca66808749720a4dd355. The other six container IDs and the signed manual registry remained unchanged.
- The startup script referenced a deleted owner Compose file. It now references the existing owner overlay, the research upgrade and an overlay pinning all seven running image digests. Thirteen effective configuration checks per service matched the running containers. Shell syntax, the systemd enablement and condition file passed. A full reboot/startup was not performed.
- A fresh authenticated, read-only exchange audit matched 174 fills across 85 historical trades: zero quantity, cost or identity mismatches. The previous 73 retained-metadata repairs and MINA exit binding remain verified. A fresh Testnet filter/book check classified all 73 residuals across 35 pairs as non-executable, individually and in aggregate. The incident detail now identifies the remaining acceptance work; its OPEN status was preserved.
- Six additional older daily backup directories were compressed, with every file's hash, size and modification time verified before removing their uncompressed copies. The latest backup and accounting rollback backups remain. Free disk is 3.6 GB after the new validation builds. No global Docker pruning was used.

The research service is isolated from operational registry promotion, has idle scanning disabled and no exchange credentials. CoinGecko/CoinMarketCap keys were still absent at verification. Existing release-envelope binding and egress proxy configuration were preserved.

## Reproducible evidence and limits

[Deployment files](../../runtime_owner/deployment/README.md) now include an allowlisted owner build, the non-secret observed policy/topology, and the exact repaired startup script and image overlay. The owner candidate image passed its 160-test suite in ten separate credential-free, read-only, networkless containers. The research image passed 93 targeted tests. Four context-materialization tests verify source tampering, traversal, exclusion of private files and refusal to merge into an existing directory.

The build depends on the retained AWS r17 base image. A clean-host rebuild of that base and a complete runnable export of the private AWS configuration are still unavailable. The legacy root Compose package must not replace the current AWS owner. The new image defaults to validation only and was not activated as the trading owner.

See [machine-readable verification](AWS_VERIFICATION_20261007.json). Earlier deployed-source indexes describe their capture time; after this upgrade the research service maps to the merged commit above. The topology snapshot was captured immediately before the research upgrade. Current image digests are in the new overlay.

## Remaining work before Resume

The owner candidate still needs aggregate retained-inventory lifecycle handling, authenticated protection/restart cases, a controlled Testnet lifecycle, one-slot and four-slot soaks, and rollback acceptance. Offline tests are not substitutes for these cases, and no acceptance certificate was fabricated. The resume gate continues to deny entries while the incident is open.

The v19.3 research implementation still lacks complete typed adverse verdicts, per-item six-step retry evidence and autonomous official-domain traversal. Old Telegram labels can still show v19.1. Research health and the controller hash do not establish complete controller compliance.

[Documentation coverage](DOCUMENT_COVERAGE_20261007.json) records all 143 supplied URLs mapped to 93 distinct documents. Six retrievals failed and two outputs were truncated; extraction does not establish end-to-end reading. These limits are retained explicitly.

No LIVE deployment, real-money orders, or changes to the separate Bitcoin Testnet bot occurred.

## Dependency audit follow-up

The new CI run detected newly published advisories affecting existing pins. Both packages now pin multidict 6.9.1, pypdf 6.19.0, urllib3 2.8.0 and monitoring PyJWT 2.15.0 with upstream PyPI distribution hashes. Both exact lock-file audits pass after the updates. Upstream references: [multidict](https://github.com/aio-libs/multidict/security/advisories/GHSA-54p9-h82j-f925), [pypdf](https://github.com/py-pdf/pypdf/releases/tag/6.19.0), [urllib3](https://github.com/urllib3/urllib3/releases/tag/2.8.0), [PyJWT](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-x33g-cr3x-6449). A passing source audit does not update older running images.

## Authenticated candidate rehearsal and historical state repair

A GET-only candidate probe against copies of the repaired AWS databases found three historical intents (trades 34/NEAR, 47/ONE and 53/LPT) whose states had been overwritten as rejected despite completed retained-balance settlement. Fifteen scoped exchange order identities were verified terminal. The candidate now rejects repeated admission before it can overwrite an existing Trade or order generation; its revised 162-test suite passed in an isolated image. This prevention change is not deployed to the trading owner.

The guarded script `scripts/recovery/repair_retained_intent_states_20261007.py` pins the authenticated evidence, validates retained economics and terminal identities, locks both databases and verifies full logical preservation. Rehearsal changed three state cells; repetition changed zero, injected failure rolled back completely, and a concurrent canonical writer was rejected. The repaired clone then passed closed-acknowledgment and authenticated REST reconciliation, with 35 retained pairs and none executable.

After review, the same three-state repair was applied on AWS with a fresh backup at `/var/backups/binana-testnet/retained-intent-repair-20261007/live-before`. Repetition changed zero. Canonical data, incidents and every other extension field were unchanged. The original recovery incident remains open; this is not an authenticated order-lifecycle or soak certificate.

## Research dependency rollout

After both updated PR heads passed CI, the research service alone was rebuilt from 0c4a9d82dcf117a516eb5c77a0052daa74441266 and deployed using the immutable image b74ef9a872624554a9377c50af7eec878ed2cd828730151a24970fbd1d8d58e4. Runtime imports confirm multidict 6.9.1, pypdf 6.19.0 and urllib3 2.8.0. Fresh health and controller hash checks passed. The manual registry and the other six container identities remained unchanged. Their older dependency sets, including the retained owner base, were not upgraded.

The persistent startup image overlay now pins this research digest. The reviewed rollback script is preserved under scripts/recovery; its private backup and receipt remain at `/var/backups/binana-testnet/research-security-20261007`. Free disk after the security build is 3.1 GB. Only the unused failed owner validation image was removed; running and rollback images were preserved.

CI passed on the runtime-source commits: Testnet run 37538798885 and LIVE run 37538445296. The deployment-record commit d81ef2f passed CI run 37540289790. Subsequent changes require their own CI result.

## Full startup rehearsal and zero-fill state repair

A full GET-only startup probe on copied databases found 25 older intents whose terminal zero-fill states had been overwritten by later signal rejections. Their generation records still said CLOSED_NO_FILL, but missing legacy plan data caused entry reconstruction to fail. The initial probe reported startup and shutdown success while creating 25 clone-only incidents; that result was not accepted as trading readiness.

A fresh authenticated audit verified all 25 lists and 75 child orders as terminal with no fills, no associated exchange trades and no open exchange obligations. The hash-bound state-only repair passed clone application, idempotency, injected rollback and concurrent-writer rejection. It then changed exactly 25 intent states to CLOSED_NO_FILL on AWS; repetition changed zero. Canonical data, incidents and every other extension field were preserved. The backup and receipts are at `/var/backups/binana-testnet/zero-fill-intent-repair-20261007`.

The candidate also now propagates per-intent entry-recovery failures into canonical readiness instead of reporting ready when no Trade is open. Two regressions reproduced the old defect before the fix; the updated 164-test suite passed in ten separate isolated containers. Its immutable r4 image is a90b7f661500f7d00b3da32ce4c22d987d41d6da01483affe849a0baccddb523. A stricter full startup rehearsal on repaired copies loaded 106 active pairs, authenticated REST and the private stream, reconciled 35 retained pairs with none executable, preserved only the original incident, and shut down cleanly. Exchange writes were blocked throughout. This candidate remains undeployed, and these checks do not certify order lifecycle, active-position restart or soak acceptance.


## October 7: authenticated parameter validation and further cleanup

The unchanged immutable r4 candidate passed 15 authenticated Binance Spot Testnet `/api/v3/order/test` checks: FOK entry, take-profit and stop-loss parameters for LTC, LINK, ADA, NEAR and PAXG, using the configured 250-USDT nominal stake and the candidate commission-reserve code. These requests never reach the matching engine. The probe explicitly rejects trading/cancellation endpoints, mainnet and redirects. Four local regressions cover those boundaries and require every host-preservation check for success. Both review axes reported no remaining findings after fixes.

The AWS owner remained paused; its container/image, trade/order counts and single open recovery incident were unchanged. Zero exchange orders/lists remained. This is parameter-validation evidence only: OTOCO activation, fills, protection replacement, active-position restart, rollback and soak acceptance remain incomplete. No release certificate was created and trading was not resumed. Provider-key presence was rechecked: CoinGecko and CoinMarketCap were both absent from the research container.

Five older BINANA backup trees from September 19 and 23 were archived. Every archived file was verified against its source SHA256 and size; source metadata and mount boundaries were rechecked before deleting duplicate folders. Archives and recent rollback backups remain. The cleanup recovered 1,503,399,083 bytes of file payload; root free space increased from 2.6 GiB to 4.1 GiB (reported by df -h). No global Docker prune or Bitcoin bot operations occurred. See ORDER_VALIDATION_AND_CLEANUP_20261007.json for receipts.

## Market collector recovery, 13:23 UTC

The collector rejected the valid one-letter assets S/USDT and U/USDT, retaining an obsolete 56-symbol universe. Its 0.07 CPU limit also caused delayed events and repeated book resets. The universe-only deployment now accepts one-letter identities, supports its bounded 200-symbol/600-stream capacity, publishes universe failure consistently to status and owner consumers, and runs with 0.75 CPU/512 MiB. No strategy or freshness threshold changed. The first observation recovered 126 subscribed symbols, 82 fresh symbols and 125 sequenced books, with no malformed events or reconnects. The other six BINANA container identities remained unchanged. Sustained observation and trading-owner acceptance are separate checks.

PR #31 had two unresolved image-reference review threads. Startup now checks retained local tags against separately recorded image IDs; recovery builds use the same preflight. No branch protections were disabled.
