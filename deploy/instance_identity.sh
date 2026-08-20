#!/usr/bin/env bash
# Immutable, non-secret host identity for this repository package.
# Keep this file release-controlled; real credentials remain in PRIVATE_ROOT/.env.
readonly INSTANCE_SLUG=binana-testnet
readonly INSTANCE_MODE=testnet
readonly COMPOSE_PROJECT_NAME=binana-testnet
readonly SERVICE_IMAGE=binana-testnet-services
readonly APP_ROOT=/opt/binana-testnet
readonly PRIVATE_ROOT=/etc/binana-testnet
readonly PERSIST_PARENT=/var/lib/binana-testnet
readonly PERSIST=/var/lib/binana-testnet/shared
readonly MONITOR_LOG_DIR=/var/log/binana-testnet/monitor
readonly DEPLOY_INBOX=/var/lib/binana-testnet/deploy-inbox
readonly INSTALL_LOCK=/var/lock/binana-testnet.install.lock
readonly BACKUP_LOCK=/var/lock/binana-testnet.backup.lock
readonly ACTIONS_LOCK=/var/lock/binana-testnet.actions-deploy.lock
readonly BOT_USER=binanatn
readonly MONITOR_USER=binanatnmon
readonly SYSTEMD_PREFIX=binana-testnet
readonly EXPECTED_MONITOR_PORT=8090
readonly OCI_OBJECT_PREFIX=binana-testnet
readonly GITHUB_RUNNER_LABEL=oracle-binana-testnet
