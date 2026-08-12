#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
ROOT_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
MEMINFO_PATH=${MEMINFO_PATH:-/proc/meminfo}
OS_RELEASE_PATH=${OS_RELEASE_PATH:-/etc/os-release}
MIN_PHYSICAL_MEMORY_MIB=${MIN_PHYSICAL_MEMORY_MIB:-5120}
MIN_TOTAL_MEMORY_MIB=${MIN_TOTAL_MEMORY_MIB:-8192}
MIN_FREE_DISK_GIB=${MIN_FREE_DISK_GIB:-35}
SWAP_SIZE_GIB=${SWAP_SIZE_GIB:-4}
DEPLOY_USER=${DEPLOY_USER:-${SUDO_USER:-ubuntu}}
BOT_USER=${BOT_USER:-binanabot}
MONITOR_USER=${MONITOR_USER:-botmon}
PRIVATE=${PRIVATE:-/etc/binana-freqtrade-v101}
APP_ROOT=${APP_ROOT:-/opt/binana-freqtrade-v101}
PERSIST=${PERSIST:-/var/lib/binana-freqtrade-v101/shared}
MONITOR_LOG_DIR=${MONITOR_LOG_DIR:-/var/log/binana-freqtrade-v101/monitor}
DEPLOY_INBOX=${DEPLOY_INBOX:-/var/lib/binana-deploy/inbox}

fail(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "required command missing after installation: $1"; }
valid_account(){ [[ "$1" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]]; }
valid_positive_integer(){ [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 > 0 )); }

valid_positive_integer "$MIN_PHYSICAL_MEMORY_MIB" || fail 'MIN_PHYSICAL_MEMORY_MIB must be a positive integer'
valid_positive_integer "$MIN_TOTAL_MEMORY_MIB" || fail 'MIN_TOTAL_MEMORY_MIB must be a positive integer'
valid_positive_integer "$MIN_FREE_DISK_GIB" || fail 'MIN_FREE_DISK_GIB must be a positive integer'
valid_positive_integer "$SWAP_SIZE_GIB" || fail 'SWAP_SIZE_GIB must be a positive integer'
(( SWAP_SIZE_GIB >= 1 && SWAP_SIZE_GIB <= 8 )) || fail 'SWAP_SIZE_GIB must be between 1 and 8'
[[ -r "$MEMINFO_PATH" ]] || fail "cannot read $MEMINFO_PATH"
physical_mib=$(awk '/MemTotal/{print int($2/1024)}' "$MEMINFO_PATH")
swap_mib=$(awk '/SwapTotal/{print int($2/1024)}' "$MEMINFO_PATH")
if (( physical_mib < MIN_PHYSICAL_MEMORY_MIB )); then
  fail "unsupported host: ${physical_mib} MiB RAM; require at least ${MIN_PHYSICAL_MEMORY_MIB} MiB for the declared 1-OCPU/6-GB topology"
fi

[[ $EUID -eq 0 ]] || fail 'run oracle_setup.sh with sudo or as root'
for account in "$DEPLOY_USER" "$BOT_USER" "$MONITOR_USER"; do
  valid_account "$account" || fail "unsafe account name: $account"
done

# These privileged roots are deliberate security boundaries. Environment
# overrides are rejected so a mistyped or hostile value cannot redirect a
# root-owned recursive operation into an arbitrary host path.
[[ "$PRIVATE" == /etc/binana-freqtrade-v101 ]] || fail 'PRIVATE must remain /etc/binana-freqtrade-v101'
[[ "$APP_ROOT" == /opt/binana-freqtrade-v101 ]] || fail 'APP_ROOT must remain /opt/binana-freqtrade-v101'
[[ "$PERSIST" == /var/lib/binana-freqtrade-v101/shared ]] || fail 'PERSIST must remain /var/lib/binana-freqtrade-v101/shared'
[[ "$MONITOR_LOG_DIR" == /var/log/binana-freqtrade-v101/monitor ]] || fail 'MONITOR_LOG_DIR must remain /var/log/binana-freqtrade-v101/monitor'
[[ "$DEPLOY_INBOX" == /var/lib/binana-deploy/inbox ]] || fail 'DEPLOY_INBOX must remain /var/lib/binana-deploy/inbox'
for protected_path in "$PRIVATE" "$APP_ROOT" "$PERSIST" "$MONITOR_LOG_DIR" "$DEPLOY_INBOX"; do
  [[ ! -L "$protected_path" ]] || fail "privileged path must not be a symlink: $protected_path"
done

# Do not silently run a second Compose namespace beside an earlier install.
# Migration is an owner-reviewed, backup-first operation, not a bootstrap side
# effect. A dormant legacy data directory without a current release is left
# untouched and can be handled by the documented migration procedure.
[[ ! -e /opt/binance-freqtrade-v101/current ]] || \
  fail 'legacy deployment detected at /opt/binance-freqtrade-v101/current; stop, back up and migrate it explicitly'
if command -v docker >/dev/null 2>&1; then
  legacy_containers=$(docker ps -aq --filter label=com.docker.compose.project=binance-freqtrade-v101 2>/dev/null || true)
  [[ -z "$legacy_containers" ]] || fail 'legacy binance-freqtrade-v101 containers detected; reconcile them before bootstrap'
fi

[[ -r "$OS_RELEASE_PATH" ]] || fail "cannot read $OS_RELEASE_PATH"
# shellcheck disable=SC1090 # operating-system metadata, not a secret/config input
source "$OS_RELEASE_PATH"
[[ "${ID:-}" == ubuntu ]] || fail 'only Ubuntu is supported by this installer'
[[ "${VERSION_ID:-}" == 24.04 ]] || fail "Ubuntu 24.04 LTS is required (found ${VERSION_ID:-unknown})"
architecture=$(dpkg --print-architecture)
[[ "$architecture" == arm64 || "$architecture" == amd64 ]] || fail "unsupported architecture: $architecture"
[[ -f "$ROOT_DIR/RELEASE_MODE" ]] || fail 'RELEASE_MODE missing from installer package'
package_mode=$(<"$ROOT_DIR/RELEASE_MODE")
[[ "$package_mode" == testnet || "$package_mode" == live ]] || fail 'invalid RELEASE_MODE'
if [[ "$package_mode" == testnet ]]; then
  expected_environment=TESTNET
  expected_instance=BINANA-TN-TYO-01
  expected_hostname=binana-testnet-tokyo
else
  expected_environment=LIVE
  expected_instance=BINANA-LIVE-TYO-01
  expected_hostname=binana-live-tokyo
fi
hostnamectl set-hostname "$expected_hostname"

free_kib=$(df -Pk / | awk 'NR==2 {print $4}')
(( free_kib >= MIN_FREE_DISK_GIB * 1024 * 1024 )) || fail "root filesystem needs at least ${MIN_FREE_DISK_GIB} GiB free"
getent passwd "$DEPLOY_USER" >/dev/null || fail "deployment account does not exist: $DEPLOY_USER"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg git jq python3 python3-venv \
  sqlite3 rsync chrony logrotate unattended-upgrades iptables openssh-server
python3 - <<'PY'
import sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit(f"Python 3.12 is required; found {sys.version.split()[0]}")
PY

# Docker's official Ubuntu repository.  No convenience script is downloaded or
# piped into a shell.  The official ASCII signing key is scoped with Signed-By.
for package in docker.io docker-compose docker-compose-v2 docker-doc podman-docker containerd runc; do
  dpkg-query -W -f='${db:Status-Abbrev}' "$package" 2>/dev/null | grep -q '^ii' && apt-get remove -y "$package"
done
install -d -m 0755 -o root -g root /etc/apt/keyrings
docker_key=$(mktemp)
trap 'rm -f -- "$docker_key"' EXIT
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
  https://download.docker.com/linux/ubuntu/gpg --output "$docker_key"
gpg --batch --show-keys "$docker_key" >/dev/null 2>&1 || fail 'Docker repository signing key is not valid OpenPGP data'
install -m 0644 -o root -g root "$docker_key" /etc/apt/keyrings/docker.asc
rm -f -- "$docker_key"
trap - EXIT
cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $architecture
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

install -d -m 0755 -o root -g root /etc/docker
python3 - /etc/docker/daemon.json <<'PY'
import json
import os
import pathlib
import tempfile
import sys

path = pathlib.Path(sys.argv[1])
value = {}
if path.exists():
    value = json.loads(path.read_text(encoding="utf-8"))
required = {
    "live-restore": True,
    "log-driver": "json-file",
    "log-opts": {"max-size": "10m", "max-file": "3"},
    "userland-proxy": False,
}
for key, expected in required.items():
    if key in value and value[key] != expected:
        raise SystemExit(f"refusing to overwrite conflicting Docker daemon setting: {key}")
    value[key] = expected
fd, temporary = tempfile.mkstemp(prefix=".daemon.", dir=path.parent)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(value, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(temporary, 0o644)
os.replace(temporary, path)
PY
dockerd --validate --config-file=/etc/docker/daemon.json
systemctl enable --now docker
systemctl restart docker
install -m 0755 -o root -g root "$SCRIPT_DIR/docker_firewall.sh" /usr/local/sbin/binana-docker-firewall
cat >/etc/systemd/system/binana-docker-firewall.service <<'EOF'
[Unit]
Description=BINANA Docker ingress guard
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/binana-docker-firewall
RemainAfterExit=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now binana-docker-firewall.service

# The deployment and monitoring identities never receive the root-equivalent
# docker group.  Root-owned wrappers perform the approved operations.
getent group "$BOT_USER" >/dev/null || groupadd --system "$BOT_USER"
id "$BOT_USER" >/dev/null 2>&1 || useradd --system --gid "$BOT_USER" --home-dir /nonexistent --shell /usr/sbin/nologin "$BOT_USER"
id "$MONITOR_USER" >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin "$MONITOR_USER"
usermod -a -G "$BOT_USER" "$MONITOR_USER"
for account in "$DEPLOY_USER" "$MONITOR_USER" "$BOT_USER"; do
  if id -nG "$account" | tr ' ' '\n' | grep -qx docker; then
    gpasswd -d "$account" docker >/dev/null
  fi
done
bot_uid=$(id -u "$BOT_USER")
bot_gid=$(getent group "$BOT_USER" | cut -d: -f3)
deploy_uid=$(id -u "$DEPLOY_USER")

install -d -m 0755 -o root -g root "$APP_ROOT" "$APP_ROOT/releases"
install -d -m 0750 -o "$BOT_USER" -g "$BOT_USER" "$PERSIST"
install -d -m 0750 -o "$BOT_USER" -g "$BOT_USER" \
  "$PERSIST/commands/inbox" "$PERSIST/runtime" "$PERSIST/sharia" \
  "$PERSIST/legacy_runtime" "$PERSIST/freqtrade/logs" "$PERSIST/audit"
install -d -m 0700 -o root -g root "$PRIVATE"
install -d -m 0750 -o "$MONITOR_USER" -g "$MONITOR_USER" "$MONITOR_LOG_DIR"
install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$DEPLOY_INBOX"
if [[ ! -e "$PRIVATE/.env" ]]; then
  install -m 0600 -o root -g root /dev/null "$PRIVATE/.env"
fi
[[ ! -L "$PRIVATE/.env" && -f "$PRIVATE/.env" ]] || fail "$PRIVATE/.env must be a regular non-symlink file"
chown root:root "$PRIVATE/.env"
chmod 0600 "$PRIVATE/.env"
install -m 0600 -o root -g root "$ROOT_DIR/.env.example" "$PRIVATE/.env.template"

install -m 0755 -o root -g root "$SCRIPT_DIR/binana-deploy-wrapper.sh" /usr/local/sbin/binana-deploy
install -m 0755 -o root -g root "$SCRIPT_DIR/binana-approve-release.sh" /usr/local/sbin/binana-approve-release
install -d -m 0755 -o root -g root /etc/sudoers.d
cat >/etc/sudoers.d/binana-deploy <<EOF
$DEPLOY_USER ALL=(root) NOPASSWD: /usr/local/sbin/binana-deploy
EOF
chmod 0440 /etc/sudoers.d/binana-deploy
visudo -cf /etc/sudoers.d/binana-deploy >/dev/null
cat >/etc/default/binana-deploy <<EOF
BINANA_DEPLOY_UID=$deploy_uid
EOF
chmod 0644 /etc/default/binana-deploy

# Four GiB swap headroom for the 6-GB A1 target.  Existing larger swap is kept.
if (( swap_mib < SWAP_SIZE_GIB * 1024 )); then
  swap_path=/swapfile-binana
  if [[ ! -e "$swap_path" ]]; then
    fallocate -l "${SWAP_SIZE_GIB}G" "$swap_path" || dd if=/dev/zero of="$swap_path" bs=1M count=$((SWAP_SIZE_GIB * 1024)) status=progress
    chown root:root "$swap_path"
    chmod 0600 "$swap_path"
    mkswap "$swap_path"
  else
    [[ -f "$swap_path" && ! -L "$swap_path" ]] || fail "$swap_path must be a regular non-symlink file"
    [[ $(stat -c '%U:%G:%a' "$swap_path") == root:root:600 ]] || fail "$swap_path must be owned root:root with mode 0600"
    [[ $(blkid -p -s TYPE -o value "$swap_path" 2>/dev/null || true) == swap ]] || fail "$swap_path is not a valid swap area"
  fi
  swapon --show=NAME --noheadings | grep -qx "$swap_path" || swapon "$swap_path"
  grep -Fqx "$swap_path none swap sw 0 0" /etc/fstab || printf '%s\n' "$swap_path none swap sw 0 0" >>/etc/fstab
fi
cat >/etc/sysctl.d/60-binana-memory.conf <<'EOF'
vm.swappiness=10
EOF
sysctl --system >/dev/null

# OCI's link-local NTP service is retained.  Do not block 169.254.169.254:
# it also provides DNS, metadata and platform services on other ports.
install -d -m 0755 -o root -g root /etc/chrony/conf.d
cat >/etc/chrony/conf.d/50-oci-binana.conf <<'EOF'
server 169.254.169.254 iburst prefer
makestep 1.0 3
EOF
systemctl disable --now systemd-timesyncd.service >/dev/null 2>&1 || true
systemctl enable --now chrony
chronyc -a makestep >/dev/null 2>&1 || true

# Official Ubuntu security pockets remain automatic; reboots and the
# third-party Docker repository remain deliberate maintenance actions.
cat >/etc/apt/apt.conf.d/52binana-unattended-upgrades <<'EOF'
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Origins-Pattern {
  "origin=Ubuntu,codename=${distro_codename},label=Ubuntu-Security";
  "origin=UbuntuESMApps,codename=${distro_codename},label=UbuntuESMApps-Security";
  "origin=UbuntuESM,codename=${distro_codename},label=UbuntuESM-Security";
};
EOF
systemctl enable --now unattended-upgrades

install -d -m 0755 -o root -g root /etc/systemd/journald.conf.d
cat >/etc/systemd/journald.conf.d/60-binana.conf <<'EOF'
[Journal]
SystemMaxUse=512M
RuntimeMaxUse=128M
MaxRetentionSec=14day
EOF
systemctl restart systemd-journald

need docker
need python3
need chronyc
docker version >/dev/null
docker compose version >/dev/null
docker info >/dev/null
systemctl is-active --quiet docker || fail 'Docker is not active'
systemctl is-active --quiet chrony || fail 'Chrony is not active'
chronyc tracking >/dev/null || fail 'Chrony tracking is unavailable'
clock_synchronized=false
for _ in $(seq 1 30); do
  if timedatectl show -p NTPSynchronized --value | grep -qx yes; then
    clock_synchronized=true
    break
  fi
  sleep 2
done
[[ "$clock_synchronized" == true ]] || fail 'host clock did not synchronize within 60 seconds'
last_offset=$(LC_ALL=C chronyc tracking | awk -F: '/^Last offset/ {gsub(/ seconds|[[:space:]]/, "", $2); print $2; exit}')
python3 - "$last_offset" <<'PY'
import sys
try:
    offset = abs(float(sys.argv[1]))
except (ValueError, IndexError):
    raise SystemExit("Chrony last offset is unavailable")
if offset > 0.100:
    raise SystemExit(f"Chrony last offset {offset:.6f}s exceeds 0.100s")
print(f"Chrony last offset acceptable: {offset:.6f}s")
PY

printf 'Oracle host bootstrap completed for BINANA %s.\n' "$expected_environment"
printf 'BOT_INSTANCE_ID=%s; BOT_UID=%s; BOT_GID=%s.\n' "$expected_instance" "$bot_uid" "$bot_gid"
printf 'Populate secrets only with: sudoedit %s/.env\n' "$PRIVATE"
printf 'The deployment and monitoring accounts have no Docker-group membership.\n'
