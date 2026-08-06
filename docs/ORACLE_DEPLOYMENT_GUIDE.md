# Oracle Cloud Free Tier — Deployment Guide

Target: Oracle Free Tier **A1/Ampere (ARM64) or AMD**, Ubuntu/Oracle Linux, **≥ 2 GiB RAM** (4 GiB
recommended). A 1 GiB E2 micro is explicitly unsupported and the installer/setup hard-fail below the
threshold.

> Status: this build is an OFFLINE-VERIFIED RELEASE CANDIDATE. Oracle deployment/soak has NOT been run.
> Do not enable live trading. Keep `EXECUTION_MODE=simulation` and `BINANCE_TESTNET=true`.

## 1. Prepare the host
```
sudo bash deploy/oracle_setup.sh
```
Installs Docker + Compose plugin, creates `/opt/binance-freqtrade-v101`,
`/var/lib/binance-freqtrade-v101/shared`, and `/etc/binance-freqtrade-v101/.env` (mode 600). Sign out/in
once so Docker group membership applies. Ensure NTP is enabled (UTC); the freshness guards depend on a
synchronized clock.

## 2. Fill the private env file
Copy `.env.example` into `/etc/binance-freqtrade-v101/.env` and set real values: Telegram token + owner id,
Freqtrade API password/JWT/WS token, the three HMAC bus keys, `SHARED_HOST_PATH=/var/lib/binance-freqtrade-v101/shared`,
and (only for live) Binance Spot keys + `LIVE_EVIDENCE_KEY`. `chmod 600` it. See SECURITY_AND_SECRETS_GUIDE.md.

## 3. Deploy an immutable artifact
Deployment is artifact-based, not `git pull`. From CI (see GITHUB_RELEASE_AND_ROLLBACK_GUIDE.md) or manually:
```
/tmp/install_artifact.sh RELEASE.tar.gz RELEASE.tar.gz.sha256
```
The installer: acquires an exclusive host `flock` (no parallel installs), verifies the artifact checksum
and safe structure, verifies the release manifest, seeds + byte-verifies the immutable V19.1 controller,
asks the running stack to pause + reconcile and waits for acknowledgement, atomically switches `current`,
starts the 5-service stack under the fixed Compose project `binance-freqtrade-v101`, and waits for all five
containers to report **healthy**. On failure it rolls back to the previous release and **verifies the old
release is healthy**, emitting a CRITICAL status if not.

## 4. Resource envelope
Per-service `mem_limit`s (universe 180m, screener 200m, freqtrade 520m, sidecar 300m, telegram 120m) fit a
2 GiB host with headroom. Log rotation (10m × 3) is set per service. The sidecar writes verified SQLite
backups daily (retain 14).

## 5. Health, restart, backups
`scripts/healthcheck.sh` requires all five services present, running, and healthy (a Created/Paused/Exited
service fails). `restart: unless-stopped` restarts crashed containers; on restart the sidecar leaves entries
paused and requires a fresh owner `Resume` via Telegram. Restore from `runtime/db_backups/` if the state DB
fails its startup integrity check.

Install the mode-isolated read-only monitor after the artifact is verified:
```
sudo bash deploy/install_monitoring.sh
```
The installer uses `RELEASE_MODE`, creates a separate `botmon` account and
monitor-only environment, and installs only the matching testnet or live
systemd units. It does not load the trading `.env` and does not mount the
Docker socket. Keep the API bound to loopback and use SSH port forwarding;
do not expose ports 8090/8091 in an Oracle security list or NSG.

## Remaining Oracle blockers (NOT done on the audit host)
ARM64/AMD image build + run, reboot/network-loss/disk-full/OOM drills, backup/restore drill, rollback drill,
and a ≥ 14-day soak. See EXTERNAL_VALIDATION_RUNBOOK.md.
