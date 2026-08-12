#!/usr/bin/env bash
# Parse a root-controlled KEY=VALUE file as literal data.  This file is trusted
# code; the environment file it reads is never sourced or evaluated.

secure_env_fail() {
  printf 'ERROR: %s\n' "$*" >&2
  return 1
}

secure_env_read() {
  local path=${1:?secure_env_read PATH DESTINATION_MAP}
  local destination=${2:?secure_env_read PATH DESTINATION_MAP}
  local parent mode owner group path_identity fd_identity line key value fd
  local line_number=0

  [[ -e "$path" ]] || secure_env_fail "environment file is missing: $path" || return 1
  [[ ! -L "$path" ]] || secure_env_fail "environment file must not be a symlink: $path" || return 1
  [[ -f "$path" ]] || secure_env_fail "environment path is not a regular file: $path" || return 1

  parent=$(dirname -- "$path")
  [[ ! -L "$parent" ]] || secure_env_fail "environment directory must not be a symlink: $parent" || return 1
  owner=$(stat -Lc '%u' -- "$path")
  group=$(stat -Lc '%g' -- "$path")
  mode=$(stat -Lc '%a' -- "$path")
  [[ "$owner" == 0 && "$group" == 0 ]] || secure_env_fail "$path must be owned by root:root" || return 1
  [[ "$mode" == 600 ]] || secure_env_fail "$path must have mode 0600 (found $mode)" || return 1
  owner=$(stat -Lc '%u' -- "$parent")
  mode=$(stat -Lc '%a' -- "$parent")
  [[ "$owner" == 0 ]] || secure_env_fail "$parent must be owned by root" || return 1
  (( (8#$mode & 0022) == 0 )) || secure_env_fail "$parent must not be group/world writable" || return 1

  exec {fd}<"$path" || secure_env_fail "cannot open environment file: $path" || return 1
  path_identity=$(stat -Lc '%d:%i' -- "$path")
  fd_identity=$(stat -Lc '%d:%i' -- "/proc/self/fd/$fd")
  if [[ "$path_identity" != "$fd_identity" ]]; then
    exec {fd}<&-
    secure_env_fail "environment file changed while opening; retry"
    return 1
  fi

  _secure_env_parse_fd "$fd" "$destination" "$path" || {
    exec {fd}<&-
    return 1
  }
  exec {fd}<&-
}

_secure_env_parse_fd() {
  local fd=${1:?_secure_env_parse_fd FD DESTINATION_MAP LABEL}
  local destination=${2:?_secure_env_parse_fd FD DESTINATION_MAP LABEL}
  local label=${3:-environment}
  local line key value
  local line_number=0
  # shellcheck disable=SC2178 # a nameref to the caller's associative array
  local -n output="$destination"
  output=()
  while IFS= read -r line <&$fd || [[ -n "$line" ]]; do
    line_number=$((line_number + 1))
    line=${line%$'\r'}
    [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ ! "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      secure_env_fail "$label:$line_number is not a strict KEY=VALUE record"
      return 1
    fi
    key=${line%%=*}
    value=${line#*=}
    if [[ -v "output[$key]" ]]; then
      secure_env_fail "$label:$line_number duplicates key $key"
      return 1
    fi
    output["$key"]=$value
  done
}

secure_env_require() {
  local destination=${1:?secure_env_require DESTINATION_MAP KEY}
  local key=${2:?secure_env_require DESTINATION_MAP KEY}
  # shellcheck disable=SC2178
  local -n values="$destination"
  [[ -n "${values[$key]:-}" ]] || secure_env_fail "required environment key is empty: $key"
}
