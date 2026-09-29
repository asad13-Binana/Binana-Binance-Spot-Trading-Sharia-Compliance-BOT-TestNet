"""Authenticated owner commands for the manual trading Sharia registry."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.common import envelope
from services.common.atomic import atomic_write_json
from services.common.audit import audit
from services.common.sharia_attestation import RESULT_PURPOSE, verify_attached
from services.common.sharia_v19 import TRADE_ELIGIBLE_CODES
from services.sharia_screener.manual_registry import load_manual_registry


COMMAND_ID_RE = re.compile(r'^[0-9a-f]{32}$')
BASE_RE = re.compile(r'^[A-Z0-9]{1,16}$')
REQUEST_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
ACTIONS = {'REGISTRY_ADD', 'REGISTRY_REMOVE'}


class RegistryCommandError(ValueError):
    """An owner registry command failed strict authentication/binding checks."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RegistryCommandError(message)


def _verified_research_result(*, base: str, request_id: str,
                              expected_report_sha256: str,
                              results_dir: Path, reports_dir: Path) -> dict:
    _require(REQUEST_ID_RE.fullmatch(request_id) is not None,
             'research request_id is invalid')
    result_path = results_dir / f'result_{request_id}.json'
    try:
        payload = envelope.read_verified_file(
            result_path, purpose=envelope.BUS_SHARIA_RESULT,
            expected_producers={'sharia-screener'})
        payload = verify_attached(payload, purpose=RESULT_PURPOSE)
    except Exception as exc:
        raise RegistryCommandError(
            f'research result cannot be verified: {type(exc).__name__}: {exc}') from exc
    _require(str(payload.get('base', '')).upper() == base,
             'research result base binding mismatch')
    _require(str(payload.get('pair', '')).upper() == f'{base}/USDT',
             'research result pair binding mismatch')
    _require(payload.get('validated') is True,
             'research result is not independently validated')
    final_code = str(payload.get('final_code', ''))
    _require(final_code in TRADE_ELIGIBLE_CODES,
             f'research result is not trade-eligible: {final_code or "UNKNOWN"}')
    report_sha = str(payload.get('report_sha256', '')).lower()
    _require(report_sha == expected_report_sha256.lower(),
             'research report SHA-256 binding mismatch')
    report_name = str(payload.get('report_file', ''))
    report_path = (reports_dir / report_name).resolve()
    root = reports_dir.resolve()
    _require(root in report_path.parents and report_path.is_file(),
             'research report file is missing or outside the reports directory')
    raw = report_path.read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == report_sha,
             'research report content hash mismatch')
    try:
        report = json.loads(raw.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RegistryCommandError('research report is malformed') from exc
    _require(str(report.get('final_code', '')) == final_code,
             'research result/report final-code mismatch')
    _require(isinstance(report.get('owner_approval'), dict),
             'research result lacks explicit owner approval')
    return payload


def apply_registry_command(path: Path, *, registry_path: Path,
                           research_results_dir: Path,
                           research_reports_dir: Path):
    try:
        payload = envelope.read_verified_file(
            path, purpose=envelope.BUS_SHARIA_DECISION,
            expected_producers={'telegram-broker'})
    except Exception as exc:
        raise RegistryCommandError(
            f'registry command authentication failed: {type(exc).__name__}: {exc}') from exc
    command_id = str(payload.get('command_id', '')).lower()
    action = str(payload.get('action', '')).upper()
    base = str(payload.get('base', '')).upper()
    _require(COMMAND_ID_RE.fullmatch(command_id) is not None,
             'registry command_id is invalid')
    _require(path.name == f'registry_{command_id}.json',
             'registry command filename binding mismatch')
    _require(action in ACTIONS, 'registry command action is invalid')
    _require(BASE_RE.fullmatch(base) is not None, 'registry base is invalid')
    registry = load_manual_registry(registry_path)
    expected_registry_sha = str(payload.get('expected_registry_sha256', '')).lower()
    _require(expected_registry_sha == registry.sha256,
             'manual registry changed after confirmation; command is stale')
    symbol = base + 'USDT'
    symbols = set(registry.symbols)
    if action == 'REGISTRY_ADD':
        _verified_research_result(
            base=base,
            request_id=str(payload.get('research_request_id', '')),
            expected_report_sha256=str(payload.get('report_sha256', '')),
            results_dir=research_results_dir,
            reports_dir=research_reports_dir)
        symbols.add(symbol)
    else:
        symbols.discard(symbol)
    now = datetime.now(timezone.utc)
    suffix = command_id[:4]
    document = {
        '_comment': (
            'Owner-maintained operational Sharia allowlist. Changes are accepted '
            'only through authenticated Telegram owner commands. Research '
            'screening is separate and is not a fatwa.'),
        'schema_version': 1,
        'version': now.strftime('aws-%y%m%d%H%M%S-') + suffix,
        'last_reviewed': now.date().isoformat(),
        'next_review': (now.date() + timedelta(days=90)).isoformat(),
        'symbols': sorted(symbols),
    }
    atomic_write_json(registry_path, document)
    updated = load_manual_registry(registry_path)
    audit('manual_sharia_registry_owner_command_applied', details={
        'command_id': command_id,
        'action': action,
        'base': base,
        'registry_sha256': updated.sha256,
        'symbol_count': len(updated.symbols),
    })
    return updated


def process_registry_commands(*, registry_path: Path, inbox: Path,
                              processed: Path, rejected: Path,
                              research_results_dir: Path,
                              research_reports_dir: Path) -> int:
    """Apply authenticated commands; invalid commands are terminally quarantined."""
    applied = 0
    inbox.mkdir(parents=True, exist_ok=True)
    processed.mkdir(parents=True, exist_ok=True)
    rejected.mkdir(parents=True, exist_ok=True)
    for path in sorted(inbox.glob('registry_*.json')):
        try:
            apply_registry_command(
                path, registry_path=registry_path,
                research_results_dir=research_results_dir,
                research_reports_dir=research_reports_dir)
        except RegistryCommandError as exc:
            audit('manual_sharia_registry_owner_command_rejected', severity='WARNING',
                  details={'file': path.name, 'error': str(exc)[:500]})
            path.replace(rejected / path.name)
            continue
        except OSError as exc:
            audit('manual_sharia_registry_owner_command_retry', severity='ERROR',
                  details={'file': path.name, 'error': str(exc)[:500]})
            continue
        path.replace(processed / path.name)
        applied += 1
    return applied
