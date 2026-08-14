from binana2.state.database import Database
from binana2.state.repositories import StateRepository
from binana2.trading.protection_manager import ProtectionManager

def test_protection_failure_latches_entries_off(tmp_path):
    db=Database(tmp_path/"p.db"); state=StateRepository(db); state.set_entry_pause(False,"test"); manager=ProtectionManager(state); manager.begin_gap("BTCUSDT","entry-1","protect-2"); manager.fail("BTCUSDT","entry-1","replacement rejected"); paused,reason=state.is_entry_paused(); assert paused; assert "protection failure" in reason; db.close()
