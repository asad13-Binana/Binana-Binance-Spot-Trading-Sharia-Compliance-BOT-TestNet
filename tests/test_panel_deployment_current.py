"""Read-only deployment UI must use fresh owner evidence, not rollout receipts."""
import ast
import hashlib
import json
import math
import re
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

PANEL = Path(__file__).resolve().parents[1] / 'runtime_owner/runtime-fixes/telegram/runtime_stable_panel.py'
IMAGE = 'sha256:' + 'a' * 64
COMMIT = 'b' * 40


def render(*, owner_changes=None, deployment_changes=None, publication=None, api_missing=False, view='_r6_deploy'):
    deployment = {'image': IMAGE, 'schema': 'binana-deployment-v1'}
    owner = {
        'release_approved': True, 'blockers': [], 'execution_environment': 'TESTNET',
        'generated_at': time.time() - 1, 'valid_until': time.time() + 30,
        'deployment_sha256': hashlib.sha256(json.dumps(deployment).encode()).hexdigest(),
    }
    owner.update(owner_changes or {})
    deployment.update(deployment_changes or {})
    files = {
        'deployment_status.json': {'status': 'TESTNET_RUNTIME_VERIFIED_R17', 'active_protected_positions': 4, 'github_updated': False},
        'owner_readiness.json': owner,
        'certificate.json': {'payload': {'image': 'sha256:' + 'c' * 64, 'valid_until': 1}},
        'source_publication.json': publication or {'owner_image': IMAGE, 'commit': COMMIT},
    }
    namespace = {
        'json': json, 'time': time, 're': re, 'math': math, 'hashlib': hashlib,
        'OWNER_RUNTIME': Path('/app/shared/freqtrade/runtime'),
        'base': SimpleNamespace(
            Path=lambda path: SimpleNamespace(name=Path(path).name, read_bytes=lambda: json.dumps(deployment).encode()),
            RUNTIME=Path('/runtime'),
            read_json=lambda path, default=None: files.get(path.name, default),
        ),
        '_lifecycle_rows': lambda *args: [], '_effective_trade_size': lambda: 250,
        '_ft_data': lambda method, endpoint: None if api_missing else {
            '/show_config': {'state': 'running', 'strategy': 'BinanaNfiSpot', 'timeframe': '5m'},
            '/whitelist': {'whitelist': ['ADA/USDT', 'LTC/USDT']},
            '/status': [],
        }.get(endpoint),
    }
    tree = ast.parse(PANEL.read_text(encoding='utf-8'))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {'_r6_deploy', '_owner_release_evidence', '_signal_runtime_summary',
                                   '_current_signal_history', '_pretty_signals'}]
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(PANEL), 'exec'), namespace)
    return namespace[view]()


class CurrentDeployment(unittest.TestCase):
    def test_current_owner_replaces_historical_claims_and_unrelated_certificate(self):
        result = render()
        self.assertIn('State: RUNNING', result)
        self.assertIn('Open positions: 0', result)
        self.assertIn('Release approved: YES', result)
        self.assertIn('Qualified owner image: ' + IMAGE, result)
        self.assertIn('Published source commit (host record): ' + COMMIT, result)
        self.assertNotIn('TESTNET_RUNTIME_VERIFIED_R17', result)
        self.assertNotIn('github_updated', result)

    def test_missing_api_is_unknown(self):
        result = render(api_missing=True)
        self.assertIn('State: UNKNOWN', result)
        self.assertIn('Open positions: UNKNOWN', result)

    def test_stale_and_future_evidence_are_unknown(self):
        for change in ({'valid_until': 1}, {'generated_at': time.time() + 60}):
            with self.subTest(change=change):
                result = render(owner_changes=change)
                self.assertIn('Release approved: UNKNOWN', result)
                self.assertIn('Qualified owner image: UNKNOWN', result)
                self.assertIn('Published source commit (host record): UNVERIFIED', result)

    def test_current_explicit_rejection_is_no(self):
        result = render(owner_changes={'release_approved': False, 'blockers': ['PUBLIC_SPOT_DATA_UNAVAILABLE']})
        self.assertIn('Release approved: NO', result)
        self.assertIn('Release blockers: PUBLIC_SPOT_DATA_UNAVAILABLE', result)

    def test_mismatched_deployment_is_not_the_current_image(self):
        result = render(deployment_changes={'image': 'sha256:' + 'd' * 64})
        self.assertIn('Qualified owner image: UNKNOWN', result)
        self.assertIn('Published source commit (host record): UNVERIFIED', result)

    def test_unknown_or_invalid_publication_is_unverified(self):
        for publication in (
            {'owner_image': 'UNKNOWN', 'commit': COMMIT},
            {'owner_image': 'sha256:' + 'd' * 64, 'commit': COMMIT},
            {'owner_image': IMAGE, 'commit': 'malformed'},
        ):
            with self.subTest(publication=publication):
                self.assertIn('Published source commit (host record): UNVERIFIED', render(publication=publication))

    def test_unknown_publication_cannot_bind_unknown_owner(self):
        result = render(owner_changes={'valid_until': 1}, publication={'owner_image': 'UNKNOWN', 'commit': COMMIT})
        self.assertIn('Published source commit (host record): UNVERIFIED', result)

    def test_empty_signal_history_shows_current_scanning_context(self):
        result = render(view='_pretty_signals')
        self.assertIn('Owner state: RUNNING', result)
        self.assertIn('Eligible active pairs: 2', result)
        self.assertIn('Strategy: BinanaNfiSpot | timeframe: 5m', result)
        self.assertIn('Count in the last hour: 0', result)
        self.assertIn('does not guarantee an entry signal', result)

    def test_signal_context_does_not_invent_zero_pairs_when_api_is_down(self):
        result = render(view='_pretty_signals', api_missing=True)
        self.assertIn('Owner state: UNKNOWN', result)
        self.assertIn('Eligible active pairs: UNKNOWN', result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
