#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'ERROR: harden_ssh.sh requires root' >&2; exit 1; }
[[ -n "${SSH_CONNECTION:-}" || "${ALLOW_NON_SSH_HARDENING:-false}" == true ]] || { echo 'ERROR: run from an active key-authenticated SSH session' >&2; exit 1; }
DEPLOY_USER=${DEPLOY_USER:-${SUDO_USER:-ubuntu}}
[[ "$DEPLOY_USER" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || { echo 'ERROR: invalid deployment account' >&2; exit 1; }
deploy_home=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
[[ -n "$deploy_home" ]] || { echo 'ERROR: deployment account not found' >&2; exit 1; }
authorized=$deploy_home/.ssh/authorized_keys
[[ -s "$authorized" && -f "$authorized" && ! -L "$authorized" ]] || { echo "ERROR: $authorized must be a non-empty regular non-symlink file" >&2; exit 1; }
[[ $(stat -c '%U' "$authorized") == "$DEPLOY_USER" ]] || { echo 'ERROR: authorized_keys has the wrong owner' >&2; exit 1; }
authorized_mode=$(stat -c '%a' "$authorized")
(( (8#$authorized_mode & 0022) == 0 )) || { echo 'ERROR: authorized_keys must not be group/world writable' >&2; exit 1; }
target=/etc/ssh/sshd_config.d/60-binana-hardening.conf
[[ ! -L "$target" ]] || { echo 'ERROR: SSH hardening target must not be a symlink' >&2; exit 1; }
[[ ! -e "$target" || -f "$target" ]] || { echo 'ERROR: SSH hardening target must be a regular file' >&2; exit 1; }
backup=$(mktemp)
temporary=''
trap 'rm -f -- "$backup" ${temporary:+"$temporary"}' EXIT
had_target=false
if [[ -f "$target" ]]; then cp -a "$target" "$backup"; had_target=true; fi
temporary=$(mktemp /etc/ssh/sshd_config.d/.60-binana-hardening.XXXXXX)
cat >"$temporary" <<'EOF'
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PermitEmptyPasswords no
X11Forwarding no
AllowTcpForwarding local
MaxAuthTries 3
LoginGraceTime 30
EOF
chown root:root "$temporary"
chmod 0644 "$temporary"
mv -f "$temporary" "$target"
if ! sshd -t; then
  if [[ "$had_target" == true ]]; then cp -a "$backup" "$target"; else rm -f "$target"; fi
  rm -f "$backup"
  echo 'ERROR: sshd validation failed; previous configuration restored' >&2
  exit 1
fi
rm -f "$backup"
systemctl reload ssh
trap - EXIT
echo 'SSH hardening applied after sshd -t; keep the current session open until a second login succeeds.'
