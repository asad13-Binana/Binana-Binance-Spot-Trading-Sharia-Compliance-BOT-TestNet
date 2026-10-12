# October 12 repair candidate

This branch repairs reproducible defects in the recovered Freqtrade 2026.8/NFI Testnet owner. It is a candidate, not a deployment or trading certificate. The NFI strategy, core entry thresholds, four-slot allocation, Spot-only contract and Testnet endpoint boundary are preserved.

## Reproduced defects and changes

- F01: the actual owner now checks its database filesystem for free bytes, inodes and write/fsync failures before entry or resume. The monitor-only sidecar is not the gate.
- F02-F05: preserve the complete residual protection plan before replacement submission; journal immutable plan revisions; reject generic plan/profile mutation; recognize replacement-pending recovery; preflight before canceling existing protection. A verified partial-fill revision can only reduce quantity, not alter protection prices.
- F06: verify raw Binance order types, list identity, prices, stop trigger, time-in-force or trailing delta against the durable plan.
- F07-F09: use reference-first percent-price rules with an exact-symbol Testnet CCXT adapter; query authenticated account filters; enforce asset and exchange-wide capacity limits; check current last price and maker crossing before standalone OCO submission. Network failures and malformed responses block preflight. These reads do not make cancellation and replacement atomic; prices and limits can change afterward. Complete account MAX_POSITION and conditional-order capacity semantics still need qualification.
- F10: a single surviving child now blocks for reconciliation without automatically canceling it. A complete native triggered-order state model is still required.
- F11: explicitly paginate account fills by order ID and trade-ID cursor, rejecting non-progress and incomplete traversal.
- F14: authenticate release evidence with Ed25519 before evaluating acceptance flags. Reject unsigned, tampered, malformed and unknown-schema envelopes, duplicate JSON keys, wrong issuers and missing authority keys. The private issuer key is never present in the trading container. Deployment still requires an owner-read-only authority mount and a read-only verifier; no production authority key or signed certificate was created.
- F18-F19: add a credential-free, network-denied owner CI job; image validation checks required test identities and rejects skips. Exact-image build validation remains separate from public CI.
- F23-F31 and F34: test the canonical panel source; unavailable trade/profit data is UNKNOWN; bind protection to exact current trade identity; remove global last-20 truncation; require displayed dependencies for readiness; preserve zero profit and require economic settlement timestamps. The corrected panel is not yet the deployed panel.
- F42: manual provider mode defaults to discovery disabled in the Python preflight, matching the shell path. Explicit discovery still requires credentials.

## Evidence

Failing regression runs were captured before the corresponding repairs. The first complete candidate image ran 192 tests with zero skips and two fixture failures; after supplying the newly required raw order geometry, all 192 passed in a container with network disabled, read-only root, no credentials or runtime mounts, dropped capabilities and bounded resources. The subsequent account/current-price and storage changes passed all 201 image tests, zero skips. The final candidate, including six issuer-signature tests, passed all 207 image tests with zero skips in image `sha256:90ad166dac7422c1644e581d055a080fb5f0d96008d03f472e6104f707593218`. The separate unsigned-authority regression is also green. Signature verification is a compatibility break for old unsigned certificates; it does not qualify the candidate to trade.

The GitHub owner job executes the actual recovered modules. The root legacy package has its own independent tests and strategy probe; those checks do not certify this recovered owner. Historical `PROVENANCE.json` is unchanged. `CANDIDATE_MANIFEST.json` binds candidate files and explicit test identities. Root release manifests and audit ledgers are regenerated from the final source tree.

## Deployment blockers

Offline tests do not qualify authenticated order lifecycle behavior. This candidate changes the code binding of the existing release certificate. Do not extend its expiry or copy historical acceptance flags to authorize new code.

Still open: native triggered-child lifecycle qualification; submission ambiguity and absence semantics; independently signed certificate issuance/renewal with complete runtime bindings and atomic activation; a clean-host owner rebuild; canonical-panel deployment parity; durable notification delivery/retry/rate limiting; sizing display and effective-policy alignment; container process reaping and least privilege; sustained storage headroom; current candle warm-up/recovery diagnostics. The current root Compose package is a legacy topology, not the installer for the recovered AWS owner.

The LIVE repository receives only independently applicable maintenance fixes. This Testnet-only owner must not be copied into LIVE. LIVE positive-24-hour-price-change eligibility is preserved as an intentional policy difference pending a separate policy decision.

## Primary sources

- [Binance Testnet filters](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/filters.md)
- [Binance Testnet REST contract](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/rest-api.md)
- [Binance user-data events](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/user-data-stream.md)
- [Freqtrade callback contract](https://www.freqtrade.io/en/2026.8/strategy-callbacks/)

The source review used Binance documentation commit `263ac1aa96556af0b4da824c82a1d8cd9a0edda9`. Authenticated Testnet GET probes independently confirmed the installed CCXT transport can reach `referencePrice` and `myFilters`; no order request is part of those probes.
