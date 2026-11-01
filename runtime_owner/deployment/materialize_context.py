"""Create an allowlisted, hash-checked owner build context without host secrets."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil


def materialize(source: Path, destination: Path, *, include_retained_dockerfile=True):
    source = source.resolve()
    manifest_path=source / 'CANDIDATE_MANIFEST.json'
    if not manifest_path.exists():
        manifest_path=source / 'PROVENANCE.json'
    manifest = json.loads(manifest_path.read_text())
    verified = []
    for relative, expected in manifest['files'].items():
        relative_path = PurePosixPath(relative)
        if (relative_path.is_absolute() or '..' in relative_path.parts
                or '\\' in relative or relative_path.parts[0] not in {'owner', 'tests', 'runtime-fixes'}):
            raise ValueError(f'Invalid manifest path: {relative}')
        path = source / relative
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != source.parent):
            raise ValueError(f'Symlink source: {relative}')
        content = path.read_bytes()
        if len(content) != expected['bytes'] or hashlib.sha256(content).hexdigest() != expected['sha256']:
            raise ValueError(f'Provenance mismatch: {relative}')
        verified.append((relative, content))
    # A valid hash for every listed file cannot detect a required patch omitted
    # from the list. Bind the complete runtime source set before exporting it.
    actual = set()
    for directory in ('owner', 'tests', 'runtime-fixes'):
        for path in (source / directory).rglob('*'):
            relative = path.relative_to(source)
            if '__pycache__' in relative.parts or path.suffix == '.pyc':
                continue
            if path.is_symlink():
                raise ValueError(f'Symlink source: {relative.as_posix()}')
            if path.is_file():
                actual.add(relative.as_posix())
    if actual != set(manifest['files']):
        raise ValueError('Manifest file set mismatch')
    # Never merge a build context with existing files, databases or configuration.
    destination.mkdir(parents=True, exist_ok=False)
    for relative, content in verified:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    shutil.copyfile(manifest_path, destination / 'PROVENANCE.json')
    deployment_files = ['validate_image.py', 'release_authority.py']
    if include_retained_dockerfile:
        deployment_files.insert(0, 'Dockerfile')
    for name in deployment_files:
        shutil.copyfile(source / 'deployment' / name, destination / name)
    return {'source_files': len(verified), 'context': str(destination.resolve()),
            'authenticated_exchange_acceptance': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(json.dumps(materialize(Path(__file__).resolve().parents[1], args.destination)))


if __name__ == '__main__':
    main()
