"""Mode-aware, fail-closed Sharia execution-gate loader.

The immutable V19.1 research verifier remains in :mod:`sharia_filter`.  This
module adds a separate verifier for the owner-maintained manual operational
allowlist.  Keeping the contracts separate prevents a manual approval from
being misrepresented as an automated V19.1 research result.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, time as wall_time, timezone
from pathlib import Path

from services.common.sharia_attestation import (
    STATUS_PURPOSE,
    ShariaAttestationError,
    verify_attached,
)
from services.sharia_screener.manual_registry import (
    MANUAL_PROJECTION_MODE,
    MANUAL_STATUS_SOURCE,
    load_manual_registry,
    validate_manual_decision_report,
)
from services.universe_service.sharia_filter import Decision, SCHEMA_VERSION, ShariaFilter


SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
REQUEST_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
MAX_FUTURE_SKEW_SECONDS = 30


class ManualRegistryFilter:
    """Verify the signed projection of ``halal_coins.json``.

    A bootstrap document is valid but approves nothing.  A complete document
    must contain exactly one signed, report-bound GREEN record for every
    symbol in the current owner registry.  Any absent, stale, changed or
    malformed input fails closed.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.records: dict[str, dict] = {}
        self.controller_sha256 = ''
        self.projection_mode = MANUAL_PROJECTION_MODE
        self.registry_sha256 = ''
        self.loaded_at: datetime | None = None
        self.reload()

    @staticmethod
    def _parse_dt(value: object) -> datetime:
        text = str(value or '').replace('Z', '+00:00')
        if not text:
            raise ValueError('timestamp required')
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _normalize_symbol(value: object) -> str:
        symbol = str(value or '').upper().replace('/', '')
        # ``USDT`` may itself appear as an owner-reviewed base asset.  Strip
        # the quote suffix only when a non-empty base precedes it; exchange
        # eligibility independently rejects the nonexistent USDT/USDT pair.
        return symbol[:-4] if len(symbol) > 4 and symbol.endswith('USDT') else symbol

    def _binding_error(self, record: dict, *, expected_base: str,
                       registry) -> str:
        try:
            unsigned = verify_attached(record, purpose=STATUS_PURPOSE)
            symbol = self._normalize_symbol(unsigned.get('symbol'))
            if symbol != expected_base:
                raise ValueError('signed symbol binding mismatch')
            if str(unsigned.get('pair', '')).upper() != f'{symbol}/USDT':
                raise ValueError('pair/base binding mismatch')
            if unsigned.get('status') != 'GREEN' or unsigned.get('final_code') != 'GREEN':
                raise ValueError('manual registry may project only GREEN records')
            if unsigned.get('validated') is not True:
                raise ValueError('manual approval record is not validated')
            if unsigned.get('source') != MANUAL_STATUS_SOURCE:
                raise ValueError('unrecognized manual registry projection source')
            if str(unsigned.get('registry_sha256', '')).lower() != registry.sha256:
                raise ValueError('manual record registry hash mismatch')
            if str(unsigned.get('registry_version', '')) != registry.version:
                raise ValueError('manual record registry version mismatch')

            request_id = str(unsigned.get('request_id', ''))
            if REQUEST_ID_RE.fullmatch(request_id) is None:
                raise ValueError('invalid request_id binding')
            report_file = str(unsigned.get('report_file', ''))
            if report_file != f'{symbol}_{request_id}.json':
                raise ValueError('report filename binding mismatch')
            report_sha = str(unsigned.get('report_sha256', '')).lower()
            if SHA256_RE.fullmatch(report_sha) is None:
                raise ValueError('invalid report hash binding')

            if registry.last_reviewed is None or registry.next_review is None:
                raise ValueError('non-empty registry review dates are required')
            if str(unsigned.get('reviewed_at', '')) != registry.last_reviewed.isoformat():
                raise ValueError('manual record review-date binding mismatch')
            completed = self._parse_dt(unsigned.get('completed_at'))
            now = datetime.now(timezone.utc)
            if completed > now.replace(microsecond=0) and (
                    completed - now).total_seconds() > MAX_FUTURE_SKEW_SECONDS:
                raise ValueError('completed_at is in the future')
            expiry = self._parse_dt(unsigned.get('expires_at'))
            expected_expiry = datetime.combine(
                registry.next_review, wall_time(23, 59, 59), tzinfo=timezone.utc)
            if expiry != expected_expiry or expiry <= completed:
                raise ValueError('manual record expiry binding mismatch')

            report_path = self.path.parent / 'reports' / report_file
            report_bytes = report_path.read_bytes()
            if hashlib.sha256(report_bytes).hexdigest() != report_sha:
                raise ValueError('report content hash mismatch')
            report = json.loads(report_bytes.decode('utf-8'))
            validate_manual_decision_report(
                report,
                expected_base=symbol,
                expected_registry_sha256=registry.sha256,
            )
            if str(report.get('registry_version', '')) != registry.version:
                raise ValueError('manual report registry version mismatch')
            if str(report.get('last_reviewed', '')) != registry.last_reviewed.isoformat():
                raise ValueError('manual report review-date binding mismatch')
            if str(report.get('next_review', '')) != registry.next_review.isoformat():
                raise ValueError('manual report next-review binding mismatch')
            return ''
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError,
                TypeError, ShariaAttestationError) as exc:
            return str(exc) or type(exc).__name__

    def reload(self) -> None:
        raw = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict) or raw.get('schema_version') != SCHEMA_VERSION:
            raise ValueError(
                f'manual projection schema_version must be {SCHEMA_VERSION}; fail closed')
        if raw.get('projection_mode') != MANUAL_PROJECTION_MODE:
            raise ValueError('manual projection mode binding missing; fail closed')
        if raw.get('registry_valid') is not True:
            raise ValueError('manual halal registry is invalid; fail closed')
        declared_hash = str(raw.get('registry_sha256', '')).lower()
        if SHA256_RE.fullmatch(declared_hash) is None:
            raise ValueError('manual registry SHA-256 binding is invalid; fail closed')

        registry = load_manual_registry(self.path.parent / 'halal_coins.json')
        if registry.sha256 != declared_hash:
            raise ValueError(
                'halal_coins.json changed after projection; fail closed until '
                'the manual registry service signs the new version')
        if str(raw.get('registry_version', '')) != registry.version:
            raise ValueError('manual registry version binding mismatch; fail closed')

        complete = raw.get('projection_complete')
        if complete not in (True, False):
            raise ValueError('projection_complete must be a boolean; fail closed')
        records = raw.get('records')
        if not isinstance(records, list):
            raise ValueError('manual projection records must be a list')
        if not complete and records:
            raise ValueError('bootstrap projection cannot contain approval records')

        out: dict[str, dict] = {}
        for index, record in enumerate(records):
            where = f'records[{index}]'
            if not isinstance(record, dict):
                raise ValueError(where + ' must be an object')
            symbol = self._normalize_symbol(record.get('symbol'))
            if not symbol or not symbol.isalnum():
                raise ValueError(where + '.symbol invalid')
            if symbol in out:
                raise ValueError(where + '.symbol duplicate')
            if record.get('status') != 'GREEN':
                raise ValueError(where + '.status must be GREEN')
            normalized = dict(record, symbol=symbol)
            error = self._binding_error(
                normalized, expected_base=symbol, registry=registry)
            normalized['_attestation_valid'] = not error
            normalized['_binding_error'] = error
            out[symbol] = normalized

        if complete and set(out) != set(registry.bases):
            raise ValueError(
                'complete manual projection does not exactly match halal_coins.json; '
                'fail closed')
        if not complete and out:
            raise ValueError('incomplete manual projection must deny all symbols')

        self.records = out
        self.registry_sha256 = registry.sha256
        self.loaded_at = datetime.now(timezone.utc)

    def decision(self, base: str, now=None) -> Decision:
        now = now or datetime.now(timezone.utc)
        record = self.records.get(self._normalize_symbol(base))
        if not record:
            return Decision(
                False, 'NO_TRADE_INFO',
                'not present in the current owner-maintained manual halal registry', {})
        if record.get('_attestation_valid') is not True:
            return Decision(
                False, 'NO_TRADE_INFO',
                'manual approval is not cryptographically/report bound: ' +
                str(record.get('_binding_error') or 'unknown error'), record)
        try:
            reviewed = datetime.fromisoformat(str(record.get('reviewed_at', ''))[:10]).date()
            expiry = self._parse_dt(record.get('expires_at'))
        except (TypeError, ValueError):
            return Decision(False, 'STALE', 'manual approval dates are malformed', record)
        if reviewed > now.astimezone(timezone.utc).date():
            return Decision(False, 'STALE', 'manual review date is in the future', record)
        if expiry <= now:
            return Decision(False, 'STALE', 'manual registry review has expired', record)
        return Decision(
            True, 'GREEN',
            'present in the current owner-maintained manual halal registry', record)

    def is_record_verified(self, base: str) -> bool:
        record = self.records.get(self._normalize_symbol(base))
        return bool(record and record.get('_attestation_valid') is True)

    def verified_projection_records(self) -> list[dict]:
        return [
            {key: value for key, value in record.items() if not key.startswith('_')}
            for record in self.records.values()
            if record.get('_attestation_valid') is True
        ]

    def current_halal_symbols(self, now=None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        return sorted(base for base in self.records if self.decision(base, now).allowed)

    def sync_legacy_compat(self, path: str | Path) -> list[str]:
        # halal_coins.json is the owner-maintained source in manual mode.  No
        # runtime service may overwrite or regenerate it.
        return [base + 'USDT' for base in self.current_halal_symbols()]


def load_sharia_gate(path: str | Path):
    """Load the exact verifier declared by the status projection."""
    source = Path(path)
    raw = json.loads(source.read_text(encoding='utf-8'))
    if isinstance(raw, dict) and raw.get('projection_mode') == MANUAL_PROJECTION_MODE:
        return ManualRegistryFilter(source)
    return ShariaFilter(source)
