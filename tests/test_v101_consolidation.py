from __future__ import annotations
"""V10.1 consolidation regression tests.

These tests pin the two release blockers that kept V8.1 in BLOCKED state
(interpreter-dependent strategy hashes; vulnerable requests pin) and the
review conclusions from BINANCE_BOT_FINAL_VERDICT_IMP.md that must never
regress: Freqtrade stays signal-only, the preserved core stays byte-identical,
and dangerous work is claimed durably before any exchange side effect.
"""
import ast
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.common.strategy_fingerprint import (
    fingerprints, method_source_segment, source_hash, token_hash,
)

STRATEGY = ROOT / 'freqtrade/user_data/strategies/IctSmcStrategy.py'
METHODS = [
    'populate_indicators_5m', 'populate_indicators',
    'populate_entry_trend', 'populate_exit_trend',
]


class FingerprintPortabilityTests(unittest.TestCase):
    """The V8.1 blocker: ast.dump() hashes changed across interpreters.

    The canonical fingerprints must (1) never use AST serialization,
    (2) survive cosmetic edits in the token form, and (3) flag any
    logical edit in both forms.
    """

    def test_no_ast_dump_serialization_in_integrity_paths(self):
        import io, tokenize
        for rel in ('tests/test_v81.py', 'scripts/build_manifest.py',
                    'scripts/verify_manifest.py', 'services/common/strategy_fingerprint.py'):
            source = (ROOT / rel).read_text(encoding='utf-8')
            code_tokens = [
                tok.string for tok in tokenize.generate_tokens(io.StringIO(source).readline)
                if tokenize.tok_name[tok.type] not in {'COMMENT', 'STRING'}
            ]
            triples = list(zip(code_tokens, code_tokens[1:], code_tokens[2:]))
            self.assertNotIn(('ast', '.', 'dump'), triples,
                             rel + ' must not serialize AST nodes for hashing')

    def test_source_segment_is_exact_text(self):
        segment = method_source_segment(STRATEGY, 'IctSmcStrategy', 'populate_exit_trend')
        self.assertTrue(segment.startswith('    def populate_exit_trend'))
        self.assertIn('lost_vwap_5m_bear', segment)
        raw = STRATEGY.read_text(encoding='utf-8')
        self.assertIn(segment.rstrip('\n'), raw)

    def test_decorators_are_part_of_the_fingerprint(self):
        segment = method_source_segment(STRATEGY, 'IctSmcStrategy', 'populate_indicators_5m')
        self.assertTrue(segment.lstrip().startswith('@informative("5m")'))

    def test_token_hash_ignores_comments_but_not_logic(self):
        with tempfile.TemporaryDirectory() as td:
            original = STRATEGY.read_text(encoding='utf-8')
            segment = method_source_segment(STRATEGY, 'IctSmcStrategy', 'populate_exit_trend')
            cosmetic = original.replace(
                segment,
                segment.rstrip('\n') + '  # cosmetic trailing comment\n', 1,
            )
            cosmetic_path = Path(td) / 'cosmetic.py'
            cosmetic_path.write_text(cosmetic, encoding='utf-8')
            self.assertEqual(
                token_hash(cosmetic_path, 'IctSmcStrategy', 'populate_exit_trend'),
                token_hash(STRATEGY, 'IctSmcStrategy', 'populate_exit_trend'),
            )
            self.assertNotEqual(
                source_hash(cosmetic_path, 'IctSmcStrategy', 'populate_exit_trend'),
                source_hash(STRATEGY, 'IctSmcStrategy', 'populate_exit_trend'),
            )
            logical = original.replace('lost_vwap_5m_bear', 'renamed_exit_tag', 1)
            logical_path = Path(td) / 'logical.py'
            logical_path.write_text(logical, encoding='utf-8')
            self.assertNotEqual(
                token_hash(logical_path, 'IctSmcStrategy', 'populate_exit_trend'),
                token_hash(STRATEGY, 'IctSmcStrategy', 'populate_exit_trend'),
            )

    def test_fingerprinted_methods_contain_no_fstrings(self):
        # The 3.12 retokenization applies to f-strings; the protected methods
        # must stay f-string-free for the token stream to be provably stable
        # on every supported interpreter.
        for method in METHODS:
            segment = method_source_segment(STRATEGY, 'IctSmcStrategy', method)
            tree = ast.parse(segment.strip() and ('class IctSmcStrategy:\n' + segment) or segment)
            joined = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)]
            self.assertEqual(joined, [], method)

    def test_manifest_expected_fingerprints_match_strategy_file(self):
        from scripts.build_manifest import EXPECTED_SIGNAL_FINGERPRINTS
        self.assertEqual(
            fingerprints(STRATEGY, 'IctSmcStrategy', METHODS),
            EXPECTED_SIGNAL_FINGERPRINTS,
        )


class DependencyPolicyTests(unittest.TestCase):
    """Exact runtime locks must not regress below known security fixes."""

    def test_every_matrix_interpreter_resolves_exactly_one_rpds_py(self):
        """DEP-MARKER-001: CI installs the monitoring lock on every interpreter
        in the matrix. rpds-py==2026.6.3 declares Requires-Python >=3.11, so the 3.10
        leg failed at install with 'No matching distribution found' -- before
        reaching any test or audit. The lock now carries an environment-marker
        pair; this asserts each matrix interpreter selects exactly one of them,
        so neither an unsatisfiable nor an ambiguous set can ship again."""
        from packaging.markers import Marker
        from packaging.version import Version

        workflow = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
        versions = re.findall(r"'(3\.\d+)'", workflow)
        matrix = sorted({v for v in versions if v.startswith('3.')})
        self.assertTrue(matrix, 'no python-version matrix parsed from ci.yml')

        lock = (ROOT / 'monitoring/requirements-monitoring.lock').read_text(encoding='utf-8')
        entries = []
        for line in lock.splitlines():
            line = line.split('#')[0].strip()
            if line.endswith('\\'):
                line = line[:-1].strip()
            if not line.startswith('rpds-py'):
                continue
            requirement, _, marker = line.partition(';')
            entries.append((requirement.strip(), marker.strip()))
        self.assertTrue(entries, 'no rpds-py pin found in the monitoring lock')

        # A pin with no marker applies to EVERY interpreter, which is exactly
        # how a 3.11+-only release reached the 3.10 leg. Requiring an explicit
        # marker is what makes this guard fail on the original defect.
        unmarked = [req for req, marker in entries if not marker]
        self.assertEqual(
            unmarked, [],
            msg=f'rpds-py pins must be marker-qualified per interpreter; '
                f'unqualified: {unmarked}')

        chosen = {}
        for version in matrix:
            selected = [req for req, marker in entries
                        if Marker(marker).evaluate({'python_version': version})]
            self.assertEqual(
                len(selected), 1,
                msg=f'python {version} selects {len(selected)} rpds-py pins '
                    f'({selected}); each interpreter must select exactly one')
            chosen[version] = Version(selected[0].split('==', 1)[1])

        # Counting selections is not enough: reversing the two mappings still
        # yields exactly one pin per interpreter while handing 3.10 the release
        # that dropped 3.10. Markers are only needed because newer releases
        # raise their floor, so the selected version must never DECREASE as the
        # interpreter version rises.
        ordered = sorted(chosen, key=Version)
        for older, newer in zip(ordered, ordered[1:]):
            self.assertLessEqual(
                chosen[older], chosen[newer],
                msg=f'python {older} selects rpds-py {chosen[older]} but python '
                    f'{newer} selects {chosen[newer]}; an older interpreter must '
                    f'never be given a newer release than a newer interpreter')

    def test_requests_pin_is_at_or_above_first_fixed_version(self):
        text = (ROOT / 'requirements.services.txt').read_text(encoding='utf-8')
        match = re.search(r'^requests==(\d+)\.(\d+)\.(\d+)\s*$', text, re.M)
        self.assertIsNotNone(match, 'requests must stay exactly pinned')
        version = tuple(int(part) for part in match.groups())
        self.assertGreaterEqual(version, (2, 32, 4),
                                'requests pin below first advisory-fixed version')

    def test_installed_requests_matches_pin_when_available(self):
        try:
            import requests
        except Exception:
            self.skipTest('requests not installed in this environment')
        text = (ROOT / 'requirements.services.txt').read_text(encoding='utf-8')
        match = re.search(r'^requests==([0-9.]+)\s*$', text, re.M)
        self.assertEqual(requests.__version__, match.group(1))

    def test_aiohttp_lock_is_at_or_above_current_security_fix(self):
        text = (ROOT / 'requirements.services.lock').read_text(encoding='utf-8')
        match = re.search(r'^aiohttp==(\d+)\.(\d+)\.(\d+)\s*\\?\s*$', text, re.M)
        self.assertIsNotNone(match, 'aiohttp must stay exactly pinned in the resolved lock')
        version = tuple(int(part) for part in match.groups())
        self.assertGreaterEqual(
            version, (3, 14, 3),
            'aiohttp pin is below the fix for PYSEC-2026-3545/3546/3547',
        )

    def test_installed_aiohttp_matches_lock_when_available(self):
        try:
            import aiohttp
        except Exception:
            self.skipTest('aiohttp not installed in this environment')
        text = (ROOT / 'requirements.services.lock').read_text(encoding='utf-8')
        match = re.search(r'^aiohttp==([0-9.]+)\s*\\?\s*$', text, re.M)
        self.assertEqual(aiohttp.__version__, match.group(1))

    def test_cryptography_lock_is_at_or_above_current_security_fix(self):
        text = (ROOT / 'monitoring/requirements-monitoring.lock').read_text(encoding='utf-8')
        match = re.search(r'^cryptography==(\d+)\.(\d+)\.(\d+)\s*\\?\s*$', text, re.M)
        self.assertIsNotNone(match, 'cryptography must stay exactly pinned')
        version = tuple(int(part) for part in match.groups())
        self.assertGreaterEqual(
            version, (50, 0, 0),
            'cryptography pin is below the fix for PYSEC-2026-3552',
        )

    def test_installed_cryptography_matches_lock_when_available(self):
        try:
            from importlib.metadata import version as installed_version
            actual = installed_version('cryptography')
        except Exception:
            self.skipTest('cryptography not installed in this environment')
        text = (ROOT / 'monitoring/requirements-monitoring.lock').read_text(encoding='utf-8')
        match = re.search(r'^cryptography==([0-9.]+)\s*\\?\s*$', text, re.M)
        self.assertEqual(actual, match.group(1))

    def test_hash_locked_runtime_installs_are_separate_from_ci_tooling(self):
        dev = (ROOT / 'requirements-dev.txt').read_text(encoding='utf-8')
        self.assertNotRegex(dev, r'(?m)^\s*(?:-r|--requirement)\s+')
        workflow = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
        self.assertIn(
            '--require-hashes -r requirements.services.lock', workflow)
        self.assertIn(
            '--require-hashes -r monitoring/requirements-monitoring.lock', workflow)
        dockerfile = (ROOT / 'Dockerfile.services').read_text(encoding='utf-8')
        self.assertIn('--require-hashes', dockerfile)
        installer = (ROOT / 'deploy/install_monitoring.sh').read_text(encoding='utf-8')
        self.assertIn('--require-hashes', installer)

    def test_ci_runs_dependency_audit_and_a_python_version_matrix(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
        self.assertIn('pip_audit', workflow)
        for version in ('3.10', '3.11', '3.12', '3.13'):
            self.assertIn(f"'{version}'", workflow)


class VerdictInvariantTests(unittest.TestCase):
    """Consolidation contract items that must survive every future edit."""

    def test_freqtrade_confirm_trade_entry_stays_signal_only(self):
        tree = ast.parse(STRATEGY.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'IctSmcStrategy')
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'confirm_trade_entry')
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        self.assertTrue(returns)
        self.assertTrue(all(
            isinstance(r.value, ast.Constant) and r.value.value is False for r in returns
        ))

    def test_signal_submit_happens_only_after_durable_claim(self):
        source = (ROOT / 'services/execution_sidecar/order_manager.py').read_text(encoding='utf-8')
        claim = source.index('claim_signal')
        submit = source.index('adapter.submit')
        self.assertLess(claim, submit,
                        'durable signal claim must precede exchange submission')

    def test_no_component_other_than_sidecar_gets_trade_secret(self):
        import yaml
        compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        for name, service in compose['services'].items():
            env = service.get('environment', {}) or {}
            if name == 'execution-sidecar':
                self.assertIn('BINANCE_API_SECRET', env)
            else:
                self.assertNotIn('BINANCE_API_SECRET', env, name)

    def test_bnb_and_btc_bases_are_rejected_before_execution(self):
        source = (ROOT / 'services/execution_sidecar/order_manager.py').read_text(encoding='utf-8')
        self.assertIn("{'BNB', 'BTC'}", source)

    def test_release_identity_comes_from_release_version_metadata(self):
        # V102-REM-009: the release label is read from RELEASE_VERSION, so
        # the manifest, the verification banner, and the metadata file can
        # never disagree. The mode gate and live_certified pin are unchanged.
        from scripts.build_manifest import main as _  # noqa: F401  (import proves it loads)
        source = (ROOT / 'scripts/build_manifest.py').read_text(encoding='utf-8')
        self.assertIn("f'{release_version}-{package_mode.upper()}'", source)
        self.assertIn("ROOT / 'RELEASE_VERSION'", source)
        self.assertIn("package_mode not in {'testnet', 'live'}", source)
        self.assertIn("'live_certified': False", source)
        version = (ROOT / 'RELEASE_VERSION').read_text(encoding='utf-8').strip()
        self.assertRegex(version, r'^V\d+\.\d+')
        verify_sh = (ROOT / 'deploy/verify_release.sh').read_text(encoding='utf-8')
        self.assertIn('cat RELEASE_VERSION', verify_sh)


if __name__ == '__main__':
    unittest.main(verbosity=2)
