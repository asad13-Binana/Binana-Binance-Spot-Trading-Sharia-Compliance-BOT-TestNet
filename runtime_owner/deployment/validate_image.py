"""Verify the recovered bytes and run offline tests; never start the bot."""
import glob
import hashlib
import json
from pathlib import Path
import sys
import unittest


def main():
    root = Path('/freqtrade')
    manifest = json.loads((root / 'recovery_validation/PROVENANCE.json').read_text())
    for relative, expected in manifest['files'].items():
        if relative.startswith('owner/'):
            destination = root / relative.removeprefix('owner/')
        elif relative.startswith('tests/'):
            destination = root / 'binana_tests' / relative.removeprefix('tests/')
        else:
            destination = root / relative
        content = destination.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected['sha256'] or len(content) != expected['bytes']:
            raise RuntimeError(f'Provenance mismatch: {relative}')
    sys.path[:0] = [str(root), *glob.glob('/home/ftuser/.local/lib/python*/site-packages')]
    suite = unittest.defaultTestLoader.discover(str(root / 'binana_tests'))
    count = suite.countTestCases()
    if count < manifest['validation']['tests']:
        raise RuntimeError(f'Incomplete test discovery: {count}')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({'tests': result.testsRun, 'successful': result.wasSuccessful(),
                      'authenticated_exchange_acceptance': False}))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
