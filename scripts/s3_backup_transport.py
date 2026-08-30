"""Optional encrypted-backup transport using EC2 instance role credentials only.

Requires an operator-installed AWS CLI v2 and an existing private bucket. Does
not create cloud resources, manage IAM, delete objects, or read bot credentials.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
from pathlib import Path
import re
import subprocess


def role_environment():
    # Remove credential files, profiles, endpoint overrides, web identities,
    # container credentials and static environment credentials from the chain.
    allowed = ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP', 'LANG')
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull,
               AWS_EC2_METADATA_DISABLED='false', AWS_EC2_METADATA_V1_DISABLED='true',
               AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off')
    return env


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.digest()


def aws(region, *args):
    proc = subprocess.run(
        ['aws', '--no-cli-pager', '--region', region,
         '--cli-connect-timeout', '10', '--cli-read-timeout', '60', 's3api', *args],
        env=role_environment(), capture_output=True, timeout=180, check=False)
    if proc.returncode:
        # AWS output may include account/bucket details; do not publish it in
        # monitoring status or propagate it into a Telegram error message.
        raise RuntimeError('S3 backup operation failed: ' + args[0])


def transfer(region, bucket, owner, key, source=None, *, download_only=False):
    if not re.fullmatch(r'[a-z]{2}(?:-gov)?-[a-z]+-[0-9]', region):
        raise ValueError('invalid AWS region')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
        raise ValueError('invalid S3 bucket')
    if not re.fullmatch(r'[0-9]{12}', owner):
        raise ValueError('expected bucket owner account is required')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,255}', key) or '..' in key:
        raise ValueError('invalid S3 object key')
    base = ('--bucket', bucket, '--expected-bucket-owner', owner)
    if download_only:
        if source is None:
            raise ValueError('download destination required')
        target = Path(source)
        if target.exists() or target.is_symlink():
            raise ValueError('download refuses to overwrite existing state')
        aws(region, 'get-object', *base, '--key', key, '--checksum-mode', 'ENABLED', str(target))
        if not target.is_file() or target.is_symlink():
            raise ValueError('download did not produce a regular file')
        return  # caller verifies encrypted checksum + age integrity before extraction
    if source is None:
        aws(region, 'head-bucket', *base)
        return
    source = Path(source)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 5 * 1024**3:
        raise ValueError('backup must be a regular file within the single-PUT limit')
    expected = digest(source)
    encoded = base64.b64encode(expected).decode('ascii')
    aws(region, 'put-object', *base, '--key', key, '--body', str(source),
        '--if-none-match', '*', '--checksum-algorithm', 'SHA256',
        '--checksum-sha256', encoded)
    # File is inside the caller's private staging directory. Refuse overwrite.
    downloaded = source.with_name(source.name + '.verified-download')
    if downloaded.exists() or downloaded.is_symlink():
        raise ValueError('verification destination already exists')
    aws(region, 'get-object', *base, '--key', key, '--checksum-mode', 'ENABLED', str(downloaded))
    if not downloaded.is_file() or downloaded.is_symlink() or digest(downloaded) != expected:
        raise RuntimeError('S3 downloaded checksum mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('region')
    parser.add_argument('bucket')
    parser.add_argument('owner')
    parser.add_argument('key')
    parser.add_argument('source', nargs='?', type=Path)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    transfer(args.region, args.bucket, args.owner, args.key, args.source,
             download_only=args.download)


if __name__ == '__main__':
    main()
