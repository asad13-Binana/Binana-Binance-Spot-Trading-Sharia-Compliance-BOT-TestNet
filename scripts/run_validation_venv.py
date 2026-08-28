#!/usr/bin/env python3
"""Run the release gate in an isolated disposable Python 3.12 environment."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATION_STATUSES = {
    'PASS', 'TOOLING_UNAVAILABLE', 'COLLECTION_FAILED',
    'TEST_FAILED', 'TIMEOUT',
}


def classify_returncode(stage: str, returncode: int) -> str:
    if returncode == 0:
        return 'PASS'
    if stage == 'tooling':
        return 'TOOLING_UNAVAILABLE'
    if stage == 'collection':
        return 'COLLECTION_FAILED'
    return 'TEST_FAILED'


def _run(command: list[str], *, env: dict[str, str] | None = None,
         timeout: int) -> int:
    try:
        return subprocess.run(
            command, cwd=ROOT, env=env, timeout=timeout,
            check=False).returncode
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127


def _emit(status: str, detail: str) -> int:
    if status not in VALIDATION_STATUSES:
        raise ValueError(f'unsupported validation status {status}')
    stream = sys.stdout if status == 'PASS' else sys.stderr
    print(f'VALIDATION_STATUS={status}', file=stream)
    print(f'VALIDATION_DETAIL={detail}', file=stream)
    return 0 if status == 'PASS' else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--python', default='python3.12',
        help='trusted Python 3.12 interpreter used only to create the venv')
    parser.add_argument(
        '--timeout-seconds', type=int, default=3600,
        help='per-stage timeout (60..14400 seconds)')
    args = parser.parse_args(argv)
    if not 60 <= args.timeout_seconds <= 14_400:
        return _emit('TOOLING_UNAVAILABLE', 'timeout must be 60..14400 seconds')
    interpreter = shutil.which(args.python)
    if not interpreter:
        return _emit(
            'TOOLING_UNAVAILABLE', f'Python interpreter not found: {args.python}')

    probe = subprocess.run(
        [interpreter, '-c',
         'import sys;raise SystemExit(0 if sys.version_info[:2]==(3,12) else 1)'],
        cwd=ROOT, check=False)
    if probe.returncode != 0:
        return _emit(
            'TOOLING_UNAVAILABLE', f'{interpreter} is not Python 3.12')

    with tempfile.TemporaryDirectory(prefix='binana-validation-') as temporary:
        venv = Path(temporary) / 'venv'
        rc = _run(
            [interpreter, '-m', 'venv', str(venv)],
            timeout=args.timeout_seconds)
        if rc == 124:
            return _emit('TIMEOUT', 'venv creation timed out')
        if rc != 0:
            return _emit('TOOLING_UNAVAILABLE', 'venv creation failed')
        python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        for requirements, hashed in (
            (ROOT / 'requirements.services.lock', True),
            (ROOT / 'monitoring/requirements-monitoring.lock', True),
            (ROOT / 'requirements-dev.txt', False),
        ):
            command = [str(python), '-m', 'pip', 'install']
            if hashed:
                command.append('--require-hashes')
            command.extend(['-r', str(requirements)])
            rc = _run(command, timeout=args.timeout_seconds)
            if rc == 124:
                return _emit('TIMEOUT', f'dependency install timed out: {requirements.name}')
            if rc != 0:
                return _emit(
                    'TOOLING_UNAVAILABLE',
                    f'dependency install failed: {requirements.name}')

        env = os.environ.copy()
        bindir = str(python.parent)
        env['PATH'] = bindir + os.pathsep + env.get('PATH', '')
        env['PYTHONUTF8'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        rc = _run(
            [str(python), '-m', 'pytest', '--collect-only', '-q'],
            env=env, timeout=args.timeout_seconds)
        if rc == 124:
            return _emit('TIMEOUT', 'pytest collection timed out')
        status = classify_returncode('collection', rc)
        if status != 'PASS':
            return _emit(status, 'pytest collection failed')

        bash = shutil.which('bash', path=env.get('PATH'))
        if not bash:
            return _emit('TOOLING_UNAVAILABLE', 'POSIX bash is unavailable')
        rc = _run(
            [bash, str(ROOT / 'deploy/verify_release.sh')],
            env=env, timeout=args.timeout_seconds)
        if rc == 124:
            return _emit('TIMEOUT', 'release verification timed out')
        status = classify_returncode('tests', rc)
        if status != 'PASS':
            return _emit(status, 'release verification failed')
        return _emit('PASS', 'isolated release verification passed')


if __name__ == '__main__':
    raise SystemExit(main())
