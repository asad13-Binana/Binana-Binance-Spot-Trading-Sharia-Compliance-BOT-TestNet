from __future__ import annotations
"""V102-REM-015 (deep-audit F-03/F-04): the generated audit ledgers must stay
byte-identical to the tree. This runs the same check as
deploy/verify_release.sh, so ledger drift fails the offline unittest gate too
and can never again ship stale (the recurring "N current files absent from the
ledger" independent-audit finding)."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AuditLedgerParityTests(unittest.TestCase):
    def test_ledgers_match_the_tree(self):
        proc = subprocess.run(
            [sys.executable, 'scripts/build_audit_ledgers.py', '--check'],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         msg=(proc.stdout + proc.stderr).strip()
                         + '\nRun: python scripts/build_audit_ledgers.py')

    def test_every_shipped_file_is_in_the_file_ledger(self):
        # Independent restatement of the parity guarantee: the FILE ledger
        # path set must equal the tree's file set (minus caches).
        import csv
        ledger = ROOT / 'docs/audit/FILE_REVIEW_LEDGER.csv'
        with ledger.open(encoding='utf-8') as fh:
            ledger_paths = {row['relative_path'] for row in csv.DictReader(fh)}
        exclude = {'.git', '__pycache__', '.pytest_cache', '.ruff_cache'}
        tree_paths = {
            p.relative_to(ROOT).as_posix()
            for p in ROOT.rglob('*')
            if p.is_file() and not any(part in exclude for part in p.parts)
        }
        self.assertEqual(ledger_paths, tree_paths)

    def test_class_methods_appear_in_the_function_ledger(self):
        # LEDGER-METHODS-001: the ClassDef recursion in _defs_in() must be
        # delegated with `yield from`. A bare walk() call builds the
        # generator and discards it, dropping every method of every class
        # from the function ledger while --check still passed, because the
        # builder and the checker share the defective walker. Asserting on
        # THIS test's own qualified name keeps the guard true after renames.
        import csv
        ledger = ROOT / 'docs/audit/FUNCTION_CALLBACK_LEDGER.csv'
        with ledger.open(encoding='utf-8') as fh:
            rows = {(row['file'], row['qualified_name'])
                    for row in csv.DictReader(fh)}
        expected = ('tests/test_audit_ledgers.py',
                    f'{type(self).__name__}.{self._testMethodName}')
        self.assertIn(
            expected, rows,
            msg='class methods are missing from FUNCTION_CALLBACK_LEDGER.csv; '
                'check the ClassDef branch of _defs_in() in '
                'scripts/build_audit_ledgers.py')

    def test_function_ledger_matches_an_independent_ast_walk(self):
        # LEDGER-CONTROLFLOW-001: the strongest guarantee available — the
        # ledger must agree, file by file, with a traversal that shares NO
        # code with the generator. ast.walk() visits every node in the tree,
        # so a definition nested in try/except/if/with/for/while/match is
        # counted here even if the generator's descent rules were to miss it
        # again. Deriving the expectation from _defs_in() would reproduce
        # whatever blind spot the generator has and prove nothing, which is
        # exactly how two successive ledger defects reached a release.
        import ast
        import csv
        from collections import Counter

        exclude = {'.git', '__pycache__', '.pytest_cache', '.ruff_cache'}
        expected = Counter()
        for path in ROOT.rglob('*.py'):
            if any(part in exclude for part in path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue
            rel = path.relative_to(ROOT).as_posix()
            expected[rel] = sum(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                for node in ast.walk(tree))

        ledger = ROOT / 'docs/audit/FUNCTION_CALLBACK_LEDGER.csv'
        with ledger.open(encoding='utf-8') as fh:
            actual = Counter(row['file'] for row in csv.DictReader(fh))

        mismatches = {rel: (n, actual.get(rel, 0))
                      for rel, n in expected.items() if actual.get(rel, 0) != n}
        self.assertEqual(
            mismatches, {},
            msg='FUNCTION_CALLBACK_LEDGER.csv disagrees with an independent '
                'AST walk (file: expected, recorded); the generator in '
                'scripts/build_audit_ledgers.py is not reaching every '
                'definition')
        self.assertEqual(sum(expected.values()), sum(actual.values()))

    def test_definitions_nested_in_control_flow_are_collected(self):
        # Fixture guard for the same defect, independent of the shipped tree:
        # if every real definition ever moved out of a try/if block, the
        # count test above would still pass while the generator stayed
        # broken. This pins the behaviour directly.
        import importlib.util
        import tempfile
        from pathlib import Path as _Path

        spec = importlib.util.spec_from_file_location(
            'build_audit_ledgers', ROOT / 'scripts' / 'build_audit_ledgers.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        source = (
            'try:\n'
            '    def inside_try():\n'
            '        pass\n'
            'except ImportError:\n'
            '    def inside_except():\n'
            '        pass\n'
            'if True:\n'
            '    def inside_if():\n'
            '        pass\n'
            'with open(__file__):\n'
            '    def inside_with():\n'
            '        pass\n'
            'for _ in ():\n'
            '    async def inside_for():\n'
            '        pass\n'
            'class Holder:\n'
            '    if True:\n'
            '        def method_inside_if(self):\n'
            '            pass\n'
            'def ordinary():\n'
            '    if True:\n'
            '        def inside_function_if():\n'
            '            pass\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            probe = _Path(tmp) / 'probe.py'
            probe.write_text(source, encoding='utf-8')
            found = {d['name'] for d in module._defs_in(probe)}

        self.assertEqual(
            found,
            {'inside_try', 'inside_except', 'inside_if', 'inside_with',
             'inside_for', 'Holder.method_inside_if', 'ordinary',
             'ordinary.inside_function_if'},
            msg='definitions nested in control-flow blocks are missing from '
                'the ledger generator')


if __name__ == '__main__':
    unittest.main()
