"""Exactly-once owner-registry admission decision for a new entry intent."""
from __future__ import annotations
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
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
    def _extract_assets(data: Any) -> set[str]:
        if isinstance(data, list):
            values = data
        elif isinstance(data, dict):
            for key in ("coins", "assets", "approved", "halal_coins", "symbols"):
                if isinstance(data.get(key), list):
                    values = data[key]
                    break
            else:
                # Support owner registries keyed by ticker with boolean/object values.
                values = [k for k, v in data.items() if v is True or isinstance(v, dict)]
        else:
            raise RegistryError("halal registry must be a JSON list or object")
        result: set[str] = set()
        for item in values:
            if isinstance(item, str):
                symbol = item
            elif isinstance(item, dict):
                symbol = item.get("symbol") or item.get("ticker") or item.get("asset")
            else:
                continue
            if symbol:
                result.add(str(symbol).upper().replace("/USDT", "").replace("USDT", ""))
        return result

    def _snapshot(self) -> tuple[set[str], str, int]:
        raw = self.path.read_bytes()
        if not raw:
            raise RegistryError("halal registry is empty")
        stat = self.path.stat()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RegistryError("halal registry JSON is invalid") from exc
        assets = self._extract_assets(data)
        if not assets:
            raise RegistryError("halal registry has no approved assets")
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
