# Recovery candidate — 30 September 2026

Status: NOT ACCEPTED FOR DEPLOYMENT. This branch is a reviewable recovery checkpoint, not an approved release.

The supplied v19.3 controller is retained byte for byte (SHA256 418e7280f0b6a5f4cd9ba3887b8be3099f5fcc4b18bfca66808749720a4dd355). The candidate adds ordered source discovery, material economic-path/context review, special frameworks, official-source identity binding and exact retained-byte replay before owner approval. External Sharia screeners are advisory. Research results are separate from the owner-maintained operational registry.

The registry now contains 375 unique symbols. The owner's final instruction was additive: preserve all 305 existing symbols and add the 70 missing from the latest 177-symbol list. All earlier requested symbols remain. AWS projected all 375 signed records with the matching registry hash. HIMSBUSDT is retained in the registry but was not Spot/TRADING on Testnet at verification. Review dates were preserved, not renewed by this code change. Listing a symbol does not override independent market availability, excluded-asset, strategy or risk gates.

## Open release blockers

- Typed adverse verdicts (yield uncertainty and specific Sharia-sensitive structures) and per-item six-step retry evidence are incomplete. Pending research stays blocked; generic NO_TRADE_INFO is currently an operational denial, not proof that the controller's final retry gate passed.
- CoinGecko/CoinMarketCap credentials were absent in the AWS research environment at the latest check. Connector access in a desktop chat does not configure the server.
- Official-domain fallback uses owner-verified registered destinations; autonomous official-site traversal is not implemented.
- Repository Docker/Freqtrade packaging does not yet reproduce the running AWS 2026.8 owner plus its effective configuration. Current repository deployment instructions must not be used to replace that runtime.
- The AWS owner candidate passed 160 isolated Linux tests, but retained-inventory disposal, authenticated lifecycle cases, controlled Testnet execution, soaks and rollback acceptance remain outstanding.
- The supplied 143-URL list has not been read end to end. Access checks are not document review.

## Verified behavior

Unknown active material revenue proportions no longer become zero. PAYMENT_CURRENCY maps consistently to the output schema's PAYMENT during evaluation and approval replay. Missing, altered or unbound evidence cannot authorize GREEN. The immutable legacy core and IctSmcStrategy source remain unchanged.

The AWS bot remains paused with zero open trades. No real-money orders or LIVE deployment occurred. Bitcoin Testnet is outside this recovery's scope.

## Validation provenance

Final local service verification passed 657 tests (three platform skips). The separate API-readiness and monitoring run passed 68 tests with one platform skip. The repaired owner passed 160 isolated Linux tests. Secret scanning and critical correctness lint passed; the preserved upstream NFI snapshot has the narrowly documented pre-existing diagnostics described in runtime_owner/README.md in the Testnet repository. GitHub CI results must be checked on the published commit; these results are not authenticated exchange acceptance.

Exact authorized protected-file transitions are recorded in docs/audit/V193_AUTHORIZED_TRANSITIONS.json. Historical validation statements elsewhere in the package refer to the earlier release and do not certify this candidate.

Testnet scope: also recovers newer deployed Telegram/universe and authenticated manual-registry command service behavior. Its Compose service now receives the two existing command-bus keys required at startup. The newer owner source and tests are preserved under runtime_owner with exact provenance hashes. They are not activated by root Compose; runnable image/config integration and deployment acceptance remain outstanding.

CI follow-up: the broader pytest/coverage command passed 817 tests, 362 subtests, with six skips and one intentionally deselected simulation test (covered in the unit suite where applicable). Branch coverage is 68.32%, above the unchanged 68% floor and below the 85% target. The auxiliary host-loss scanner hash now matches the reviewed v19.3 baseline. Testnet-only Windows cache artifacts were quarantined outside the release. Both monitoring locks use hash-verified PyJWT 2.14.0; the dependency audit reports no known vulnerabilities. See the [upstream advisory](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-w6j9-cwv2-h6wq). GitHub CI must recheck the new commit.

## September 30 follow-up

The owner merged Testnet PR #29 (d60521ae4592078f362f6a3550a78e1d269fd5fe) and LIVE PR #24 (17d6ad4254c52c72d7f4ee514395230aa85ed733). Their source-head CI passed before merge. The new additive registry is hash-bound in REGISTRY_ADDITIONS_20260930.json.

Fresh authenticated AWS GET-only audit matched all 174 filled orders for 85 closed trades: zero quantity or cost mismatches. There are 73 retained balances with total cost 32.71048800073747509766618172 USDT; none was executable individually or in aggregate at that check. At that initial audit, historical MINA trade 29 still lacked one extension identity binding; this was subsequently repaired as recorded below. The accounting incident remains open while repairs and execution acceptance are incomplete. No Resume or real-money deployment is implied by this registry update.

Live metadata repair: all 73 retained-balance records were updated through one checked SQLAlchemy transaction. Every Trade field, every Order, schema and unrelated metadata remained unchanged. A repeat made zero changes. The fresh clone rehearsal verified complete rollback equivalence after an injected failure, and optimized Python was rejected. Backup: `/var/backups/binana-testnet/metadata-repair-20260929T224816Z`. Script SHA256: `546efb49335e2e01f2aaaeab6edba83070fb66c60493718d848bd402dae3b661`. Incident clearance and autonomous trading remain unapproved.

Eight older daily backups were compressed only after each archived file hash matched its source. The latest and accounting rollback backups were preserved uncompressed. AWS free disk increased from 1.7 GB to 4.2 GB.

PR #28 preservation: restore Telegram SIGNAL_HMAC_KEY wiring, exclude rotating backup snapshots, preserve operator notification/callback regressions, and default manual-projector preflight discovery to false while honoring explicit true for research. Targeted checks passed; full suite: 825 passed, 6 skipped, 406 subtests.

Historical MINA binding completed: one authenticated CANONICAL_EXIT identity for trade 29/order 64987 was added using the deployed StateStore API inside one transaction locking both databases. Repeat made zero changes; injected-failure equivalence and concurrent-writer rejection passed for both databases. Canonical data and other extension tables were unchanged. Backup: `/var/backups/binana-testnet/mina-binding-20260929T230026Z`. Script SHA256: `d8db7bd987f268d51ba10f7fee475e61f06d743ffc8cdc1d1062f688292132a1`. The incident remains open for runtime/lifecycle acceptance; accounting and identity repair are now complete.

Deployed Python source synchronization: `runtime_owner/deployed_r17/SOURCE_INDEX.json` binds all 24 custom owner/integration/strategy files, and `runtime_owner/deployed_services/SOURCE_INDEX.json` binds 494 Python source references across the six running service containers. Fourteen owner differences and 40 distinct service differences are preserved; unchanged sources are referenced to avoid duplicate copies. These indexes distinguish deployed bytes from the newer candidate. Effective image/Compose/configuration integration remains outstanding.

Merged-main CI was also verified successful after the owner's merges: Testnet run 36637403076 on d60521ae4592078f362f6a3550a78e1d269fd5fe; LIVE run 36637389234 on 17d6ad4254c52c72d7f4ee514395230aa85ed733.
