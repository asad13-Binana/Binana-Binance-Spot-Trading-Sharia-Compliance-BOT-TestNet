#!/usr/bin/env bash
set -euo pipefail
echo 'BLOCKED: legacy pull-based deployment is disabled in V8.1.' >&2
echo 'Use the root GitHub Actions immutable-artifact workflow and deploy/install_artifact.sh.' >&2
exit 1
