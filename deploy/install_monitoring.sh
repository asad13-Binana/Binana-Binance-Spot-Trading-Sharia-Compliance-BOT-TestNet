#!/usr/bin/env bash
set -euo pipefail
umask 022

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=deploy/instance_identity.sh
source "$SCRIPT_DIR/instance_identity.sh"
RELEASE_DIR=${1:?usage: install_monitoring.sh RELEASE_DIR MODE RELEASE_HASH}
MODE=${2:?usage: install_monitoring.sh RELEASE_DIR MODE RELEASE_HASH}
RELEASE_HASH=${3:?usage: install_monitoring.sh RELEASE_DIR MODE RELEASE_HASH}
readonly PRIVATE=$PRIVATE_ROOT

fail(){ echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail 'install_monitoring.sh must run as root'
[[ "$APP_ROOT" == /opt/binana-testnet ]] || fail 'APP_ROOT identity mismatch'
[[ "$PRIVATE" == /etc/binana-testnet ]] || fail 'PRIVATE identity mismatch'
[[ "$PERSIST" == /var/lib/binana-testnet/shared ]] || fail 'PERSIST identity mismatch'
[[ "$MONITOR_LOG_DIR" == /var/log/binana-testnet/monitor ]] || fail 'MONITOR_LOG_DIR identity mismatch'
for protected_path in "$APP_ROOT" "$PRIVATE" "$PERSIST" "$MONITOR_LOG_DIR"; do
  [[ ! -L "$protected_path" ]] || fail "privileged monitoring path must not be a symlink: $protected_path"
done
[[ "$MODE" == "$INSTANCE_MODE" ]] || fail "MODE must be $INSTANCE_MODE for $INSTANCE_SLUG"
[[ "$RELEASE_HASH" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid release hash'
RELEASE_DIR=$(readlink -f "$RELEASE_DIR")
case "$RELEASE_DIR" in
  "$APP_ROOT"/releases/*) ;;
  *) fail 'monitoring release must be inside the fixed release root' ;;
esac
[[ -d "$RELEASE_DIR/monitoring" ]] || fail 'monitoring source missing from release'
[[ $(<"$RELEASE_DIR/RELEASE_MODE") == "$MODE" ]] || fail 'release mode does not match installer mode'

if ! id "$MONITOR_USER" >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin "$MONITOR_USER"
fi
id "$BOT_USER" >/dev/null 2>&1 || fail "$BOT_USER account is required before monitoring installation"
usermod -a -G "$BOT_USER" "$MONITOR_USER"
install -d -m 0755 -o root -g root "$APP_ROOT/monitoring-venvs" /usr/local/libexec
[[ ! -L "/var/backups/$INSTANCE_SLUG" ]] || fail 'backup root must not be a symlink'
install -d -m 0700 -o root -g root "/var/backups/$INSTANCE_SLUG"
install -d -m 0750 -o "$MONITOR_USER" -g "$MONITOR_USER" "$MONITOR_LOG_DIR"
# Runtime state remains writable only by the instance bot. The monitor is a supplementary
# member with group read/traverse but receives no group write permission.
install -d -m 0750 -o "$BOT_USER" -g "$BOT_USER" "$PERSIST/runtime"

VENV_ROOT="$APP_ROOT/monitoring-venvs"
VENV_TARGET="$VENV_ROOT/$RELEASE_HASH"
if [[ ! -f "$VENV_TARGET/.complete" ]]; then
  [[ ! -L "$VENV_TARGET" ]] || fail 'monitoring venv must not be a symlink'
  if [[ -e "$VENV_TARGET" ]]; then
    mv -T "$VENV_TARGET" "$VENV_TARGET.incomplete-$(date -u +%s%N)"
  fi
  # Create at the final path; moving venvs leaves broken pip/console shebangs.
  /usr/bin/python3 -I -m venv "$VENV_TARGET"
  # DEP-HASH-001: reject substituted or tampered distributions before the
  # monitoring environment can be activated on the Oracle host.
  env -i PATH=/usr/bin:/bin HOME=/root PIP_CONFIG_FILE=/dev/null \
    "$VENV_TARGET/bin/python" -I -m pip install --disable-pip-version-check \
    --require-hashes \
    --requirement "$RELEASE_DIR/monitoring/requirements-monitoring.lock"
  "$VENV_TARGET/bin/python" -I -m pip check
  chmod -R go-w "$VENV_TARGET"
  touch "$VENV_TARGET/.complete"
fi
ln -sfn "$VENV_TARGET" "$APP_ROOT/monitoring-current.new"
mv -Tf "$APP_ROOT/monitoring-current.new" "$APP_ROOT/monitoring-current"

# The privileged Docker helper is copied out of the user-controlled release
# tree and made root-owned. The botmon process never receives the Docker socket.
helper=$(mktemp "/usr/local/libexec/.${INSTANCE_SLUG}-snapshot.XXXXXX")
sed -e "s#/var/lib/binana-freqtrade-v101/shared#$PERSIST#g" \
    -e "s#binana-freqtrade-v101#$INSTANCE_SLUG#g" \
    "$RELEASE_DIR/monitoring/snapshot.py" >"$helper"
install -m 0755 -o root -g root "$helper" \
  "/usr/local/libexec/${SYSTEMD_PREFIX}-monitor-snapshot"
rm -f -- "$helper"

ENV_FILE="$PRIVATE/${MODE}-monitor.env"
TEMPLATE="$RELEASE_DIR/monitoring/.env.monitor.${MODE}.example"
if [[ ! -f "$ENV_FILE" ]]; then
  install -m 0640 -o root -g "$MONITOR_USER" "$TEMPLATE" "$ENV_FILE"
fi
[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail 'monitor environment must be a regular non-symlink file'
if grep -Eq '^MONITOR_TOKEN=(replace_on_oracle_only|changeme|)$' "$ENV_FILE"; then
  TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  sed -i "s/^MONITOR_TOKEN=.*/MONITOR_TOKEN=${TOKEN}/" "$ENV_FILE"
fi
chown "root:$MONITOR_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

UNIT_DIR="$RELEASE_DIR/monitoring/systemd"
RENDERED=$(mktemp -d "/run/${INSTANCE_SLUG}-units.XXXXXX")
trap 'rm -rf -- "$RENDERED"' EXIT
render_unit(){
  local source=$1 base target
  base=$(basename -- "$source")
  target="$RENDERED/${SYSTEMD_PREFIX}-${base#binana-}"
  sed \
    -e "s#/opt/binana-freqtrade-v101#$APP_ROOT#g" \
    -e "s#/etc/binana-freqtrade-v101#$PRIVATE#g" \
    -e "s#/var/lib/binana-freqtrade-v101/shared#$PERSIST#g" \
    -e "s#/var/log/binana-freqtrade-v101/monitor#$MONITOR_LOG_DIR#g" \
    -e "s#binana-monitor#${SYSTEMD_PREFIX}-monitor#g" \
    -e "s#binana-disk#${SYSTEMD_PREFIX}-disk#g" \
    -e "s#binana-state#${SYSTEMD_PREFIX}-state#g" \
    -e "s#binana-offhost#${SYSTEMD_PREFIX}-offhost#g" \
    -e "s#binana-api#${SYSTEMD_PREFIX}-api#g" \
    -e "s#User=botmon#User=$MONITOR_USER#g" \
    -e "s#Group=botmon#Group=$MONITOR_USER#g" \
    -e "s#binanabot#$BOT_USER#g" \
    -e "s#botmon#$MONITOR_USER#g" \
    -e "s#binana-freqtrade-v101#$INSTANCE_SLUG#g" \
    "$source" >"$target"
  grep -Fq '/opt/binana-freqtrade-v101' "$target" && fail "unrendered APP_ROOT in $base"
  grep -Fq '/var/lib/binana-freqtrade-v101' "$target" && fail "unrendered PERSIST in $base"
  install -m 0644 -o root -g root "$target" "/etc/systemd/system/$(basename -- "$target")"
}
for unit in \
  "binana-monitor-${MODE}.service" \
  "binana-monitor-report-${MODE}.service" \
  "binana-monitor-report-${MODE}.timer" \
  binana-monitor-snapshot.service binana-monitor-snapshot.timer \
  binana-disk-guard.service binana-disk-guard.timer \
  binana-state-backup.service binana-state-backup.timer \
  binana-offhost-backup.service binana-offhost-backup.timer \
  binana-api-readiness.service binana-api-readiness.timer; do
  render_unit "$UNIT_DIR/$unit"
done

configured_port=$(awk -F= '$1 == "MONITOR_PORT" {print $2; exit}' "$ENV_FILE")
[[ "$configured_port" == "$EXPECTED_MONITOR_PORT" ]] || \
  fail "monitor template port ${configured_port:-missing} does not match instance port $EXPECTED_MONITOR_PORT"

MONITOR_SERVICE="${SYSTEMD_PREFIX}-monitor-${MODE}.service"
REPORT_TIMER="${SYSTEMD_PREFIX}-monitor-report-${MODE}.timer"
SNAPSHOT_TIMER="${SYSTEMD_PREFIX}-monitor-snapshot.timer"
systemctl daemon-reload
systemctl enable --now "${SYSTEMD_PREFIX}-disk-guard.timer" \
  "${SYSTEMD_PREFIX}-state-backup.timer" "${SYSTEMD_PREFIX}-api-readiness.timer"

if grep -Eq '^MONITOR_ENABLED=true$' "$ENV_FILE"; then
  systemctl enable --now "$SNAPSHOT_TIMER"
  systemctl start "${SYSTEMD_PREFIX}-monitor-snapshot.service"
  systemctl enable "$MONITOR_SERVICE"
  systemctl restart "$MONITOR_SERVICE"
  systemctl is-active --quiet "$MONITOR_SERVICE" || fail 'monitor API did not become active'
else
  systemctl disable --now "$MONITOR_SERVICE" "$SNAPSHOT_TIMER" >/dev/null 2>&1 || true
fi
if grep -Eq '^TELEGRAM_REPORTS_ENABLED=true$' "$ENV_FILE"; then
  systemctl enable --now "$REPORT_TIMER"
else
  systemctl disable --now "$REPORT_TIMER" >/dev/null 2>&1 || true
fi

echo "Monitoring installed for $MODE; credentials were not printed."
