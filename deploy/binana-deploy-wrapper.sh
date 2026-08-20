#!/usr/bin/env bash
set -euo pipefail

fail(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail 'the deployment wrapper must run as root'
[[ $# -eq 2 ]] || fail 'usage: binana-testnet-deploy RELEASE.tar.gz RELEASE.tar.gz.sha256'

ARTIFACT=$1
CHECKSUM=$2
INBOX=${BINANA_DEPLOY_INBOX:-/var/lib/binana-testnet/deploy-inbox}
APPROVED_FILE=${BINANA_APPROVED_RELEASE_FILE:-/etc/binana-testnet/approved-release.sha256}
DEPLOY_DEFAULTS=/etc/default/binana-testnet-deploy
[[ "$INBOX" == /var/lib/binana-testnet/deploy-inbox ]] || fail 'deployment inbox must remain fixed'
[[ "$APPROVED_FILE" == /etc/binana-testnet/approved-release.sha256 ]] || fail 'approval file must remain fixed'
[[ -e "$DEPLOY_DEFAULTS" && ! -L "$DEPLOY_DEFAULTS" && -f "$DEPLOY_DEFAULTS" ]] || fail 'deployment identity file is missing'
[[ $(stat -Lc '%u:%g:%a' -- "$DEPLOY_DEFAULTS") == '0:0:644' ]] || fail 'deployment identity file must be root:root 0644'
DEPLOY_UID=$(awk -F= '$1 == "BINANA_DEPLOY_UID" && $2 ~ /^[0-9]+$/ {print $2; exit}' "$DEPLOY_DEFAULTS")
[[ "$DEPLOY_UID" =~ ^[0-9]+$ ]] || fail 'deployment identity file is invalid'

canonical_inbox=$(readlink -f -- "$INBOX")
[[ -d "$canonical_inbox" ]] || fail "deployment inbox is missing: $INBOX"
for candidate in "$ARTIFACT" "$CHECKSUM"; do
  [[ -e "$candidate" && ! -L "$candidate" && -f "$candidate" ]] || fail "input must be a regular non-symlink file: $candidate"
  canonical=$(readlink -f -- "$candidate")
  [[ "$(dirname -- "$canonical")" == "$canonical_inbox" ]] || fail "input must be directly inside $canonical_inbox"
  [[ $(stat -Lc '%u' -- "$candidate") == "$DEPLOY_UID" ]] || fail "input owner is not the deployment account: $candidate"
  mode=$(stat -Lc '%a' -- "$candidate")
  (( (8#$mode & 0022) == 0 )) || fail "input must not be group/world writable: $candidate"
done

[[ "$(basename -- "$ARTIFACT")" =~ ^binance-bot-(testnet|live-trading)-[0-9a-f]{40}\.tar\.gz$ ]] || fail 'unexpected artifact filename'
[[ "$(basename -- "$CHECKSUM")" == "$(basename -- "$ARTIFACT").sha256" ]] || fail 'checksum filename does not match artifact'
expected=$(awk 'NF {print $1; exit}' "$CHECKSUM")
[[ "$expected" =~ ^[0-9a-f]{64}$ ]] || fail 'checksum file does not contain one SHA-256 digest'
actual=$(sha256sum -- "$ARTIFACT" | awk '{print $1}')
[[ "$actual" == "$expected" ]] || fail 'artifact checksum mismatch'

[[ -e "$APPROVED_FILE" && ! -L "$APPROVED_FILE" && -f "$APPROVED_FILE" ]] || fail "owner approval is missing: $APPROVED_FILE"
[[ $(stat -Lc '%u:%g:%a' -- "$APPROVED_FILE") == '0:0:600' ]] || fail "$APPROVED_FILE must be root:root 0600"
approved=$(awk 'NF {print $1; exit}' "$APPROVED_FILE")
[[ "$approved" == "$actual" ]] || fail 'artifact digest has not been explicitly approved on this host'

stage=$(mktemp -d /var/tmp/binana-testnet-deploy.XXXXXX)
trap 'rm -rf -- "$stage"' EXIT
install -m 0600 -o root -g root -- "$ARTIFACT" "$stage/release.tar.gz"
staged_actual=$(sha256sum -- "$stage/release.tar.gz" | awk '{print $1}')
[[ "$staged_actual" == "$expected" && "$staged_actual" == "$approved" ]] || fail 'artifact changed while entering the root-only stage'
printf '%s  release.tar.gz\n' "$staged_actual" >"$stage/release.tar.gz.sha256"
chown root:root "$stage/release.tar.gz.sha256"
chmod 0600 "$stage/release.tar.gz.sha256"

python3 - "$stage/release.tar.gz" "$stage" <<'PY'
import pathlib
import sys
import tarfile

archive_path = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2]) / "extracted"
with tarfile.open(archive_path, "r:gz") as archive:
    members = archive.getmembers()
    roots = set()
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit("unsafe archive path")
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise SystemExit("unsafe archive member type")
        if path.parts:
            roots.add(path.parts[0])
    if len(roots) != 1:
        raise SystemExit("archive must contain exactly one root")
    archive.extractall(target, filter="data")
root = target / next(iter(roots))
installer = root / "deploy" / "install_artifact.sh"
if not installer.is_file():
    raise SystemExit("artifact deployment installer is missing")
print(installer)
PY
installer=$(find "$stage/extracted" -mindepth 3 -maxdepth 3 -type f -path '*/deploy/install_artifact.sh' -print -quit)
[[ -n "$installer" ]] || fail 'verified artifact installer not found'
chmod 0700 "$installer"
"$installer" "$stage/release.tar.gz" "$stage/release.tar.gz.sha256"
