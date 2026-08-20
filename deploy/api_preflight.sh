#!/usr/bin/env bash
# Authenticate every configured external provider using GET-only requests.
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=deploy/instance_identity.sh
source "$SCRIPT_DIR/instance_identity.sh"
# shellcheck source=deploy/lib/secure_env.sh
source "$SCRIPT_DIR/lib/secure_env.sh"
readonly ENV_FILE=$PRIVATE_ROOT/.env
readonly STATUS=$PERSIST/runtime/api_readiness_status.json

fail(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail 'api_preflight.sh must run with sudo'
[[ $# -eq 0 ]] || fail 'api_preflight.sh accepts no arguments'
command -v python3 >/dev/null 2>&1 || fail 'python3 is unavailable'
[[ -d "$APP_ROOT/current" && ! -L "$APP_ROOT/current/RELEASE_MODE" ]] || fail 'installed release is unavailable'
package_mode=$(tr -d '\r\n' <"$APP_ROOT/current/RELEASE_MODE")
[[ "$package_mode" == testnet || "$package_mode" == live ]] || fail 'installed package mode is invalid'
checker=$APP_ROOT/current/scripts/api_readiness.py
[[ -f "$checker" && ! -L "$checker" ]] || fail 'API readiness checker is unavailable'

declare -A VALUES=()
secure_env_read "$ENV_FILE" VALUES
config=$(printf '%s\0' \
  "${VALUES[BINANCE_API_KEY]:-}" \
  "${VALUES[BINANCE_API_SECRET]:-}" \
  "${VALUES[TELEGRAM_BOT_TOKEN]:-}" \
  "${VALUES[TELEGRAM_OWNER_CHAT_ID]:-}" \
  "${VALUES[COINGECKO_API_KEY]:-}" \
  "${VALUES[COINMARKETCAP_API_KEY]:-${VALUES[CMC_API_KEY]:-}}" \
  "${VALUES[ENABLE_COINGECKO_SIGNALS]:-false}" \
  "${VALUES[ENABLE_CMC_TRENDING]:-false}" \
  "${VALUES[SHARIA_AUTO_SOURCE_DISCOVERY_ENABLED]:-true}" | python3 -c '
import json, sys
names = (
    "BINANCE_API_KEY", "BINANCE_API_SECRET", "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_OWNER_CHAT_ID", "COINGECKO_API_KEY", "COINMARKETCAP_API_KEY",
    "ENABLE_COINGECKO_SIGNALS", "ENABLE_CMC_TRENDING",
    "SHARIA_AUTO_SOURCE_DISCOVERY_ENABLED",
)
values = sys.stdin.buffer.read().split(b"\0")
if values and values[-1] == b"":
    values.pop()
if len(values) != len(names):
    raise SystemExit("invalid API preflight input")
json.dump(dict(zip(names, (value.decode("utf-8") for value in values))), sys.stdout)
')

status_dir=$(dirname "$STATUS")
[[ -d "$status_dir" && ! -L "$status_dir" ]] || fail 'runtime status directory is unavailable or a symlink'
temporary=$(mktemp "$status_dir/.api-readiness.XXXXXX")
cleanup(){ rm -f -- "$temporary"; }
trap cleanup EXIT
set +e
printf '%s' "$config" | python3 "$checker" --package-mode "$package_mode" >"$temporary"
checker_status=$?
set -e
unset config VALUES
if ! python3 - "$temporary" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(value, dict) or value.get("schema_version") != 1:
    raise SystemExit("API readiness checker returned an invalid status")
for provider in value.get("providers", {}).values():
    if not isinstance(provider, dict) or provider.get("status") not in {"PASS", "FAIL", "SKIPPED"}:
        raise SystemExit("API readiness checker returned an invalid provider result")
PY
then
  checker_status=1
  python3 - "$temporary" "$package_mode" <<'PY'
import datetime as dt
import json
import pathlib
import sys
payload = {
    "schema_version": 1,
    "ok": False,
    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    "package_mode": sys.argv[2],
    "network_operations": "GET_ONLY_NO_ORDERS",
    "providers": {},
    "error": "checker_failed_before_valid_status",
}
pathlib.Path(sys.argv[1]).write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
PY
fi
chown root:"$(stat -Lc '%g' -- "$status_dir")" "$temporary"
chmod 0640 "$temporary"
mv -fT -- "$temporary" "$STATUS"
trap - EXIT

python3 - "$STATUS" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print("api_readiness=" + ("PASS" if value.get("ok") is True else "FAIL"))
for name, result in sorted(value.get("providers", {}).items()):
    details = result.get("details", {})
    reason = details.get("reason") if isinstance(details, dict) else None
    suffix = f" reason={reason}" if reason else ""
    print(f"{name}={result.get('status')} required={str(bool(result.get('required'))).lower()}{suffix}")
print("network_operations=GET_ONLY_NO_ORDERS")
PY
exit "$checker_status"
