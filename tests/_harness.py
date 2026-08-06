"""Shared test harness for V10.1 + V19.1.

Sets deterministic bus keys and a release-hash binding at import so that
HMAC-envelope signing/verification works inside the offline test suite, and
provides helpers to construct signed signal/command envelopes, V19.1 status
projections, and a real POSIX bash resolver (the bare `bash` name on a Windows
dev host without WSL resolves to the non-functional WSL stub).

Importing this module must precede importing any service module that reads a
bus key at construction time.
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

TEST_RELEASE_HASH = 'a' * 64
TEST_SHARIA_PRIVATE_KEY_B64 = 'MC4CAQAwBQYDK2VwBCIEIJ/Aq/D8B6gWr/2Qi2sNSk722YRvTITjusOD90DJGHnn'
TEST_SHARIA_PUBLIC_KEY_B64 = 'MCowBQYDK2VwAyEAODWRab3eQeB77rQ6NmSc1rSwVcOzsbOTxPOdfov48Yk='
os.environ.setdefault('SIGNAL_HMAC_KEY', 'test-signal-key-0123456789abcdef0123')
os.environ.setdefault('COMMAND_HMAC_KEY', 'test-command-key-0123456789abcdef012')
os.environ.setdefault('SHARIA_HMAC_KEY', 'test-sharia-key-0123456789abcdef0123')
os.environ.setdefault('SHARIA_RESULT_HMAC_KEY', 'test-sharia-result-key-0123456789abcdef')
os.environ.setdefault('SHARIA_RESULT_SIGNING_PRIVATE_KEY_B64', TEST_SHARIA_PRIVATE_KEY_B64)
os.environ.setdefault('SHARIA_RESULT_VERIFY_PUBLIC_KEY_B64', TEST_SHARIA_PUBLIC_KEY_B64)
os.environ.setdefault('LIVE_EVIDENCE_KEY', 'test-live-evidence-key-0123456789abc')
os.environ.setdefault('ENVELOPE_RELEASE_HASH', TEST_RELEASE_HASH)

from services.common import envelope  # noqa: E402
from services.common.sharia_v19 import (  # noqa: E402
    GREEN_PROOF_CHECKS, KEYWORD_CATEGORIES, REQUIRED_WHITEPAPER_SECTIONS,
    V19_CONTROLLER_FILENAME, V19_CONTROLLER_SHA256, V19_MAIN_FRAMEWORK,
    V19_RUNNER_CONTROLLER,
)
from services.common.sharia_attestation import RESULT_PURPOSE, STATUS_PURPOSE, attach  # noqa: E402

TEST_BUS_KEYS = {
    'SIGNAL_HMAC_KEY': os.environ['SIGNAL_HMAC_KEY'],
    'COMMAND_HMAC_KEY': os.environ['COMMAND_HMAC_KEY'],
    'SHARIA_HMAC_KEY': os.environ['SHARIA_HMAC_KEY'],
    'SHARIA_RESULT_HMAC_KEY': os.environ['SHARIA_RESULT_HMAC_KEY'],
    'SHARIA_RESULT_SIGNING_PRIVATE_KEY_B64': os.environ['SHARIA_RESULT_SIGNING_PRIVATE_KEY_B64'],
    'SHARIA_RESULT_VERIFY_PUBLIC_KEY_B64': os.environ['SHARIA_RESULT_VERIFY_PUBLIC_KEY_B64'],
    'LIVE_EVIDENCE_KEY': os.environ['LIVE_EVIDENCE_KEY'],
    'ENVELOPE_RELEASE_HASH': TEST_RELEASE_HASH,
}


def sign_command(command: str, args=None, *, command_id: str, created_at: float | None = None,
                 ttl: int = 300) -> dict:
    payload = {'command_id': command_id, 'command': command, 'args': args or {},
               'created_at': created_at if created_at is not None else _now_ts()}
    return envelope.sign_envelope(producer='telegram-broker', purpose=envelope.BUS_COMMAND,
                                  payload=payload, ttl_seconds=ttl)


def sign_signal(payload: dict, *, ttl: int = 300) -> dict:
    return envelope.sign_envelope(producer='freqtrade-strategy', purpose=envelope.BUS_SIGNAL,
                                  payload=payload, ttl_seconds=ttl)


def sign_sharia_result(payload: dict, *, ttl: int = 3600) -> dict:
    payload = attach(payload, purpose=RESULT_PURPOSE)
    return envelope.sign_envelope(producer='sharia-screener', purpose=envelope.BUS_SHARIA_RESULT,
                                  payload=payload, ttl_seconds=ttl)


def _now_ts() -> float:
    # Fixed offset from epoch is unnecessary; time.time() is fine in tests.
    import time
    return time.time()


def v19_status(records, *, controller_sha256: str = V19_CONTROLLER_SHA256) -> dict:
    """Build a schema_version-2 V19.1 status projection.

    records: iterable of (base, status) or (base, status, expires_at_iso).
    """
    now = datetime.now(timezone.utc)
    future = (now + timedelta(days=30)).isoformat()
    out = []
    for rec in records:
        base, status = rec[0], rec[1]
        expires = rec[2] if len(rec) > 2 else future
        out.append({
            'symbol': base, 'status': status, 'final_code': status,
            'reviewed_at': now.date().isoformat(), 'expires_at': expires,
            'source': 'test-v19-screener',
        })
    return {
        'schema_version': 2, 'controller_sha256': controller_sha256,
        'controller_version': V19_MAIN_FRAMEWORK,
        'generated_at': now.isoformat(), 'records': out,
    }


def green_report(base: str = 'ETH', code: str = 'GREEN') -> dict:
    sections = {name: {'status': 'NOT_FOUND', 'quote': ''}
                for name in REQUIRED_WHITEPAPER_SECTIONS}
    sections['S1_PROJECT_OVERVIEW'] = {
        'status': 'FOUND', 'quote': 'The official project provides decentralized payment utility.'}
    sections['S2_TOKEN_UTILITY'] = {
        'status': 'FOUND', 'quote': 'The token pays for useful network transaction services.'}
    sections['S3_REVENUE_MODEL'] = {
        'status': 'FOUND', 'quote': 'Revenue comes from ordinary network service fees.'}
    return {
        'coin_name': base, 'ticker': base, 'main_framework': V19_MAIN_FRAMEWORK,
        'runner_controller': V19_RUNNER_CONTROLLER, 'token_type': 'PAYMENT',
        'mal_status': 'CONFIRMED', 'sub_framework_applied': 'NONE',
        'shariah_screener_check': {name: 'not listed' for name in (
            'cryptoummah', 'sharlife', 'islamicfinanceguru', 'saraf',
            'halalscreener', 'gethalalcrypto', 'musaffa')},
        'whitepaper_parsing': sections,
        'keyword_scan_results': {name: {'hits': 0, 'quotes': []}
                                 for name in KEYWORD_CATEGORIES},
        'contradictions_found': [], 'contradiction_resolution': 'all checked; none found',
        'sources_opened': [{
            'url': 'https://example.org/whitepaper', 'tier': 'TIER_1_OFFICIAL',
            'opened': True, 'identity_match': True,
            'quote': 'The official project provides useful network transaction services.',
        }],
        'sources_failed': [], 'tool_access_limits': [], 'final_code': code,
        'tool_evidence': {
            'provider': 'openai-responses',
            'completed_web_search_calls': [{
                'id': 'ws_test_fixture', 'status': 'completed', 'action_type': 'search',
                'source_urls': ['https://example.org/whitepaper'],
            }],
            'url_citations': [{
                'url': 'https://example.org/whitepaper', 'title': 'Official whitepaper',
                'start_index': 0, 'end_index': 10,
            }],
        },
        'direct_result': 'HALAL', 'haram_narrative_code': 'NOT_PROVEN',
        'haram_narrative_name': 'NOT_PROVEN',
        'haram_proof_card': {'C1': False, 'C2': False, 'C3': False, 'C4': False,
                             'C5': False, 'quote': None, 'url': None, 'tier': None},
        'green_proof_card': {name: True for name in GREEN_PROOF_CHECKS},
        'tech_stop_trigger': 'NONE', 'purification_required': 'NO',
        'human_escalation_required': False, 'human_escalation_reason': 'none',
        'next_rescreen_date': (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat(),
        'shariah_result': 'HALAL under this screening',
        'user_personal_action': 'Spot buy/sell permitted', 'confidence_level': 'HIGH',
    }


def write_attested_status(path: Path, records) -> Path:
    """Write a test projection with real controller/report/Ed25519 bindings."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    controller = Path(__file__).resolve().parents[1] / 'shared/sharia' / V19_CONTROLLER_FILENAME
    (path.parent / V19_CONTROLLER_FILENAME).write_bytes(controller.read_bytes())
    report_dir = path.parent / 'reports'
    report_dir.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    projected = []
    for index, item in enumerate(records):
        base, code = item[0].upper(), item[1]
        expiry = datetime.fromisoformat(item[2].replace('Z', '+00:00')) if len(item) > 2 else now + timedelta(days=30)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if code not in {'GREEN', 'GREEN_AVOID_OPTIONAL'}:
            projected.append(v19_status([(base, code, expiry.isoformat())])['records'][0])
            continue
        completed = min(now, expiry - timedelta(days=1))
        request_id = f'test-{base}-{index}'
        report = green_report(base, code)
        report_name = f'{base}_{request_id}.json'
        report_path = report_dir / report_name
        report_path.write_text(json.dumps(report, sort_keys=True), encoding='utf-8')
        projected.append(attach({
            'symbol': base, 'pair': f'{base}/USDT', 'status': code, 'final_code': code,
            'validated': True, 'reviewed_at': completed.date().isoformat(),
            'completed_at': completed.isoformat(), 'expires_at': expiry.isoformat(),
            'source': 'sharia-screener/v19.1', 'controller_sha256': V19_CONTROLLER_SHA256,
            'confidence': 'HIGH', 'human_escalation_required': False,
            'request_id': request_id, 'report_file': report_name,
            'report_sha256': hashlib.sha256(report_path.read_bytes()).hexdigest(),
        }, purpose=STATUS_PURPOSE))
    path.write_text(json.dumps({
        'schema_version': 2, 'controller_sha256': V19_CONTROLLER_SHA256,
        'controller_version': V19_MAIN_FRAMEWORK, 'generated_at': now.isoformat(),
        'records': projected,
    }), encoding='utf-8')
    return path


def write_live_evidence(runtime_dir, release_hash: str, strategy_path,
                        *, controller_sha256: str = V19_CONTROLLER_SHA256,
                        trades: int = 120) -> Path:
    """Build and sign a complete live-promotion evidence envelope (C-003/H-007).

    Binds the envelope to `release_hash` so the sidecar's live gate can verify
    it. Used to prove the STRONGER gate can still pass with full evidence.
    """
    from services.common.strategy_fingerprint import fingerprints
    prints = fingerprints(Path(strategy_path), 'IctSmcStrategy', [
        'populate_indicators_5m', 'populate_indicators',
        'populate_entry_trend', 'populate_exit_trend'])
    payload = {
        'release_hash': release_hash, 'controller_sha256': controller_sha256,
        'strategy_fingerprints': prints,
        'freqtrade_backtest': {
            'strategy': 'IctSmcStrategy', 'artifact_sha256': 'd' * 64,
            'trades': trades, 'timerange': '20260101-20260301',
        },
        'assertions': {
            'binance_spot_testnet_lifecycle_passed': True,
            'oracle_deployment_soak_completed': True,
            'step1_clean_passes_completed': True,
            'final_clean_passes_completed': True,
        },
    }
    old = os.environ.get('ENVELOPE_RELEASE_HASH')
    os.environ['ENVELOPE_RELEASE_HASH'] = release_hash
    try:
        signed = envelope.sign_envelope(
            producer='release-certifier', purpose=envelope.BUS_LIVE_EVIDENCE,
            payload=payload, ttl_seconds=3600)
    finally:
        if old is not None:
            os.environ['ENVELOPE_RELEASE_HASH'] = old
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target = runtime_dir / 'LIVE_EVIDENCE.json'
    target.write_text(json.dumps(signed), encoding='utf-8')
    return target


def posix_bash() -> str | None:
    """Path to a working POSIX bash, or None. Avoids the Windows WSL stub."""
    candidates = []
    env_bash = os.environ.get('POSIX_BASH') or os.environ.get('BASH')
    if env_bash:
        candidates.append(env_bash)
    candidates += [
        r'C:\Program Files\Git\bin\bash.exe',
        r'C:\Program Files\Git\usr\bin\bash.exe',
        r'C:\Program Files (x86)\Git\bin\bash.exe',
        '/usr/bin/bash', '/bin/bash',
    ]
    which = shutil.which('bash')
    if which:
        candidates.append(which)
    for cand in candidates:
        if not cand or not Path(cand).exists():
            continue
        try:
            proc = subprocess.run([cand, '-c', 'echo ok'], capture_output=True,
                                  text=True, timeout=10)
        except Exception:
            continue
        if proc.returncode == 0 and 'ok' in proc.stdout:
            return cand
    return None
