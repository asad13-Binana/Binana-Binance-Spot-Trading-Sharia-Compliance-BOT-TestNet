#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'ERROR: update_docker.sh requires root' >&2; exit 1; }
[[ $# -eq 1 ]] || { echo 'usage: update_docker.sh EXACT_DOCKER_CE_VERSION' >&2; exit 1; }
version=$1
apt-cache madison docker-ce | awk '{print $3}' | grep -Fqx "$version" || { echo 'ERROR: requested Docker CE version is unavailable' >&2; exit 1; }
current=$(docker version --format '{{.Server.Version}}')
requested_upstream=${version#*:}
requested_upstream=${requested_upstream%%-*}
current_major=${current%%.*}
requested_major=${requested_upstream%%.*}
[[ "$current_major" =~ ^[0-9]+$ && "$requested_major" =~ ^[0-9]+$ ]] || { echo 'ERROR: could not determine Docker major versions' >&2; exit 1; }
[[ "$current_major" == "$requested_major" ]] || { echo 'ERROR: automatic Docker major-version changes are prohibited' >&2; exit 1; }
echo "Current: $current"
echo "Requested apt version: $version"
apt-get install -y --only-upgrade "docker-ce=$version" "docker-ce-cli=$version"
dockerd --validate --config-file=/etc/docker/daemon.json
systemctl is-active --quiet docker
docker version
docker compose version
echo 'Docker upgrade completed; run deploy/oracle_validate.sh and the reboot/rollback soak checks.'
