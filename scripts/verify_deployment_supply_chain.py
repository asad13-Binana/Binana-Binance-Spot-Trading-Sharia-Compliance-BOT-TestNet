"""Fail closed when mutable dependencies could reach an Oracle deployment.

The offline/source gate may run without registry access, so digest and wheel
hash resolution are separate preparation steps.  The Oracle installer calls
this verifier after extracting the manifest-verified artifact and before it
changes any active release.  Missing pins therefore remain visible during
development but can never be silently promoted.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA256_REF = re.compile(r'@sha256:[0-9a-f]{64}(?:\s|$)')
HASH_OPTION = re.compile(r'--hash=sha256:[0-9a-f]{64}(?:\s|$)')
HASH_TOKEN = re.compile(r'--hash=([^\s]+)')
HASH_VALUE = re.compile(r'sha256:[0-9a-f]{64}$')


def _logical_requirements(path: Path) -> list[str]:
    records: list[str] = []
    current = ''
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        continued = line.endswith('\\')
        part = line[:-1].strip() if continued else line
        current = f'{current} {part}'.strip()
        if not continued:
            records.append(current)
            current = ''
    if current:
        records.append(current)
    return records


def deployment_supply_chain_errors(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    dockerfile = (root / 'Dockerfile.services').read_text(encoding='utf-8')
    base = next((line.strip() for line in dockerfile.splitlines()
                 if line.strip().startswith('FROM ')), '')
    if not SHA256_REF.search(base):
        errors.append('Dockerfile.services base image is not pinned by sha256 digest')

    compose = (root / 'docker-compose.yml').read_text(encoding='utf-8')
    freqtrade = next((line.strip() for line in compose.splitlines()
                      if line.strip().startswith(
                          'image: freqtradeorg/freqtrade:')), '')
    if not SHA256_REF.search(freqtrade):
        errors.append('Freqtrade runtime image is not pinned by sha256 digest')

    locks = (
        root / 'requirements.services.lock',
        root / 'monitoring/requirements-monitoring.lock',
    )
    for lock in locks:
        records = _logical_requirements(lock)
        unhashed = [record for record in records
                    if not HASH_OPTION.search(record)]
        if unhashed:
            errors.append(
                f'{lock.relative_to(root).as_posix()} has {len(unhashed)} '
                'requirement record(s) without sha256 hashes')
        malformed = [token for record in records
                     for token in HASH_TOKEN.findall(record)
                     if not HASH_VALUE.fullmatch(token)]
        if malformed:
            errors.append(
                f'{lock.relative_to(root).as_posix()} has {len(malformed)} '
                'malformed or non-sha256 hash option(s)')

    if '--require-hashes' not in dockerfile:
        errors.append('Dockerfile.services does not enforce pip --require-hashes')
    monitoring_installer = (
        root / 'deploy/install_monitoring.sh').read_text(encoding='utf-8')
    if '--require-hashes' not in monitoring_installer:
        errors.append('monitoring installer does not enforce pip --require-hashes')
    return errors


def main() -> None:
    errors = deployment_supply_chain_errors()
    if errors:
        print('DEPLOYMENT SUPPLY-CHAIN BLOCKED:', file=sys.stderr)
        for error in errors:
            print(f'  - {error}', file=sys.stderr)
        print(
            'Resolve and review immutable image and distribution hashes before '
            'Oracle deployment.', file=sys.stderr)
        raise SystemExit(1)
    print('deployment supply-chain verification passed')


if __name__ == '__main__':
    main()
