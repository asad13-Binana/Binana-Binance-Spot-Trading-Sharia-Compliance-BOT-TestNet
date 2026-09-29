#!/usr/bin/env bash
set -euo pipefail
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=deploy/instance_identity.sh
source "$SCRIPT_DIR/instance_identity.sh"
[[ $EUID -eq 0 ]] || { echo 'ERROR: backup_state.sh requires root' >&2; exit 1; }
readonly BACKUP_ROOT=/var/backups/binana-testnet
BACKUP_RETAIN=${BACKUP_RETAIN:-14}
[[ "$BACKUP_RETAIN" =~ ^[0-9]+$ ]] && (( BACKUP_RETAIN >= 2 && BACKUP_RETAIN <= 90 )) || { echo 'ERROR: BACKUP_RETAIN must be 2..90' >&2; exit 1; }
[[ -d "$PERSIST" && ! -L "$PERSIST" ]] || { echo 'ERROR: persistent root unavailable' >&2; exit 1; }
[[ ! -L "$BACKUP_ROOT" ]] || { echo 'ERROR: backup root must not be a symlink' >&2; exit 1; }
install -d -m 0700 -o root -g root "$BACKUP_ROOT"
exec 9>"$BACKUP_ROOT/.backup.lock"
flock -n 9 || { echo 'ERROR: another backup is running' >&2; exit 1; }
python3 "$SCRIPT_DIR/../scripts/backup_integrity.py" release "$APP_ROOT/current" "$INSTANCE_MODE" >/dev/null
stamp=$(date -u +%Y%m%dT%H%M%SZ)
stage=$(mktemp -d "$BACKUP_ROOT/.stage.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
install -d -m 0700 "$stage/state" "$stage/sqlite" "$stage/release"
rsync -a --no-links --no-devices --no-specials \
  --exclude='*.sqlite' --exclude='*.sqlite-*' --exclude='*.db' --exclude='*.db-*' \
  "$PERSIST/" "$stage/state/"
while IFS= read -r -d '' database; do
  relative=${database#"$PERSIST/"}
  target="$stage/sqlite/$relative"
  install -d -m 0700 "$(dirname -- "$target")"
  python3 "$SCRIPT_DIR/../scripts/backup_integrity.py" snapshot "$database" "$target"
done < <(find "$PERSIST" -xdev \
  \( -path "$PERSIST/runtime/db_backups" -o \
     -path "$PERSIST/runtime/db_backups/*" \) -prune -o \
  -type f \( -name '*.sqlite' -o -name '*.db' \) -print0)
for metadata in RELEASE_VERSION RELEASE_MODE RELEASE_SHA256.txt RELEASE_MANIFEST.json .git-commit; do
  install -m 0600 "$APP_ROOT/current/$metadata" "$stage/release/$metadata"
done
(cd "$stage" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS)
destination="$BACKUP_ROOT/$stamp"
[[ ! -e "$destination" ]] || { echo 'ERROR: timestamped backup already exists' >&2; exit 1; }
python3 "$SCRIPT_DIR/../scripts/backup_integrity.py" validate "$stage" "$INSTANCE_MODE" >/dev/null
mv -T "$stage" "$destination"
trap - EXIT
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20??????T??????Z' -printf '%p\n' | \
  sort -r | awk -v keep="$BACKUP_RETAIN" 'NR>keep {print}' | while IFS= read -r old; do
    [[ -n "$old" && "$(dirname -- "$(readlink -f -- "$old")")" == "$(readlink -f -- "$BACKUP_ROOT")" ]] && rm -rf -- "$old"
  done
echo "backup_created=$destination (secrets excluded)"
