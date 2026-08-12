# GitHub-to-Oracle deployment

## Repository and private-data model

Use one public GitHub repository for source, tests, safe examples, Docker/deployment files, and documentation. Do not place trading credentials in any branch, including a private branch.

Keep private trading material only on Oracle in root-owned `/etc/binana-freqtrade-v101/.env` with mode `600`, and runtime/private data under `/var/lib/binana-freqtrade-v101/shared`.

Required GitHub **environment secrets**:

- `ORACLE_HOST`
- `ORACLE_USER`
- `ORACLE_SSH_PORT`
- `ORACLE_SSH_PRIVATE_KEY`
- `ORACLE_SSH_KNOWN_HOSTS` — a pre-verified host-key line; the workflow deliberately does not use `ssh-keyscan`

Configure a protected `oracle-testnet` GitHub environment and set repository
variable `ORACLE_TESTNET_DEPLOY_ENABLED=true` only after the host, private
environment, persistent ownership, and rollback process are prepared.

## Oracle preparation

```bash
sudo ./deploy/oracle_setup.sh
sudoedit /etc/binana-freqtrade-v101/.env
sudo chmod 600 /etc/binana-freqtrade-v101/.env
```

In the private `.env` set:

```text
BOT_UID=<BOT_UID printed by oracle_setup.sh>
BOT_GID=<BOT_GID printed by oracle_setup.sh>
SHARED_HOST_PATH=/var/lib/binana-freqtrade-v101/shared
EXECUTION_MODE=testnet
BINANCE_TESTNET=true
```

The installer requires Ubuntu 24.04, at least 5 GiB physical RAM and 35 GiB free disk. Use the declared A1 target of 1 OCPU, 6 GiB RAM, approximately 50 GiB boot storage and 4 GiB swap.

Recommended Binance key controls:

- dedicated account/sub-account
- Spot trading permission only
- withdrawals disabled
- Oracle public IP allow-listed
- no universal-transfer permission unless separately justified
- testnet credentials during lifecycle validation

## Persistent data

`/var/lib/binana-freqtrade-v101/shared` retains:

- private Sharia records and legacy-compatible HALAL export
- sidecar SQLite state and exchange events
- incoming/processed/rejected signal files
- universe snapshots and exact snapshot hashes
- audit logs and bounded backups
- command results and deployment status
- preserved-core runtime state/logs/cache
- Freqtrade database and logs
- live approval marker

A release never overwrites an existing private Sharia dataset. Release directories and service images are immutable and retained in a bounded rollback set.

## Deployment transaction

A push to `main` performs verification and artifact creation. Only when the
testnet deployment variable is enabled may CI transfer the artifact and invoke
the narrow wrapper; the wrapper still fails closed unless an operator has
independently approved that exact archive digest on the host. CI cannot write
the root-owned approval file.

1. Python compilation, the complete merged core suite, and monitoring tests
2. original 33-test V4.9.16 self-test
3. source-preservation, Sharia-schema, JSON, shell, YAML, secret, and manifest checks
4. service image build
5. deterministic immutable one-root tar artifact and SHA-256 checksum
6. SSH transfer using a pinned known-hosts secret
7. checksum, traversal, link, special-file, and archive-root validation
8. safe extraction plus release-manifest, secret, full regression, Compose and image-build verification against the extracted artifact
9. build of the new immutable image tag before touching the running release
10. explicit pause and reconciliation commands to the current sidecar, with verified acknowledgements
11. atomic `current` symlink switch
12. startup of all six bot services plus the isolated monitoring units, with health verification
13. deployment status and Telegram notification
14. automatic rollback to the prior release on failed health checks
15. bounded old-release and old-image cleanup

This is not an uncontrolled `git pull && restart` process. Every restart leaves new entries paused; the owner must review status and explicitly confirm resume.

## Container secret boundaries

The root Compose file does not pass the whole `.env` into containers:

- `execution-sidecar`: Binance key/secret and execution controls
- `telegram-broker`: Telegram token/owner and Freqtrade internal API password
- `freqtrade`: internal API credentials; no Binance secret in the merged signal-only runtime
- `universe`: public-market filters and Sharia/universe paths only

The `.dockerignore` is allowlist-based so the private `.env`, SQLite files, caches, and unrelated files are excluded from image build context.

## Live promotion marker

Only after all external gates pass, set `SIDECAR_RELEASE_HASH` in the private Oracle environment to the exact hash from the **installed** `RELEASE_SHA256.txt`, and place the same value in:

```text
/var/lib/binana-freqtrade-v101/shared/runtime/SIDECAR_LIVE_OK
```

The sidecar refuses live mode unless the installed release hash, private environment hash and persistent marker all match. The preserved V4.9.16 live markers must independently pass. Matching values are only interlocks; they are not evidence of testnet correctness or live readiness.

## Mandatory external gates

Before production consideration, document for the exact artifact hash:

1. OTO, OTOCO, fixed OCO, OCO+trailing, cancellation, replacement, and user-stream events on Binance Spot Testnet.
2. Partial fills, accepted-order REST timeouts, duplicate callbacks, stream disconnection, and restart with open order lists.
3. Oracle simulation with real market data, real Telegram controls, dynamic top-50 snapshots, and active Sharia expiry handling.
4. Resource/network/restart soak, emergency pause/exit, deployment rollback, and persistent-state recovery.
5. Three consecutive full clean audit passes.

Keep `EXECUTION_MODE=simulation` or controlled `testnet` until all gates are complete.
