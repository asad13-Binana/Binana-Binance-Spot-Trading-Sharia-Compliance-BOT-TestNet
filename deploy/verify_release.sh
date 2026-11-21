#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1
echo "Verifying $(cat RELEASE_VERSION) ($(cat RELEASE_MODE))"
python scripts/verify_dependency_consistency.py
python scripts/verify_deployment_supply_chain.py
python scripts/verify_manifest.py
python scripts/build_audit_ledgers.py --check
python scripts/verify_github_action_pins.py
python -m pytest -q -p no:cacheprovider tests monitoring/tests
python tests/secret_scan.py
python -m services.universe_service.validate_sharia shared/sharia/sharia_status.json
python - <<'PY'
import ast
import json
import os
import stat
from pathlib import Path
from services.common.owner_source_integrity import verify
verify(Path('.'))
if os.name == 'posix':
    for path in Path('.').rglob('*'):
        if not path.is_file() or '.git' in path.parts:
            continue
        mode = path.stat().st_mode
        assert not mode & (stat.S_IWGRP | stat.S_IWOTH), f'Unsafe release mode: {path}'
        if path.suffix == '.sh':
            assert mode & stat.S_IXUSR, f'Non-executable release script: {path}'
for path in Path('.').rglob('*.py'):
    if not any(part in {'.git', '__pycache__'} for part in path.parts):
        ast.parse(path.read_bytes(), str(path))
for path in Path('.').rglob('*.json'):
    if not any(part in {'.git', '__pycache__', '.pytest_cache', '.hypothesis'} for part in path.parts):
        json.loads(path.read_text(encoding='utf-8'))
print('Owner/support source identities and source syntax verified')
PY
while IFS= read -r -d '' script; do bash -n "$script"; done < <(find deploy scripts monitoring runtime_owner -type f -name '*.sh' -print0)

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
echo 'Offline source verification passed; image, host and exchange acceptance are separate gates.'
