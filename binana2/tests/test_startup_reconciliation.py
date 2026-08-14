from decimal import Decimal
import pytest
from binana2.state.database import Database
from binana2.state.repositories import StateRepository
from binana2.trading.execution_state_machine import ExecutionState
from binana2.trading.order_manager import OrderManager
from binana2.trading.reconciliation import Reconciler

class Exchange:
    def __init__(self,observed=None): self.observed=observed
    async def query_order(self,symbol,*,client_order_id): return self.observed

@pytest.mark.asyncio
async def test_crashed_submitting_order_absent_from_exchange_stays_paused(tmp_path):
    db=Database(tmp_path/"s.db"); state=StateRepository(db); state.create_order_intent(client_order_id="c1",symbol="BTCUSDT",side="BUY",order_type="LIMIT",quantity=Decimal("0.001"),price=Decimal("50000"),state=ExecutionState.ENTRY_SUBMITTING.value); state.set_entry_pause(False,"test"); ex=Exchange(None); orders=OrderManager(ex,state); result=await Reconciler(ex,state,orders).startup(); assert result.unresolved==1; assert state.is_entry_paused()[0]; assert state.get_order("c1").state==ExecutionState.RECONCILIATION_REQUIRED.value; db.close()
