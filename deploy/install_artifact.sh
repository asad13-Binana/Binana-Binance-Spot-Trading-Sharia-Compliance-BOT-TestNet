#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=deploy/instance_identity.sh
source "$SCRIPT_DIR/instance_identity.sh"
# shellcheck source=deploy/lib/secure_env.sh
source "$SCRIPT_DIR/lib/secure_env.sh"
ARTIFACT=${1:?usage: install_artifact.sh RELEASE.tar.gz RELEASE.tar.gz.sha256}
CHECKSUM=${2:?usage: install_artifact.sh RELEASE.tar.gz RELEASE.tar.gz.sha256}
readonly RELEASES=$APP_ROOT/releases
readonly CURRENT=$APP_ROOT/current
readonly ENV_FILE=$PRIVATE_ROOT/.env
KEEP_RELEASES=${KEEP_RELEASES:-3}
MIN_PHYSICAL_MEMORY_MIB=${MIN_PHYSICAL_MEMORY_MIB:-11264}
MIN_TOTAL_MEMORY_MIB=${MIN_TOTAL_MEMORY_MIB:-14336}
MIN_CPU_COUNT=${MIN_CPU_COUNT:-2}
MIN_DEPLOY_FREE_DISK_GIB=${MIN_DEPLOY_FREE_DISK_GIB:-5}
# H-004: one fixed Compose project so overlapping installs cannot start a
# second parallel stack against the same account.
export COMPOSE_PROJECT_NAME
REQUIRED_SERVICES=(
  universe sharia-screener freqtrade execution-sidecar telegram-broker
)

fail(){ echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail 'install_artifact.sh must run through the root-owned binana-testnet-deploy wrapper'
[[ "$APP_ROOT" == /opt/binana-testnet ]] || fail 'APP_ROOT identity mismatch'
[[ "$RELEASES" == /opt/binana-testnet/releases ]] || fail 'RELEASES identity mismatch'
[[ "$CURRENT" == /opt/binana-testnet/current ]] || fail 'CURRENT identity mismatch'
[[ "$PERSIST" == /var/lib/binana-testnet/shared ]] || fail 'PERSIST identity mismatch'
[[ "$ENV_FILE" == /etc/binana-testnet/.env ]] || fail 'ENV_FILE identity mismatch'
[[ "$KEEP_RELEASES" =~ ^[0-9]+$ ]] && (( KEEP_RELEASES >= 2 && KEEP_RELEASES <= 10 )) || fail 'KEEP_RELEASES must be an integer from 2 through 10'
[[ ! -e "$CURRENT" || -L "$CURRENT" ]] || fail 'current must be a release symlink, not a file or directory'
if [[ -L "$CURRENT" ]]; then
  resolved_current=$(readlink -f "$CURRENT") || fail 'current release link cannot be resolved'
  [[ -d "$resolved_current" && "$(dirname "$resolved_current")" == "$RELEASES" ]] || fail 'current release link is stale or outside releases; review recovery before deployment'
fi
[[ "$MIN_PHYSICAL_MEMORY_MIB" =~ ^[0-9]+$ && "$MIN_TOTAL_MEMORY_MIB" =~ ^[0-9]+$ ]] || fail 'memory limits must be integers'
[[ "$MIN_CPU_COUNT" =~ ^[0-9]+$ ]] && (( MIN_CPU_COUNT >= 1 )) || fail 'MIN_CPU_COUNT must be a positive integer'
[[ "$MIN_DEPLOY_FREE_DISK_GIB" =~ ^[0-9]+$ ]] && (( MIN_DEPLOY_FREE_DISK_GIB >= 2 && MIN_DEPLOY_FREE_DISK_GIB <= 20 )) || fail 'MIN_DEPLOY_FREE_DISK_GIB must be 2..20'
for protected_path in "$APP_ROOT" "$RELEASES" "$PERSIST" "$(dirname -- "$ENV_FILE")"; do
  [[ ! -L "$protected_path" ]] || fail "privileged path must not be a symlink: $protected_path"
done

# H-004: acquire an exclusive host lock before any mutation. A concurrent or
# manual install running during an Actions deploy exits immediately instead of
# racing and creating a second execution sidecar.
readonly LOCK_FILE=$INSTALL_LOCK
mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
exec 9>"$LOCK_FILE" || fail "cannot open deploy lock $LOCK_FILE"
flock -n 9 || fail "another install holds $LOCK_FILE; refusing to run a second deploy"

compose_for(){
  local release_dir=$1 release_tag=$2
  shift 2
  RELEASE_TAG="$release_tag" docker compose --project-name "$COMPOSE_PROJECT_NAME" \
    --env-file "$ENV_FILE" -f "$release_dir/docker-compose.yml" "$@"
}

as_root(){
  if [[ $EUID -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

install_monitoring_for(){
  local release_dir=$1 mode=$2 release_hash=$3
  as_root "$release_dir/deploy/install_monitoring.sh" \
    "$release_dir" "$mode" "$release_hash"
}

disable_monitoring_after_failed_first_install(){
  # No previous release exists to reinstall. Stop every release-specific unit,
  # verify none remains active, and remove only the monitoring-current link.
  # Root-owned unit files and immutable venvs may remain inert for diagnosis.
  local unit
  local units=(
    ${SYSTEMD_PREFIX}-monitor-testnet.service
    ${SYSTEMD_PREFIX}-monitor-report-testnet.timer
    ${SYSTEMD_PREFIX}-monitor-snapshot.timer
    ${SYSTEMD_PREFIX}-monitor-snapshot.service
    ${SYSTEMD_PREFIX}-monitor-report-${INSTANCE_MODE}.service
    ${SYSTEMD_PREFIX}-disk-guard.timer ${SYSTEMD_PREFIX}-disk-guard.service
    ${SYSTEMD_PREFIX}-state-backup.timer ${SYSTEMD_PREFIX}-state-backup.service
    ${SYSTEMD_PREFIX}-offhost-backup.timer ${SYSTEMD_PREFIX}-offhost-backup.service
    ${SYSTEMD_PREFIX}-api-readiness.timer ${SYSTEMD_PREFIX}-api-readiness.service
  )
  for unit in "${units[@]}"; do
    as_root systemctl disable --now "$unit" >/dev/null 2>&1 || true
  done
  as_root systemctl daemon-reload || return 1
  for unit in "${units[@]}"; do
    systemctl is-active --quiet "$unit" && return 1
  done
  as_root rm -f "$APP_ROOT/monitoring-current"
}

[[ -f "$ARTIFACT" && -f "$CHECKSUM" ]] || fail 'artifact/checksum missing'
declare -A DEPLOY_ENV=()
secure_env_read "$ENV_FILE" DEPLOY_ENV || exit 1
declare -A ALLOWED_ENV=()
allowed_keys=$(
  python3 - "$SCRIPT_DIR/../docker-compose.yml" <<'PY'
import pathlib, re, sys
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
names = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", text))
names.update({
    "BOT_NAMESPACE", "MIN_PHYSICAL_MEMORY_MIB", "MIN_TOTAL_MEMORY_MIB",
    "MIN_CPU_COUNT", "DEPLOYMENT_PROFILE",
    "LEGACY_HALAL_FILE", "LEGACY_RUNTIME_DIR", "SHARIA_EVIDENCE_DIR",
    "SHARIA_FILE", "SHARIA_SOURCE_REGISTRY", "SIGNAL_INBOX", "UNIVERSE_FILE",
})
print("\n".join(sorted(names)))
PY
)
while IFS= read -r key; do ALLOWED_ENV["$key"]=1; done <<<"$allowed_keys"
for key in "${!DEPLOY_ENV[@]}"; do
  [[ -v "ALLOWED_ENV[$key]" ]] || fail "unsupported environment key: $key"
  export "$key=${DEPLOY_ENV[$key]}"
done
unset key ALLOWED_ENV
for required in BOT_PRODUCT BOT_ENVIRONMENT BOT_INSTANCE_ID BOT_UID BOT_GID SHARED_HOST_PATH EXECUTION_MODE BINANCE_TESTNET; do
  secure_env_require DEPLOY_ENV "$required" || exit 1
done
BOT_PRODUCT=${DEPLOY_ENV[BOT_PRODUCT]}
BOT_ENVIRONMENT=${DEPLOY_ENV[BOT_ENVIRONMENT]}
BOT_INSTANCE_ID=${DEPLOY_ENV[BOT_INSTANCE_ID]}
BOT_UID=${DEPLOY_ENV[BOT_UID]}
BOT_GID=${DEPLOY_ENV[BOT_GID]}
EXECUTION_MODE=${DEPLOY_ENV[EXECUTION_MODE]}
BINANCE_TESTNET=${DEPLOY_ENV[BINANCE_TESTNET]}
SHARED_HOST_PATH=${DEPLOY_ENV[SHARED_HOST_PATH]}
SHARIA_SIGNAL_GATE_MODE=${DEPLOY_ENV[SHARIA_SIGNAL_GATE_MODE]:-cached}
TELEGRAM_BOT_TOKEN=${DEPLOY_ENV[TELEGRAM_BOT_TOKEN]:-}
TELEGRAM_OWNER_CHAT_ID=${DEPLOY_ENV[TELEGRAM_OWNER_CHAT_ID]:-}
[[ "$BOT_PRODUCT" == BINANA ]] || fail 'BOT_PRODUCT must be BINANA'
[[ "$BOT_INSTANCE_ID" =~ ^BINANA-TN-[A-Z0-9][A-Z0-9-]{2,47}$ ]] || fail 'BOT_INSTANCE_ID must use the BINANA-TN namespace'
[[ "$BOT_UID" =~ ^[0-9]+$ && "$BOT_GID" =~ ^[0-9]+$ ]] || fail 'BOT_UID and BOT_GID must be numeric'
expected_bot_uid=$(id -u "$BOT_USER" 2>/dev/null) || fail "$BOT_USER account is missing"
expected_bot_gid=$(getent group "$BOT_USER" | cut -d: -f3)
[[ "$BOT_UID" == "$expected_bot_uid" && "$BOT_GID" == "$expected_bot_gid" ]] || \
  fail "BOT_UID and BOT_GID must match the dedicated $BOT_USER account"
install -d -m 0755 -o root -g root "$RELEASES"
install -d -m 0750 -o "$BOT_UID" -g "$BOT_GID" \
  "$PERSIST" "$PERSIST/commands/inbox" "$PERSIST/runtime" \
  "$PERSIST/sharia" "$PERSIST/sharia/evidence" \
  "$PERSIST/sharia/discovery/current" "$PERSIST/sharia/discovery/archive" \
  "$PERSIST/sharia_decisions/inbox" "$PERSIST/sharia_decisions/processed" \
  "$PERSIST/legacy_runtime" \
  "$PERSIST/universe" "$PERSIST/signals/inbox" "$PERSIST/signals/processed" \
  "$PERSIST/signals/rejected" "$PERSIST/audit" \
  "$PERSIST/freqtrade" "$PERSIST/freqtrade/logs"
[[ ! -L "$RELEASES" && ! -L "$PERSIST" ]] || fail 'release and persistent roots must not be symlinks'
[[ $(stat -Lc '%u' "$RELEASES") == 0 ]] || fail "$RELEASES must be root-owned"
release_mode=$(stat -Lc '%a' "$RELEASES")
(( (8#$release_mode & 0022) == 0 )) || fail "$RELEASES must not be group/world writable"
[[ $(stat -c '%u' "$PERSIST") == "$BOT_UID" ]] || fail "$PERSIST owner UID must match BOT_UID=$BOT_UID"
[[ $(stat -c '%g' "$PERSIST") == "$BOT_GID" ]] || fail "$PERSIST group GID must match BOT_GID=$BOT_GID"
[[ "$SHARED_HOST_PATH" == "$PERSIST" ]] || fail "SHARED_HOST_PATH in $ENV_FILE must equal $PERSIST"

# Require the declared capacity for the shared four-bot Oracle host. A 1 GiB
# E2 micro and the former 1-OCPU/6-GB topology are unsupported.
source "$SCRIPT_DIR/lib/capacity_profile.sh"
apply_capacity_profile || fail 'capacity profile rejected'
physical_mib=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
swap_mib=$(awk '/SwapTotal/{print int($2/1024)}' /proc/meminfo)
total_mib=$((physical_mib + swap_mib))
cpu_count=$(nproc)
free_kib=$(df -Pk / | awk 'NR==2 {print $4}')
(( physical_mib >= MIN_PHYSICAL_MEMORY_MIB )) || fail \
  "physical memory ${physical_mib} MiB is below required ${MIN_PHYSICAL_MEMORY_MIB} MiB"
(( total_mib >= MIN_TOTAL_MEMORY_MIB )) || fail \
  "RAM+swap ${total_mib} MiB is below required ${MIN_TOTAL_MEMORY_MIB} MiB"
(( cpu_count >= MIN_CPU_COUNT )) || fail \
  "CPU count ${cpu_count} is below required ${MIN_CPU_COUNT} for the four-bot host"
(( free_kib >= MIN_DEPLOY_FREE_DISK_GIB * 1024 * 1024 )) || fail \
  "root filesystem needs at least ${MIN_DEPLOY_FREE_DISK_GIB} GiB free before deployment"

# Verify only the supplied artifact hash; never allow a checksum file to name
# an arbitrary path. Reject links, special files, multiple roots, and traversal
# before extraction.
EXPECTED=$(awk 'NF{print $1;exit}' "$CHECKSUM")
[[ "$EXPECTED" =~ ^[0-9a-fA-F]{64}$ ]] || fail 'invalid checksum file'
ACTUAL=$(sha256sum "$ARTIFACT" | awk '{print $1}')
[[ "${ACTUAL,,}" == "${EXPECTED,,}" ]] || fail 'artifact checksum mismatch'
python3 - "$ARTIFACT" <<'PY'
import pathlib, sys, tarfile
p=sys.argv[1]
roots=set()
with tarfile.open(p,'r:*') as archive:
    members=archive.getmembers()
    if not members:
        raise SystemExit('empty archive')
    for member in members:
        name=member.name
        pure=pathlib.PurePosixPath(name)
        if pure.is_absolute() or '..' in pure.parts:
            raise SystemExit('unsafe archive member: '+name)
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise SystemExit('unsupported archive member type: '+name)
        if pure.parts and pure.parts[0] not in ('.', ''):
            roots.add(pure.parts[0])
if len(roots) != 1:
    raise SystemExit('archive must contain exactly one top-level release directory')
print('archive structure safe')
PY

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
TMP=$(mktemp -d "$RELEASES/.extract.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
tar -xzf "$ARTIFACT" -C "$TMP"
NEW=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
[[ -n "$NEW" && -f "$NEW/RELEASE_MANIFEST.json" && -f "$NEW/RELEASE_SHA256.txt" \
   && -f "$NEW/RELEASE_MODE" ]] || fail 'invalid release root'
python3 "$NEW/scripts/verify_manifest.py"
python3 "$NEW/scripts/verify_deployment_supply_chain.py"
python3 "$NEW/tests/secret_scan.py"
source "$NEW/deploy/lib/host_python.sh"
prepare_host_python "$NEW" || fail 'locked host validation environment could not be prepared'
RELEASE_HASH=$(awk 'NF{print $1;exit}' "$NEW/RELEASE_SHA256.txt")
[[ "$RELEASE_HASH" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid release hash'
PACKAGE_MODE=$(<"$NEW/RELEASE_MODE")
[[ "$PACKAGE_MODE" == testnet || "$PACKAGE_MODE" == live ]] || fail 'invalid RELEASE_MODE'
if [[ "$PACKAGE_MODE" == testnet && "${EXECUTION_MODE:-simulation}" == live ]]; then
  fail 'testnet package refuses EXECUTION_MODE=live'
fi
if [[ "$PACKAGE_MODE" == live && "${EXECUTION_MODE:-simulation}" == testnet ]]; then
  fail 'live package refuses EXECUTION_MODE=testnet; use simulation until live promotion'
fi
if [[ "${SHARIA_SIGNAL_GATE_MODE:-cached}" != cached ]]; then
  fail 'self-hosted Sharia screening requires SHARIA_SIGNAL_GATE_MODE=cached'
fi
if [[ "$PACKAGE_MODE" == testnet ]]; then
  [[ "$BOT_ENVIRONMENT" == TESTNET ]] || fail 'testnet package requires BOT_ENVIRONMENT=TESTNET'
  [[ "$BINANCE_TESTNET" == true ]] || fail 'testnet package requires BINANCE_TESTNET=true'
  [[ "$EXECUTION_MODE" == testnet || "$EXECUTION_MODE" == simulation ]] || fail 'testnet package execution mode mismatch'
else
  [[ "$BOT_ENVIRONMENT" == LIVE ]] || fail 'live package requires BOT_ENVIRONMENT=LIVE'
  [[ "$EXECUTION_MODE" != testnet ]] || fail 'live package cannot use testnet execution mode'
fi
NEW_TAG="v101-${RELEASE_HASH:0:16}"
printf '%s\n' "$NEW_TAG" > "$NEW/.release-tag"
ln -sfn "$ENV_FILE" "$NEW/.env"

# Validate the owner-maintained manual registry before touching the running
# release. An exact legacy empty compatibility file is safely migrated to the
# reviewed registry shipped in this release. A non-empty or already-manual
# owner file is never overwritten; malformed owner data blocks deployment.
for state_file in sharia_status.json halal_coins.json source_registry.json HALAL_CRYPTO_SPOT_SCREENING_V19_1_PRODUCTION.json; do
  [[ ! -L "$PERSIST/sharia/$state_file" ]] || fail 'Sharia state must not be a symlink'
done
SHARIA_REGISTRY_INSTALL=preserve
if [[ ! -f "$PERSIST/sharia/halal_coins.json" ]]; then
  SHARIA_REGISTRY_INSTALL=release
elif "$HOST_PYTHON" -I - "$PERSIST/sharia/halal_coins.json" <<'PY'
import json,sys
try:
    value=json.load(open(sys.argv[1],encoding='utf-8'))
except Exception:
    raise SystemExit(1)
legacy_empty=(isinstance(value,dict) and 'schema_version' not in value
              and value.get('symbols') == [])
raise SystemExit(0 if legacy_empty else 1)
PY
then
  SHARIA_REGISTRY_INSTALL=release
fi
if [[ "$SHARIA_REGISTRY_INSTALL" == release ]]; then
  MANUAL_REGISTRY_SOURCE="$NEW/shared/sharia/halal_coins.json"
else
  MANUAL_REGISTRY_SOURCE="$PERSIST/sharia/halal_coins.json"
fi
PYTHONPATH="$NEW" "$HOST_PYTHON" -m services.sharia_screener.manual_registry \
  validate "$MANUAL_REGISTRY_SOURCE"
PYTHONPATH="$NEW" "$HOST_PYTHON" -m services.universe_service.validate_sharia \
  "$NEW/shared/sharia/sharia_status.json"

if [[ ! -f "$PERSIST/sharia/source_registry.json" ]]; then
  install -m 0640 -o "$BOT_UID" -g "$BOT_GID" "$NEW/shared/sharia/source_registry.json" \
     "$PERSIST/sharia/source_registry.json"
fi
install -m 0640 -o "$BOT_UID" -g "$BOT_GID" "$NEW/shared/sharia/HALAL_CRYPTO_SPOT_SCREENING_V19_1_PRODUCTION.json" \
   "$PERSIST/sharia/HALAL_CRYPTO_SPOT_SCREENING_V19_1_PRODUCTION.json"
# Repair permissions on non-transactional retained state. The registry and
# status projection are switched together only after the old stack stops.
for state_file in source_registry.json; do
  chown "$BOT_UID:$BOT_GID" "$PERSIST/sharia/$state_file"
  chmod 0640 "$PERSIST/sharia/$state_file"
done
# Verify the seeded controller is byte-identical before anything starts.
PYTHONPATH="$NEW" "$HOST_PYTHON" - "$PERSIST/sharia/HALAL_CRYPTO_SPOT_SCREENING_V19_1_PRODUCTION.json" <<'PY'
import sys
from services.common.sharia_v19 import controller_sha256, V19_CONTROLLER_SHA256
actual = controller_sha256(sys.argv[1])
if actual != V19_CONTROLLER_SHA256:
    raise SystemExit(f'V19.1 controller hash mismatch after seeding: {actual}')
print('V19.1 controller byte-integrity verified')
PY
# Validate and build an immutable service-image tag before touching the running release.
compose_for "$NEW" "$NEW_TAG" config -q
compose_for "$NEW" "$NEW_TAG" build universe freqtrade

OLD=''
OLD_TAG=''
OLD_MODE=''
OLD_RELEASE_HASH=''
if [[ -L "$CURRENT" ]]; then
  OLD=$(readlink -f "$CURRENT" || true)
  if [[ -n "$OLD" && -f "$OLD/.release-tag" ]]; then
    OLD_TAG=$(<"$OLD/.release-tag")
  else
    OLD_TAG=${RELEASE_TAG:-local}
  fi
  if [[ -n "$OLD" && -f "$OLD/RELEASE_MODE" ]]; then
    OLD_MODE=$(<"$OLD/RELEASE_MODE")
  fi
  if [[ -n "$OLD" && -f "$OLD/RELEASE_SHA256.txt" ]]; then
    OLD_RELEASE_HASH=$(awk 'NF{print $1;exit}' "$OLD/RELEASE_SHA256.txt")
  fi
fi

restore_monitoring(){
  [[ ${stop_result:-0} -eq 0 ]] || return 1
  if [[ -n "$OLD" && -d "$OLD" ]]; then
    [[ "$OLD_MODE" == testnet || "$OLD_MODE" == live ]] || {
      echo 'CRITICAL: rollback target has invalid monitoring mode metadata.' >&2
      return 1
    }
    [[ "$OLD_RELEASE_HASH" =~ ^[0-9a-f]{64}$ ]] || {
      echo 'CRITICAL: rollback target has invalid monitoring release hash.' >&2
      return 1
    }
    [[ -x "$OLD/deploy/install_monitoring.sh" ]] || {
      echo 'CRITICAL: rollback target monitoring installer is unavailable.' >&2
      return 1
    }
    install_monitoring_for "$OLD" "$OLD_MODE" "$OLD_RELEASE_HASH"
  else
    disable_monitoring_after_failed_first_install
  fi
}

# Ask the current system to pause and reconcile, and verify both acknowledgements
# before stopping any container. Exchange-native protection remains active.
if [[ -n "$OLD" && -d "$OLD" ]]; then
  [[ "$OLD_RELEASE_HASH" =~ ^[0-9a-f]{64}$ ]] || fail 'current release hash is unavailable; refusing unsigned deployment controls'
  command_ids=$(
    PYTHONPATH="$OLD" ENVELOPE_RELEASE_HASH="$OLD_RELEASE_HASH" \
    COMMAND_HMAC_KEY="${DEPLOY_ENV[COMMAND_HMAC_KEY]:-}" \
    python3 - "$PERSIST" <<'PY'
import json,os,sys,tempfile,time,uuid
from pathlib import Path
from services.common.envelope import BUS_COMMAND, sign_envelope
root=Path(sys.argv[1]); inbox=root/'commands/inbox'; inbox.mkdir(parents=True,exist_ok=True)
def atomic_json(path, payload):
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        os.fchown(fd, int(os.environ['BOT_UID']), int(os.environ['BOT_GID']))
        os.fchmod(fd, 0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as handle:
            json.dump(payload,handle,separators=(',',':'),sort_keys=True)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp,path)
        dfd=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    except BaseException:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise
for command,args in [('entries',{'enabled':False}),('reconcile',{})]:
    cid=uuid.uuid4().hex
    payload = {
        'command_id':cid,'command':command,'args':args,'created_at':time.time()
    }
    atomic_json(inbox/f'{cid}.json', sign_envelope(
        producer='deploy-installer', purpose=BUS_COMMAND,
        payload=payload, ttl_seconds=120))
    print(cid)
PY
  )
  mapfile -t COMMAND_IDS <<<"$command_ids"
  [[ ${#COMMAND_IDS[@]} -eq 2 ]] || fail 'both signed deployment controls must be created'
  for cid in "${COMMAND_IDS[@]}"; do
    [[ "$cid" =~ ^[0-9a-f]{32}$ ]] || fail 'invalid deployment command identifier'
  done
  for cid in "${COMMAND_IDS[@]}"; do
    result="$PERSIST/runtime/command_result_${cid}.json"
    for _ in $(seq 1 30); do
      [[ -s "$result" ]] && break
      sleep 1
    done
    [[ -s "$result" ]] || fail "current sidecar did not acknowledge deployment command $cid"
    python3 - "$result" <<'PY'
import json,sys
payload=json.load(open(sys.argv[1],encoding='utf-8'))
if not payload.get('ok'):
    raise SystemExit('deployment command failed: '+json.dumps(payload,sort_keys=True))
PY
    rm -f "$result"
  done
fi

DEST="$RELEASES/$STAMP"
[[ ! -e "$DEST" ]] || fail 'release timestamp already exists; retry after one second'
mv "$NEW" "$DEST"
trap - EXIT
rm -rf "$TMP"
ln -sfn "$ENV_FILE" "$DEST/.env"

SHARIA_STATE_BACKUP="$PERSIST/runtime/.manual-sharia-deploy-$STAMP"
SHARIA_STATE_BACKED_UP=false

backup_manual_sharia_state(){
  install -d -m 0700 -o root -g root "$SHARIA_STATE_BACKUP"
  local state_file
  for state_file in halal_coins.json sharia_status.json; do
    if [[ -f "$PERSIST/sharia/$state_file" ]]; then
      install -m 0600 -o root -g root "$PERSIST/sharia/$state_file" \
        "$SHARIA_STATE_BACKUP/$state_file"
    else
      : >"$SHARIA_STATE_BACKUP/$state_file.missing"
      chmod 0600 "$SHARIA_STATE_BACKUP/$state_file.missing"
    fi
  done
  SHARIA_STATE_BACKED_UP=true
}

restore_manual_sharia_state(){
  [[ "$SHARIA_STATE_BACKED_UP" == true ]] || return 0
  local state_file
  for state_file in halal_coins.json sharia_status.json; do
    if [[ -f "$SHARIA_STATE_BACKUP/$state_file.missing" ]]; then
      rm -f "$PERSIST/sharia/$state_file"
    else
      install -m 0640 -o "$BOT_UID" -g "$BOT_GID" \
        "$SHARIA_STATE_BACKUP/$state_file" "$PERSIST/sharia/$state_file" || return 1
    fi
  done
  return 0
}

apply_manual_sharia_state(){
  if [[ "$SHARIA_REGISTRY_INSTALL" == release ]]; then
    install -m 0640 -o "$BOT_UID" -g "$BOT_GID" \
      "$DEST/shared/sharia/halal_coins.json" "$PERSIST/sharia/halal_coins.json"
  fi
  PYTHONPATH="$DEST" "$HOST_PYTHON" \
    -m services.sharia_screener.manual_registry bootstrap-status \
    "$PERSIST/sharia/halal_coins.json" "$PERSIST/sharia/sharia_status.json"
  chown "$BOT_UID:$BOT_GID" \
    "$PERSIST/sharia/halal_coins.json" "$PERSIST/sharia/sharia_status.json"
  chmod 0640 "$PERSIST/sharia/halal_coins.json" "$PERSIST/sharia/sharia_status.json"
}

discard_manual_sharia_backup(){
  [[ "$SHARIA_STATE_BACKED_UP" == true ]] || return 0
  rm -f "$SHARIA_STATE_BACKUP/halal_coins.json" \
    "$SHARIA_STATE_BACKUP/sharia_status.json" \
    "$SHARIA_STATE_BACKUP/halal_coins.json.missing" \
    "$SHARIA_STATE_BACKUP/sharia_status.json.missing"
  rmdir "$SHARIA_STATE_BACKUP"
  SHARIA_STATE_BACKED_UP=false
}

rollback(){
  trap - ERR EXIT INT TERM
  set +e
  echo 'New release health check failed; rolling back.' >&2
  compose_for "$DEST" "$NEW_TAG" down --remove-orphans
  stop_result=$?
  rollback_status='ROLLED_BACK'
  if (( stop_result != 0 )); then
    echo 'CRITICAL: new stack could not be stopped; refusing to start another stack.' >&2
    rollback_status='ROLLBACK_STOP_FAILED_CRITICAL'
  elif ! restore_manual_sharia_state; then
    echo 'CRITICAL: prior Sharia state could not be restored; refusing to start another stack.' >&2
    rollback_status='ROLLBACK_SHARIA_RESTORE_FAILED_CRITICAL'
  elif [[ -n "$OLD" && -d "$OLD" ]]; then
    ln -sfn "$OLD" "$CURRENT.new" && mv -Tf "$CURRENT.new" "$CURRENT"
    compose_for "$OLD" "$OLD_TAG" up -d --remove-orphans
    # M-009: verify the recovered old release is actually healthy; a rollback
    # onto an unhealthy previous release is a CRITICAL condition, not success.
    old_ok=false
    for _ in $(seq 1 36); do
      running=$(compose_for "$OLD" "$OLD_TAG" ps --status running --services | wc -l | tr -d ' ')
      all_ok=true
      for service in "${REQUIRED_SERVICES[@]}"; do
        cid=$(compose_for "$OLD" "$OLD_TAG" ps -q "$service" 2>/dev/null || true)
        [[ -n "$cid" ]] || { all_ok=false; break; }
        st=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true)
        [[ "$st" == healthy ]] || { all_ok=false; break; }
      done
      if [[ "$running" -ge "${#REQUIRED_SERVICES[@]}" && "$all_ok" == true ]]; then old_ok=true; break; fi
      sleep 5
    done
    [[ "$old_ok" == true ]] && rollback_status='ROLLED_BACK_OLD_HEALTHY' || rollback_status='ROLLED_BACK_OLD_UNHEALTHY_CRITICAL'
    [[ "$old_ok" == true ]] || echo 'CRITICAL: rollback target did not become healthy; entries stay paused, manual action required.' >&2
  else
    rm -f "$CURRENT"
  fi
  if restore_monitoring; then
    echo 'Monitoring rollback completed.' >&2
  else
    rollback_status="${rollback_status}_MONITORING_UNHEALTHY_CRITICAL"
    echo 'CRITICAL: monitoring rollback failed; all entries remain paused and manual recovery is required.' >&2
  fi
  python3 - "$PERSIST" "$rollback_status" <<'PY'
import json,os,sys,tempfile
from datetime import datetime,timezone
from pathlib import Path
p=Path(sys.argv[1])/'runtime/deployment_status.json'; p.parent.mkdir(parents=True,exist_ok=True)
payload={'ok':False,'status':sys.argv[2],'at':datetime.now(timezone.utc).isoformat()}
fd,tmp=tempfile.mkstemp(prefix='.'+p.name+'.',suffix='.tmp',dir=p.parent)
try:
    os.fchown(fd, 0, p.parent.stat().st_gid)
    os.fchmod(fd, 0o640)
    with os.fdopen(fd,'w',encoding='utf-8') as handle:
        json.dump(payload,handle,indent=2,sort_keys=True); handle.write('\n')
        handle.flush(); os.fsync(handle.fileno())
    os.replace(tmp,p)
    dfd=os.open(p.parent,os.O_RDONLY)
    try: os.fsync(dfd)
    finally: os.close(dfd)
except BaseException:
    try: os.unlink(tmp)
    except FileNotFoundError: pass
    raise
PY
  exit 1
}

# Catch partial startup, monitoring and status-write failures after switching.
trap 'rollback' ERR
trap 'rollback' INT TERM

if [[ -n "$OLD" && -d "$OLD" ]]; then
  compose_for "$OLD" "$OLD_TAG" down --remove-orphans
fi
backup_manual_sharia_state
apply_manual_sharia_state
ln -sfn "$DEST" "$CURRENT.new" && mv -Tf "$CURRENT.new" "$CURRENT"
rm -f "$PERSIST/runtime/sidecar_health.json" \
      "$PERSIST/runtime/telegram_health.json" \
      "$PERSIST/universe/status.json"
compose_for "$DEST" "$NEW_TAG" up -d --remove-orphans

healthy=false
for _ in $(seq 1 48); do
  running=$(compose_for "$DEST" "$NEW_TAG" ps --status running --services | wc -l | tr -d ' ')
  all_healthy=true
  for service in "${REQUIRED_SERVICES[@]}"; do
    cid=$(compose_for "$DEST" "$NEW_TAG" ps -q "$service" 2>/dev/null || true)
    [[ -n "$cid" ]] || { all_healthy=false; break; }
    status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true)
    [[ "$status" == healthy ]] || { all_healthy=false; break; }
  done
  if [[ "$running" -ge "${#REQUIRED_SERVICES[@]}" && "$all_healthy" == true ]]; then
    healthy=true
    break
  fi
  sleep 5
done
[[ "$healthy" == true ]] || rollback

# Monitoring activation is part of the same release transaction. Do not write
# DEPLOYED/release-validation success until root-owned units, helper, venv link,
# and enabled-state transitions all complete. A partial failure invokes the
# application rollback and then reinstalls the previous release's monitoring
# state (or disables all monitoring units on a failed first install).
install_monitoring_for "$DEST" "$PACKAGE_MODE" "$RELEASE_HASH" || rollback

python3 - "$PERSIST" "$RELEASE_HASH" "$DEST" "$NEW_TAG" <<'PY'
import json,os,sys,tempfile
from datetime import datetime,timezone
from pathlib import Path
root=Path(sys.argv[1]); release_hash=sys.argv[2]; dest=sys.argv[3]; tag=sys.argv[4]
p=root/'runtime/deployment_status.json'; p.parent.mkdir(parents=True,exist_ok=True)
payload={
 'ok':True,'status':'DEPLOYED','release_hash':release_hash,'image_tag':tag,
 'release_path':dest,'at':datetime.now(timezone.utc).isoformat()
}
fd,tmp=tempfile.mkstemp(prefix='.'+p.name+'.',suffix='.tmp',dir=p.parent)
try:
    os.fchown(fd, 0, p.parent.stat().st_gid)
    os.fchmod(fd, 0o640)
    with os.fdopen(fd,'w',encoding='utf-8') as handle:
        json.dump(payload,handle,indent=2,sort_keys=True); handle.write('\n')
        handle.flush(); os.fsync(handle.fileno())
    os.replace(tmp,p)
    dfd=os.open(p.parent,os.O_RDONLY)
    try: os.fsync(dfd)
    finally: os.close(dfd)
except BaseException:
    try: os.unlink(tmp)
    except FileNotFoundError: pass
    raise
PY

# Record the exact on-host release gates exposed by the read-only monitor.
python3 - "$PERSIST" "$RELEASE_HASH" "$PACKAGE_MODE" <<'PY'
import json,os,sys,tempfile
from datetime import datetime,timezone
from pathlib import Path
root=Path(sys.argv[1]); release_hash=sys.argv[2]; mode=sys.argv[3]
p=root/'runtime/release_validation.json'; p.parent.mkdir(parents=True,exist_ok=True)
payload={
 'release_hash':release_hash,'package_mode':mode,
 'manifest_verification':'passed','secret_scan':'passed',
 'compose_config':'passed','container_health_gate':'passed',
 'validated_at':datetime.now(timezone.utc).isoformat(),
}
fd,tmp=tempfile.mkstemp(prefix='.'+p.name+'.',suffix='.tmp',dir=p.parent)
try:
    os.fchown(fd, 0, p.parent.stat().st_gid)
    os.fchmod(fd, 0o640)
    with os.fdopen(fd,'w',encoding='utf-8') as handle:
        json.dump(payload,handle,indent=2,sort_keys=True); handle.write('\n')
        handle.flush(); os.fsync(handle.fileno())
    os.replace(tmp,p)
except BaseException:
    try: os.unlink(tmp)
    except FileNotFoundError: pass
    raise
PY

# Optional Telegram deployment notification without printing secrets.
discard_manual_sharia_backup
trap - ERR INT TERM
if [[ "$TELEGRAM_BOT_TOKEN" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ \
   && "$TELEGRAM_OWNER_CHAT_ID" =~ ^-?[0-9]+$ ]]; then
  notification="[BINANA | ${BOT_ENVIRONMENT} | ${BOT_INSTANCE_ID}] $(cat "$DEST/RELEASE_VERSION" 2>/dev/null || echo release) deployment succeeded: ${RELEASE_HASH}"
  # Token and owner ID are delivered through curl's standard-input config,
  # never as process-list-visible command-line arguments.
  curl --config - --data-urlencode "text=$notification" >/dev/null 2>&1 <<EOF || true
url = "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"
request = "POST"
data = "chat_id=${TELEGRAM_OWNER_CHAT_ID}"
max-time = 15
fail
silent
show-error
EOF
fi

# Keep only the newest N release directories and their immutable service images.
ACTIVE=$(readlink -f "$CURRENT")
find "$RELEASES" -mindepth 1 -maxdepth 1 -type d -name '20??????T??????Z' -printf '%T@ %p\n' | sort -nr | \
  awk -v keep="$KEEP_RELEASES" 'NR>keep{$1="";sub(/^ /,"");print}' | while read -r old; do
    if [[ -n "$old" && "$old" != "$ACTIVE" && "$old" != "$OLD" ]]; then
      tag=''
      [[ -f "$old/.release-tag" ]] && tag=$(<"$old/.release-tag")
      rm -rf "$old"
      # A retry can reuse the same immutable image. Never untag the active or
      # retained rollback image while pruning a failed directory.
      [[ -n "$tag" && "$tag" != "$NEW_TAG" && "$tag" != "$OLD_TAG" ]] && \
        docker image rm "$SERVICE_IMAGE:$tag" >/dev/null 2>&1 || true
    fi
  done

echo "Deployed $RELEASE_HASH ($NEW_TAG)"
