#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=deploy/instance_identity.sh
source "$SCRIPT_DIR/instance_identity.sh"
[[ $# -eq 1 ]] || { echo 'usage: restore_validate.sh BACKUP_DIRECTORY' >&2; exit 1; }
readonly BACKUP_ROOT=/var/backups/binana-testnet
candidate=$1
[[ -d "$candidate" && ! -L "$candidate" ]] || { echo 'ERROR: backup must be a non-symlink directory' >&2; exit 1; }
root=$(readlink -f -- "$BACKUP_ROOT")
resolved=$(readlink -f -- "$candidate")
[[ "$(dirname -- "$resolved")" == "$root" ]] || { echo 'ERROR: backup is outside the approved root' >&2; exit 1; }
[[ "$(basename -- "$resolved")" =~ ^20[0-9]{6}T[0-9]{6}Z$ ]] || { echo 'ERROR: backup directory name is invalid' >&2; exit 1; }
[[ -f "$resolved/SHA256SUMS" ]] || { echo 'ERROR: SHA256SUMS missing' >&2; exit 1; }
unsafe=$(find "$resolved" -xdev ! -type f ! -type d -print -quit)
[[ -z "$unsafe" ]] || { echo "ERROR: backup contains a link or special file: $unsafe" >&2; exit 1; }
python3 - "$resolved" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
for raw in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
    parts = raw.split(maxsplit=1)
    if len(parts) != 2:
        raise SystemExit("invalid SHA256SUMS line")
    relative = parts[1].lstrip(" *")
    candidate = (root / relative).resolve()
    if root not in candidate.parents or candidate.is_symlink():
        raise SystemExit(f"unsafe SHA256SUMS path: {relative}")
PY
(cd "$resolved" && sha256sum -c SHA256SUMS)
while IFS= read -r -d '' database; do
  python3 - "$database" <<'PY'
import pathlib
import sqlite3
import sys

database = pathlib.Path(sys.argv[1])
with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=10) as connection:
    result = connection.execute("PRAGMA integrity_check").fetchone()
if result != ("ok",):
    raise SystemExit(f"invalid SQLite backup: {database}")
PY
done < <(find "$resolved/sqlite" -type f -print0 2>/dev/null)
echo 'restore_validation=PASS; no live files were modified'
