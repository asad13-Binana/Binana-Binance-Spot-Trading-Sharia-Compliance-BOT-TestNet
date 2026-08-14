from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

class ShariaDecision(StrEnum):
    PASS="PASS"; FAIL="FAIL"; UNKNOWN="UNKNOWN"

@dataclass(frozen=True)
class ShariaResult:
    symbol: str; decision: ShariaDecision; status_code: str; source: str; reviewed_at: datetime | None; expires_at: datetime | None; reason: str
