"""Read-only Sharia readiness projection; does not approve or modify records."""
from services.universe_service.sharia_gate import load_sharia_gate


def screening_readiness(path):
    try:
        gate = load_sharia_gate(path)
        eligible = sum(gate.decision(base).allowed for base in gate.records)
        return {'eligible_assets': eligible,
                'sharia_trade_ready': eligible > 0,
                'eligibility_blocker': '' if eligible else 'OWNER_REVIEWED_ELIGIBLE_EVIDENCE_REQUIRED'}
    except Exception:
        return {'eligible_assets': 0, 'sharia_trade_ready': False,
                'eligibility_blocker': 'SHARIA_STATUS_UNREADABLE_OR_UNVERIFIABLE'}
