# Freqtrade Spot Testnet owner

The current source package uses Freqtrade 2026.10 and the approved `BinanaNfiSpot` strategy. Build the standalone [Oracle owner bundle](../docs/ORACLE_OWNER_BUNDLE.md). The earlier [AWS deployment record](../docs/recovery/AWS_2026_9_RELEASE_20261018.md) is historical evidence, not acceptance of this source or an Oracle deployment.

`PROVENANCE.json` is the historical recovery receipt and is preserved unchanged. `CANDIDATE_MANIFEST.json` binds the modified owner, canonical Telegram panel and current regression tests. Build-context materialization verifies every candidate hash. CI builds the current image on AMD64 and ARM64 and runs the complete owner suite with no network, credentials or production mounts.

The [public-source build](deployment/README.md) pins official Freqtrade 2026.10, verifies its archive, applies the seven reviewed upstream overlays and installs hash-locked dependencies on Python 3.14.10. The retained AWS image builder and legacy order engines are retired. Each new image still requires its own runtime acceptance. This repository's owner contract remains Testnet-only.

The latest corrected panel source is `runtime-fixes/telegram/runtime_stable_panel.py`; historical fixture copies are not deployment proof. No private configuration, credentials or trading databases are included.

Upstream components: [Freqtrade](https://github.com/freqtrade/freqtrade) and [NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity), both GPL-3.0. Modified files retain their upstream notices, and the NFI strategy bytes remain unchanged.

## Preserved upstream diagnostics

The exact NFI snapshot SHA256 is fcf9f622a3ad797bd143561d1017f3930220f22c793905af929ff1d33bbde686, enforced by the wrapper before import. Ruff identifies 15 existing F821 diagnostics: 13 Orders forward annotations and two undefined derisk_4 values in a position-adjustment implementation. The wrapper disables position adjustment and overrides adjust_trade_position to return None; the existing strategy-contract regression exercises that boundary. The narrow Ruff exception applies only to F821 in this pinned upstream file. It does not certify other NFI configurations, resolve those upstream defects, or exempt BINANA code.
