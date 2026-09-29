"""Fail-closed owner decisions bound to one immutable local evidence report."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.common.sharia_v19 import (
    GREEN_PROOF_CHECKS,
    validate_local_evidence_files,
    validate_result,
)

DECISION_ID_RE = re.compile(r'^[0-9a-f]{32}$')
REPORT_FILE_RE = re.compile(
    r'^(?P<base>[A-Z0-9]+)_(?P<request>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json$')
SCOPE_CONFIRMATION = 'I REVIEWED THE QUOTED SCOPE'


class OwnerDecisionError(ValueError):
    """An owner decision was malformed, stale, unbound or unsafe."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise OwnerDecisionError(message)


def _load_bound_proposal(payload: dict, reports_root: str | Path) -> tuple[dict, str]:
    base = str(payload.get('base', '')).upper().strip()
    pair = str(payload.get('pair', '')).upper().strip()
    report_name = str(payload.get('report_file', '')).strip()
    expected_sha = str(payload.get('report_sha256', '')).lower().strip()
    match = REPORT_FILE_RE.fullmatch(report_name)
    _require(base.isalnum() and pair == f'{base}/USDT',
             'owner decision base/pair binding is invalid')
    _require(match is not None and match.group('base') == base,
             'owner decision report filename is invalid or belongs to another asset')
    _require(str(payload.get('proposal_request_id', '')).strip() ==
             match.group('request'),
             'owner decision proposal_request_id does not match report filename')
    _require(re.fullmatch(r'[0-9a-f]{64}', expected_sha) is not None,
             'owner decision report SHA-256 is invalid')

    root = Path(reports_root).resolve()
    path = (root / report_name).resolve()
    _require(root in path.parents and path.is_file(),
             'owner decision proposal report is missing')
    raw = path.read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    _require(actual_sha == expected_sha,
             'owner decision proposal hash mismatch; stale or changed card rejected')
    try:
        report = json.loads(raw)
    except Exception as exc:
        raise OwnerDecisionError(f'proposal report is not valid JSON: {exc}') from exc
    _require(isinstance(report, dict), 'proposal report must be a JSON object')
    _require(str(report.get('ticker', '')).upper() == base,
             'proposal report ticker does not match owner decision')
    return report, actual_sha


def _green_from_proposal(report: dict, payload: dict, proposal_sha: str,
                         evidence_root: str | Path) -> dict:
    base = str(payload['base']).upper()
    review = report.get('local_review')
    _require(isinstance(review, dict) and review.get('schema_version') == 1,
             'only a local-review proposal may be owner-approved')
    _require(report.get('final_code') == 'NO_TRADE_INFO',
             'only a fail-closed review proposal may be promoted')
    _require((report.get('tool_evidence') or {}).get('provider') == 'local-oracle-v1',
             'proposal was not produced by the local Oracle backend')
    _require(review.get('promotable') is True,
             'proposal is not mechanically eligible for owner approval')
    _require(review.get('all_registered_sources_opened') is True,
             'proposal has one or more unavailable registered sources')
    checks = review.get('green_checks')
    _require(isinstance(checks, dict) and GREEN_PROOF_CHECKS <= set(checks),
             'proposal is missing named GREEN proof checks')
    _require(all(checks.get(name) is True for name in GREEN_PROOF_CHECKS),
             'proposal has a failed GREEN proof check')
    _require(not report.get('sources_failed'),
             'proposal with a failed source cannot be approved')
    if review.get('scope_review_only') is True:
        _require(payload.get('scope_confirmation') == SCOPE_CONFIRMATION,
                 'scope-review proposal requires the explicit owner confirmation phrase')

    # Re-hash every retained source before transforming the proposal. This
    # proves the owner decision still refers to the bytes shown by the card.
    validate_local_evidence_files(report, evidence_root)
    approved = copy.deepcopy(report)
    approved.update({
        'mal_status': 'CONFIRMED',
        'sub_framework_applied': 'NONE',
        'tool_access_limits': [],
        'final_code': 'GREEN',
        'direct_result': 'HALAL',
        'haram_narrative_code': 'NOT_PROVEN',
        'haram_narrative_name': 'NOT_PROVEN',
        'green_proof_card': {name: True for name in GREEN_PROOF_CHECKS},
        'human_escalation_required': False,
        'human_escalation_reason': 'none - exact local evidence proposal approved by owner',
        'next_rescreen_date': (
            datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat(),
        'shariah_result': 'HALAL under this owner-approved local screening',
        'user_personal_action': 'Spot buy/sell permitted until the next local rescreen',
        'confidence_level': 'HIGH',
        'owner_approval': {
            'decision_id': str(payload['decision_id']),
            'proposal_report_sha256': proposal_sha,
            'proposal_request_id': str(payload.get('proposal_request_id', '')),
            'decided_at': str(payload.get('decided_at', '')),
            'scope_confirmation': bool(review.get('scope_review_only')),
            'research_only_not_fatwa': True,
        },
    })
    approved['local_review']['owner_decision_required'] = False
    approved['local_review']['owner_decision'] = 'APPROVE'
    validate_result(approved, expected_base=base)
    validate_local_evidence_files(approved, evidence_root)
    return approved


def _rejected_proposal(report: dict, payload: dict, proposal_sha: str) -> dict:
    rejected = copy.deepcopy(report)
    rejected.update({
        'final_code': 'NO_TRADE_INFO',
        'direct_result': 'NO TRADE',
        'human_escalation_required': False,
        'human_escalation_reason': 'none - owner rejected the local review proposal',
        'shariah_result': 'NO TRADE - owner rejected this local screening proposal',
        'user_personal_action': 'Do not trade; rescan only after source information changes',
        'confidence_level': 'HIGH',
        'owner_approval': {
            'decision_id': str(payload['decision_id']),
            'proposal_report_sha256': proposal_sha,
            'proposal_request_id': str(payload.get('proposal_request_id', '')),
            'decided_at': str(payload.get('decided_at', '')),
            'decision': 'REJECT',
            'research_only_not_fatwa': True,
        },
    })
    review = rejected.get('local_review')
    if isinstance(review, dict):
        review['owner_decision_required'] = False
        review['owner_decision'] = 'REJECT'
    validate_result(rejected, expected_base=str(payload['base']).upper())
    return rejected


def apply_owner_decision(payload: dict, *, reports_root: str | Path,
                         evidence_root: str | Path) -> tuple[dict, str]:
    """Validate and apply an authenticated owner decision.

    Returns ``(result_report, decision_request_id)``. The returned report is
    still passed through the normal bridge validator and Ed25519/HMAC signing
    path by the service.
    """
    _require(isinstance(payload, dict), 'owner decision payload must be an object')
    decision_id = str(payload.get('decision_id', '')).lower().strip()
    action = str(payload.get('action', '')).upper().strip()
    _require(DECISION_ID_RE.fullmatch(decision_id) is not None,
             'owner decision_id must be 32 lowercase hexadecimal characters')
    _require(action in {'APPROVE', 'REJECT'},
             'owner decision action must be APPROVE or REJECT')
    proposal, proposal_sha = _load_bound_proposal(payload, reports_root)
    if action == 'APPROVE':
        report = _green_from_proposal(
            proposal, payload, proposal_sha, evidence_root)
    else:
        report = _rejected_proposal(proposal, payload, proposal_sha)
    return report, f'decision-{decision_id}'
