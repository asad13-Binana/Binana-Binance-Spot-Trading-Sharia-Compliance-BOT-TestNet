# Recovered AWS Testnet owner candidate

This source snapshot preserves the newer Freqtrade 2026.8/NFI owner lineage that is absent from the older root deployment package. It includes the reviewed recovery changes and 164 isolated Linux test cases. It is NOT the currently deployed r17 image and is not wired into root Compose or CI deployment.

Do not activate this snapshot or copy it into LIVE. Its runtime contract forbids real-money trading. Source/config/image reproducibility, retained-inventory disposal and authenticated lifecycle/soak acceptance remain release blockers.

PROVENANCE.json binds every copied source file to its exact SHA256 and records the validation environment. The owner modules were recovered from the AWS working tree (base commit 4169c54) and modified during the September recovery. The NFI strategy bytes were preserved. The tests directory is the latest canonical test set; obsolete duplicate test copies and Python caches are excluded.

Upstream components: Freqtrade https://github.com/freqtrade/freqtrade (GPL-3.0) and NostalgiaForInfinity by iterativ https://github.com/iterativv/NostalgiaForInfinity (GPL-3.0). Modified Freqtrade files retain their upstream notices. The separate Telegram fixture is retained only for the owner regression suite. No credentials, private databases, or account-state snapshots are included.

## Preserved upstream diagnostics

The exact NFI snapshot SHA256 is fcf9f622a3ad797bd143561d1017f3930220f22c793905af929ff1d33bbde686, enforced by the wrapper before import. Ruff identifies 15 existing F821 diagnostics: 13 Orders forward annotations and two undefined derisk_4 values in a position-adjustment implementation. The wrapper disables position adjustment and overrides adjust_trade_position to return None; the existing strategy-contract regression exercises that boundary. The narrow Ruff exception applies only to F821 in this pinned upstream file. It does not certify other NFI configurations, resolve those upstream defects, or exempt BINANA code.
