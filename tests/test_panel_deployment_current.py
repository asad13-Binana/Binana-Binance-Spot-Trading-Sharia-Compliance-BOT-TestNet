"""Read-only deployment UI must use fresh owner evidence, not rollout receipts."""
import ast
import hashlib
import json
import math
import re
import time
import unittest
import tempfile
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

PANEL = Path(__file__).resolve().parents[1] / 'runtime_owner/runtime-fixes/telegram/runtime_stable_panel.py'
IMAGE = 'sha256:' + 'a' * 64
COMMIT = 'b' * 40


def render(*, owner_changes=None, deployment_changes=None, publication=None, api_missing=False, view='_r6_deploy', events=None):
    deployment = {'image': IMAGE, 'schema': 'binana-deployment-v1'}
    owner = {
        'release_approved': True, 'blockers': [], 'execution_environment': 'TESTNET',
        'generated_at': time.time() - 1, 'valid_until': time.time() + 30,
        'scope_id': 'testnet-current',
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
        '_lifecycle_rows': lambda *args, **kwargs: [r for r in events or []
            if (not kwargs.get('scope') or r.get('scope')==kwargs['scope'])
            and (not kwargs.get('event_types') or r.get('type') in kwargs['event_types'])],
        '_effective_trade_size': lambda: 250,
        '_ft_data': lambda method, endpoint: None if api_missing else {
            '/show_config': {'state': 'running', 'strategy': 'BinanaNfiSpot', 'timeframe': '5m'},
            '/whitelist': {'whitelist': ['ADA/USDT', 'LTC/USDT']},
            '/status': [],
        }.get(endpoint),
    }
    tree = ast.parse(PANEL.read_text(encoding='utf-8'))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {'_r6_deploy', '_owner_release_evidence', '_signal_runtime_summary',
                                   '_current_signal_history', '_pretty_signals', '_pretty_last_signal',
                                   '_current_scope_lifecycle_rows', '_signal_decision'}]
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
        self.assertIn('Shown from the last 24 hours: 0', result)
        self.assertIn('does not guarantee an entry signal', result)

    def test_signal_context_does_not_invent_zero_pairs_when_api_is_down(self):
        result = render(view='_pretty_signals', api_missing=True)
        self.assertIn('Owner state: UNKNOWN', result)
        self.assertIn('Eligible active pairs: UNKNOWN', result)

    def test_signal_history_keeps_yesterdays_candidate_and_its_admission_reason(self):
        signal = {'type': 'STRATEGY_SIGNAL', 'scope': 'testnet-current', 'ts': time.time()-7200,
                  'pair': 'FOGO/USDT', 'entry_tag': '61 ', 'candle_time': '2026-10-15 02:40:00+00:00'}
        decision = {**signal, 'type': 'ADMISSION_REJECTED', 'ts': time.time()-7190,
                    'reason': 'CONFLUENCE_NOT_CONFIRMED'}
        result = render(view='_pretty_signals', events=[signal, decision])
        self.assertIn('FOGO/USDT', result)
        self.assertIn('CONFLUENCE_NOT_CONFIRMED', result)
        self.assertIn('last 24 hours', result)

    def test_last_signal_does_not_mix_account_scopes(self):
        signal = {'type': 'STRATEGY_SIGNAL', 'scope': 'testnet-current', 'ts': time.time()-30,
                  'pair': 'FOGO/USDT', 'entry_tag': '61', 'candle_time': '2026-10-15 02:40:00+00:00'}
        foreign = {**signal, 'scope': 'old-account', 'pair': 'WRONG/USDT', 'ts': time.time()-10}
        result = render(view='_pretty_last_signal', events=[signal, foreign])
        self.assertIn('FOGO/USDT', result)
        self.assertNotIn('WRONG/USDT', result)

    def test_distinct_candles_with_same_pair_and_tag_are_not_collapsed(self):
        signal = {'type': 'STRATEGY_SIGNAL', 'scope': 'testnet-current', 'ts': time.time()-30,
                  'pair': 'EPIC/USDT', 'entry_tag': '63', 'candle_time': '2026-10-15 02:40:00+00:00'}
        later = {**signal, 'candle_time': '2026-10-15 02:45:00+00:00', 'ts': time.time()-10}
        result = render(view='_pretty_signals', events=[signal, later])
        self.assertIn('02:40:00', result)
        self.assertIn('02:45:00', result)

    def test_canonical_reader_filters_scope_and_type_before_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder)/'events.jsonl'
            with closing(sqlite3.connect(journal.with_suffix('.sqlite'))) as db:
                db.execute('CREATE TABLE lifecycle_events(seq INTEGER PRIMARY KEY, payload TEXT)')
                rows = [{'type':'STRATEGY_SIGNAL','scope':'testnet-current','pair':'FOGO/USDT'}]
                rows += [{'type':'ENTRY_FILL','scope':'other-account','pair':'WRONG/USDT'}]*501
                db.executemany('INSERT INTO lifecycle_events(payload) VALUES (?)', [(json.dumps(r),) for r in rows])
                db.commit()
            tree = ast.parse(PANEL.read_text(encoding='utf-8'))
            node = next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='_lifecycle_rows')
            ns = {'json':json,'LIFECYCLE_JOURNAL':journal}
            exec(compile(ast.Module(body=[node],type_ignores=[]),str(PANEL),'exec'),ns)
            result = ns['_lifecycle_rows'](20,scope='testnet-current',event_types=('STRATEGY_SIGNAL',))
            self.assertEqual([r['pair'] for r in result], ['FOGO/USDT'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
