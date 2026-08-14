from binana2.state.database import Database
from binana2.state.repositories import StateRepository

def test_new_database_is_paused_and_pause_survives_restart(tmp_path):
    path=tmp_path/"state.sqlite3"; db=Database(path); state=StateRepository(db); paused,_=state.is_entry_paused(); assert paused is True; state.set_entry_pause(False,"test-only explicit enable"); db.close(); db2=Database(path); state2=StateRepository(db2); paused,reason=state2.is_entry_paused(); assert paused is False; assert reason=="test-only explicit enable"; db2.close()
