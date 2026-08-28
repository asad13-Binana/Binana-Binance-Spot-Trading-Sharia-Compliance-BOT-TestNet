# Oracle Cloud Free Tier — Deployment Guide

Target: Oracle Free Tier **A1/Ampere (ARM64)**,
Ubuntu 24.04 LTS, **at least 2 OCPUs and 12 GiB RAM**, 100 GiB boot storage,
at least 80 GiB free on the root filesystem at bootstrap,
and 4 GiB swap. The installer hard-fails below 11,264 MiB physical RAM,
14,336 MiB RAM+swap, 2 CPUs, or 80 GiB free root storage; a 1 GiB E2 micro
and the former 1-OCPU/6-GiB layout are unsupported.

> Status: this build is an OFFLINE-VERIFIED RELEASE CANDIDATE. Oracle deployment/soak has NOT been run.
> Do not enable live trading. Keep `EXECUTION_MODE=simulation` and `BINANCE_TESTNET=true`.

After credentials are placed in the root-owned Oracle environment file, run
the GET-only check documented in `docs/API_READINESS_RUNBOOK.md`:

```bash
sudo /opt/binana-testnet/current/deploy/api_preflight.sh
```

It verifies provider authentication without placing, testing, cancelling or
modifying a Binance order. It does not replace the separate authenticated
TestNet lifecycle, soak/recovery or LIVE promotion gates.

## 1. Prepare the host
```
sudo bash deploy/oracle_setup.sh
```
Installs official Docker CE + Compose, creates `/opt/binana-testnet`,
`/var/lib/binana-testnet/shared`, and root-owned
`/etc/binana-testnet/.env` (mode 600). No application or deployment
identity receives Docker-group membership. Chrony must synchronize before the
bootstrap succeeds.

## 2. Fill the private env file
Copy `.env.example` into `/etc/binana-testnet/.env` and set real values: Telegram token + owner id,
Freqtrade API password/JWT/WS token, the HMAC bus keys, `SHARED_HOST_PATH=/var/lib/binana-testnet/shared`,
and (only for live) Binance Spot keys + `LIVE_EVIDENCE_KEY`. `chmod 600` it. See SECURITY_AND_SECRETS_GUIDE.md.

## 3. Deploy an immutable artifact
Deployment is artifact-based, not `git pull`. From CI (see GITHUB_RELEASE_AND_ROLLBACK_GUIDE.md) or manually:
```
sudo /usr/local/sbin/binana-testnet-deploy \
  /var/lib/binana-testnet/deploy-inbox/RELEASE.tar.gz \
  /var/lib/binana-testnet/deploy-inbox/RELEASE.tar.gz.sha256
```
The installer: acquires an exclusive host `flock` (no parallel installs), verifies the artifact checksum
and safe structure, verifies the release manifest, seeds + byte-verifies the immutable V19.1 controller,
asks the running stack to pause + reconcile and waits for acknowledgement, atomically switches `current`,
starts the 6-service stack under the fixed Compose project `binana-testnet`, and waits for all six
containers to report **healthy**. On failure it rolls back to the previous release and **verifies the old
release is healthy**, emitting a CRITICAL status if not.

## 4. Resource envelope
Per-service memory and process limits are sized for the declared shared 12 GiB host.
Log rotation (10m × 3) is set per service. A root-owned daily timer creates
secret-free state snapshots using SQLite's online backup API and retains 14.

## 5. Health, restart, backups
`scripts/healthcheck.sh` requires all six services present, running, and healthy (a Created/Paused/Exited
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
ARM64 image build + run, reboot/network-loss/disk-full/OOM drills, backup/restore drill, rollback drill,
an initial ≥ 72-hour soak followed by the existing longer qualification soak.
See EXTERNAL_VALIDATION_RUNBOOK.md and `ORACLE_SETUP_GUIDE.md`.
