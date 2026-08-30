#!/usr/bin/env bash
# CI only: a disposable Ubuntu container without the host Docker socket.
set -euo pipefail
[[ $EUID -eq 0 && -f /.dockerenv ]] || { echo 'Disposable Docker test container required' >&2; exit 1; }
[[ ! -S /var/run/docker.sock ]] || { echo 'Never mount a real Docker socket into this test' >&2; exit 1; }
touch /.binana-clean-host
export DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1
apt-get update -qq
apt-get install -y -qq python3 python3-venv ca-certificates curl rsync util-linux passwd
exec /usr/bin/python3 /source/tests/clean_host/scenarios.py /source
