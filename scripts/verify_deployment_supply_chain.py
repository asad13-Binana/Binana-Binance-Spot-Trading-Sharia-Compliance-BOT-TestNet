"""Validate immutable inputs for the standalone Oracle bundle; no host changes."""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA256_REF = re.compile(r'@sha256:[0-9a-f]{64}(?:\s|$)')
HASH_OPTION = re.compile(r'--hash=sha256:[0-9a-f]{64}(?:\s|$)')
HASH_TOKEN = re.compile(r'--hash=([^\s]+)')
HASH_VALUE = re.compile(r'sha256:[0-9a-f]{64}$')
DOCKERFILES = ('runtime_owner/deployment/Dockerfile.clean', 'runtime_owner/deployment/oracle_services.Dockerfile')
LOCKS = ('requirements.services.lock', 'requirements.host.lock', 'monitoring/requirements-monitoring.lock',
         'runtime_owner/deployment/requirements.owner.lock', 'runtime_owner/deployment/requirements.build.lock')


def _logical_requirements(path: Path) -> list[str]:
    records = []
    current = ''
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        continued = line.endswith('\\')
        current = (current + ' ' + (line[:-1] if continued else line)).strip()
        if not continued:
            records.append(current)
            current = ''
    if current:
        records.append(current)
    return records


def deployment_supply_chain_errors(root: Path = ROOT) -> list[str]:
    errors = []
    for relative in DOCKERFILES:
        path = root / relative
        if not path.is_file():
            errors.append(relative + ' is missing')
            continue
        source = path.read_text(encoding='utf-8')
        bases = [line.strip() for line in source.splitlines() if line.strip().startswith('FROM ')]
        if not bases or any(not SHA256_REF.search(line) for line in bases):
            errors.append(relative + ' base image is not pinned by sha256 digest')
        if '--require-hashes' not in source:
            errors.append(relative + ' does not enforce pip --require-hashes')
    for relative in LOCKS:
        path = root / relative
        if not path.is_file():
            errors.append(relative + ' is missing')
            continue
        records = _logical_requirements(path)
        if not records:
            errors.append(relative + ' contains no dependency records')
        if any(not HASH_OPTION.search(record) for record in records):
            errors.append(relative + ' has requirement records without sha256 hashes')
        if any(not HASH_VALUE.fullmatch(token) for record in records for token in HASH_TOKEN.findall(record)):
            errors.append(relative + ' has malformed or non-sha256 hash options')
    installer = root / 'deploy/install_monitoring.sh'
    if not installer.is_file() or '--require-hashes' not in installer.read_text(encoding='utf-8'):
        errors.append('monitoring installer does not enforce pip --require-hashes')
    return errors


def main():
    errors = deployment_supply_chain_errors()
    if errors:
        print('Oracle source supply-chain verification failed:\n' + '\n'.join(errors), file=sys.stderr)
        return 1
    print('Oracle source supply-chain verification passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
