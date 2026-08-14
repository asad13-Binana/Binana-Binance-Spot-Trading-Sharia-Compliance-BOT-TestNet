import pytest
from binana2.trading.execution_state_machine import ExecutionState, InvalidTransition, assert_transition

def test_unknown_can_reconcile_to_fill(): assert_transition(ExecutionState.ENTRY_SUBMITTING,ExecutionState.ENTRY_UNKNOWN); assert_transition(ExecutionState.ENTRY_UNKNOWN,ExecutionState.FILLED)
def test_closed_cannot_reopen():
    with pytest.raises(InvalidTransition): assert_transition(ExecutionState.CLOSED,ExecutionState.ENTRY_OPEN)
