#!/usr/bin/env bash
set -euo pipefail

RELEASE_DIR=${1:?usage: install_monitoring.sh RELEASE_DIR MODE RELEASE_HASH}
MODE=${2:?usage: install_monitoring.sh RELEASE_DIR MODE RELEASE_HASH}
RELEASE_HASH=${3:?usage: install_monitoring.sh RELEASE_DIR MODE RELEASE_HASH}
APP_ROOT=${APP_ROOT:-/opt/binance-freqtrade-v101}
PRIVATE=${PRIVATE:-/etc/binance-freqtrade-v101}
PERSIST=${PERSIST:-/var/lib/binance-freqtrade-v101/shared}
MONITOR_LOG_DIR=${MONITOR_LOG_DIR:-/var/log/binance-freqtrade-v101/monitor}

fail(){ echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail 'install_monitoring.sh must run as root'
[[ "$MODE" == testnet || "$MODE" == live ]] || fail 'MODE must be testnet or live'
[[ "$RELEASE_HASH" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid release hash'
RELEASE_DIR=$(readlink -f "$RELEASE_DIR")
[[ -d "$RELEASE_DIR/monitoring" ]] || fail 'monitoring source missing from release'
[[ $(<"$RELEASE_DIR/RELEASE_MODE") == "$MODE" ]] || fail 'release mode does not match installer mode'

if ! id botmon >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin botmon
fi
install -d -m 0755 -o root -g root "$APP_ROOT/monitoring-venvs" /usr/local/libexec
install -d -m 0750 -o botmon -g botmon "$MONITOR_LOG_DIR"
install -d -m 0755 -o root -g root "$PERSIST/runtime"

VENV_ROOT="$APP_ROOT/monitoring-venvs"
VENV_TARGET="$VENV_ROOT/$RELEASE_HASH"
if [[ ! -f "$VENV_TARGET/.complete" ]]; then
  [[ ! -e "$VENV_TARGET" ]] || fail "incomplete monitoring venv exists: $VENV_TARGET"
  BUILD=$(mktemp -d "$VENV_ROOT/.build.XXXXXX")
  python3 -m venv "$BUILD/venv"
  # DEP-HASH-001: reject substituted or tampered distributions before the
  # monitoring environment can be activated on the Oracle host.
  "$BUILD/venv/bin/python" -m pip install --disable-pip-version-check \
    --require-hashes \
    --requirement "$RELEASE_DIR/monitoring/requirements-monitoring.lock"
  "$BUILD/venv/bin/python" -m pip check
  touch "$BUILD/venv/.complete"
  mv "$BUILD/venv" "$VENV_TARGET"
  rmdir "$BUILD"
fi
ln -sfn "$VENV_TARGET" "$APP_ROOT/monitoring-current.new"
mv -Tf "$APP_ROOT/monitoring-current.new" "$APP_ROOT/monitoring-current"

# The privileged Docker helper is copied out of the user-controlled release
# tree and made root-owned. The botmon process never receives the Docker socket.
install -m 0755 -o root -g root \
  "$RELEASE_DIR/monitoring/snapshot.py" \
  /usr/local/libexec/binance-bot-monitor-snapshot

ENV_FILE="$PRIVATE/${MODE}-monitor.env"
TEMPLATE="$RELEASE_DIR/monitoring/.env.monitor.${MODE}.example"
if [[ ! -f "$ENV_FILE" ]]; then
  install -m 0640 -o root -g botmon "$TEMPLATE" "$ENV_FILE"
fi
if grep -Eq '^MONITOR_TOKEN=(replace_on_oracle_only|changeme|)$' "$ENV_FILE"; then
  TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  sed -i "s/^MONITOR_TOKEN=.*/MONITOR_TOKEN=${TOKEN}/" "$ENV_FILE"
fi
chown root:botmon "$ENV_FILE"
chmod 0640 "$ENV_FILE"

UNIT_DIR="$RELEASE_DIR/monitoring/systemd"
install -m 0644 -o root -g root \
  "$UNIT_DIR/binance-bot-monitor-${MODE}.service" \
  "$UNIT_DIR/binance-bot-monitor-report-${MODE}.service" \
  "$UNIT_DIR/binance-bot-monitor-report-${MODE}.timer" \
  "$UNIT_DIR/binance-bot-monitor-snapshot.service" \
  "$UNIT_DIR/binance-bot-monitor-snapshot.timer" \
  /etc/systemd/system/

OTHER=testnet
[[ "$MODE" == testnet ]] && OTHER=live
systemctl disable --now "binance-bot-monitor-${OTHER}.service" \
  "binance-bot-monitor-report-${OTHER}.timer" >/dev/null 2>&1 || true
systemctl daemon-reload

if grep -Eq '^MONITOR_ENABLED=true$' "$ENV_FILE"; then
  systemctl enable --now binance-bot-monitor-snapshot.timer
  systemctl enable --now "binance-bot-monitor-${MODE}.service"
else
  systemctl disable --now "binance-bot-monitor-${MODE}.service" \
    binance-bot-monitor-snapshot.timer >/dev/null 2>&1 || true
fi
if grep -Eq '^TELEGRAM_REPORTS_ENABLED=true$' "$ENV_FILE"; then
  systemctl enable --now "binance-bot-monitor-report-${MODE}.timer"
else
  systemctl disable --now "binance-bot-monitor-report-${MODE}.timer" >/dev/null 2>&1 || true
fi

echo "Monitoring installed for $MODE; credentials were not printed."
