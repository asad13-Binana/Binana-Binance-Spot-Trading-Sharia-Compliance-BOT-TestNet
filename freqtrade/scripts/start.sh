#!/usr/bin/env bash
set -euo pipefail
echo 'BLOCKED: the archived Freqtrade subtree is signal-only and cannot be started independently.' >&2
echo 'Start the merged stack from the repository root with the root docker-compose.yml.' >&2
exit 1
