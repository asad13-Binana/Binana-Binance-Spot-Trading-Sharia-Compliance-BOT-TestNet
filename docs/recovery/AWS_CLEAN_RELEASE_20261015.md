# BINANA AWS Testnet continuation — 15 October 2026

This report supplements the October 14 deployment report. Times are Asia/Karachi (UTC+5) unless labeled UTC. Uploaded audit documents were treated as evidence to verify, not independent authorization to change accounts or policy.

## Runtime and release

The clean owner was deployed on the existing AWS host and resumed at **18:29:12 on October 15, 2026** (13:29:12 UTC). Authenticated state is RUNNING, dry_run=false, Spot Testnet, with no resume blockers. The accepted release is `binana-testnet-freqtrade:clean-20261015-r2`, image `sha256:b38bdf557b1b6af0408cc13402d64ea232e201940eca663b41128bdda0403206`. It was built on the existing AWS host from public source commit `5bdab1e7daf3b4a79d027574bc7ea735657a7c4c`, whose tree matches merged Testnet commit `5f4b19f5fdf0fbe18519c81de097c4a06c7c67aa` (PR #40).

The exact image passed ten consecutive 222-test offline runs, the one-slot and four-slot timed OCO checks, restart/exits, a contiguous read-only backup restoration and the actual candidate/previous-owner/candidate rollout. Accounting was preserved. A fresh image-bound certificate was issued, automatic daily renewal was rebound to this evidence and successfully executed, and the broker's normal checked Resume path started entries. The renewed certificate expires October 22 at 18:28:46 (13:28:46 UTC); the timer runs daily around 08:45-08:55 Pakistan time.

Private evidence roots are `/var/backups/binana-testnet/clean-20261015-r2-evidence`, `/var/backups/binana-testnet/controlled-owner-20261015T132538Z` (lifecycle/restore), `/var/backups/binana-testnet/controlled-owner-20261015T124915Z` (one slot), `/var/backups/binana-testnet/controlled-owner-20261015T130023Z` (four slots), and `/var/backups/binana-testnet/completion-rollout-20261015T132701Z` (rollout/rollback). Credentials and raw exchange responses remain private.

Before maintenance, an authenticated observation at 17:29:55 confirmed the prior owner RUNNING, Spot Testnet execution, release approved, no blockers, no open exchange orders/lists and no open canonical positions. Its container had been running since October 14 at 00:53:59. The previous authenticated resume was October 14 at 00:56:13. This refresh corrected the outdated claim that it was still paused throughout that interval.

The core remains BinanaNfiSpot/NFI X8, five-minute candles, 1,000 USDT allocation, four slots and nominal 250 USDT. The private source configuration SHA256 remains `eabd0e6b44b2fe9d74dd054e3a22077c96ce4bc37c5fefc2016c9d194b002123`. No LIVE account, LIVE order or LIVE runtime was used.

## Why signals did not become trades

The current-account journal recorded fourteen native strategy candidates after the October 14 resume. Seven received a final bullish-flow rejection and seven a final confluence rejection. Four transient PAIR_NOT_READY observations were subsequently retried into those final decisions; these are not four additional unique signals. Eleven candidates remained within the later 24-hour analyzed-data window, across six pairs. All 120 active pairs had 288 analyzed five-minute candles ending at 12:25 UTC on October 15; there were no dataframe API errors.

These are actual strategy signals, followed by existing admission gates. For example, the FOGO candidate at 02:40 UTC had confluence score 0/7 against required 5/7; the EPIC candidates at 15:20 and 15:25 UTC on October 14 scored 2/7. The recorded inputs include weak RSI/MFI and price below the rolling VWAP/EMA. No evidence supports bypassing these rules merely to manufacture an order. Changing those thresholds changes trading policy, so they were preserved.

The bot deliberately does not push every raw candidate as a Telegram order alert. Filled/submitted trade events use the durable notification outbox. Candidate observations remain available through Signal History and Last Signal. The repaired views now show their admission decision and candle identity, so a rejected candidate is distinguishable from an absent signal.

## Confirmed repairs and test evidence

| Defect or maintenance gap | Repair and evidence |
|---|---|
| Deployment button displayed the September 19 receipt | Authenticated current owner state, fresh approval and hash-bound image identity replaced the static JSON. UNKNOWN is preserved when evidence is unavailable. |
| Oversized shared deployment JSON hid later safety fields | Bound individual details, retain complete JSON and all top-level status fields. Reproduced and fixed in both repositories; four shared-view regressions pass. |
| Signal History discarded observations older than one hour and omitted decisions | Display 24-hour candidates and match current-scope admission decisions by pair, candle and tag. Actual AWS preview shows the recorded flow/confluence reasons. |
| Separate candles with the same pair/tag collapsed | Include candle identity in deduplication; failing regression now passes. |
| Last Signal mixed account/epoch scopes; unrelated events displaced candidates before filtering | Filter the canonical query by current scope and event type before limits. A real SQLite test with 501 unrelated events confirms the candidate remains visible. All thirteen panel regressions pass. |
| Clean owner source missed the custom exchange export | Added the exact deployed `BinanaBinance` export. All seven patched upstream modules are represented; all 35 owner Python files match the previously accepted AWS owner. |
| New image could pass unit tests but fail existing AWS Compose startup | Reproduced missing named user/path failure (exit 125). Added the `binana` user with unchanged UID/GID and compatible executable path. Exact AWS launch-contract check now exits 0; CI runs it too. |
| Owner rebuild depended on an old private AWS image | Build from SHA256-verified official Freqtrade 2026.8 source, reviewed patches, a digest-pinned Python base and hash-locked dependency/build closures. AWS built without using any retained BINANA image as a base. |
| Known dependency advisories in the old owner closure | Upgrade compatible CCXT to 4.5.87, multidict to 6.9.1, urllib3 to 2.8.0, PyJWT to 2.15.0, setuptools to 83.0.0 and wheel to 0.46.2. The refreshed October 15 pip-audit reports zero known advisories across the 93 locked packages. |

The corrected exact AWS image passed ten consecutive runs of all 222 owner tests with zero skips, network disabled, no credentials and no runtime database mounts. This is 222 distinct tests repeated ten times, not 2,220 distinct cases. The earlier clean candidate also passed unit tests but failed its actual startup-contract test; it was rejected and removed rather than presented as deployment-ready.

The October 12–14 OCO residual replacement, immutable protection-plan revisions, bounded cancellation verification, reference-price/account-filter compatibility, freshness, durable accounting, certificate issuance/renewal and notification repairs remain in the unchanged owner source. Their individual dispositions and regression limits are recorded in `docs/audit/AWS_DEPLOYMENT_RESULT_2026-10-14.md`. The new independent build closes that report's retained-base dependency gap (F18/F20); it does not certify every fresh-host deployment path.

## Acceptance sequencing and disk recovery

The first restoration attempt correctly failed its exchange-balance check. The lifecycle baseline preceded the one-slot and four-slot rehearsals; their later balance changes were absent from that lifecycle's receipts. The observed USDT delta was -2.69902000, exactly the sum of the lifecycle (-0.47135000), one-slot (-0.98624000) and four-slot (-1.24143000) results. This was a sequencing error in the operational qualification wrapper, not a reason to disable accounting reconciliation.

The old owner container restarted, but entry approval remained blocked by PERSISTENCE_DISK_CRITICAL: 4.56 GB free was below the existing 5 GiB minimum. Verified-unused Puppeteer downloads, Docker build cache and apt package cache were cleared; none was a trading database, credential, backup archive or accepted image. This recovered 1,856,983,040 bytes and restored 6,418,329,600 bytes free. A fresh lifecycle was then immediately followed by the read-only restore, and both passed. The first failed attempt remains preserved separately. The passed ten-run offline and one/four-slot evidence was reused for the same unchanged image and configuration.

## Sidecar, pairs, providers and backups

On this AWS Testnet host, Freqtrade is the sole order owner. The execution-sidecar runs `monitor_only.py` in simulation mode, checking shared registry/universe/incident evidence without order authority. Its monitor-only `trading_ready=false` is not the Freqtrade pause state.

The approved registry has 375 assets. `UNIVERSE_LIMIT=0` means no 120-pair cap. Eligible USDT Spot availability, listing age, volume, spread, stablecoin/excluded-asset rules and exchange order-list support narrow it to the current active set. The observed count varied between 117 and 120. BTC, ETH, BNB, SOL and XRP remain excluded by existing policy.

The October 15 11:23 UTC provider report records authenticated PASS for Binance Testnet (`can_trade=true`), CoinGecko Demo, CoinMarketCap and Telegram. The CoinGecko circuit breaker had zero failures and its budget was not quarantined. External research data remains advisory and does not replace Binance execution prices.

Fourteen retained daily archives are recovery copies, not fourteen bot installations. All fourteen archive hashes were rechecked against their receipts on October 15. They occupy 782,851,960 bytes and cover September 29; October 1–8 and 10–14. Named repair/rollback evidence is retained separately. The earlier cleanup removed twelve expired daily archives; today's cleanup first removed the unused failed clean-image candidate and build cache, then the disposable caches described above, increasing free disk from about 1.85 GB to 6.42 GB. Live databases, credentials, accepted images and rollback evidence were preserved.

## GitHub and portable deployment boundaries

After deployment, SHA256 comparison verified all 35 running owner Python files against the published source with zero mismatches. The deployed Telegram panel hash is `89cc66b7e7b99f900dc5e66a8bc68552fa562e5aa470f7e6db7c6abbb30d10c2`. The host publication record binds that source to the accepted image. At 18:36:36, the actual panel renderer displayed RUNNING, TESTNET, release approved, no blockers and the verified image/commit; Signal History showed ten distinct candidates and their admission reasons, with configured stake 250 USDT.

The checked-in host image map SHA256 is `d26cdb1dc42cc21e5f8782de630db485fe525206a3fb0349221b09726e5080e7`; the Compose image overlay SHA256 is `ac4412a0c8ea052cdae70350c9a8883457cfe8e70a741d3600d0a967f2b599f9`. Both are exact byte copies from AWS, including the new owner image. Source-capture provenance and earlier dated receipts remain historical records, not current runtime status.

Testnet PRs #39 and #40 are merged, with passing CI and completed code/security reviews. LIVE PRs #30 and #31 are merged, including the shared Deployment-view repair, its regressions and the current maintenance-scope report; CI and code/security reviews completed successfully. LIVE remains a source-only maintenance target: its root package uses IctSmcStrategy with an order-owning sidecar and LIVE interlocks. It cannot safely be made identical to the Testnet-only NFI owner by relabeling files. Shared source is synchronized where the behavior is shared; architecture-specific differences are documented.

A clean image is now independently buildable from GitHub source. The same source also built successfully for linux/arm64 on Docker Desktop; 222 offline tests passed with zero skips under emulation, and the existing named-user/executable startup contract returned Freqtrade 2026.8, Python 3.14.7 and CCXT 4.5.87. This is an ARM64 build/runtime check, not acceptance on a real Oracle host. A complete new AWS/Oracle host still needs private configuration, account-scoped database restore/initialization, correct current owner topology, credentials and fresh image-bound acceptance. The legacy root Compose topology is not a drop-in installer for this AWS owner. No new paid cloud resource was created. No promise of perpetual free hosting or zero future errors is made.

The ten requested OpenAI repository reviews and pinned source inventory remain in the original consolidated audit. They informed isolated regression tests, evidence-linked findings, constrained review tooling and deterministic release gates. Agents and model output have not been placed in the exchange order path. Selected source and relevant official documentation were reviewed; an exhaustive reading of every linked repository and website is not claimed.
