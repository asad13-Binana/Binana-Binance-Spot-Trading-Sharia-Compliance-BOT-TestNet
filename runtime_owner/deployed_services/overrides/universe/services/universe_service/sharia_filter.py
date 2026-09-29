from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.common.sharia_attestation import (
    STATUS_PURPOSE,
    ShariaAttestationError,
    verify_attached,
)
from services.common.sharia_v19 import (
    FINAL_CODES,
    TRADE_ELIGIBLE_CODES,
    V19_CONTROLLER_FILENAME,
    V19_CONTROLLER_SHA256,
    load_controller,
    validate_result,
)

# The status file may only contain V19.1 final codes. The legacy
# HALAL/HARAM/DOUBTFUL/UNKNOWN vocabulary is intentionally rejected so the
# previous Sharia definition can never be loaded again (master protocol 8.2).
VALID = set(FINAL_CODES)
ALLOWED = set(TRADE_ELIGIBLE_CODES)
SCHEMA_VERSION = 2
MAX_STATUS_VALIDITY_SECONDS = 366 * 86_400
MAX_FUTURE_SKEW_SECONDS = 30
MAX_APPROVED_AGE_SECONDS = 7 * 86_400
STATUS_SOURCE = 'sharia-screener/v19.1'
REQUEST_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')


@dataclass(frozen=True)
class Decision:
    allowed: bool
    status: str
    reason: str
    record: dict


class ShariaFilter:
    """Fail-closed V19.1 execution gate over the screener-generated status file.

    This code enforces records produced by the separate sharia-screener
    service from the immutable V19.1 controller; it does not determine
    religious permissibility and is not a fatwa. Only a current GREEN or
    GREEN_AVOID_OPTIONAL record allows trading; everything else — including a
    missing, expired, malformed or legacy-format record — fails closed.
    """

    def __new__(cls, path: str | Path):
        # The owner explicitly selected manual-registry mode. Dispatch it to a
        # separate verifier so the V19.1 research contract below stays intact
        # and a manual approval is never represented as automated research.
        if cls is ShariaFilter:
            try:
                raw = json.loads(Path(path).read_text(encoding='utf-8'))
            except Exception:
                raw = None
            if isinstance(raw, dict) and raw.get('projection_mode') == 'manual-registry/v1':
                from services.universe_service.sharia_gate import ManualRegistryFilter
                return ManualRegistryFilter(path)
        return super().__new__(cls)

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.records: dict[str, dict] = {}
        self.controller_sha256 = ''
        self.loaded_at = None
        self.reload()

    @staticmethod
    def _parse_dt(value: str):
        if not value:
            return None
        value = value.replace('Z', '+00:00')
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    @staticmethod
    def _normalize_symbol(value: object) -> str:
        symbol = str(value or '').upper().replace('/', '')
        return symbol[:-4] if symbol.endswith('USDT') else symbol

    def _verify_controller_binding(self, raw: dict):
        declared = str(raw.get('controller_sha256', ''))
        if declared != V19_CONTROLLER_SHA256:
            raise ValueError(
                'sharia_status.json is not bound to the immutable V19.1 controller '
                f'(controller_sha256={declared!r}); fail closed')
        controller_path = self.path.parent / V19_CONTROLLER_FILENAME
        try:
            load_controller(controller_path)
        except Exception as exc:
            raise ValueError(
                f'installed V19.1 controller is missing or invalid at {controller_path}; '
                'fail closed') from exc

    def _record_binding_error(self, record: dict) -> str:
        """Return an authorization error, or ``''`` for a fully bound record."""
        try:
            unsigned = verify_attached(record, purpose=STATUS_PURPOSE)
            symbol = str(unsigned['symbol'])
            status = str(unsigned['status'])
            if str(unsigned.get('final_code', '')) != status:
                raise ValueError('status/final_code mismatch')
            if unsigned.get('validated') is not True and status in ALLOWED:
                raise ValueError('trade-eligible record is not validated')
            if str(unsigned.get('source', '')) != STATUS_SOURCE:
                raise ValueError('unrecognized projection source')
            if str(unsigned.get('controller_sha256', '')) != V19_CONTROLLER_SHA256:
                raise ValueError('record controller binding mismatch')
            request_id = str(unsigned.get('request_id', ''))
            if not REQUEST_ID_RE.fullmatch(request_id):
                raise ValueError('invalid request_id binding')
            pair = str(unsigned.get('pair', '')).upper()
            if pair != f'{symbol}/USDT':
                raise ValueError('pair/base binding mismatch')
            report_file = str(unsigned.get('report_file', ''))
            if report_file != f'{symbol}_{request_id}.json':
                raise ValueError('report filename binding mismatch')
            report_sha = str(unsigned.get('report_sha256', '')).lower()
            if not re.fullmatch(r'[0-9a-f]{64}', report_sha):
                raise ValueError('invalid report hash binding')

            completed = self._parse_dt(str(unsigned.get('completed_at', '')))
            expiry = self._parse_dt(str(unsigned.get('expires_at', '')))
            if completed is None or expiry is None:
                raise ValueError('completed_at/expires_at required')
            now = datetime.now(timezone.utc)
            if completed > now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
                raise ValueError('completed_at is in the future')
            if expiry <= completed:
                raise ValueError('expires_at must be after completed_at')
            if (expiry - completed).total_seconds() > MAX_STATUS_VALIDITY_SECONDS:
                raise ValueError('status validity exceeds the hard maximum')
            if str(unsigned.get('reviewed_at', '')) != completed.date().isoformat():
                raise ValueError('reviewed_at/completed_at mismatch')

            report_path = self.path.parent / 'reports' / report_file
            raw_report = report_path.read_bytes()
            if hashlib.sha256(raw_report).hexdigest() != report_sha:
                raise ValueError('report content hash mismatch')
            report = json.loads(raw_report.decode('utf-8'))
            validate_result(report, expected_base=symbol)
            if str(report.get('final_code', '')) != status:
                raise ValueError('report/status final_code mismatch')
            return ''
        except (OSError, ValueError, KeyError, TypeError, ShariaAttestationError) as exc:
            return str(exc) or type(exc).__name__

    def reload(self):
        raw = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict) or raw.get('schema_version') != SCHEMA_VERSION:
            raise ValueError(
                'legacy or unknown Sharia dataset rejected: only the V19.1 projection '
                f'(schema_version={SCHEMA_VERSION}) may load; fail closed')
        self._verify_controller_binding(raw)
        records = raw.get('records')
        if not isinstance(records, list) or not records:
            raise ValueError('records must be a non-empty list')

        out: dict[str, dict] = {}
        for index, record in enumerate(records):
            where = f'records[{index}]'
            if not isinstance(record, dict):
                raise ValueError(where + ' must be an object')
            symbol = self._normalize_symbol(record.get('symbol'))
            status = str(record.get('status', '')).upper()
            source = str(record.get('source', '')).strip()
            if not symbol or not symbol.isalnum():
                raise ValueError(where + '.symbol invalid')
            if symbol in out:
                raise ValueError(where + '.symbol duplicate')
            if status not in VALID:
                raise ValueError(
                    where + f'.status {status!r} is not a V19.1 final code — '
                    'legacy Sharia statuses are rejected')
            if not source:
                raise ValueError(where + '.source required')
            try:
                reviewed = datetime.fromisoformat(str(record.get('reviewed_at', ''))[:10])
            except Exception as exc:
                raise ValueError(where + '.reviewed_at invalid') from exc
            try:
                expiry = self._parse_dt(str(record.get('expires_at', '')))
            except Exception as exc:
                raise ValueError(where + '.expires_at invalid') from exc
            if expiry is None:
                raise ValueError(where + '.expires_at required')
            out[symbol] = dict(
                record,
                symbol=symbol,
                status=status,
                source=source,
                reviewed_at=reviewed.date().isoformat(),
                expires_at=expiry.isoformat(),
            )
            binding_error = self._record_binding_error(out[symbol])
            out[symbol]['_attestation_valid'] = not binding_error
            out[symbol]['_binding_error'] = binding_error

        self.records = out
        self.controller_sha256 = str(raw.get('controller_sha256', ''))
        self.loaded_at = datetime.now(timezone.utc)

    def decision(self, base: str, now=None) -> Decision:
        now = now or datetime.now(timezone.utc)
        base = self._normalize_symbol(base)
        record = self.records.get(base)
        if not record:
            return Decision(False, 'NO_TRADE_INFO', 'no V19.1 screening record', {})
        status = record['status']
        if record.get('_attestation_valid') is not True:
            return Decision(False, 'NO_TRADE_INFO',
                            'screening record is not cryptographically/report bound: ' +
                            str(record.get('_binding_error') or 'unknown error'), record)
        if not str(record.get('source', '')).strip():
            return Decision(False, 'NO_TRADE_INFO', 'screening source missing', record)
        try:
            reviewed = datetime.fromisoformat(str(record.get('reviewed_at', ''))[:10]).date()
        except Exception:
            return Decision(False, 'STALE', 'malformed reviewed_at', record)
        if reviewed > now.astimezone(timezone.utc).date():
            return Decision(False, 'STALE', 'reviewed_at is in the future', record)
        try:
            expiry = self._parse_dt(str(record.get('expires_at', '')))
        except Exception:
            return Decision(False, 'STALE', 'malformed expires_at', record)
        if not expiry or expiry <= now:
            return Decision(False, 'STALE', 'record expired — rescreen required', record)
        try:
            completed = self._parse_dt(str(record.get('completed_at', '')))
        except Exception:
            return Decision(False, 'STALE', 'malformed completed_at', record)
        if not completed or (now - completed).total_seconds() > MAX_APPROVED_AGE_SECONDS:
            return Decision(
                False, 'STALE',
                'approved record is older than the seven-day local rescreen window',
                record)
        if status not in ALLOWED:
            return Decision(False, status,
                            'only GREEN or GREEN_AVOID_OPTIONAL passes under V19.1', record)
        return Decision(True, status, 'current V19.1 ' + status + ' record', record)

    def is_record_verified(self, base: str) -> bool:
        record = self.records.get(self._normalize_symbol(base))
        return bool(record and record.get('_attestation_valid') is True)

    def verified_projection_records(self) -> list[dict]:
        """Records safe for the screener bridge to preserve during a merge."""
        records = []
        for record in self.records.values():
            if record.get('_attestation_valid') is not True:
                continue
            records.append({k: v for k, v in record.items() if not k.startswith('_')})
        return records

    def current_halal_symbols(self, now=None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        return sorted(base for base in self.records if self.decision(base, now).allowed)

    def sync_legacy_compat(self, path: str | Path) -> list[str]:
        """Regenerate the preserved core's whitelist view.

        Only the sharia-screener service may call this in production: every
        other container mounts the Sharia directory read-only.
        """
        from services.common.atomic import atomic_write_json
        symbols = [base + 'USDT' for base in self.current_halal_symbols()]
        atomic_write_json(path, {
            '_comment': 'Generated from the V19.1 screener status file; do not edit. '
                        'Empty until a valid current V19.1 GREEN result exists.',
            'symbols': symbols,
        })
        return symbols
