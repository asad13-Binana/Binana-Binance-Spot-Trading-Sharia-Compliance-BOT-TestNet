from decimal import Decimal
import pytest
from binana2.exchange.base import ExchangeOrder, OrderIntent, OrderSide, OrderType, UnknownExecutionStatus
from binana2.state.database import Database
from binana2.state.repositories import StateRepository
from binana2.trading.execution_state_machine import ExecutionState
from binana2.trading.order_manager import OrderManager

class FakeExchange:
    def __init__(self): self.query_result=None
    async def place_order(self,intent): raise UnknownExecutionStatus("timeout",client_order_id=intent.client_order_id)
    async def query_order(self,symbol,*,client_order_id): return self.query_result

@pytest.mark.asyncio
async def test_unknown_submission_is_durable_and_never_resubmitted(tmp_path):
    db=Database(tmp_path/"state.db"); state=StateRepository(db); exchange=FakeExchange(); manager=OrderManager(exchange,state); intent=OrderIntent("BTCUSDT",OrderSide.BUY,OrderType.LIMIT,Decimal("0.001"),"binana-test-1",price=Decimal("50000"),time_in_force="GTC")
    result=await manager.submit_entry(intent); assert result.state is ExecutionState.ENTRY_UNKNOWN; assert state.get_order(intent.client_order_id).state==ExecutionState.ENTRY_UNKNOWN.value
    exchange.query_result=ExchangeOrder(symbol="BTCUSDT",order_id=42,client_order_id=intent.client_order_id,status="FILLED",side="BUY",order_type="LIMIT",orig_qty=Decimal("0.001"),executed_qty=Decimal("0.001"),cumulative_quote_qty=Decimal("50"),raw={"status":"FILLED"})
    reconciled=await manager.reconcile_unknown(intent.client_order_id,attempts=1); assert reconciled.state is ExecutionState.FILLED; assert state.get_order(intent.client_order_id).exchange_order_id==42; db.close()
