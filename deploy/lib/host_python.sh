#!/usr/bin/env bash
# Sourced only after artifact manifest and supply-chain verification.
prepare_host_python(){
  local release_dir=$1 lock digest target marker
  lock=$release_dir/requirements.host.lock
  [[ -f "$lock" && ! -L "$lock" ]] || return 1
  /usr/bin/python3 -I -c 'import sys; assert sys.version_info[:2] == (3, 12)' || {
    echo 'ERROR: host validation requires Ubuntu 24.04 Python 3.12 and python3-venv' >&2
    return 1
  }
  digest=$(sha256sum "$lock" | awk '{print $1}') || return 1
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || return 1
  [[ ! -L "$APP_ROOT/host-venvs" ]] || return 1
  install -d -m 0755 -o root -g root "$APP_ROOT/host-venvs" || return 1
  target=$APP_ROOT/host-venvs/$digest-py312
  [[ ! -L "$target" ]] || return 1
  marker=$target/.complete
  if [[ ! -f "$marker" ]]; then
    if [[ -e "$target" ]]; then
      # Preserve incomplete evidence instead of clearing an active environment.
      mv -T "$target" "$target.incomplete-$(date -u +%s%N)" || return 1
    fi
    # Build at the final path: venv console-script shebangs are absolute.
    /usr/bin/python3 -I -m venv "$target" || return 1
    env -i PATH=/usr/bin:/bin HOME=/root PIP_CONFIG_FILE=/dev/null \
      "$target/bin/python" -I -m pip install --disable-pip-version-check \
      --no-cache-dir --only-binary=:all: --require-hashes -r "$lock" || return 1
    "$target/bin/python" -I -m pip check || return 1
    "$target/bin/python" -I -c 'from Crypto.PublicKey import ECC; from Crypto.Signature import eddsa' || return 1
    printf '%s\n' "$digest" >"$marker" || return 1
    chmod -R go-w "$target" || return 1
  fi
  [[ ! -L "$marker" && $(<"$marker") == "$digest" ]] || return 1
  [[ $(stat -Lc '%u' "$target") == 0 ]] || return 1
  HOST_PYTHON=$target/bin/python
  "$HOST_PYTHON" -I -c 'from Crypto.PublicKey import ECC; from Crypto.Signature import eddsa' || return 1
}
