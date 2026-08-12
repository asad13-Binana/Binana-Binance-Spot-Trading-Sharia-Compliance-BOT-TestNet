#!/usr/bin/env bash
set -euo pipefail
fail(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail 'approval must run as root'
[[ $# -eq 1 && "$1" =~ ^[0-9a-f]{64}$ ]] || fail 'usage: binana-approve-release SHA256'
APPROVED_FILE=${BINANA_APPROVED_RELEASE_FILE:-/etc/binana-freqtrade-v101/approved-release.sha256}
[[ "$APPROVED_FILE" == /etc/binana-freqtrade-v101/approved-release.sha256 ]] || fail 'approval file must remain fixed'
install -d -m 0700 -o root -g root "$(dirname -- "$APPROVED_FILE")"
temporary=$(mktemp "$(dirname -- "$APPROVED_FILE")/.approved.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
printf '%s\n' "$1" >"$temporary"
chown root:root "$temporary"
chmod 0600 "$temporary"
mv -fT "$temporary" "$APPROVED_FILE"
trap - EXIT
printf 'Approved immutable release digest %s\n' "$1"
