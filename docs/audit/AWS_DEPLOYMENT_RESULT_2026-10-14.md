# BINANA Testnet repair and AWS deployment report

This consolidated report supersedes the October 12 partial-results report and records the completed Testnet rollout observed on October 14 PKT. Original user audits and historical failed receipts remain preserved.

## Current deployment observation

**DEPLOYED AND RUNNING ON AWS.** The authenticated resume completed at 00:56:13 PKT on October 14. At 01:03:45 PKT, the new order owner was RUNNING, all seven containers were healthy, release approval was true, and there were no release blockers. The original 85 trades and 176 orders were preserved; there were zero open canonical trades, exchange orders or order lists at that observation.

Both provider integrations are enabled and have fetched data. The owner has a read-only root, init enabled, all capabilities dropped and no-new-privileges enforced. Of its bind mounts, only the owner database/runtime subtree is writable. The issuer key is mode 0600 and is not mounted into any running container.

The certificate was successfully renewed while trading remained enabled; it is valid until **01:00:54 PKT on October 21**. Its active daily timer checks renewal at 03:45 UTC plus up to ten minutes of jitter; it renews when fewer than two days remain, subject to unchanged bindings and fresh evidence. This fixes the renewal workflow without bypassing readiness. The existing fail-closed startup policy remains: a restart starts the owner stopped and requires a qualified resume.

The bot analyzed all 120 active pairs. Later spot checks confirmed advancing closed candles; the affected 2Z pair had current 5-minute, 15-minute, 1-hour, 4-hour and daily context. No organic entry signal appeared in the 120-pair observation. One Binance -1001 candle-request warning occurred, was retried, and recovered. Continuous external API availability and future trade opportunities are not guarantees.

Candidate image: `sha256:7b12004b889ad49384c310735bde26c93721300c936c94576f685ff079984a12`, tag `binana-testnet-freqtrade:completion-20261013-r2`. Owner source and its 222-test manifest were built from commit `ea3be56c524a57e3d1e209e966b2498825a52a64`. Later maintenance-script and documentation changes do not alter those owner bytes.

The retained rollback owner is `sha256:a90b7f661500f7d00b3da32ce4c22d987d41d6da01483affe849a0baccddb523`. The recovered base image is retained too. The original Spot Testnet configuration hash is `eabd0e6b44b2fe9d74dd054e3a22077c96ce4bc37c5fefc2016c9d194b002123`: allocation 1,000 USDT, four slots, nominal 250 USDT, existing NFI strategy, 5-minute timeframe and excluded assets preserved.

## Why trading stopped, and what prevented the earlier deployment

There were two distinct periods. Before maintenance, the inspected signal journal showed strategy/confluence rejections, including pair readiness, bullish flow and order-book confirmation. These observations do not establish that Binance refused all trading. The independent Testnet account check returned Spot capability and `canTrade=true`.

During repairs, entries were intentionally paused and the old owner was later stopped to freeze its accounting databases. The earlier work delivered source repairs before completing exchange acceptance, certificate issuance and the immutable owner rollout. That left the operational request unfinished. CoinGecko key formatting and CoinMarketCap's disabled optional-provider flag were separate provider issues, not a justified explanation for every missed trade.

The four-position rehearsal then exposed a specific promotion problem: immediately after canceling an OCO list, an authenticated child query could still report an old child open. The code entered `CANCEL_UNKNOWN`; later reconciliation recovered protection. The instrumented run at `controlled-owner-20261013T185700Z` recorded `CANCEL_CHILD_VERIFICATION_FAILED:AdmissionRejected`, identifying the `OLD_CHILD_STILL_LIVE` branch. Binance documents asynchronous data visibility in its [REST data-source contract](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#data-sources).

The repair rechecks both old child identities with three bounded delays before replacement. It never repeats the cancellation or order POST. A persistent live child, read failure or identity mismatch still blocks replacement. After a successful promotion, the full authenticated child geometry and open-order reconciliation runs in the same cycle, so the owner need not wait for its next process cycle to verify the new protection. The strategy's entry conditions, allocation and trailing parameters were preserved.

## Evidence and its limits

| Check | Observed result |
|---|---|
| Exact new AWS image, isolated offline validation | Ten runs of 222 tests; all passed, zero skips; no network, credentials or runtime database mounts |
| Promotion regressions | Delayed visibility and same-cycle verification failed before repair; four new tests cover successful recovery, persistent live children and foreign identities |
| Targeted owner suite | 43 tests passed after repair, including partial-fill residual and durable-plan tests |
| Required CI owner subset | 112 tests passed locally with network/process calls denied; the promotion regressions are now included in CI |
| Four positions | Passed 601.72 seconds with four protected positions and 1,000 USDT reserved; active restart, exits and closed restart passed; original databases preserved |
| One position | Passed 600.55 seconds protected, then active restart, exits and closed restart; original databases preserved |
| Full lifecycle and backup restore | Passed entry, two active restarts, exit and closed restart; SQLite backup/restore logical hashes matched; restored-owner exchange access was GET-only |
| Candidate → rollback → candidate rollout | All three stages passed; only the freshness timestamp of an already-closed incident changed, with its content and all economics preserved |
| Signed certificate and renewal | Issuance, installed systemd service/timer and forced unchanged-release renewal passed; renewal ran another ten complete 222-test offline suites |
| Compressed backup retention | Four tests pass, including corrupted retained archive, missing receipt and historical receipt compatibility; actual retention removed twelve old archives and preserved fourteen |

The suites overlap: their counts must not be added as unique tests. Controlled Testnet admissions exercise execution and reconciliation, not organic NFI signal generation or profitability. Partial-fill/base-fee/ambiguous-acknowledgement cases have deterministic regression coverage; arbitrary real matching-engine races are not exhaustively reproduced. The restore check uses a completed flat lifecycle; active-position restart is a separate check.

Failed rehearsals are retained. Two earlier NEAR runs ended when the exchange stop legitimately filled. An ETH trial was rejected by the unchanged excluded-asset policy before orders. A DOT transport-scope defect in the test harness was fixed with failing-before regressions and all-pair preflight. Two DOT promotion runs exposed the delayed-cancel path; cleanup closed their known test positions. None of these is represented as a successful ten-minute soak.

Private raw logs and database receipts are retained under `/var/backups/binana-testnet`. Current-image evidence uses `completion-20261013-r2-evidence`; earlier evidence uses `completion-20261012-evidence`. Credentials are excluded from public source, reports and delivery files.

## Deployment, provider and cleanup work

The deployed Telegram panel uses a durable notification outbox. It acknowledges delivery only after Telegram reports success, retries after restart, and uses a shared per-chat limiter with 429 backoff. This provides at-least-once delivery; a crash after Telegram accepts a message but before the local acknowledgement can still produce a duplicate. The panel also uses exact current trade/intent records, distinguishes unavailable accounting from zero, and shares the owner's sizing policy and readiness checks.

CoinGecko's Demo credential was corrected to its required format and authenticated with real data requests. CoinMarketCap's key authenticated; its universe feature flag was found disabled and explicitly enabled on AWS. Its authoritative usage reconciliation cleared the initial budget quarantine. Both fetched data successfully; neither breaker is open and neither quota ledger is quarantined. CoinMarketCap key/info reports a 15,000-credit monthly and 50-request/minute plan, above the aggregate configured ceilings of 14,400 credits and 48 requests/minute. Both integrations remain advisory; they do not replace exchange execution prices or alter the core strategy.

Verified cleanup removed unused verification dependencies, unused download caches and obsolete image tags while preserving the current image, rollback image, retained base, live databases, credentials and unique repair evidence. Daily backups are compressed only after archive content verification. The recurring compactor now also honors the existing default retention of fourteen daily archives and preserves named repair/rehearsal/rollout evidence. Historical receipt formats are validated by their archive hash and size; absent original duplicates are checked directly. Fourteen verified archives remained after twelve expired copies were removed. Disk availability subsequently measured approximately 6.5 GB on the existing server.

No additional AWS instance, disk resize or paid monitoring resource was created. This does not guarantee perpetual free AWS hosting. No LIVE trading or LIVE runtime deployment occurred.

## Disposition of all 44 supplied findings

“Implemented/tested” describes source and regression evidence. The owner changes are now deployed in the exact image above; the table retains limitations instead of claiming every audit concern is closed.

| Finding | Disposition and remaining boundary |
|---|---|
| F01 | Actual owner-volume free-space, inode and fsync gates implemented; host cleanup and recurring compression/retention applied. Continued disk monitoring remains necessary. |
| F02 | Replacement residual quantity and complete plan persist before the order POST. |
| F03 | Status changes preserve immutable plan/profile; verified quantity reductions have a revision journal. |
| F04 | Replacement-pending states participate in deterministic absence reconciliation. |
| F05 | Replacement preflight runs before cancellation and again before submission. Cancel/query delay repair added; exchange races still require reconciliation. |
| F06 | Authenticated native child type, list identity, quantity and price/trigger/trailing geometry must match the persisted plan. |
| F07 | Reference-price-first filter checks and guarded Testnet adapter implemented; malformed or unavailable evidence fails closed. |
| F08 | MAX_POSITION includes free, locked, pending buys and proposed quantity; account/symbol/exchange capacity checks implemented. |
| F09 | Current last price and book checks added. A snapshot cannot eliminate price movement between preflight and matching. |
| F10 | Native surviving working exit is recognized only with exact ownership and working-state evidence. Ambiguous states remain blocked. |
| F11 | Order-specific fill pagination continues beyond 1,000 rows; stalled/incomplete history blocks reconciliation. |
| F12 | Conservative UNKNOWN handling remains for uncertain submissions. This may delay recovery but prevents blind duplicate orders. |
| F13 | Used reference-price/account-filter compatibility paths verified. Unused executionRules/amend adapters are not claimed as qualified. |
| F14 | Ed25519 envelope and independent public-key anchor implemented; host trust-boundary and private issuer isolation verified in the successful rollout. |
| F15 | Code, effective public configuration, account, epoch, dependency and deployment bindings implemented; Docker image/mount/environment attestation required. |
| F16 | Host renewal tooling reuses qualified acceptance only for unchanged bindings, after ten fresh offline runs and current readiness. The installed timer is active and an actual forced renewal succeeded while RUNNING. |
| F17 | Certificate activation uses one atomic, fsynced signed file; original certificate is archived. |
| F18 | CI owner gate, multi-Python checks and exact-image offline runs implemented. The recovered owner's clean-host base rebuild remains open. |
| F19 | Explicit required test identities, duplicate detection and skip rejection implemented; current manifest requires 222 tests. |
| F20 | Open: owner build still depends on the retained recovered base image. |
| F21 | Actual AWS overlay/start chain is documented and used by rollout tooling. Legacy root Compose is not the recovered-owner installer. |
| F22 | Deployment/maintenance documentation distinguishes source, image tests, authenticated acceptance and runtime state. |
| F23 | Canonical repaired panel deployed on AWS. |
| F24 | Unavailable local RPC is UNKNOWN, not zero positions. |
| F25 | Exact current trade-to-intent association replaces historical same-pair fallback. |
| F26 | Active/current trade queries have no global last-20 truncation. |
| F27 | Panel readiness includes owner/API/poller dependencies. |
| F28 | A real zero profit remains zero. |
| F29 | Retained dust requires verified non-executable accounting evidence; no new dust liquidation was introduced. |
| F30 | Economic settlement time is required; metadata-update time is not a substitute. |
| F31 | Accounting failure displays UNAVAILABLE rather than fresh zero profit. |
| F32 | Durable outbox with acknowledgement after transport success deployed. |
| F33 | Shared durable per-chat rate limiting and 429 deferral deployed. |
| F34 | Signal views filter current scope, epoch and time; organic strategy performance is not certified by manual admissions. |
| F35 | Sizing bounds and Self-Test share the effective owner policy; allocation unchanged. |
| F36 | Panel uses init/reaping; candidate owner rollout requires init. Other unchanged services are not all claimed to have been migrated. |
| F37 | Neighboring Bitcoin bot's backup failure remains outside this BINANA runtime repair. |
| F38 | AWS free-plan eligibility/credits remain an account constraint, not a software defect that can be guaranteed away. |
| F39 | Earlier inspection found no CloudWatch alarms. No new paid-resource monitoring was provisioned. |
| F40 | Owner rollout requires read-only code/config/release mounts, dropped capabilities and only the required database subtree writable. All restrictions were confirmed on the running candidate. |
| F41 | LIVE backup-selection repair merged earlier; LIVE runtime untouched. |
| F42 | Optional provider defaults stay false in distributed examples; AWS explicitly opts in with private credentials. |
| F43 | LIVE positive 24-hour-change policy is intentional and unchanged. |
| F44 | All 120 pairs produced analyzed data. A subsequent Binance -1001 error on 2Z/USDT 1h retried and recovered with current informative candles. Outside API availability cannot be guaranteed. |

## Four audits reconciled

1. The V2 audit and blocker report supplied historical claims and hashes. Current source/tests and authenticated host evidence take precedence over those older observations.
2. The renewal/security audit identified real unsigned-certificate and incomplete-binding risks. Signature, trust-boundary, binding, issuance and renewal work is tracked separately from deployment proof.
3. The OCO/PR #32 audit produced concrete plan-loss, residual-plan, pending-state and cancel-before-preflight regressions. This session added delayed-cancel and same-cycle verification regressions.
4. The earlier Testnet/LIVE audit supplied separate deployment/policy contexts. LIVE source-only backup/default repairs were kept separate; Testnet owner code was not installed into LIVE.

The 44-finding AWS/GitHub audit is mapped above. Attached documents were treated as evidence and recommendations, not independent executable authorization. Work followed the user's later explicit authorization to repair, deploy, resume Testnet and remove obsolete files.

## Practical use of the ten official OpenAI repositories


All ten were reviewed at pinned commits in the earlier source-review phase of this conversation. That inventory and hashes remain in `audit-20261011/SOURCES.md` and `references/manifest.json`. Selected relevant source/documentation was reviewed, not every file in every repository. No model library was added to the order loop.

| Official repository | Practical BINANA use and boundary |
|---|---|
| [codex](https://github.com/openai/codex) | Reproduce bugs, prepare patches and record tests in isolated checkouts. This workflow was used; it supplies no trading authority. |
| [codex-security](https://github.com/openai/codex-security) | Review certificate trust, command authentication, secrets and container permissions with evidence/counterevidence. The CLI was reviewed, not run as a security scan. |
| [evals](https://github.com/openai/evals) | Evaluate diagnostic explanations against known incident cases. Use deterministic tests for money, order identity and exchange transitions. |
| [openai-agents-python](https://github.com/openai/openai-agents-python) | Optional read-only diagnosis and traceable incident summaries. Keep it outside order ownership; guardrails do not replace authorization. |
| [codex-action](https://github.com/openai/codex-action) | A future restricted CI assistant can consume failed-test artifacts and propose patches. The new owner CI uses deterministic tests; this action was not installed. |
| [openai-python](https://github.com/openai/openai-python) | Optional typed incident analysis with bounded timeouts. Do not copy generic HTTP retries into ambiguous Binance order mutations. |
| [openai-structured-outputs-samples](https://github.com/openai/openai-structured-outputs-samples) | Structured findings with commit, file, test, observed result and uncertainty. A valid schema does not establish truth or permission. |
| [fence](https://github.com/openai/fence) | Egress restrictions for supported GitHub-hosted Linux runners; not a Windows/AWS runtime sandbox. Separate test and Docker-build needs. |
| [openai-guardrails-python](https://github.com/openai/openai-guardrails-python) | Screen optional diagnostic/research inputs and outputs. It is not protection-plan validation or Sharia certification. |
| [openai-mcpkit](https://github.com/openai/openai-mcpkit) | Narrow authenticated read-only incident/readiness tools. Do not expose shell, raw credentials, signing or trading mutation as diagnostic tools. |

Prioritize reproducible owner builds, deterministic regressions and secure release authority over adding AI dependencies to a small trading host.


## Scope limits

This report does not certify zero defects, profitable trading, exhaustive exchange-race coverage, an off-host disaster recovery service, or a clean-host rebuild of the recovered base. It also does not claim every supplied URL or every file in the linked repositories was read end-to-end. The earlier retrieval inventory contains 166 distinct URLs / 116 fragment-free pages, with 86 successful page responses and 30 retrieval failures; retrieval is not equivalent to review.

The deployment and running-state claims are supported by authenticated host receipts, not inferred from a green unit suite. The remaining limitations above are explicit; this is not a claim that every possible defect or every requested research item is complete.

## Source and rollout references

- [Testnet PR #38](https://github.com/asad13-Binana/Binana-Binance-Spot-Trading-Sharia-Compliance-BOT-TestNet/pull/38) contains the final promotion, rollout and maintenance changes.
- [Successful full CI for the implementation](https://github.com/asad13-Binana/Binana-Binance-Spot-Trading-Sharia-Compliance-BOT-TestNet/actions/runs/38459944659) tested commit `7eb67d85db0f19d9fc24cb4994f951d84293647a`, after rebasing onto the current main branch. Its complete file tree is identical to the previously validated `57ac42878a0d9f6b044387097695e084b0c575ff`, verified with `git diff --exit-code`.
- Successful rollout receipt: `/var/backups/binana-testnet/completion-rollout-20261013T195247Z/receipt.json`.
- Four-position receipt: `controlled-owner-20261013T191551Z`; one-position receipt: `controlled-owner-20261013T192743Z`; lifecycle/restore receipt: `controlled-owner-20261013T193848Z`, under the same private backup root.
- Earlier failed rollout receipts remain preserved. They caught duplicate Compose security options and the overly strict comparison of a closed-incident timestamp; both were corrected without changing the qualified owner image.
- Selected public runtime evidence accompanies this report as `AWS_DEPLOYMENT_OBSERVATION_2026-10-14.json`.
