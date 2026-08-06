#!/usr/bin/env bash
set -euo pipefail
echo 'BLOCKED: this archived V7 Oracle installer is disabled in the merged release.' >&2
echo 'Run the root deploy/oracle_setup.sh instead.' >&2
exit 1
