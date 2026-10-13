# Recovered AWS Testnet owner

The recovered Freqtrade 2026.8/NFI owner is separate from the legacy root deployment package. The accepted owner was deployed and resumed on October 14, 2026 (Asia/Karachi), image `7b12004b…`. See [deployment evidence](../docs/audit/AWS_DEPLOYMENT_RESULT_2026-10-14.md). Earlier audit findings remain historical evidence; current readiness must be queried from the owner.

`PROVENANCE.json` is the historical recovery receipt and is preserved unchanged. `CANDIDATE_MANIFEST.json` binds the modified owner, canonical Telegram panel and current regression tests. Build-context materialization verifies every candidate hash. The new CI owner job runs the isolated selection with hash-locked cryptography; the complete suite must also pass in the pinned retained runtime with no network, credentials or production mounts.

The retained-image recovery build and the independent public-source build are separate procedures; see [build instructions](deployment/README.md). The public-source procedure pins official Freqtrade 2026.8, verifies the archive, restores all seven patched upstream modules, and installs a hash-locked dependency closure. Each new image still requires its own runtime acceptance. The owner contract is Testnet-only. Root Compose describes the legacy sidecar-owned architecture and must not replace this AWS owner.

The latest corrected panel source is `runtime-fixes/telegram/runtime_stable_panel.py`; historical fixture copies are not deployment proof. No private configuration, credentials or trading databases are included.

Upstream components: [Freqtrade](https://github.com/freqtrade/freqtrade) and [NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity), both GPL-3.0. Modified files retain their upstream notices, and the NFI strategy bytes remain unchanged.

## Preserved upstream diagnostics

The exact NFI snapshot SHA256 is fcf9f622a3ad797bd143561d1017f3930220f22c793905af929ff1d33bbde686, enforced by the wrapper before import. Ruff identifies 15 existing F821 diagnostics: 13 Orders forward annotations and two undefined derisk_4 values in a position-adjustment implementation. The wrapper disables position adjustment and overrides adjust_trade_position to return None; the existing strategy-contract regression exercises that boundary. The narrow Ruff exception applies only to F821 in this pinned upstream file. It does not certify other NFI configurations, resolve those upstream defects, or exempt BINANA code.
