# Runtime and repository comparison — 14 October 2026

Times below use Asia/Karachi (UTC+5). This record distinguishes source publication, an installed image, runtime readiness and strategy signals.

## Verified AWS state

The accepted Binance Spot Testnet owner resumed at 00:56:13 on 14 October. The running image is `sha256:7b12004b889ad49384c310735bde26c93721300c936c94576f685ff079984a12` (`completion-20261013-r2`). Authenticated owner observations during this review showed RUNNING, release approved, no release blockers and no open positions. The owner container has not been restarted or paused during the display repair.

At 02:17, the corrected Telegram panel was deployed and verified healthy. Its source SHA256 is `5e24840e9d2391dc81757554a724ae878e5ee10d951bf37d7f419bd725b1a406`. Only telegram-broker was recreated. System → Deployment previously printed a September 19 rollout receipt; it now reads authenticated current Freqtrade state and fresh owner evidence. Displayed image identity must match the deployment bytes hashed by the owner. Unavailable information stays UNKNOWN. The host publication record is separately labeled and never guessed from an old `github_updated` boolean.

## Why no new trades were observed

The 24-hour analyzed-data inspection found 14 native NFI signals across 8 pairs; every one preceded the 00:56 resume. All 118 active pairs at the inspection had 288 analyzed five-minute candles and current indicator columns; the inspection had no API errors. No fresh native signal after resume was observed in that evidence. Historical candles are not orders to replay. The Telegram empty-signal view now includes owner state, strategy/timeframe and actual active-pair count. Scanning does not itself authorize or produce an entry.

## What the sidecar does

On this AWS Testnet deployment Freqtrade is the sole order owner. `execution-sidecar` runs `monitor_only.py` in simulation mode, checking shared registry/universe/incident evidence. It has no order authority. Its `trading_ready=false` monitor-only status does not mean the Freqtrade owner is paused.

## Why the number of pairs changes

The owner-maintained registry contains 375 approved assets. Scanner `UNIVERSE_LIMIT=0` means all eligible pairs, not a 120-pair cap. Availability as a trading USDT Spot pair, 30-day listing age, at least 1,000,000 USDT daily volume, maximum 0.5% spread, stablecoin/excluded-base rules and exchange order-list eligibility narrow that list. BTC, ETH, BNB, SOL and XRP remain excluded by the existing policy. The measured active count changed from 118 to 117 during the review; scanner and owner refreshes need not happen at the same instant. Rejection reason counts overlap.

## The fourteen retained backups

These are the latest fourteen successful daily state/release recovery archives, not fourteen bot installations. Together they occupy 768,163,765 bytes. Dates: September 28–29; October 1–8, 10–13. Each contains release identity/manifests, database snapshots and verification hashes. Separately named repair/rollback evidence is retained outside that daily-retention count. Twelve expired daily archives were removed during the earlier verified cleanup.

## Repository boundaries

Both repositories retain their current core strategy. Shared Binance/common modules compared after newline normalization are already aligned. The remaining shared-file differences include controller-label comments, Testnet owner-approved research commands, and a Testnet NFI universe rule that allows flat/negative movers. Those are not blindly transplanted into the LIVE strategy.

The AWS owner is Freqtrade 2026.8 plus the pinned NFI wrapper and BINANA Testnet execution modules. The LIVE root package still uses `IctSmcStrategy` with the order-owning sidecar and LIVE interlocks. It has no deployed or certified LIVE runtime. A Testnet-only owner must not be relabeled LIVE. The shared current Deployment view is repaired and regression-tested in both repositories.

All 35 repository owner Python files, including the previously missing custom exchange export, match the AWS owner by SHA256. Comparing the remainder against official Freqtrade 2026.8 found seven patched upstream modules, now all represented in the repository. Two unused legacy strategy copies remain inside the retained AWS base image; the new public-source build does not need that hidden image content.

The independent owner build is documented under Testnet `runtime_owner/deployment/README.md`. Private keys, API keys, account databases and effective private configuration do not belong in GitHub. A new image or host needs its own restored/initialized state and image-bound acceptance certificate; existing certificates cannot authorize different bytes.

## Regression evidence

Confirmed failing regressions preceded these repairs: stale Telegram deployment claims, unknown/mismatched deployment publication, missing custom exchange export, empty signal view lacking current context, and shared deployment view hiding safety halts/runtime faults. The focused local suite passes 22 tests (9 owner-panel, 4 shared-panel, 5 build-context, 4 archive security); the 4 shared-panel tests also pass in LIVE. The independent Linux/amd64 image built locally from public source with no retained AWS image and passed all 222 owner tests, with zero skips and no network/credentials/runtime mounts. The final 93-package dependency lock reports zero known advisories in pip-audit. The isolated owner CI selection also passed 112 tests. Oracle ARM and complete fresh-host owner installation are not established by these image tests.

Earlier Testnet deployment evidence: `docs/audit/AWS_DEPLOYMENT_RESULT_2026-10-14.md`. Testnet PR #38 merged at 01:24 on 14 October. LIVE PR #29 merged on 12 October; it was a source-only maintenance update, not a LIVE deployment.
