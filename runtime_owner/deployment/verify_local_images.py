"""Fail before startup/build if a retained local image is absent or retagged."""
import argparse
import json
from pathlib import Path
import re
import subprocess


def verify(manifest, compose=None, inspect=None):
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError('Empty image identity manifest')
    if compose is not None and set(compose['services']) != set(manifest):
        raise ValueError('Compose services differ from the image identity manifest')
    verified = {}
    for service, item in manifest.items():
        reference, expected = item['reference'], item['image_id']
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', expected):
            raise ValueError(f'Invalid image identity: {service}')
        if compose is not None and compose['services'][service]['image'] != reference:
            raise ValueError(f'Compose reference mismatch: {service}')
        if inspect is None:
            raw = subprocess.check_output(
                ['docker', 'image', 'inspect', reference], text=True, timeout=30
            )
            result = json.loads(raw)
        else:
            result = inspect(reference)
        if len(result) != 1 or result[0]['Id'] != expected:
            raise ValueError(f'Local image identity mismatch: {service}')
        verified[service] = expected
    return verified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('compose', nargs='?', type=Path)
    parser.add_argument('--service')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if args.service:
        manifest = {args.service: manifest[args.service]}
    compose = json.loads(args.compose.read_text()) if args.compose else None
    print(json.dumps(verify(manifest, compose), sort_keys=True))


if __name__ == '__main__':
    main()
