#!/usr/bin/env bash
set -euo pipefail
APP_ROOT=${APP_ROOT:-/opt/binance-freqtrade-v101}
PERSIST=${PERSIST:-/var/lib/binance-freqtrade-v101/shared}
PRIVATE=${PRIVATE:-/etc/binance-freqtrade-v101}
MEMINFO_PATH=${MEMINFO_PATH:-/proc/meminfo}
MIN_PHYSICAL_MEMORY_MIB=${MIN_PHYSICAL_MEMORY_MIB:-1500}
MIN_TOTAL_MEMORY_MIB=${MIN_TOTAL_MEMORY_MIB:-2000}

physical_mib=$(awk '/MemTotal/{print int($2/1024)}' "$MEMINFO_PATH")
swap_mib=$(awk '/SwapTotal/{print int($2/1024)}' "$MEMINFO_PATH")
if (( physical_mib < MIN_PHYSICAL_MEMORY_MIB || physical_mib + swap_mib < MIN_TOTAL_MEMORY_MIB )); then
  echo "ERROR: host has ${physical_mib} MiB RAM and ${swap_mib} MiB swap; minimum is ${MIN_PHYSICAL_MEMORY_MIB} MiB RAM and ${MIN_TOTAL_MEMORY_MIB} MiB combined." >&2
  echo 'A 1 GiB E2 micro is unsupported. Use an A1 free-tier allocation with at least 2 GiB, preferably 4 GiB.' >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin git curl jq python3 python3-venv
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
if ! id botmon >/dev/null 2>&1; then
  sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin botmon
fi
sudo mkdir -p "$APP_ROOT/releases" "$PERSIST" "$PERSIST/commands/inbox" \
  "$PERSIST/runtime" "$PERSIST/sharia" "$PERSIST/legacy_runtime" \
  "$PERSIST/freqtrade/logs" "$PRIVATE" \
  /var/log/binance-freqtrade-v101/monitor
sudo chown -R "$USER:$USER" "$APP_ROOT" "$PERSIST"
sudo chown botmon:botmon /var/log/binance-freqtrade-v101/monitor
sudo chmod 0750 /var/log/binance-freqtrade-v101/monitor
if [[ ! -f "$PRIVATE/.env" ]]; then
  sudo install -m 600 /dev/null "$PRIVATE/.env"
  echo "Created $PRIVATE/.env. Populate it from .env.example before deployment."
fi
sudo chmod 600 "$PRIVATE/.env"
sudo chown "$USER:$USER" "$PRIVATE/.env"
echo 'Oracle host prepared. Sign out and back in once so Docker group membership applies. API keys should have Spot permission only, withdrawals disabled, and IP restriction enabled. Monitoring receives no trading credentials and no Docker-socket access.'
