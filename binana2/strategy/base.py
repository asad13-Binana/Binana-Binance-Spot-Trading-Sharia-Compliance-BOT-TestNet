from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import pandas as pd


@dataclass(frozen=True)
class Signal:
    strategy: str
    symbol: str
    candle_time: datetime
    generated_at: datetime
    enter: bool
    exit: bool
    entry_tag: str = ""
    exit_tag: str = ""


class Strategy(Protocol):
    name: str

    def evaluate(self, symbol: str, one_minute: pd.DataFrame, five_minute: pd.DataFrame) -> Signal: ...
