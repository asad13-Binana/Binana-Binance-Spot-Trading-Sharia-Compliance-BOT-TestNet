# Recovered AWS Testnet owner

The recovered Freqtrade 2026.8/NFI owner is separate from the legacy root deployment package. This branch contains the October 12 repair candidate. See [current findings and release blockers](../docs/recovery/AUDIT_REPAIR_20261012.md).

`PROVENANCE.json` is the historical recovery receipt and is preserved unchanged. `CANDIDATE_MANIFEST.json` binds the modified owner, canonical Telegram panel and current regression tests. Build-context materialization verifies every candidate hash. The new CI owner job runs the dependency-free selection; the complete suite must also pass in the pinned retained runtime with no network, credentials or production mounts.

The build still requires a retained AWS base image. This does not establish a clean-host rebuild, authenticated lifecycle acceptance or permission to use the owner in LIVE. Its runtime contract is Testnet-only. Root Compose is not its deployment manifest.

The latest corrected panel source is `runtime-fixes/telegram/runtime_stable_panel.py`; historical fixture copies are not deployment proof. No private configuration, credentials or trading databases are included.

Upstream components: [Freqtrade](https://github.com/freqtrade/freqtrade) and [NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity), both GPL-3.0. Modified files retain their upstream notices, and the NFI strategy bytes remain unchanged.

## Preserved upstream diagnostics

The exact NFI snapshot SHA256 is fcf9f622a3ad797bd143561d1017f3930220f22c793905af929ff1d33bbde686, enforced by the wrapper before import. Ruff identifies 15 existing F821 diagnostics: 13 Orders forward annotations and two undefined derisk_4 values in a position-adjustment implementation. The wrapper disables position adjustment and overrides adjust_trade_position to return None; the existing strategy-contract regression exercises that boundary. The narrow Ruff exception applies only to F821 in this pinned upstream file. It does not certify other NFI configurations, resolve those upstream defects, or exempt BINANA code.
