#!/usr/bin/env bash
# Imported from V10 after review: run the preserved V4.9.16 self-tests with a
# bounded runtime. The byte-preserved module starts background helper threads
# that can keep the process alive after the "33/33 passed" marker; bound the
# process and validate the marker instead of hanging CI indefinitely.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-${TMPDIR:-/tmp}/binance-v4.9.16-selftest-$$.log}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cp "$ROOT/legacy_core/halal_coins.json" "$TMP/halal_coins.json"
set +e
(
  cd "$TMP"
  timeout --signal=TERM --kill-after=5s 60s \
    python "$ROOT/legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py" --selftest
) 2>&1 | tee "$OUT"
RC=${PIPESTATUS[0]}
set -e
grep -q '33/33 passed' "$OUT"
if [[ "$RC" -ne 0 && "$RC" -ne 124 && "$RC" -ne 137 ]]; then
  echo "Legacy self-test process failed before a valid completion marker (rc=$RC)" >&2
  exit "$RC"
fi
echo "legacy V4.9.16 self-tests: 33/33 marker verified"
