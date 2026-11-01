"""Verify the recovered bytes and run offline tests; never start the bot."""
import ast
import glob
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from collections import Counter


def test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id()


def require_tests(suite, required):
    counts=Counter(test_ids(suite))
    if not required or len(required)!=len(set(required)):
        raise RuntimeError('Required test identity manifest is missing or duplicated')
    missing=set(required)-counts.keys()
    duplicates=[key for key,count in counts.items() if count!=1]
    if missing or duplicates:
        raise RuntimeError(f'Invalid test collection: missing={sorted(missing)} duplicates={duplicates}')
    return counts


def configure_test_environment(root, manifest):
    """Select offline fixtures from immutable verified bytes, never runtime overrides."""
    relative = 'owner/freqtrade/binana/package_profile.py'
    expected = manifest.get('files', {}).get(relative)
    if expected is None:
        raise RuntimeError('VERIFIED_PACKAGE_PROFILE_REQUIRED')
    raw = (root / relative.removeprefix('owner/')).read_bytes()
    if (len(raw) != expected['bytes']
            or hashlib.sha256(raw).hexdigest() != expected['sha256']):
        raise RuntimeError('PACKAGE_PROFILE_PROVENANCE_MISMATCH')
    declarations = [node for node in ast.parse(raw).body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == 'PACKAGE_ENVIRONMENT']
    if (len(declarations) != 1 or not isinstance(declarations[0].value, ast.Constant)
            or declarations[0].value.value not in {'testnet', 'live'}):
        raise RuntimeError('INVALID_PACKAGE_ENVIRONMENT')
    mode = declarations[0].value.value
    supplied = os.environ.get('BINANA_TEST_EXPECTED_MODE')
    if supplied is not None and supplied != mode:
        raise RuntimeError('TEST_EXPECTED_MODE_MISMATCH')
    os.environ['BINANA_TEST_EXPECTED_MODE'] = mode
    return mode


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
    package_mode = configure_test_environment(root, manifest)
    sys.path[:0] = [str(root), *glob.glob('/home/ftuser/.local/lib/python*/site-packages')]
    suite = unittest.defaultTestLoader.discover(str(root / 'binana_tests'))
    required=manifest['validation'].get('required_test_ids',[])
    require_tests(suite,required)
    count = suite.countTestCases()
    if count < manifest['validation']['tests']:
        raise RuntimeError(f'Incomplete test discovery: {count}')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    successful=result.wasSuccessful() and not result.skipped
    print(json.dumps({'tests': result.testsRun, 'successful': successful,
                      'skipped':[test.id() for test,reason in result.skipped],
                      'package_environment': package_mode,
                      'authenticated_exchange_acceptance': False}))
    return 0 if successful else 1


if __name__ == '__main__':
    raise SystemExit(main())
