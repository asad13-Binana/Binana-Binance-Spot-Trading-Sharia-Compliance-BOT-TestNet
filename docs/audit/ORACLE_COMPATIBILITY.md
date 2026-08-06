> **HISTORICAL DOCUMENT (V8.1-era paths) — SUPERSEDED.** This file is retained as an
> audit-trail record of an earlier release. It does NOT describe the current
> V10.2-EXT-001 package. For current status see `VALIDATION_STATUS.json`,
> `docs/DEEP_AUDIT_RESPONSE.md`, and `docs/audit/ISSUE_LEDGER.csv`.

# Oracle Compatibility and Resource Report

## Static result

- Root deployment is the only supported deployment path. Nested standalone Freqtrade update/start services are disabled archival stubs.
- Default mode is `simulation`; `BINANCE_TESTNET=true`; restart leaves entries paused pending owner confirmation.
- Installer rejects physical memory below 1500 MiB or total RAM+swap below 2000 MiB. A 1 GiB E2 micro is rejected.
- Compose memory limits: universe 180 MiB, Freqtrade 520 MiB, sidecar 280 MiB, Telegram 110 MiB.
- Persistent state is under the configured shared host path; secrets remain in `/etc/binance-freqtrade-v81/.env` mode 600.
- Release install validates checksum, archive paths/types/root, manifest, service image, pause/reconcile acknowledgements, health, rollback and bounded retention.
- Deployment command/status JSON is published with fsync + atomic replace.

## Blocked runtime evidence

No Oracle host was connected. CPU architecture, image pulls, TA-Lib wheels, actual memory peaks, DNS/Binance/Telegram egress, NTP, firewall, disk growth, OOM behavior, reboot recovery, network-loss behavior and multi-day soak were not measured.
