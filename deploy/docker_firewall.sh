#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'ERROR: docker_firewall.sh requires root' >&2; exit 1; }
external_interface=$(ip route show default | awk 'NR==1 {for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')
[[ -n "$external_interface" ]] || { echo 'ERROR: default-route interface not found' >&2; exit 1; }
iptables -nL DOCKER-USER >/dev/null 2>&1 || { echo 'ERROR: DOCKER-USER chain unavailable' >&2; exit 1; }
add_rule(){ iptables -C DOCKER-USER "$@" 2>/dev/null || iptables -I DOCKER-USER 1 "$@"; }
add_rule -i "$external_interface" -m conntrack --ctstate RELATED,ESTABLISHED -m comment --comment BINANA-ESTABLISHED -j ACCEPT
add_rule -i "$external_interface" -m conntrack --ctstate NEW -m comment --comment BINANA-NO-PUBLISHED-PORTS -j DROP
# Containers do not need OCI instance metadata.  Restrict host TCP/80 metadata
# to root while leaving Oracle link-local DNS, NTP and block-volume traffic
# untouched on their separate ports.
add_rule -d 169.254.169.254/32 -p tcp --dport 80 -m comment --comment BINANA-NO-CONTAINER-IMDS -j DROP
iptables -C OUTPUT -d 169.254.169.254/32 -p tcp --dport 80 -m owner ! --uid-owner 0 \
  -m comment --comment BINANA-ROOT-ONLY-IMDS -j REJECT 2>/dev/null || \
  iptables -I OUTPUT 1 -d 169.254.169.254/32 -p tcp --dport 80 -m owner ! --uid-owner 0 \
    -m comment --comment BINANA-ROOT-ONLY-IMDS -j REJECT
echo "BINANA Docker ingress guard active on $external_interface"
