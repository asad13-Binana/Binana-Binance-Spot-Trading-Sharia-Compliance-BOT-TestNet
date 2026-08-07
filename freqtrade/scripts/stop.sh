#!/usr/bin/env bash
set -euo pipefail
echo 'BLOCKED: do not manage the merged runtime from the archived Freqtrade subtree.' >&2
echo 'Use the root docker-compose.yml or the root deployment rollback/emergency procedures.' >&2
exit 1
