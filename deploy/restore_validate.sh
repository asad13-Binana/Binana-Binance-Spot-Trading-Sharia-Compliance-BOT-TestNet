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
python3 "$SCRIPT_DIR/../scripts/backup_integrity.py" validate "$resolved" "$INSTANCE_MODE"

echo 'restore_validation=PASS; integrity and release identity checked; approved artifact comparison and restore drill still required; no live files were modified'
