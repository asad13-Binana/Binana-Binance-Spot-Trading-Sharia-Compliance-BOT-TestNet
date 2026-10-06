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
