# BINANA Oracle A1 deployment guide — Hardening V2

Status: **source-reviewed implementation; Oracle validation pending**. Git publication alone does not mean this package has been deployed or connected to real credentials. Completing these steps proves host deployment only; it does not prove profitability, authenticated TestNet order handling, Oracle soak, or LIVE safety.

## 1. Target architecture

- Oracle Cloud Infrastructure `VM.Standard.A1.Flex`, Ubuntu 24.04 LTS ARM64.
- One OCPU, 6 GB RAM, approximately 50 GB boot volume, and 4 GiB swap headroom.
- Dedicated VM hostname: `binana-testnet-tokyo` for TestNet; `binana-live-tokyo` for the separately deployed LIVE package.
- Only SSH is allowed inbound at the OCI NSG/Security List. Do not open 8090, 8080, Docker, SQLite, sidecar, Sharia, Telegram broker, or Freqtrade ports.
- The six application services communicate through Docker networks. No Compose service publishes a host port. Monitoring is a loopback-only systemd service at `127.0.0.1:8090`.
- The normal SSH/deployment account, `botmon`, and `binanabot` are not members of the root-equivalent Docker group.
- Do not install a permanent self-hosted GitHub Actions runner on the trading VM. GitHub-hosted CI builds an immutable artifact; the operator separately approves its exact SHA-256; a narrow root-owned wrapper performs deployment.

Oracle availability and free-tier entitlements vary by tenancy/home region. Tokyo is a preference, not a latency claim. Oracle documents that A1 capacity can be temporarily unavailable; benchmark Binance HTTPS from the actual VM before acceptance.

## 2. Create the Oracle VM

In the OCI Console:

1. Create the instance in the account's home region. Select Tokyo only if it is the home region and A1 capacity is available.
2. Select Ubuntu 24.04 LTS ARM64 and `VM.Standard.A1.Flex` with 1 OCPU and 6 GB RAM.
3. Use a boot volume near 50 GB and an SSH key pair. Never enable password SSH.
4. Assign a public IPv4 address only because direct SSH administration is required. Prefer a reserved administration source IP.
5. Create an NSG with inbound TCP/22 from the operator's exact public IP/CIDR only. Allow normal stateful outbound HTTPS, DNS, and NTP. Do not add application ports.
6. Under advanced instance options, require IMDSv2/authorization headers. The repository itself uses no OCI metadata. The host firewall blocks container metadata access and limits host metadata TCP/80 to root without blocking Oracle's link-local NTP, DNS, or volume paths.

Primary references: [Oracle A1/Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm), [Oracle instance creation](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/launchinginstance.htm), [Oracle IMDSv2](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/gettingmetadata.htm), and [Oracle compute security](https://docs.oracle.com/en-us/iaas/Content/Security/Reference/compute_security.htm).

## 3. Bootstrap the host

Transfer this reviewed repository package to the VM without a `.env` file, then run from the package root:

```bash
sudo bash deploy/oracle_setup.sh
```

The idempotent bootstrap:

- fails unless the host is Ubuntu 24.04, `arm64` or explicitly supported `amd64`, Python 3.12, at least 5 GiB physical RAM, at least 35 GiB free disk, and a valid package mode;
- removes conflicting distribution Docker packages and installs Docker CE, CLI, containerd, Buildx, and the Compose plugin from Docker's official signed Ubuntu repository;
- configures bounded Docker JSON logs and live-restore, while leaving Docker major/minor upgrades manual;
- creates `binanabot` and `botmon`, removes Docker-group membership from application identities, and installs the narrow deployment wrapper;
- creates root-owned private configuration and separate BINANA paths;
- adds 4 GiB swap if current swap is insufficient and sets `vm.swappiness=10`;
- enables Chrony with the OCI local NTP service, waits up to 60 seconds for synchronisation, and fails when the last measured offset exceeds 100 ms;
- enables Ubuntu security updates but disables automatic reboot and excludes the third-party Docker repository from unattended upgrades;
- installs the Docker ingress/metadata firewall guard and bounded journal storage.

Docker's official documentation warns that Docker-published ports can bypass UFW. This stack publishes none; the persistent `DOCKER-USER` guard is defence in depth. See [Docker Ubuntu installation](https://docs.docker.com/engine/install/ubuntu/), [Docker firewall behaviour](https://docs.docker.com/engine/network/packet-filtering-firewalls/), [Docker daemon security](https://docs.docker.com/engine/security/), and [Docker live restore](https://docs.docker.com/engine/daemon/live-restore/).

## 4. Configure root-owned secrets as data

The canonical file is:

```text
/etc/binana-freqtrade-v101/.env
root:root 0600
```

Edit it only with:

```bash
sudoedit /etc/binana-freqtrade-v101/.env
```

Copy values from `/etc/binana-freqtrade-v101/.env.template`. Preserve these TestNet identities:

```dotenv
BOT_PRODUCT=BINANA
BOT_ENVIRONMENT=TESTNET
BOT_INSTANCE_ID=BINANA-TN-TYO-01
BOT_NAMESPACE=binana-freqtrade-v101
EXECUTION_MODE=testnet
BINANCE_TESTNET=true
SHARED_HOST_PATH=/var/lib/binana-freqtrade-v101/shared
```

Set `BOT_UID` and `BOT_GID` to the numeric values printed by `oracle_setup.sh`. Add independent Binance TestNet, Telegram, CoinGecko, and CoinMarketCap credentials. Binance API keys should have only required Spot permissions, withdrawals disabled, and source-IP restriction where Binance supports it. Never reuse credentials from another bot.

The installer never sources or evaluates this file. A strict parser validates root ownership, `0600`, non-symlink identity, parent permissions, duplicate/malformed keys, and treats values—including `$()`, backticks, semicolons, pipes, redirections and shell expansions—as literal bytes.

## 5. Harden SSH without lockout

Open and keep one working key-authenticated SSH session. Then run:

```bash
sudo bash deploy/harden_ssh.sh
```

The script requires an existing `authorized_keys`, writes a drop-in, runs `sshd -t`, restores the prior file on validation failure, and reloads SSH only after success. Open a second SSH session before closing the first. The posture is public-key authentication, no password/keyboard-interactive login, no root login, and local-only forwarding.

## 6. Build, transfer, approve, and deploy an immutable artifact

Keep `ORACLE_TESTNET_DEPLOY_ENABLED` false while reviewing. The GitHub workflow uses hosted runners and creates a checksum-addressed artifact. It does not install a self-hosted runner or access trading credentials.

After CI is green for the exact commit:

1. Download the immutable `.tar.gz` and `.sha256` files.
2. Independently calculate the archive SHA-256 and compare it with the workflow artifact/checksum.
3. Transfer both files to `/var/lib/binana-deploy/inbox/` as the deployment user.
4. Approve the exact digest from an interactive root session:

   ```bash
   sudo /usr/local/sbin/binana-approve-release EXACT_64_HEX_ARCHIVE_SHA256
   ```

5. Deploy through the narrow wrapper:

   ```bash
   sudo /usr/local/sbin/binana-deploy \
     /var/lib/binana-deploy/inbox/binance-bot-testnet-COMMIT_SHA.tar.gz \
     /var/lib/binana-deploy/inbox/binance-bot-testnet-COMMIT_SHA.tar.gz.sha256
   ```

The wrapper constrains input paths/types/owners/names, rechecks SHA-256 and root approval, copies into a root-only stage, validates archive paths, and invokes the artifact's installer. The installer re-verifies manifests, dependency supply chain, secrets scan, package mode, Sharia controller, Compose config, resource capacity, and all six container health checks. It pauses entries/reconciles the old release, atomically switches releases, installs monitoring within the same transaction, and rolls back on failure.

The pause and reconciliation requests are HMAC-authenticated `deploy-installer` command envelopes bound to the currently installed release hash. Plain or cross-release deployment commands are rejected by the unchanged sidecar.

The GitHub deployment job can transfer and invoke this wrapper only when explicitly enabled. It cannot approve its own artifact. GitHub warns that self-hosted runners can persistently retain a compromise and should almost never be used for public repositories; see [GitHub secure-use guidance](https://docs.github.com/en/actions/reference/security/secure-use).

## 7. Monitoring and AI-agent access

The monitor remains private on `127.0.0.1:8090`, bearer-authenticated, rate-limited, redacted, read-only, and credential-free. `botmon` has no Docker group, trading secrets, sudo, or Docker socket. A root-only fixed helper writes a sanitized container snapshot.

From the operator computer, create an SSH tunnel:

```bash
ssh -N -L 8090:127.0.0.1:8090 ubuntu@ORACLE_PUBLIC_IP
```

Then a trusted local AI agent can call, with the monitoring-only bearer token:

```bash
curl -H 'Authorization: Bearer MONITOR_TOKEN' http://127.0.0.1:8090/api/v1/report
```

Useful read-only routes include `/health`, `/status`, `/performance`, `/trades`, `/errors`, `/crashes`, `/latency`, `/system`, `/deployment`, `/sharia`, and `/report`. Do not give an AI agent the SSH private key, Binance key, Telegram bot token, trading `.env`, Docker socket, or monitor bind access on `0.0.0.0`.

Monitoring start fails clearly if the configured loopback port is already occupied. Every response and Telegram message identifies `BINANA`, environment, and instance ID.

The disk guard records 80% warning and 90% critical states. At the critical threshold it queues an HMAC-authenticated command to pause new entries; it does not cancel exchange-native protection or delete reconciliation/audit evidence.

## 8. Validate the real host

Run:

```bash
sudo bash deploy/oracle_validate.sh | tee oracle-validation.txt
```

This prints no credentials. It reports host identity, OS, architecture, CPU/RAM/swap/disk, Docker/Compose and daemon state, Chrony state, listening sockets, container health/resources, release/Git/package/execution identities, provider connectivity, and monitoring state. It performs ten real HTTPS requests to Binance `/api/v3/time` and reports DNS, TCP, TLS, first-byte, and total minimum/median/p95/maximum timings. ICMP ping is not used as evidence.

For Binance timing security, keep a small `recvWindow`; do not hide clock drift by increasing it. Binance recommends 5,000 ms or less. See [Binance Spot REST timing security](https://developers.binance.com/en/docs/products/spot/rest-api).

## 9. Backup and restore validation

The installed daily timer invokes `deploy/backup_state.sh`. It excludes plaintext secrets, uses Python's SQLite online backup API with literal path arguments, validates each database with `PRAGMA integrity_check`, copies application/audit state and release metadata, creates a SHA-256 manifest, and retains a bounded number of root-only snapshots.

Validate a candidate without changing live state:

```bash
sudo bash deploy/restore_validate.sh /var/backups/binana-freqtrade-v101/YYYYMMDDTHHMMSSZ
```

Actual restore must occur during a documented maintenance window after entries are paused, reconciliation is complete, containers are stopped, the current state is separately preserved, and the validated snapshot is restored to a staging path first.

## 10. Updates and maintenance

- Ubuntu security updates are automatic; reboot is manual during a maintenance window after pause/reconciliation/backup.
- Docker changes are manual and exact-version selected:

  ```bash
  sudo bash deploy/update_docker.sh EXACT_APT_DOCKER_CE_VERSION
  ```

- Docker live-restore helps during compatible patch upgrades but is not proof that major-version upgrades are safe.
- Run `oracle_validate.sh`, six-service health, backup/restore validation, and rollback checks after every host or Docker change.

## 11. Required Oracle soak and fault drills

Before accepting TestNet operation, record evidence for: fresh installation; Docker restart; each-container restart; VM reboot; network and DNS interruption; Binance TestNet interruption; Telegram interruption; disk warning/critical pressure; memory pressure/OOM; consistent backup; staged restore; previous-release rollback; artifact corruption; unapproved artifact rejection; malformed/symlink `.env`; occupied monitor port; and time-sync loss.

Do not call Oracle validation complete until these tests run on the actual VM. Do not enable LIVE because TestNet infrastructure works. LIVE promotion remains a separate, prohibited action until all existing evidence gates and owner approvals are satisfied.
