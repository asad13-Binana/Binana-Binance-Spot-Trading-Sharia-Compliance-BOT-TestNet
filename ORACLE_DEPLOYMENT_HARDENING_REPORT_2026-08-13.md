# BINANA Oracle Deployment Hardening Report — 13 August 2026

## Release and review status

This is a **source-review package**, not a deployment or production approval.
Publication in Git does not mean it has been deployed, connected to real
credentials, authenticated against Binance, or validated on Oracle.

| Field | Value |
|---|---|
| Repository package | BINANA Binance Spot TestNet |
| Baseline branch | `origin/main` |
| Baseline and local HEAD | `f6d68ff26b9fa3b8d1f8097ce0bc2396d8d3e0fb` |
| Local working branch | `local/oracle-hardening-v2-20260813` |
| Release label | `V10.3-LOCAL-SHARIA-2026-08-10A` |
| Package mode | `testnet` |
| Target | OCI A1 Flex, Ubuntu 24.04 ARM64, 1 OCPU, 6 GiB RAM, ~50 GiB boot volume, 4 GiB swap |
| Publication state | Consult the GitHub commit/PR; publication is not deployment evidence |
| Oracle state | Not deployed; not host-validated; not soaked |
| LIVE state | Prohibited |

## Scope and protected boundary

The change is infrastructure-only: host bootstrap, Docker/Compose hardening,
deployment transaction, monitoring, logging, backup/restore, operational
identity, validation scripts, CI deployment controls, tests, and documentation.

No strategy indicator, entry/exit condition, pair-selection decision, position
size/risk calculation, execution decision, order lifecycle, Sharia rule,
Sharia approval policy, package-mode interlock, or LIVE evidence gate was
changed. The execution sidecar remains the only Binance order owner and
Freqtrade remains signal-only.

## Architecture baseline confirmed

The package contains six Compose services:

1. `universe`
2. `sharia-egress-proxy`
3. `sharia-screener`
4. `freqtrade`
5. `execution-sidecar`
6. `telegram-broker`

No Compose service publishes a host port. Internal control traffic uses an
internal Docker network. Sharia screening has an internal-only network and its
own governed egress proxy. Runtime services requiring public exchange/provider
access use a separate egress network. The monitor is not a container; it is a
loopback-only systemd service and receives no trading environment, Docker
socket, Docker-group access, or sudo capability.

Persistent state is under `/var/lib/binana-freqtrade-v101/shared`; immutable
releases are under `/opt/binana-freqtrade-v101/releases`; root-only secrets are
under `/etc/binana-freqtrade-v101`; monitoring logs and backups use separate
BINANA paths.

## Security findings and fixes

| Severity | Finding | Resolution |
|---|---|---|
| High | A Docker-group deployment account would have root-equivalent Docker control. | Deployment, bot, and monitor identities are removed from the Docker group. A root-owned, command-constrained wrapper performs only approved artifact installation. |
| High | Treating `.env` as shell code permits command substitution and metacharacter execution. | The root-owned `0600` file is parsed as literal `KEY=VALUE` data. Ownership, permissions, parent directory, symlink, duplicate key, malformed line, and open-file identity checks fail closed. Unsupported variable names are rejected before export. |
| High | CI could transfer an artifact without an independent host-side trust decision. | The wrapper requires an exact 64-hex archive digest in a separate root-owned `0600` approval file. CI cannot create or alter that file. The artifact is copied into a root-only stage and re-hashed there to close substitution/TOCTOU risk. |
| High | The existing deployment pause/reconcile generator wrote plain JSON, while the protected sidecar accepts only authenticated command envelopes. | The installer now signs `deploy-installer` command envelopes with the command-bus HMAC key and binds them to the currently installed release hash. Plain, forged, expired, or cross-release commands remain rejected by the unchanged sidecar. |
| High | A second Compose/path namespace could silently coexist with a legacy `binance-freqtrade-v101` deployment. | All active resources use `binana-freqtrade-v101`. Bootstrap fails if a legacy current release or legacy-labelled containers are detected; migration is explicit and backup-first. |
| High | Critical disk pressure could exhaust SQLite/log capacity while new entries remained armed. | The disk guard records 80% warning/90% critical status. At critical pressure it queues an authenticated, release-bound pause-new-entries command. It does not cancel exchange-native protection or delete evidence. |
| Medium | Root scripts accepted privileged root/path/UID overrides. | Fixed BINANA roots, lock, inbox, approval, backup, monitor and service UID/GID boundaries are validated before mutation; symlink redirection is rejected. |
| Medium | Existing swap could be reused without proving file type, ownership, mode, or swap signature. | Existing swap must be a regular non-symlink root:root `0600` file with a real swap signature. Creation and `/etc/fstab` changes are idempotent. |
| Medium | Clock service activity alone does not prove acceptable Binance timestamp accuracy. | Bootstrap waits a bounded 60 seconds for synchronisation, requires Chrony tracking, and fails when the absolute last offset exceeds 100 ms. `recvWindow` is not enlarged. |
| Medium | Unbounded container/journal growth and weak deployment free-space checks could exhaust the VM. | Docker JSON logs are limited to 10 MiB × 3, journald is bounded, application/audit retention remains controlled, bootstrap/deployment perform disk preflights, and a periodic disk guard reports/pauses at thresholds. |
| Medium | Naive SQLite/file copying during writes and unvalidated restore manifests risk inconsistent or unsafe recovery. | Root-only backups use Python's SQLite online backup API, integrity checks, no links/devices/special files, a SHA-256 manifest with path validation, bounded retention, and non-mutating restore validation. Plaintext secrets are excluded. |
| Medium | Monitoring port/path/unit names could collide with another bot. | Unique BINANA units, paths, Compose identity, locks, logs and databases are used. Port 8090 is configurable; a pre-start bind test fails clearly when occupied. |
| Medium | Blind SSH edits could lock out the operator. | The hardener requires a valid owned `authorized_keys`, rejects symlink targets, writes atomically, validates with `sshd -t`, restores the prior drop-in on failure, and reloads only after validation. |

## Host, users and secret handling

- Hostname: `binana-testnet-tokyo`.
- Non-secret identity: `BOT_PRODUCT=BINANA`, `BOT_ENVIRONMENT=TESTNET`, configurable `BOT_INSTANCE_ID` with default `BINANA-TN-TYO-01`.
- `binanabot`: system account owning writable application state; no login shell and no Docker group.
- `botmon`: system account reading only monitor-approved state/logs; no trading secrets, Docker socket, sudo, or Docker group.
- Normal deployment account: owns only `/var/lib/binana-deploy/inbox`; sudo is restricted to `/usr/local/sbin/binana-deploy`.
- Trading/provider/Telegram secrets: only in `/etc/binana-freqtrade-v101/.env`, root:root `0600`, never copied into reports or image layers.
- Monitoring credentials: separate root:botmon `0640` mode-specific file; no reuse of the trading environment.

Host bootstrap validates Ubuntu 24.04, ARM64 or explicitly supported AMD64,
Python 3.12, ≥5 GiB physical RAM, ≥35 GiB free disk, official Docker CE and
Compose availability, synchronized Chrony, and the package mode. The target is
the declared 6 GiB A1 VM; a 1 GiB E2 micro is rejected.

## Docker and network changes

- Docker CE, CLI, containerd, Buildx and Compose are installed from Docker's
  official signed Ubuntu repository; the convenience installer and Ubuntu
  `docker.io` package are not used.
- Docker daemon uses `live-restore`, bounded JSON logs and no userland proxy.
- The `DOCKER-USER` guard rejects new ingress on the external interface and
  container access to OCI metadata TCP/80 while preserving OCI link-local NTP,
  DNS and volume services on their own ports.
- Every service has a memory limit, PID limit, `cap_drop: [ALL]`,
  `no-new-privileges`, health check, restart policy and bounded logs.
- Read-only filesystems/tmpfs and non-root users are applied where compatible.
  Freqtrade retains the minimum writable runtime behaviour needed by its
  existing image; changing that without an actual container run would be unsafe.
- Docker's default seccomp profile remains active. A custom seccomp or AppArmor
  policy was not invented without ARM64/Oracle runtime traces; that remains a
  host-validation hardening opportunity, not a claimed completed test.
- No Freqtrade, sidecar, Sharia, database, Docker or monitor port is public.
  OCI NSG/Security List guidance permits SSH/22 only from the operator CIDR.

## Time, memory, updates and logging

- Chrony uses OCI's `169.254.169.254` NTP service, requires active tracking,
  bounded synchronisation and ≤100 ms absolute last offset.
- Four GiB swap is added only when existing swap is insufficient; root:root
  `0600`, real swap signature, no duplicate fstab line, `vm.swappiness=10`.
- Ubuntu security updates remain automatic. Automatic reboot is disabled.
  Third-party Docker upgrades remain manual; the update helper requires an
  exact available Docker CE version and prohibits automatic major-version jumps.
- Docker and journal logs are bounded. Existing audit/reconciliation retention
  remains intact. Disk status is atomic and observable by the monitor.

## Backup, restore and rollback

The daily root timer backs up application state, SQLite databases, release
metadata and audit evidence to `/var/backups/binana-freqtrade-v101`. It excludes
the private `.env`, links, devices and special files. Each database is backed up
online and integrity-checked; every regular file is covered by `SHA256SUMS`.

`deploy/restore_validate.sh BACKUP_DIRECTORY` verifies the approved root,
timestamp directory name, member types, checksum paths/checksums and SQLite
integrity without modifying current state.

Automatic rollback stops the failed new release, atomically restores the prior
release symlink, starts it, verifies all six prior containers are healthy,
restores the matching monitor and leaves entries paused. A prior release that
does not become healthy is reported as critical, not successful rollback.

Manual rollback requires an owner maintenance window: pause entries, reconcile,
make a fresh backup, select an existing immutable release beneath
`/opt/binana-freqtrade-v101/releases`, switch `current`, start with
`COMPOSE_PROJECT_NAME=binana-freqtrade-v101` and the root-owned env, reinstall
that release's monitoring units, run `scripts/healthcheck.sh`, and then run
`deploy/oracle_validate.sh`. Do not resume entries until reconciliation and
health evidence are reviewed.

## CI and release-security decision

No permanent self-hosted GitHub Actions runner is installed on the trading VM.
GitHub-hosted CI verifies and builds an immutable checksum-addressed artifact.
When the TestNet deployment variable is deliberately enabled, CI can transfer
the artifact and invoke the narrow wrapper, but cannot approve the digest or
read trading credentials. Branch protection, a real CODEOWNER, protected
environment reviewers and retained provenance remain GitHub configuration work.

The installer preserves manifest verification, exact release hashes, immutable
release directories, dependency supply-chain checks, protected fingerprints,
secret scanning, the host deployment lock, atomic switch and verified rollback.

## Exact local changed-file inventory

Modified:

- `.env.example`; `.github/workflows/ci.yml`; `ARCHITECTURE.md`; `README.md`
- `deploy/install_artifact.sh`; `deploy/install_monitoring.sh`; `deploy/oracle_setup.sh`; `deploy/verify_release.sh`
- `docker-compose.yml`; `scripts/healthcheck.sh`; `services/telegram_broker/bot.py`
- `docs/GITHUB_ORACLE_DEPLOYMENT.md`; `docs/GITHUB_RELEASE_AND_ROLLBACK_GUIDE.md`; `docs/ORACLE_DEPLOYMENT_GUIDE.md`; `docs/SECURITY_AND_SECRETS_GUIDE.md`; `docs/SHARIA_LOCAL_SCREENING.md`
- `monitoring/.env.monitor.live.example`; `monitoring/.env.monitor.testnet.example`; `monitoring/INSTALL.md`
- `monitoring/api/app.py`; `monitoring/api/configuration.py`; `monitoring/control.py`; `monitoring/snapshot.py`; `monitoring/tests/test_monitoring.py`
- `tests/test_sharia_egress_proxy.py`
- generated `docs/audit/FILE_REVIEW_LEDGER.csv`; `docs/audit/FUNCTION_CALLBACK_LEDGER.csv`; `docs/audit/TEST_EVIDENCE_LEDGER.csv`; `RELEASE_MANIFEST.json`; `RELEASE_SHA256.txt`

Added:

- `ORACLE_SETUP_GUIDE.md`; `ORACLE_DEPLOYMENT_HARDENING_REPORT_2026-08-13.md`; `ORACLE_HARDENING_V2_CHANGES_2026-08-13.txt`
- `deploy/lib/secure_env.sh`; `deploy/binana-deploy-wrapper.sh`; `deploy/binana-approve-release.sh`
- `deploy/oracle_validate.sh`; `deploy/docker_firewall.sh`; `deploy/disk_guard.sh`; `deploy/backup_state.sh`; `deploy/restore_validate.sh`; `deploy/harden_ssh.sh`; `deploy/update_docker.sh`
- `monitoring/systemd/binana-disk-guard.service`; `monitoring/systemd/binana-disk-guard.timer`
- `monitoring/systemd/binana-monitor-live.service`; `monitoring/systemd/binana-monitor-testnet.service`
- `monitoring/systemd/binana-monitor-report-live.service`; `monitoring/systemd/binana-monitor-report-live.timer`
- `monitoring/systemd/binana-monitor-report-testnet.service`; `monitoring/systemd/binana-monitor-report-testnet.timer`
- `monitoring/systemd/binana-monitor-snapshot.service`; `monitoring/systemd/binana-monitor-snapshot.timer`
- `monitoring/systemd/binana-state-backup.service`; `monitoring/systemd/binana-state-backup.timer`
- `tests/test_oracle_hardening_v2.py`

Removed/replaced by collision-free units:

- `monitoring/systemd/binance-bot-monitor-live.service`
- `monitoring/systemd/binance-bot-monitor-testnet.service`
- `monitoring/systemd/binance-bot-monitor-report-live.service`
- `monitoring/systemd/binance-bot-monitor-report-live.timer`
- `monitoring/systemd/binance-bot-monitor-report-testnet.service`
- `monitoring/systemd/binance-bot-monitor-report-testnet.timer`
- `monitoring/systemd/binance-bot-monitor-snapshot.service`
- `monitoring/systemd/binance-bot-monitor-snapshot.timer`

## Protected-file before/after evidence

| Protected item | Baseline SHA-256 | Local SHA-256 | Result |
|---|---|---|---|
| `freqtrade/user_data/strategies/IctSmcStrategy.py` | `9f6bafc78c8cd0d9b9cbde615ddce89e304ab09738584b88d05bfdf92ff4e830` | same | PASS |
| `legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py` | `70b1d67cc0092b5b8db4a68b343cf893641bde1aae580e9ef51e2adec1062459` | same | PASS |
| `services/common/sharia_v19.py` | `5eb9fd5338d80fcaf0d39bb3f4935a75b57dd91136c72a83a7551b659b04d865` | same | PASS |
| `shared/sharia/HALAL_CRYPTO_SPOT_SCREENING_V19_1_PRODUCTION.json` | `07106bb8bfc1924d8d0c6f61ced4e0c51c2ac2054988423f42c1fd67f3b2ba78` | same | PASS |
| Complete frozen set | 41 files inventoried | `tests/test_oracle_hardening_v2.py` pins every non-empty protected file | PASS |

## Validation classification

### Executed and passed locally

- Focused monitoring/Oracle hardening tests: 69 passed, 49 subtests passed.
- Complete repository unittest suite after final ledger generation: PASS.
- Bash syntax for all deployment/library/operational shell scripts: PASS.
- Python compile/import validation: PASS.
- Secret scan, exact manifest verification, generated audit-ledger parity,
  deployment supply-chain verification, JSON/YAML structure, protected hashes,
  legacy self-tests and release gate: PASS.
- Git whitespace/error check: PASS.

The final command transcript and exact aggregate counts are represented by the
generated test/audit evidence. The single Starlette/httpx deprecation warning
in the local monitoring test environment is non-failing and does not affect
runtime behaviour.

### Statically verified locally

- Ubuntu 24.04 and ARM64 bootstrap branches.
- Official Docker CE repository setup and absence of `docker.io` regression.
- Compose service/network/resource/privilege structure.
- systemd service/timer pairing and hardening directives.
- OCI NSG/IMDS/NTP guidance and no-public-port design.
- CI workflow graph, hosted-runner design and owner-approval separation.

### Unavailable on this Windows audit host

- Docker Engine/Compose execution, image pull/build and six-container health.
- `systemd-analyze verify` against an installed Linux filesystem and active Docker unit.
- `shellcheck` (Bash `-n` and repository static tests were used; shellcheck remains an external gate).
- Linux ownership/mode execution tests and actual iptables/DOCKER-USER behaviour.

### Requires the real Oracle host

- Fresh Ubuntu 24.04 A1 installation and reboot persistence.
- Actual Docker/Compose versions, ARM64 image builds and service health.
- Chrony source/tracking/offset under OCI and signed Binance timestamp behaviour.
- OCI NSG/Security List, IMDSv2-only setting, host firewall and listening-socket confirmation.
- Ten-request Binance TestNet DNS/TCP/TLS/TTFB/total latency benchmark from the selected region.
- Authenticated Telegram and configured CoinGecko/CMC connectivity; authenticated Binance TestNet account/order lifecycle.
- Docker restart, each-container restart, VM reboot, network/DNS/Binance/Telegram interruption.
- Disk warning/critical pause behaviour, OOM/memory pressure, backup, staged restore, automatic/manual rollback and invalid-artifact rejection.
- Minimum 72-hour initial TestNet soak plus the repository's longer qualification soak.

## Three QA passes

1. **Functional:** Ubuntu/ARM64, resource, artifact, monitor, backup and rollback flows were traced statically; local syntax, Python and repository tests passed. Actual deployment remains pending.
2. **Adversarial security:** deployment-account compromise, CI substitution, malicious env values, symlink/path redirection, stale approval, archive traversal/special files, Docker privilege, public ports, secret output, rollback failure, disk pressure and host collision were challenged. The review found and fixed the unsigned deployment-control defect and privileged-path/backup edge cases.
3. **Regression:** protected hashes are unchanged; the TestNet package cannot request LIVE; Freqtrade remains signal-only; execution ownership, risk logic and Sharia policy remain unchanged. A temporary LIVE-port text corruption found by the complete suite was restored to exact paired-file parity before final validation.

## Residual risks and unresolved external work

- No infrastructure hardening proves profitability; strategy edge remains unproven.
- No local/static result proves Binance order lifecycle correctness, Oracle capacity, regional latency, network recovery or soak stability.
- OCI free-tier capacity and home-region availability are external conditions.
- Shared runtime-egress networking is a residual lateral-movement surface among services that require Internet access; Sharia egress remains separately isolated. Further segmentation requires real-container compatibility testing.
- Custom seccomp/AppArmor profiles require observed ARM64 runtime syscalls and are not claimed.
- GitHub branch protection, CODEOWNER identity, protected environments/reviewers and retained signed provenance require repository-owner configuration.
- Runtime secrets, provider quotas, Binance permissions/IP allowlist, Telegram ownership and monitor token must be configured and validated on the host.
- The Sharia source/owner-review process and research-not-fatwa limitation remain unchanged.

## Final status

- **INFRASTRUCTURE HARDENING: PASS** — local implementation/static scope only
- **LOCAL TESTS: PASS**
- **PROTECTED TRADING CORE UNCHANGED: PASS**
- **TESTNET ENFORCEMENT: PASS**
- **REAL ORACLE VALIDATION: PENDING**
- **ORACLE SOAK: PENDING**
- **LIVE PROMOTION: PROHIBITED**

This package is suitable for further AI-agent and human review. It is not yet
evidence that the bot is ready for authenticated TestNet operation on Oracle,
and it is not permission to trade real money.

## Primary references

- [Oracle Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Oracle instance creation](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/launchinginstance.htm)
- [Oracle NTP configuration](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/configuringntpservice.htm)
- [Oracle instance metadata](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/gettingmetadata.htm)
- [Oracle Compute security](https://docs.oracle.com/en-us/iaas/Content/Security/Reference/compute_security.htm)
- [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Docker firewall behaviour](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
- [Docker iptables and `DOCKER-USER`](https://docs.docker.com/engine/network/firewall-iptables/)
- [Docker JSON log rotation](https://docs.docker.com/engine/logging/drivers/json-file/)
- [Docker live restore](https://docs.docker.com/engine/daemon/live-restore/)
- [Ubuntu Chrony guidance](https://documentation.ubuntu.com/server/how-to/networking/chrony-client/)
- [Ubuntu automatic security updates](https://documentation.ubuntu.com/server/how-to/software/automatic-updates/)
- [Binance Spot REST timing security](https://developers.binance.com/en/docs/products/spot/rest-api)
- [GitHub secure use](https://docs.github.com/en/actions/reference/security/secure-use)
- [GitHub self-hosted runner guidance](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
