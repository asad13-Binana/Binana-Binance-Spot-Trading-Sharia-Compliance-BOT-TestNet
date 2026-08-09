"""Deterministic execution of the immutable V19.1 screening controller.

Nothing in this package interprets Sharia law. Every rule, keyword, narrative
code, source tier and gate condition is read at runtime from the immutable
controller JSON, so the controller stays the single authoritative definition
and this package is only its executor. A rule that is not in the controller
cannot be applied, and changing a rule means changing the controller (whose
SHA-256 is pinned and verified before use).

The engine can reach three conclusions on its own, all of them restrictive:
HARAM, NO_TRADE_INFO, or DOUBTFUL. It can never conclude GREEN. A clean
result is only ever a *proposal* that the owner must approve and sign.
"""
from services.sharia_rules.engine import (  # noqa: F401
    Disposition,
    KeywordHit,
    RetrievedDocument,
    RulesFinding,
    evaluate,
)
