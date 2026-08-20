#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# The legacy self-test prints unicode status glyphs; force UTF-8 so it does not
# crash on a Windows cp1252 console during offline verification.
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

# Ephemeral, test-only HMAC bus keys and a release binding so the signed-envelope
# suite runs without any real secret. Regenerated every run; never a real key.
export SIGNAL_HMAC_KEY="$(python -c 'import secrets;print(secrets.token_hex(24))')"
export COMMAND_HMAC_KEY="$(python -c 'import secrets;print(secrets.token_hex(24))')"
export SHARIA_HMAC_KEY="$(python -c 'import secrets;print(secrets.token_hex(24))')"
export SHARIA_APPROVAL_HMAC_KEY="$(python -c 'import secrets;print(secrets.token_hex(24))')"
export LIVE_EVIDENCE_KEY="$(python -c 'import secrets;print(secrets.token_hex(24))')"
export ENVELOPE_RELEASE_HASH="$(python -c 'print("a"*64)')"

python -m compileall -q services legacy_core freqtrade/user_data/strategies tests scripts monitoring

# Fail fast when the release label, package mode, safety default or
# certification state drifts across the shipped control files.
python - <<'PY'
import json
import pathlib
import sys

root = pathlib.Path('.')
version = (root / 'RELEASE_VERSION').read_text(encoding='utf-8').strip()
mode = (root / 'RELEASE_MODE').read_text(encoding='utf-8').strip()
status = json.loads((root / 'VALIDATION_STATUS.json').read_text(encoding='utf-8'))
manifest = json.loads((root / 'RELEASE_MANIFEST.json').read_text(encoding='utf-8'))
problems = []
revision = status.get('revision')
expected_default = 'simulation' if mode == 'live' else 'testnet'
if status.get('release') != version:
    problems.append(f'validation release {status.get("release")!r} != {version!r}')
if not isinstance(revision, str) or not revision or not version.endswith(revision):
    problems.append(f'release {version!r} does not carry revision {revision!r}')
if mode not in {'testnet', 'live'}:
    problems.append(f'unsupported RELEASE_MODE {mode!r}')
if status.get('package_mode') != mode:
    problems.append(f'validation package mode {status.get("package_mode")!r} != {mode!r}')
if status.get('package_interlock', {}).get('release_mode') != mode:
    problems.append('validation package interlock does not match RELEASE_MODE')
if status.get('default_execution_mode') != expected_default:
    problems.append('validation default execution mode is inconsistent')
if manifest.get('release') != f'{version}-{mode.upper()}':
    problems.append('manifest release identity is inconsistent')
if manifest.get('package_mode') != mode:
    problems.append('manifest package mode is inconsistent')
certified = status.get('production_live_certified')
if manifest.get('live_certified') != certified:
    problems.append('manifest and validation certification states disagree')
if mode == 'testnet' and certified is not False:
    problems.append('a testnet package cannot be production-live certified')
if mode == 'live' and certified is True:
    policy = status.get('sharia_live_policy')
    if not isinstance(policy, dict) or policy.get('approved') is not True:
        problems.append('live certification requires an approved Sharia host/model policy')
if problems:
    print('FAIL: release identity is inconsistent:', file=sys.stderr)
    for problem in problems:
        print(f'  - {problem}', file=sys.stderr)
    raise SystemExit(1)
print(f'release identity consistent: {version} ({mode})')
PY
python -m unittest discover -s tests -p 'test_*.py' -v
python -m pytest -q tests/test_api_readiness.py
python -m pytest -q monitoring/tests
python tests/secret_scan.py
python -m services.universe_service.validate_sharia shared/sharia/sharia_status.json

# The immutable V19.1 controller must be byte-identical.
python - <<'PY'
from services.common.sharia_v19 import controller_sha256, V19_CONTROLLER_SHA256, V19_CONTROLLER_FILENAME
import pathlib
path = pathlib.Path('shared/sharia') / V19_CONTROLLER_FILENAME
actual = controller_sha256(path)
assert actual == V19_CONTROLLER_SHA256, f'V19.1 controller hash mismatch: {actual}'
print(f'V19.1 controller byte-integrity verified: {actual}')
PY

SELFTEST_TMP=$(mktemp -d)
trap 'rm -rf "$SELFTEST_TMP"' EXIT
cp legacy_core/halal_coins.json "$SELFTEST_TMP/halal_coins.json"
(
  cd "$SELFTEST_TMP"
  python "$OLDPWD/legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py" --selftest
)
rm -rf "$SELFTEST_TMP"
trap - EXIT
python scripts/verify_manifest.py
# V102-REM-015 (deep-audit F-03/F-04): the generated audit ledgers must match
# the shipped tree exactly, so "N current files absent from the ledger" can
# never recur.
python scripts/build_audit_ledgers.py --check

# PKG-001: the extracted release must be operationally usable and safe. The
# installer executes deploy/install_monitoring.sh directly, so a script stored
# without its executable bit breaks deployment; a group/world-writable file is
# a security defect. Checked on POSIX (where modes are meaningful); Windows
# build hosts cannot express these bits, so the ZIP builder sets them instead.
case "$(uname -s 2>/dev/null || echo unknown)" in
  Linux|Darwin)
    NON_EXEC=$(find . \
      -path './.git' -prune -o \
      -path './.pytest_cache' -prune -o \
      -path './.hypothesis' -prune -o \
      -path '*/__pycache__' -prune -o \
      -type f -name '*.sh' ! -perm -u+x -print | head -20 || true)
    if [ -n "$NON_EXEC" ]; then
      echo 'FAIL: packaged shell scripts are not executable:' >&2
      printf '%s\n' "$NON_EXEC" >&2
      exit 1
    fi
    WRITABLE=$(find . \
      -path './.git' -prune -o \
      -path './.pytest_cache' -prune -o \
      -path './.hypothesis' -prune -o \
      -path '*/__pycache__' -prune -o \
      -type f -perm /022 -print | head -20 || true)
    if [ -n "$WRITABLE" ]; then
      echo 'FAIL: packaged files are group/world-writable:' >&2
      printf '%s\n' "$WRITABLE" >&2
      exit 1
    fi
    echo 'file mode check passed (shell scripts executable; nothing world-writable)'
    ;;
  *)
    echo 'file mode check skipped (non-POSIX build host; the ZIP builder sets 0755/0644)'
    ;;
esac

python - <<'PY'
import json, pathlib
for path in pathlib.Path('.').rglob('*.json'):
    if any(x in path.parts for x in ('__pycache__','.git')):
        continue
    json.loads(path.read_text(encoding='utf-8'))
print('JSON validation passed')
PY

find . \
  -path './.git' -prune -o \
  -path './.pytest_cache' -prune -o \
  -path './.hypothesis' -prune -o \
  -path '*/__pycache__' -prune -o \
  -type f -name '*.sh' -print0 | while IFS= read -r -d '' script; do bash -n "$script"; done

python - <<'PY'
from pathlib import Path
units=Path('monitoring/systemd')
services={p.stem for p in units.glob('*.service')}
for timer in units.glob('*.timer'):
    text=timer.read_text(encoding='utf-8')
    target=next((line.split('=',1)[1].strip() for line in text.splitlines() if line.startswith('Unit=')), timer.with_suffix('.service').name)
    assert Path(target).stem in services, f'{timer.name} has no matching service {target}'
print(f'systemd service/timer pairing passed ({len(services)} services)')
PY
if command -v systemd-analyze >/dev/null 2>&1; then
  # A-003 (deep-audit): the shipped units legitimately reference deployment
  # paths that exist only AFTER installation (/opt/binana-freqtrade-v101/...,
  # /usr/local/libexec/..., docker.service). Running `systemd-analyze verify`
  # against an UNPACKED checkout — exactly what GitHub Actions does — therefore
  # reports missing-executable/missing-unit errors that are NOT unit defects,
  # and under `set -e` that aborted the whole gate. Classify the output: fail
  # on genuine syntax/parse defects, tolerate pre-deployment context with an
  # explicit notice. Never blanket-ignore a non-zero exit.
  SYSTEMD_LOG="$(mktemp)"
  set +e
  systemd-analyze verify monitoring/systemd/*.service monitoring/systemd/*.timer >"$SYSTEMD_LOG" 2>&1
  SYSTEMD_RC=$?
  set -e
  python - "$SYSTEMD_RC" "$SYSTEMD_LOG" <<'PY'
import re, sys
rc = int(sys.argv[1])
text = open(sys.argv[2], encoding='utf-8', errors='replace').read()
lines = [l.strip() for l in text.splitlines() if l.strip()]
# Problems fully explained by "the package is not installed on this host yet".
context = re.compile(
    r'(?i)(not executable|no such file or directory|command [^ ]* is not|'
    r'unit [^ ]* (not found|does not exist)|references units that do not exist|'
    r'failed to (load|prepare)[^\n]*(docker\.service|/opt/|/usr/local/libexec/))')
# Genuine unit defects that must never be tolerated.
defect = re.compile(
    r'(?i)(unknown key|unknown section|failed to parse|invalid (setting|value|option)|'
    r'syntax error|assignment outside of section)')
defects = [l for l in lines if defect.search(l)]
if defects:
    print('systemd-analyze reported UNIT DEFECTS:')
    for l in defects:
        print('  ' + l)
    sys.exit(1)
if rc != 0:
    unexplained = [l for l in lines
                   if re.search(r'(?i)(error|warning|failed)', l) and not context.search(l)]
    if unexplained:
        print('systemd-analyze produced UNEXPLAINED problems:')
        for l in unexplained:
            print('  ' + l)
        sys.exit(1)
    print(f'systemd-analyze verify exited {rc}; every reported problem is '
          'pre-deployment context (installed paths / docker.service absent).')
    for l in lines[:8]:
        print('  context: ' + l)
    print('Unit syntax, sections and hardening keys are valid; full verification '
          'requires a staged install (documented external gate).')
else:
    print('systemd-analyze verify passed')
PY
  rm -f "$SYSTEMD_LOG"
else
  echo 'systemd-analyze unavailable; pairing and static unit tests passed'
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose --env-file .env.example config -q
  echo 'Docker Compose validation passed'
else
  python - <<'PY'
import pathlib, yaml
parsed = {}
for path in list(pathlib.Path('.').rglob('*.yml')) + list(pathlib.Path('.').rglob('*.yaml')):
    if any(part in {'.git', '__pycache__', '.ruff_cache'} for part in path.parts):
        continue
    parsed[path.as_posix()] = yaml.safe_load(path.read_text(encoding='utf-8'))
root = parsed.get('docker-compose.yml')
expected = {
    'universe', 'sharia-egress-proxy', 'sharia-screener', 'freqtrade',
    'execution-sidecar', 'telegram-broker',
}
assert isinstance(root, dict) and set(root.get('services', {})) == expected, \
    ('root compose service set mismatch', sorted(root.get('services', {})))
assert isinstance(parsed.get('freqtrade/docker-compose.yml'), dict)
print(f'YAML structural validation passed for {len(parsed)} file(s) (Docker unavailable)')
PY
fi

# V102-REM-009: the printed version comes from release metadata, not a
# hardcoded string; the v101 operational namespace (paths, image names) is
# retained separately for upgrade compatibility.
RELEASE_VERSION_LABEL=$(cat RELEASE_VERSION 2>/dev/null || echo 'V10.2')
echo "${RELEASE_VERSION_LABEL} release verification passed."
