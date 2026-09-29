"""Exactly-once owner-registry admission decision for a new entry intent."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from threading import RLock
from typing import Any


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class HalalDecision:
    intent_id: str
    base_asset: str
    allowed: bool
    registry_sha256: str
    registry_mtime_ns: int


class OwnerRegistry:
    """Read a versioned owner list and memoize one decision per intent.

    Replaying the same intent returns the same decision even if the list changes.
    A genuinely new intent consumes the current validated snapshot once.
    """
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = RLock()
        self._decisions: dict[str, HalalDecision] = {}
        self.lookup_count = 0

    @staticmethod
    def _extract_assets(data: Any, *, today: date | None = None) -> set[str]:
        if not isinstance(data, dict) or type(data.get('schema_version')) is not int or data['schema_version'] != 1:
            raise RegistryError('owner registry schema_version must be integer 1')
        version = data.get('version')
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,31}', version):
            raise RegistryError('owner registry version is invalid')
        symbols = data.get('symbols')
        if not isinstance(symbols, list):
            raise RegistryError('owner registry symbols must be an array')
        if any(not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{1,16}USDT', symbol) for symbol in symbols):
            raise RegistryError('owner registry requires exact uppercase Spot/USDT symbols')
        if symbols != sorted(set(symbols)):
            raise RegistryError('owner registry symbols must be sorted and unique')
        if symbols or data.get('last_reviewed') or data.get('next_review'):
            try:
                reviewed = date.fromisoformat(data['last_reviewed'])
                expires = date.fromisoformat(data['next_review'])
            except (KeyError, TypeError, ValueError) as exc:
                raise RegistryError('owner registry requires ISO review dates') from exc
            current = today or datetime.now(timezone.utc).date()
            if not reviewed <= current < expires or not 0 < (expires-reviewed).days <= 366:
                raise RegistryError('owner registry review dates are expired or invalid')
        return {symbol[:-4] for symbol in symbols}

    def _snapshot(self) -> tuple[set[str], str, int]:
        try:
            with self.path.open('rb') as source:
                raw = source.read()
                stat = os.fstat(source.fileno())
        except OSError as exc:
            raise RegistryError('owner registry unavailable') from exc
        if not raw:
            raise RegistryError("halal registry is empty")
        try:
            data = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RegistryError("halal registry JSON is invalid") from exc
        assets = self._extract_assets(data)
        return assets, sha256(raw).hexdigest(), stat.st_mtime_ns

    def decide(self, *, intent_id: str, pair: str) -> HalalDecision:
        if not intent_id or not isinstance(intent_id, str):
            raise RegistryError("intent_id is required")
        base = pair.split("/", 1)[0].upper()
        with self._lock:
            prior = self._decisions.get(intent_id)
            if prior is not None:
                if prior.base_asset != base:
                    raise RegistryError("intent replay attempted with a different asset")
                return prior
            assets, digest, mtime = self._snapshot()
            self.lookup_count += 1
            decision = HalalDecision(intent_id, base, base in assets, digest, mtime)
            self._decisions[intent_id] = decision
            return decision
