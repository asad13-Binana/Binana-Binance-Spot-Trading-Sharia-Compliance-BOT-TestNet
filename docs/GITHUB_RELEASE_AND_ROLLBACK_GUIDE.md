# GitHub Release & Rollback Guide

## One repository, secret-free
One public, secret-free source repository. Secrets live only on Oracle (`/etc/.../.env`, mode 600) and in
protected GitHub Actions secrets for the deploy job. Never commit Binance/Telegram/API keys.

## CI pipeline (`.github/workflows/ci.yml`)
Actions are **commit-SHA pinned**. Jobs:

1. **verify** (matrix Python 3.10–3.13): `deploy/verify_release.sh` (compile, full unittest suite, secret
   scan, V19.1 schema + controller byte-integrity, legacy 33 self-tests, manifest build+verify, JSON/shell/
   YAML) + `pip-audit --strict`.
2. **artifact**: builds a deterministic, sorted, fixed-mtime `.tar.gz` + SHA-256, extracts it to a fresh
   directory, re-verifies the manifest, re-runs `verify_release.sh`, validates + builds the Compose image,
   and `cmp`s the extracted manifest against the workspace manifest. Uploads the immutable artifact.
3. **deploy-oracle** (only on `main` push when `vars.ORACLE_TESTNET_DEPLOY_ENABLED == 'true'`): SSHes the verified
   artifact to Oracle and runs `install_artifact.sh`. The service does **not** go live merely because a
   commit was pushed — deployment is gated on CI success + explicit enablement, and live mode still requires
   the signed evidence envelope.

## Required repository settings (external, not done here)
- Replace `@OWNER` in `.github/CODEOWNERS` with the real owner.
- Enable branch protection on `main`: require CI to pass and require review from Code Owners (this closes
  H-010 — a strategy change cannot be silently re-baselined in the same commit).
- Configure the `oracle-testnet` environment secrets: `ORACLE_SSH_PRIVATE_KEY`, `ORACLE_SSH_KNOWN_HOSTS`,
  `ORACLE_HOST`, `ORACLE_USER`, `ORACLE_SSH_PORT`.
- REMAINING: pin base `python` and `freqtradeorg/freqtrade` images by registry digest and retain signed
  image/artifact provenance (H-003/H-009).

## Rollback
If the new release's six-service health check fails, `install_artifact.sh` automatically: brings the new
release down, restores the previous release symlink, starts it, and **verifies the old release becomes
healthy**. The deployment status file records `ROLLED_BACK_OLD_HEALTHY` or the CRITICAL
`ROLLED_BACK_OLD_UNHEALTHY_CRITICAL`; entries stay paused until an owner resumes via Telegram.

## Manual rollback
```
ln -sfn /opt/binance-freqtrade-v101/releases/<previous-stamp> /opt/binance-freqtrade-v101/current
cd "$(readlink -f /opt/binance-freqtrade-v101/current)"
RELEASE_TAG="$(cat .release-tag)" COMPOSE_PROJECT_NAME=binance-freqtrade-v101 \
  docker compose --env-file /etc/binance-freqtrade-v101/.env up -d
bash scripts/healthcheck.sh
```
