from __future__ import annotations
from dataclasses import dataclass
from binana2.state.database import Database
from binana2.state.repositories import StateRepository
@dataclass(frozen=True)
class Health:
    ok: bool; entries_paused: bool; pause_reason: str; active_halt: bool
def health_snapshot(db:Database,state:StateRepository)->Health:
    row=db.execute("PRAGMA quick_check").fetchone(); db_ok=row is not None and row[0]=="ok"; paused,reason=state.is_entry_paused(); halted,_=state.has_active_halt(); return Health(db_ok,paused,reason,halted)
