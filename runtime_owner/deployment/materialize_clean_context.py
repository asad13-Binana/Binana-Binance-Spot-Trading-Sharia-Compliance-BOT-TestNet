"""Build a secret-free owner context from pinned public source and reviewed patches."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import urllib.request

from materialize_context import materialize

UPSTREAM_COMMIT = '1f394eaebc2f46a83d26971388628707802f8602'
UPSTREAM_SHA256 = 'a453638c8ad59f8655036eaabd0858cb432331e70caa259976299e5c59108c89'
UPSTREAM_URL = 'https://codeload.github.com/freqtrade/freqtrade/tar.gz/' + UPSTREAM_COMMIT
ROOT_FILES = {'pyproject.toml', 'README.md', 'LICENSE', 'MANIFEST.in',
              'requirements.txt', 'requirements-hyperopt.txt'}


def source_files(raw):
    if hashlib.sha256(raw).hexdigest() != UPSTREAM_SHA256:
        raise ValueError('Upstream archive hash mismatch')
    result = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or '\\' in member.name:
                raise ValueError('Unsafe upstream archive path')
            relative = PurePosixPath(*path.parts[1:])
            if not relative.parts:
                continue
            allowed = str(relative) in ROOT_FILES or relative.parts[0] in {'freqtrade', 'ft_client'}
            if not allowed or member.isdir():
                continue
            # Upstream's client LICENSE is the sole documented link. Copy the
            # pinned archive's regular license bytes; never create a symlink.
            if (str(relative) == 'ft_client/LICENSE' and member.issym()
                    and member.linkname == '../LICENSE'):
                member = archive.getmember(path.parts[0] + '/LICENSE')
            if not member.isfile() or str(relative) in result:
                raise ValueError('Non-regular or duplicate upstream member')
            result[str(relative)] = archive.extractfile(member).read()
    return result


def clean_context(source, destination, raw):
    files = source_files(raw)  # Validate before creating any destination.
    result = materialize(source, destination)
    upstream = destination / 'upstream'
    for relative, content in files.items():
        target = upstream / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    for name in ('requirements.owner.lock', 'requirements.build.lock', 'Dockerfile.clean'):
        shutil.copyfile(source / 'deployment' / name, destination / name)
    result.update(upstream_commit=UPSTREAM_COMMIT, upstream_archive_sha256=UPSTREAM_SHA256,
                  upstream_files=len(files), retained_aws_image_required=False)
    (destination / 'CLEAN_SOURCE.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--archive', type=Path, help='Optional previously downloaded pinned archive')
    args = parser.parse_args()
    if args.archive:
        raw = args.archive.read_bytes()
    else:
        with urllib.request.urlopen(UPSTREAM_URL, timeout=120) as response:
            raw = response.read(100_000_001)
        if len(raw) > 100_000_000:
            raise ValueError('Upstream archive exceeds size limit')
    print(json.dumps(clean_context(Path(__file__).resolve().parents[1], args.destination, raw)))


if __name__ == '__main__':
    main()
