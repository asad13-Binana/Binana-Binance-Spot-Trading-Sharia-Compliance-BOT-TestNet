#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=deploy/instance_identity.sh
source "$SCRIPT_DIR/instance_identity.sh"
# shellcheck source=deploy/lib/secure_env.sh
source "$SCRIPT_DIR/lib/secure_env.sh"
readonly ENV_FILE=$PRIVATE_ROOT/.env
[[ $EUID -eq 0 ]] || { echo 'ERROR: run oracle_validate.sh with sudo' >&2; exit 1; }

declare -A VALUES=()
secure_env_read "$ENV_FILE" VALUES
product=${VALUES[BOT_PRODUCT]:-UNCONFIGURED}
environment=${VALUES[BOT_ENVIRONMENT]:-UNCONFIGURED}
instance_id=${VALUES[BOT_INSTANCE_ID]:-UNCONFIGURED}
execution_mode=${VALUES[EXECUTION_MODE]:-UNCONFIGURED}
package_mode=$(cat "$APP_ROOT/current/RELEASE_MODE" 2>/dev/null || echo unavailable)
release_version=$(cat "$APP_ROOT/current/RELEASE_VERSION" 2>/dev/null || echo unavailable)
release_sha=$(awk 'NF {print $1; exit}' "$APP_ROOT/current/RELEASE_SHA256.txt" 2>/dev/null || echo unavailable)
git_sha=$(cat "$APP_ROOT/current/.git-commit" 2>/dev/null || echo unavailable)

printf '=== BINANA ORACLE VALIDATION (NO SECRETS) ===\n'
printf 'hostname=%s\nBOT_PRODUCT=%s\nBOT_ENVIRONMENT=%s\nBOT_INSTANCE_ID=%s\n' \
  "$(hostname -f 2>/dev/null || hostname)" "$product" "$environment" "$instance_id"
printf 'os=%s\narchitecture=%s\nkernel=%s\ncpu_count=%s\n' \
  "$(. /etc/os-release; printf '%s %s' "$NAME" "$VERSION_ID")" \
  "$(dpkg --print-architecture)" "$(uname -r)" "$(nproc)"
free -h
df -hT /
swapon --show --bytes || true
printf 'release_version=%s\nrelease_sha256=%s\ngit_sha=%s\npackage_mode=%s\nexecution_mode=%s\n' \
  "$release_version" "$release_sha" "$git_sha" "$package_mode" "$execution_mode"

printf '\n=== DOCKER ===\n'
docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'
docker compose version
systemctl is-active docker
docker info --format 'driver={{.Driver}} logging={{.LoggingDriver}} live_restore={{.LiveRestoreEnabled}}'
docker ps --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
container_ids=$(docker ps -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")
if [[ -n "$container_ids" ]]; then
  mapfile -t instance_containers <<<"$container_ids"
  docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.PIDs}}' \
    "${instance_containers[@]}"
fi

printf '\n=== TIME ===\n'
systemctl is-active chrony
timedatectl show -p NTPSynchronized -p TimeUSec -p Timezone
chronyc -N tracking
last_offset=$(LC_ALL=C chronyc tracking | awk -F: '/^Last offset/ {gsub(/ seconds|[[:space:]]/, "", $2); print $2; exit}')
python3 - "$last_offset" <<'PY'
import sys
offset = abs(float(sys.argv[1]))
if offset > 0.100:
    raise SystemExit(f"chrony_offset_check=FAIL offset_seconds={offset:.6f} limit_seconds=0.100")
print(f"chrony_offset_check=PASS offset_seconds={offset:.6f} limit_seconds=0.100")
PY

printf '\n=== LISTENING SOCKETS ===\n'
ss -lntup

printf '\n=== PROVIDER CONNECTIVITY ===\n'
if [[ "$package_mode" == testnet ]]; then
  binance_base=https://testnet.binance.vision
else
  binance_base=https://api.binance.com
fi
curl --proto '=https' --tlsv1.2 -fsS --max-time 15 "$binance_base/api/v3/time" >/dev/null \
  && echo 'binance_public_https=PASS' || echo 'binance_public_https=FAIL'
if [[ -n "${VALUES[COINGECKO_API_KEY]:-}" ]]; then
  cg_key=${VALUES[COINGECKO_API_KEY]}
  curl --config - >/dev/null 2>&1 <<EOF && echo 'coingecko_authenticated_https=PASS' || echo 'coingecko_authenticated_https=FAIL'
url = "https://api.coingecko.com/api/v3/ping"
header = "x-cg-demo-api-key: ${cg_key}"
max-time = 15
fail
silent
show-error
EOF
else
  curl --proto '=https' --tlsv1.2 -fsS --max-time 15 https://api.coingecko.com/api/v3/ping >/dev/null \
    && echo 'coingecko_public_https=PASS' || echo 'coingecko_public_https=FAIL'
fi

if [[ -n "${VALUES[COINMARKETCAP_API_KEY]:-${VALUES[CMC_API_KEY]:-}}" ]]; then
  cmc_key=${VALUES[COINMARKETCAP_API_KEY]:-${VALUES[CMC_API_KEY]:-}}
  curl --config - >/dev/null 2>&1 <<EOF && echo 'coinmarketcap_authenticated_https=PASS' || echo 'coinmarketcap_authenticated_https=FAIL'
url = "https://pro-api.coinmarketcap.com/v1/key/info"
header = "X-CMC_PRO_API_KEY: ${cmc_key}"
max-time = 15
fail
silent
show-error
EOF
else
  echo 'coinmarketcap_authenticated_https=SKIPPED_KEY_NOT_CONFIGURED'
fi

if [[ "${VALUES[TELEGRAM_BOT_TOKEN]:-}" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]]; then
  telegram_token=${VALUES[TELEGRAM_BOT_TOKEN]}
  curl --config - >/dev/null 2>&1 <<EOF && echo 'telegram_authenticated_https=PASS' || echo 'telegram_authenticated_https=FAIL'
url = "https://api.telegram.org/bot${telegram_token}/getMe"
max-time = 15
fail
silent
show-error
EOF
else
  echo 'telegram_authenticated_https=SKIPPED_TOKEN_NOT_CONFIGURED'
fi

printf '\n=== BINANCE HTTPS LATENCY (10 REQUESTS) ===\n'
latency_file=$(mktemp)
trap 'rm -f -- "$latency_file"' EXIT
for _ in $(seq 1 10); do
  curl --proto '=https' --tlsv1.2 -fsS --max-time 15 -o /dev/null \
    -w '%{time_namelookup},%{time_connect},%{time_appconnect},%{time_starttransfer},%{time_total}\n' \
    "$binance_base/api/v3/time" >>"$latency_file"
done
python3 - "$latency_file" <<'PY'
import math
import statistics
import sys

names = ("time_namelookup", "time_connect", "time_appconnect", "time_starttransfer", "time_total")
rows = [[float(value) * 1000 for value in line.split(",")]
        for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
if len(rows) != 10:
    raise SystemExit("latency sample set is incomplete")
for index, name in enumerate(names):
    values = sorted(row[index] for row in rows)
    p95 = values[max(0, math.ceil(0.95 * len(values)) - 1)]
    print(f"{name}_ms min={values[0]:.3f} median={statistics.median(values):.3f} p95={p95:.3f} max={values[-1]:.3f}")
PY

printf '\nmonitor_state='
systemctl is-active "${SYSTEMD_PREFIX}-monitor-${package_mode}.service" 2>/dev/null || true
printf 'local_backup_timer='
systemctl is-active "${SYSTEMD_PREFIX}-state-backup.timer" 2>/dev/null || true
printf 'offhost_backup_timer='
systemctl is-active "${SYSTEMD_PREFIX}-offhost-backup.timer" 2>/dev/null || true
printf 'api_readiness_timer='
systemctl is-active "${SYSTEMD_PREFIX}-api-readiness.timer" 2>/dev/null || true
if [[ -f "$PERSIST/runtime/offhost_backup_status.json" && ! -L "$PERSIST/runtime/offhost_backup_status.json" ]]; then
  python3 - "$PERSIST/runtime/offhost_backup_status.json" <<'PY'
import json, pathlib, sys
data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
safe = {key: data.get(key) for key in (
    "ok", "completed_at", "failed_at", "source_backup", "object_name",
    "encrypted_sha256", "authentication", "exit_code"
) if key in data}
print("offhost_backup_status=" + json.dumps(safe, sort_keys=True))
PY
else
  echo 'offhost_backup_status=NOT_CONFIGURED_OR_NOT_RUN'
fi
if [[ -f "$PERSIST/runtime/api_readiness_status.json" && ! -L "$PERSIST/runtime/api_readiness_status.json" ]]; then
  python3 - "$PERSIST/runtime/api_readiness_status.json" <<'PY'
import json, pathlib, sys
data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
providers = data.get("providers", {}) if isinstance(data, dict) else {}
safe = {
    name: {
        "status": value.get("status"),
        "required": bool(value.get("required")),
        "reason": (value.get("details") or {}).get("reason"),
    }
    for name, value in providers.items()
    if name in {"binance", "telegram", "coingecko", "coinmarketcap"}
    and isinstance(value, dict)
}
print("api_readiness_status=" + json.dumps({
    "ok": data.get("ok"),
    "generated_at": data.get("generated_at"),
    "package_mode": data.get("package_mode"),
    "network_operations": data.get("network_operations"),
    "providers": safe,
}, sort_keys=True))
PY
else
  echo "api_readiness_status=NOT_RUN; run sudo $APP_ROOT/current/deploy/api_preflight.sh"
fi
printf 'validation_complete=YES; Oracle soak/fault drills are separate and are not implied.\n'
