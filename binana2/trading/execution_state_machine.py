from __future__ import annotations

from enum import StrEnum


class ExecutionState(StrEnum):
    SIGNAL = "SIGNAL"
    SHARIA_APPROVED = "SHARIA_APPROVED"
    RISK_APPROVED = "RISK_APPROVED"
    ENTRY_SUBMITTING = "ENTRY_SUBMITTING"
    ENTRY_UNKNOWN = "ENTRY_UNKNOWN"
    ENTRY_OPEN = "ENTRY_OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PROTECTION_PENDING = "PROTECTION_PENDING"
    PROTECTED = "PROTECTED"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    ENTRY_REJECTED = "ENTRY_REJECTED"
    PROTECTION_FAILED = "PROTECTION_FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    SAFETY_PAUSE = "SAFETY_PAUSE"
    MANUAL_INTERVENTION_REQUIRED = "MANUAL_INTERVENTION_REQUIRED"


_ALLOWED: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.SIGNAL: frozenset({ExecutionState.SHARIA_APPROVED, ExecutionState.ENTRY_REJECTED, ExecutionState.SAFETY_PAUSE}),
    ExecutionState.SHARIA_APPROVED: frozenset({ExecutionState.RISK_APPROVED, ExecutionState.ENTRY_REJECTED, ExecutionState.SAFETY_PAUSE}),
    ExecutionState.RISK_APPROVED: frozenset({ExecutionState.ENTRY_SUBMITTING, ExecutionState.SAFETY_PAUSE}),
    ExecutionState.ENTRY_SUBMITTING: frozenset({ExecutionState.ENTRY_UNKNOWN, ExecutionState.ENTRY_OPEN, ExecutionState.PARTIALLY_FILLED, ExecutionState.FILLED, ExecutionState.ENTRY_REJECTED, ExecutionState.RECONCILIATION_REQUIRED, ExecutionState.SAFETY_PAUSE}),
    ExecutionState.ENTRY_UNKNOWN: frozenset({ExecutionState.ENTRY_OPEN, ExecutionState.PARTIALLY_FILLED, ExecutionState.FILLED, ExecutionState.ENTRY_REJECTED, ExecutionState.RECONCILIATION_REQUIRED, ExecutionState.SAFETY_PAUSE}),
    ExecutionState.ENTRY_OPEN: frozenset({ExecutionState.PARTIALLY_FILLED, ExecutionState.FILLED, ExecutionState.ENTRY_REJECTED, ExecutionState.RECONCILIATION_REQUIRED, ExecutionState.EXITING, ExecutionState.SAFETY_PAUSE}),
    ExecutionState.PARTIALLY_FILLED: frozenset({ExecutionState.FILLED, ExecutionState.EXITING, ExecutionState.RECONCILIATION_REQUIRED, ExecutionState.SAFETY_PAUSE}),
    ExecutionState.FILLED: frozenset({ExecutionState.PROTECTION_PENDING, ExecutionState.EXITING, ExecutionState.SAFETY_PAUSE}),
    ExecutionState.PROTECTION_PENDING: frozenset({ExecutionState.PROTECTED, ExecutionState.PROTECTION_FAILED, ExecutionState.EXITING, ExecutionState.SAFETY_PAUSE}),
    ExecutionState.PROTECTED: frozenset({ExecutionState.EXITING, ExecutionState.PROTECTION_FAILED, ExecutionState.SAFETY_PAUSE}),
    ExecutionState.PROTECTION_FAILED: frozenset({ExecutionState.RECONCILIATION_REQUIRED, ExecutionState.EXITING, ExecutionState.MANUAL_INTERVENTION_REQUIRED, ExecutionState.SAFETY_PAUSE}),
    ExecutionState.EXITING: frozenset({ExecutionState.CLOSED, ExecutionState.RECONCILIATION_REQUIRED, ExecutionState.MANUAL_INTERVENTION_REQUIRED, ExecutionState.SAFETY_PAUSE}),
    ExecutionState.RECONCILIATION_REQUIRED: frozenset({ExecutionState.ENTRY_OPEN, ExecutionState.PARTIALLY_FILLED, ExecutionState.FILLED, ExecutionState.PROTECTION_PENDING, ExecutionState.PROTECTED, ExecutionState.EXITING, ExecutionState.CLOSED, ExecutionState.MANUAL_INTERVENTION_REQUIRED, ExecutionState.SAFETY_PAUSE}),
    ExecutionState.SAFETY_PAUSE: frozenset({ExecutionState.RECONCILIATION_REQUIRED, ExecutionState.MANUAL_INTERVENTION_REQUIRED}),
    ExecutionState.ENTRY_REJECTED: frozenset(), ExecutionState.MANUAL_INTERVENTION_REQUIRED: frozenset(), ExecutionState.CLOSED: frozenset(),
}


class InvalidTransition(RuntimeError):
    pass


def assert_transition(current: ExecutionState, target: ExecutionState) -> None:
    if current == target:
        return
    if target not in _ALLOWED[current]:
        raise InvalidTransition(f"illegal execution transition: {current} -> {target}")
