#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=deploy/instance_identity.sh
source "$SCRIPT_DIR/instance_identity.sh"
# shellcheck source=deploy/lib/secure_env.sh
source "$SCRIPT_DIR/lib/secure_env.sh"
readonly ENV_FILE=$PRIVATE_ROOT/.env
WARNING_PERCENT=${DISK_WARNING_PERCENT:-80}
CRITICAL_PERCENT=${DISK_CRITICAL_PERCENT:-90}
[[ -d "$PERSIST" && ! -L "$PERSIST" ]] || { echo 'ERROR: persistent root unavailable' >&2; exit 1; }
[[ "$WARNING_PERCENT" =~ ^[0-9]+$ && "$CRITICAL_PERCENT" =~ ^[0-9]+$ ]] || exit 1
(( WARNING_PERCENT < CRITICAL_PERCENT && CRITICAL_PERCENT < 100 )) || exit 1
used=$(df -P "$PERSIST" | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
[[ "$used" =~ ^[0-9]+$ ]] || { echo 'ERROR: disk usage is unavailable' >&2; exit 1; }
status=ok
severity=INFO
exit_code=0
if (( used >= CRITICAL_PERCENT )); then status=critical; severity=CRITICAL; exit_code=2
elif (( used >= WARNING_PERCENT )); then status=warning; severity=WARNING; exit_code=1
fi
if [[ "$status" == critical ]]; then
  entries_enabled=$(python3 - "$PERSIST/runtime/sidecar_health.json" <<'PY'
import json
import pathlib
import sys
import time
path = pathlib.Path(sys.argv[1])
try:
    value = json.loads(path.read_text(encoding="utf-8")).get("entries_enabled")
    fresh = 0 <= time.time() - path.stat().st_mtime <= 30
except Exception:
    value = None
    fresh = False
print("false" if value is False and fresh else "true")
PY
  )
  if [[ "$entries_enabled" == true ]]; then
    release_hash=$(awk 'NF {print $1; exit}' "$APP_ROOT/current/RELEASE_SHA256.txt" 2>/dev/null || true)
    [[ "$release_hash" =~ ^[0-9a-f]{64}$ ]] || { logger -p user.crit -t binana-disk-guard 'cannot pause entries: release hash unavailable' || true; exit 3; }
    declare -A DISK_ENV=()
    secure_env_read "$ENV_FILE" DISK_ENV || { logger -p user.crit -t binana-disk-guard 'cannot pause entries: secure env invalid' || true; exit 3; }
    [[ -n "${DISK_ENV[COMMAND_HMAC_KEY]:-}" ]] || { logger -p user.crit -t binana-disk-guard 'cannot pause entries: command key unavailable' || true; exit 3; }
    PYTHONPATH="$APP_ROOT/current" ENVELOPE_RELEASE_HASH="$release_hash" \
      COMMAND_HMAC_KEY="${DISK_ENV[COMMAND_HMAC_KEY]}" python3 - "$PERSIST/commands/inbox" <<'PY'
import json
import os
import pathlib
import sys
import tempfile
import time
import uuid
from services.common.envelope import BUS_COMMAND, sign_envelope

inbox = pathlib.Path(sys.argv[1])
inbox.mkdir(parents=True, exist_ok=True)
command_id = uuid.uuid4().hex
payload = {
    "command_id": command_id,
    "command": "entries",
    "args": {"enabled": False},
    "created_at": time.time(),
}
envelope = sign_envelope(
    producer="deploy-installer", purpose=BUS_COMMAND,
    payload=payload, ttl_seconds=120,
)
descriptor, temporary = tempfile.mkstemp(prefix=f".{command_id}.", suffix=".tmp", dir=inbox)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(envelope, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, inbox / f"{command_id}.json")
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
    logger -p user.crit -t binana-disk-guard 'critical disk pressure: authenticated pause-new-entries command queued' || true
  fi
fi
python3 - "$PERSIST/runtime/disk_status.json" "$status" "$used" "$WARNING_PERCENT" "$CRITICAL_PERCENT" <<'PY' || exit_code=3
import json, os, pathlib, sys, tempfile
from datetime import datetime, timezone
path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
payload = {"status": sys.argv[2], "used_percent": int(sys.argv[3]),
           "warning_percent": int(sys.argv[4]), "critical_percent": int(sys.argv[5]),
           "generated_at": datetime.now(timezone.utc).isoformat()}
fd, temporary = tempfile.mkstemp(prefix=".disk-status.", dir=path.parent)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
os.replace(temporary, path)
PY
logger -p "user.$([[ $severity == CRITICAL ]] && echo crit || echo "${severity,,}")" -t binana-disk-guard "status=$status used_percent=$used" || true
exit "$exit_code"
