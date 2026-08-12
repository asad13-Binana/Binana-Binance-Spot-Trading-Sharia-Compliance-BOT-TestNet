#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'ERROR: backup_state.sh requires root' >&2; exit 1; }
PERSIST=${PERSIST:-/var/lib/binana-freqtrade-v101/shared}
APP_ROOT=${APP_ROOT:-/opt/binana-freqtrade-v101}
BACKUP_ROOT=${BACKUP_ROOT:-/var/backups/binana-freqtrade-v101}
BACKUP_RETAIN=${BACKUP_RETAIN:-14}
[[ "$PERSIST" == /var/lib/binana-freqtrade-v101/shared ]] || { echo 'ERROR: PERSIST must remain fixed' >&2; exit 1; }
[[ "$APP_ROOT" == /opt/binana-freqtrade-v101 ]] || { echo 'ERROR: APP_ROOT must remain fixed' >&2; exit 1; }
[[ "$BACKUP_ROOT" == /var/backups/binana-freqtrade-v101 ]] || { echo 'ERROR: BACKUP_ROOT must remain fixed' >&2; exit 1; }
[[ "$BACKUP_RETAIN" =~ ^[0-9]+$ ]] && (( BACKUP_RETAIN >= 2 && BACKUP_RETAIN <= 90 )) || { echo 'ERROR: BACKUP_RETAIN must be 2..90' >&2; exit 1; }
[[ -d "$PERSIST" && ! -L "$PERSIST" ]] || { echo 'ERROR: persistent root unavailable' >&2; exit 1; }
[[ ! -L "$BACKUP_ROOT" ]] || { echo 'ERROR: backup root must not be a symlink' >&2; exit 1; }
install -d -m 0700 -o root -g root "$BACKUP_ROOT"
exec 9>"$BACKUP_ROOT/.backup.lock"
flock -n 9 || { echo 'ERROR: another backup is running' >&2; exit 1; }
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
  python3 - "$database" "$target" <<'PY'
import pathlib
import sqlite3
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=10) as source_db:
    with sqlite3.connect(target, timeout=10) as target_db:
        source_db.backup(target_db)
with sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True, timeout=10) as check_db:
    result = check_db.execute("PRAGMA integrity_check").fetchone()
if result != ("ok",):
    raise SystemExit(f"SQLite backup failed integrity check: {source}")
PY
done < <(find "$PERSIST" -xdev -type f \( -name '*.sqlite' -o -name '*.db' \) -print0)
for metadata in RELEASE_VERSION RELEASE_MODE RELEASE_SHA256.txt RELEASE_MANIFEST.json .git-commit; do
  [[ -f "$APP_ROOT/current/$metadata" ]] && install -m 0600 "$APP_ROOT/current/$metadata" "$stage/release/$metadata"
done
(cd "$stage" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS)
destination="$BACKUP_ROOT/$stamp"
mv "$stage" "$destination"
trap - EXIT
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20????????T??????Z' -printf '%T@ %p\n' | \
  sort -nr | awk -v keep="$BACKUP_RETAIN" 'NR>keep {$1=""; sub(/^ /,""); print}' | while IFS= read -r old; do
    [[ -n "$old" && "$(dirname -- "$(readlink -f -- "$old")")" == "$(readlink -f -- "$BACKUP_ROOT")" ]] && rm -rf -- "$old"
  done
echo "backup_created=$destination (secrets excluded)"
