from __future__ import annotations
"""Deterministically (re)generate the audit evidence ledgers from the tree.

V102-REM-015 (deep-audit F-03/F-04): the hand-maintained
docs/audit/FILE_REVIEW_LEDGER.csv and FUNCTION_CALLBACK_LEDGER.csv drifted
from the shipped tree every time files were added (independent audits kept
reporting "N current files absent from the ledger" and stale row hashes).
Hand-maintained evidence cannot stay exact across rebuilds, so the ledgers
are now GENERATED from the tree and CI-ENFORCED:

  python scripts/build_audit_ledgers.py            # regenerate both ledgers
  python scripts/build_audit_ledgers.py --check    # fail if either drifted

``--check`` regenerates both ledgers in memory and byte-compares them to the
committed files. deploy/verify_release.sh and tests/test_audit_ledgers.py both
run the check, so a ledger can never again diverge from the release tree.

The two ledger CSVs plus RELEASE_MANIFEST.json / RELEASE_SHA256.txt are
self-referential (their bytes change when regenerated), so the FILE ledger
records a stable sentinel in their sha256 cell instead of a hash. Their
integrity is still bound by RELEASE_MANIFEST.json, which hashes every file
including the ledgers. Together the manifest and the ledgers give complete,
non-circular coverage of the tree.
"""
import ast
import csv
import hashlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / 'docs' / 'audit'
FILE_LEDGER = AUDIT_DIR / 'FILE_REVIEW_LEDGER.csv'
FUNCTION_LEDGER = AUDIT_DIR / 'FUNCTION_CALLBACK_LEDGER.csv'

EXCLUDE_DIR_PARTS = {'.git', '__pycache__', '.pytest_cache', '.ruff_cache'}
# Self-referential release metadata: listed as paths, but their sha256 cell is
# a sentinel because their bytes change as part of regenerating the release.
SELF_REFERENTIAL = {
    'docs/audit/FILE_REVIEW_LEDGER.csv',
    'RELEASE_MANIFEST.json',
    'RELEASE_SHA256.txt',
}
SELF_SENTINEL = '(self-referential; integrity bound by RELEASE_MANIFEST.json)'

_CATEGORY_BY_TOP = {
    'services': 'runtime-service',
    'legacy_core': 'preserved-core',
    'freqtrade': 'freqtrade-strategy-and-deploy',
    'monitoring': 'monitoring',
    'tests': 'test',
    'scripts': 'release-script',
    'deploy': 'deploy-script',
    'docs': 'documentation',
    'shared': 'runtime-data-scaffold',
    '.github': 'ci',
}

_TYPE_BY_SUFFIX = {
    '.py': 'python', '.json': 'json', '.yml': 'yaml', '.yaml': 'yaml',
    '.sh': 'shell', '.md': 'markdown', '.txt': 'text', '.csv': 'csv',
    '.service': 'systemd-unit', '.timer': 'systemd-unit', '.lock': 'lockfile',
    '.example': 'env-example', '.log': 'log', '.flag': 'flag',
}


def _iter_files():
    for path in sorted(ROOT.rglob('*'), key=lambda p: p.relative_to(ROOT).as_posix()):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIR_PARTS for part in path.parts):
            continue
        yield path


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _file_type(path: Path) -> str:
    name = path.name.lower()
    if name.startswith('.env'):
        return 'env-example'
    if name == 'dockerfile' or name.startswith('dockerfile'):
        return 'dockerfile'
    if name == 'makefile':
        return 'makefile'
    return _TYPE_BY_SUFFIX.get(path.suffix.lower(), 'other')


def _category(rel: str) -> str:
    top = rel.split('/', 1)[0]
    if top == rel:  # root-level file
        return 'release-root'
    return _CATEGORY_BY_TOP.get(top, 'other')


def build_file_ledger_text() -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow(['relative_path', 'file_type', 'category', 'size_bytes',
                     'sha256', 'generated_by'])
    for path in _iter_files():
        rel = _rel(path)
        if rel in SELF_REFERENTIAL:
            # Both size and hash are sentinels: these files' bytes change as a
            # side effect of regenerating the release, so recording either
            # would be self-referential. RELEASE_MANIFEST.json binds them.
            writer.writerow([rel, _file_type(path), _category(rel), '-',
                             SELF_SENTINEL, 'scripts/build_audit_ledgers.py'])
            continue
        data = path.read_bytes()
        writer.writerow([rel, _file_type(path), _category(rel), len(data),
                         hashlib.sha256(data).hexdigest(),
                         'scripts/build_audit_ledgers.py'])
    return buf.getvalue()


def _defs_in(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return
    stack = [('', tree, False)]
    # Walk with qualified names and a test-context flag.
    def walk(node, prefix, in_func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                # LEDGER-METHODS-001: this recursion MUST be delegated with
                # `yield from`. Calling walk() bare creates the generator and
                # discards it, so every method of every class — including all
                # unittest test methods — was silently absent from the
                # function ledger, even though the 'top-level-or-method'
                # scope label records that methods belong in it.
                yield from walk(child, f'{prefix}{child.name}.', in_func)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = 'async-function' if isinstance(child, ast.AsyncFunctionDef) else 'function'
                nested = 'nested' if in_func else 'top-level-or-method'
                decorators = ';'.join(
                    ast.unparse(d) if hasattr(ast, 'unparse') else getattr(d, 'id', '?')
                    for d in child.decorator_list) or '-'
                yield {
                    'name': f'{prefix}{child.name}',
                    'line_start': child.lineno,
                    'line_end': getattr(child, 'end_lineno', child.lineno),
                    'kind': kind,
                    'scope': nested,
                    'decorators': decorators,
                }
                yield from walk(child, f'{prefix}{child.name}.', True)
            else:
                # LEDGER-CONTROLFLOW-001: descend through every OTHER node
                # too. Recursing only into classes and functions skipped any
                # definition nested in control flow — try/except, if, with,
                # for, while, match — which hid the platform-specific audit
                # lock implementations, the execution sidecar's order-sending
                # closures and the stream reconnect handler from the ledger.
                # The qualified-name prefix does not change here: control flow
                # introduces no namespace.
                yield from walk(child, prefix, in_func)
    yield from walk(tree, '', False)


def build_function_ledger_text() -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow(['file', 'qualified_name', 'kind', 'scope', 'line_start',
                     'line_end', 'decorators', 'context'])
    rows = []
    for path in _iter_files():
        if path.suffix != '.py':
            continue
        rel = _rel(path)
        context = 'test' if rel.startswith('tests/') or '/tests/' in rel else _category(rel)
        for d in _defs_in(path):
            rows.append([rel, d['name'], d['kind'], d['scope'],
                         d['line_start'], d['line_end'], d['decorators'], context])
    rows.sort(key=lambda r: (r[0], int(r[4]), r[1]))
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def write_ledgers() -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    FUNCTION_LEDGER.write_bytes(build_function_ledger_text().encode('utf-8'))
    # FILE ledger is built AFTER the function ledger so it records the
    # function ledger's real (final) hash.
    FILE_LEDGER.write_bytes(build_file_ledger_text().encode('utf-8'))


def check_ledgers() -> int:
    errors = []
    expected_functions = build_function_ledger_text()
    actual_functions = FUNCTION_LEDGER.read_text(encoding='utf-8') if FUNCTION_LEDGER.exists() else ''
    if expected_functions != actual_functions:
        errors.append('FUNCTION_CALLBACK_LEDGER.csv is stale; run '
                      'python scripts/build_audit_ledgers.py')
    expected_files = build_file_ledger_text()
    actual_files = FILE_LEDGER.read_text(encoding='utf-8') if FILE_LEDGER.exists() else ''
    if expected_files != actual_files:
        errors.append('FILE_REVIEW_LEDGER.csv is stale; run '
                      'python scripts/build_audit_ledgers.py')
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    file_rows = expected_files.count('\n') - 1
    func_rows = expected_functions.count('\n') - 1
    print(f'audit ledgers verified: {file_rows} files, {func_rows} functions; '
          f'exact tree parity')
    return 0


def main() -> int:
    if '--check' in sys.argv[1:]:
        return check_ledgers()
    write_ledgers()
    file_rows = build_file_ledger_text().count('\n') - 1
    func_rows = build_function_ledger_text().count('\n') - 1
    print(f'audit ledgers regenerated: {file_rows} files, {func_rows} functions')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
