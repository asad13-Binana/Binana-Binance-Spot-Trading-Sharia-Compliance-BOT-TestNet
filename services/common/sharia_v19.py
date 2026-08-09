from __future__ import annotations

"""V19.1 Sharia screening controller binding and result validation.

The file shared/sharia/HALAL_CRYPTO_SPOT_SCREENING_V19_1_PRODUCTION.json is the
single authoritative Sharia screening definition. It is IMMUTABLE: this module
verifies its exact SHA-256 before any use and the release suite fails if a
single byte changes. No code in this repository interprets Sharia law itself;
the controller is executed by a registered screening backend and the strictly
validated result is research screening only — not a fatwa.

Everything here fails closed: a missing controller, a wrong hash, a malformed
result, a missing proof card, a wrong ticker, or an unknown code can never
authorize a trade.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from services.common.evidence_providers import (
    is_registered as is_registered_provider,
)
from services.common.evidence_providers import (
    required_record_keys as provider_required_record_keys,
)

V19_CONTROLLER_FILENAME = 'HALAL_CRYPTO_SPOT_SCREENING_V19_1_PRODUCTION.json'
V19_CONTROLLER_SHA256 = '07106bb8bfc1924d8d0c6f61ced4e0c51c2ac2054988423f42c1fd67f3b2ba78'
V19_MAIN_FRAMEWORK = 'V19.1_PRODUCTION_ALL_20_FIXES_APPLIED_SPOT_ONLY'
V19_RUNNER_CONTROLLER = '4.1.0_AI_AGENT_HARDENED_WEB_PARSER_ANTI_HALLUCINATION_LOCK'

FINAL_CODES = {
    'GREEN', 'GREEN_AVOID_OPTIONAL', 'NO_TRADE_INFO', 'NO_TRADE_YIELD',
    'DOUBTFUL', 'HARAM', 'TECH_STOP',
}
TRADE_ELIGIBLE_CODES = {'GREEN', 'GREEN_AVOID_OPTIONAL'}
FAIL_CLOSED_CODE = 'NO_TRADE_INFO'

DIRECT_RESULT_BY_CODE = {
    'GREEN': 'HALAL',
    'GREEN_AVOID_OPTIONAL': 'HALAL',
    'NO_TRADE_INFO': 'NO TRADE',
    'NO_TRADE_YIELD': 'NO TRADE',
    'DOUBTFUL': 'NO TRADE',
    'TECH_STOP': 'TECH STOP',
    'HARAM': 'HARAM',
}
TOKEN_TYPES = {'PAYMENT', 'UTILITY', 'GOVERNANCE', 'TOKENIZED_ASSET',
               'EQUITY_SECURITY', 'STABLECOIN', 'WRAPPED_BRIDGED', 'NFT', 'UNKNOWN'}
MAL_STATUSES = {'CONFIRMED', 'LIKELY', 'DOUBTFUL', 'FAIL'}
SUB_FRAMEWORKS = {'STABLECOIN', 'WRAPPED_BRIDGED', 'GOVERNANCE', 'NONE'}
CONFIDENCE_LEVELS = {'HIGH', 'MEDIUM', 'LOW_MEDIUM', 'LOW'}
TECH_STOP_TRIGGERS = {'T1', 'T2', 'T3', 'NONE'}
NARRATIVE_CODES = {f'N{i}' for i in range(1, 12)}
_PURIFICATION_RE = re.compile(r'^(NO|YES \(\d+(\.\d+)?%\))$')
MIN_HARAM_QUOTE_WORDS = 15
CONTENT_PATH_RE = re.compile(r'^sha256/[0-9a-f]{2}/([0-9a-f]{64})\.bin$')

# These names are the executable form of GREEN_PROOF_GATE.green_requires_all
# in the immutable controller.  Arbitrary true booleans are not evidence.
GREEN_PROOF_CHECKS = {
    'identity_verified',
    'token_type_classified',
    'tier1_official_source_opened',
    'real_utility_official_quote',
    'revenue_clean_or_non_material',
    'no_confirmed_haram_narrative',
    'no_automatic_haram_income',
    'no_unresolved_yield_treasury_reward',
    'no_unresolved_identity_conflict',
    'keyword_scan_completed',
    'no_unresolved_material_contradiction',
    'shariah_screener_check_completed',
}
REQUIRED_WHITEPAPER_SECTIONS = {
    'S1_PROJECT_OVERVIEW', 'S2_TOKEN_UTILITY', 'S3_REVENUE_MODEL',
    'S4_TOKENOMICS', 'S5_STAKING_YIELD', 'S6_GOVERNANCE', 'S7_TREASURY',
    'S8_PARTNERSHIPS', 'S9_COMPLIANCE_LEGAL', 'S10_RISK_FACTORS',
}
SCREENER_SITES = {
    'cryptoummah', 'sharlife', 'islamicfinanceguru', 'saraf',
    'halalscreener', 'gethalalcrypto', 'musaffa',
}
# Exact website identities for the controller's seven named screeners. A
# result labelled ``musaffa`` is not evidence unless it came from Musaffa's
# own host (or one of its subdomains); a caller-chosen URL must not satisfy a
# named external check.
SCREENER_HOSTS = {
    'cryptoummah': frozenset({'cryptoummah.com'}),
    'sharlife': frozenset({'sharlife.my'}),
    'islamicfinanceguru': frozenset({'islamicfinanceguru.com'}),
    'saraf': frozenset({'saraf.app'}),
    'halalscreener': frozenset({'halalscreener.app'}),
    'gethalalcrypto': frozenset({'gethalalcrypto.com'}),
    'musaffa': frozenset({'musaffa.com'}),
}
KEYWORD_CATEGORIES = {
    'RIBA_LENDING_KEYWORDS', 'DEBT_YIELD_KEYWORDS', 'FIXED_APY_KEYWORDS',
    'REBASE_DISTINCTION', 'GAMBLING_KEYWORDS', 'PREDICTION_MARKET_NUANCE',
    'DERIVATIVES_KEYWORDS', 'AUTO_YIELD_KEYWORDS', 'HARAM_BUSINESS_KEYWORDS',
    'STABLECOIN_RESERVE_KEYWORDS', 'LOOTBOX_KEYWORDS',
    'MEME_NO_UTILITY_KEYWORDS', 'VARIABLE_POS_PASS_KEYWORDS',
    'CLEAN_FEE_PASS_KEYWORDS',
}
# Executable form of SOURCE_QUALITY_AND_CONFIDENCE_SCORING.tiers. Both the
# bare and suffixed spellings are accepted because sources_opened already
# uses TIER_1/TIER_1_OFFICIAL interchangeably.
SOURCE_TIERS = {
    'TIER_1', 'TIER_1_OFFICIAL',
    'TIER_2', 'TIER_2_PRIMARY_MARKET',
    'TIER_3', 'TIER_3_SECONDARY',
    'TIER_4', 'TIER_4_WEAK_REJECT',
}


class ControllerIntegrityError(RuntimeError):
    """The immutable V19.1 controller is missing, altered, or unreadable."""


class ResultValidationError(ValueError):
    """A screening result failed strict V19.1 validation. Fail closed."""


def controller_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_controller(path: str | Path) -> tuple[bytes, dict]:
    """Load and verify the immutable controller. Returns (exact bytes, parsed)."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except Exception as exc:
        raise ControllerIntegrityError(f'V19.1 controller unreadable at {path}: {exc}') from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != V19_CONTROLLER_SHA256:
        raise ControllerIntegrityError(
            f'V19.1 controller hash mismatch: expected {V19_CONTROLLER_SHA256}, got {digest}. '
            'The controller is immutable; refusing to run.')
    parsed = json.loads(raw.decode('utf-8'))
    if parsed.get('VERSION') != V19_MAIN_FRAMEWORK:
        raise ControllerIntegrityError('V19.1 controller VERSION field mismatch')
    if parsed.get('RUNNER_CONTROLLER_VERSION') != V19_RUNNER_CONTROLLER:
        raise ControllerIntegrityError('V19.1 runner controller version mismatch')
    return raw, parsed


def _require(condition: bool, message: str):
    if not condition:
        raise ResultValidationError(message)


def normalize_evidence_url(value: object) -> str:
    """Canonicalize an HTTPS provider-evidence URL for exact matching."""
    try:
        parsed = urlparse(str(value or '').strip())
        if (parsed.scheme.lower() != 'https' or not parsed.hostname or
                parsed.username or parsed.password):
            return ''
        host = parsed.hostname.encode('idna').decode('ascii').lower().rstrip('.')
        port = parsed.port
    except (TypeError, ValueError, UnicodeError):
        return ''
    netloc = host if port in (None, 443) else f'{host}:{port}'
    path = parsed.path or '/'
    if path != '/':
        path = path.rstrip('/')
    return urlunparse(('https', netloc, path, parsed.params, parsed.query, ''))


def _provider_tool_evidence_urls(report: dict) -> tuple[set[str], set[str]]:
    """Return all provider URLs and the subset proving content was opened.

    A generic search-result source is discovery evidence, not proof that the
    model opened the page.  Evidentiary URLs therefore come only from provider
    URL citations or completed ``open_page``/``find_in_page`` actions.
    """
    evidence = report.get('tool_evidence')
    _require(isinstance(evidence, dict), 'verdict requires provider tool_evidence')
    # LOCAL-BACKEND-001: this was `== 'openai-responses'`, which made a
    # separately billed external API structurally mandatory — no self-hosted
    # backend could ever produce an accepted verdict.  The registry keeps the
    # check exact (an unregistered name is still rejected, so a forged report
    # cannot invent its own evidence format) while allowing a local backend
    # that is held to *stronger* per-record requirements below.
    provider = evidence.get('provider')
    _require(is_registered_provider(provider),
             'tool_evidence provider is not a registered evidence provider')
    required_keys = provider_required_record_keys(provider)
    calls = evidence.get('completed_web_search_calls')
    _require(isinstance(calls, list) and bool(calls),
             'verdict requires at least one completed provider web search call')
    provider_urls: set[str] = set()
    evidentiary_urls: set[str] = set()
    hashed_evidentiary_urls: set[str] = set()
    for index, call in enumerate(calls):
        _require(isinstance(call, dict),
                 f'tool_evidence completed_web_search_calls[{index}] must be an object')
        _require(call.get('status') == 'completed' and
                  isinstance(call.get('id'), str) and bool(call['id'].strip()),
                  'provider web search call must have an id and completed status')
        action_type = call.get('action_type')
        _require(action_type in {'search', 'open_page', 'find_in_page'},
                 'provider web search call action_type is invalid')
        # A self-hosted retrieval record must prove *what bytes* were read, so
        # every quote can be re-verified offline against the stored content
        # instead of being trusted because the backend reported it.
        _require(required_keys.issubset(call.keys()),
                 f'retrieval record is missing required {provider} evidence keys: '
                 f'{sorted(required_keys - set(call.keys()))}')
        if 'content_sha256' in required_keys:
            _require(isinstance(call.get('content_sha256'), str) and
                     re.fullmatch(r'[0-9a-f]{64}', call['content_sha256']) is not None,
                     'retrieval record content_sha256 must be a lowercase SHA-256 digest')
            _require(call.get('http_status') == 200,
                     'retrieval record must record an HTTP 200 retrieval')
            _require(isinstance(call.get('retrieved_utc'), str) and
                     bool(call['retrieved_utc'].strip()),
                     'retrieval record must record a retrieval timestamp')
            _require(call.get('source_tier') in SOURCE_TIERS,
                     'retrieval record source_tier is invalid')
            content_path = call.get('content_path')
            match = (CONTENT_PATH_RE.fullmatch(content_path)
                     if isinstance(content_path, str) else None)
            _require(match is not None and match.group(1) == call['content_sha256'],
                     'retrieval record content_path must be content-addressed '
                     'by content_sha256')
        source_urls = call.get('source_urls')
        _require(isinstance(source_urls, list),
                 'provider web search call source_urls must be an array')
        normalized_source_urls: set[str] = set()
        for raw_url in source_urls:
            normalized = normalize_evidence_url(raw_url)
            _require(bool(normalized), 'provider web search source URL is invalid')
            normalized_source_urls.add(normalized)
            provider_urls.add(normalized)
            if action_type in {'open_page', 'find_in_page'}:
                evidentiary_urls.add(normalized)
                if 'content_sha256' in required_keys:
                    hashed_evidentiary_urls.add(normalized)
        if 'content_sha256' in required_keys:
            record_url = normalize_evidence_url(call.get('url'))
            _require(bool(record_url),
                     'local retrieval record URL is invalid')
            _require(record_url in normalized_source_urls,
                     'local retrieval record URL must match its source_urls entry')

    citations = evidence.get('url_citations')
    _require(isinstance(citations, list), 'tool_evidence.url_citations must be an array')
    for index, citation in enumerate(citations):
        _require(isinstance(citation, dict),
                 f'tool_evidence url_citations[{index}] must be an object')
        normalized = normalize_evidence_url(citation.get('url'))
        _require(bool(normalized), 'provider URL citation is invalid')
        _require(isinstance(citation.get('title'), str) and
                 bool(citation['title'].strip()),
                 'provider URL citation title must be a non-empty string')
        start, end = citation.get('start_index'), citation.get('end_index')
        _require(isinstance(start, int) and not isinstance(start, bool) and
                 isinstance(end, int) and not isinstance(end, bool) and
                  0 <= start < end,
                  'provider URL citation offsets are invalid')
        if 'content_sha256' in required_keys:
            _require(normalized in hashed_evidentiary_urls,
                     'local provider citation lacks a matching hashed open-page record')
        provider_urls.add(normalized)
        # Hosted-provider citations can themselves demonstrate that a page
        # was opened. Local citations are merely text offsets; the URL becomes
        # evidentiary only through the matching hashed retrieval above.
        if 'content_sha256' not in required_keys:
            evidentiary_urls.add(normalized)
    return provider_urls, evidentiary_urls


def validate_local_evidence_files(report: dict, evidence_root: str | Path) -> None:
    """Re-hash every local evidence object before accepting a new result."""
    evidence = report.get('tool_evidence')
    _require(isinstance(evidence, dict), 'verdict requires provider tool_evidence')
    if evidence.get('provider') != 'local-oracle-v1':
        return
    root = Path(evidence_root).resolve()
    calls = evidence.get('completed_web_search_calls')
    _require(isinstance(calls, list) and bool(calls),
             'local verdict requires completed retrieval records')
    for index, call in enumerate(calls):
        _require(isinstance(call, dict),
                 f'local retrieval record {index} must be an object')
        digest = call.get('content_sha256')
        relative = call.get('content_path')
        match = (CONTENT_PATH_RE.fullmatch(relative)
                 if isinstance(relative, str) else None)
        _require(isinstance(digest, str) and match is not None and
                 match.group(1) == digest,
                 f'local retrieval record {index} content path is invalid')
        target = (root / relative).resolve()
        _require(root in target.parents and target.is_file(),
                 f'local evidence object is missing for retrieval record {index}')
        hasher = hashlib.sha256()
        try:
            with target.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    hasher.update(chunk)
        except OSError as exc:
            raise ResultValidationError(
                f'local evidence object is unreadable for retrieval record {index}: '
                f'{exc}') from exc
        _require(hasher.hexdigest() == digest,
                 f'local evidence digest mismatch for retrieval record {index}')


def _validate_green_evidence(report: dict) -> None:
    _provider_urls, evidentiary_urls = _provider_tool_evidence_urls(report)
    sources = report['sources_opened']
    tier1 = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        parsed = urlparse(str(source.get('url', '')).strip())
        tier = str(source.get('tier', '')).upper()
        quote = str(source.get('quote', '')).strip()
        if (parsed.scheme == 'https' and parsed.hostname and
                tier in {'TIER_1', 'TIER_1_OFFICIAL'} and
                source.get('opened') is True and
                source.get('identity_match') is True and
                len(quote.split()) >= 5):
            source_url = normalize_evidence_url(source.get('url'))
            _require(source_url in evidentiary_urls,
                     'GREEN Tier 1 source lacks provider citation/open-page evidence')
            tier1.append(source)
    _require(bool(tier1),
             'GREEN requires an opened identity-matched Tier 1 source over HTTPS with an exact quote')

    screener = report['shariah_screener_check']
    normalized_screener = {}
    for raw_name, value in screener.items():
        name = str(raw_name).lower().replace('_', '').replace('-', '')
        _require(name not in normalized_screener,
                 f'duplicate normalized Shariah screener result: {name}')
        normalized_screener[name] = value
    _require(SCREENER_SITES <= set(normalized_screener),
             'GREEN requires a completed result for every named Shariah screener site')
    for name in SCREENER_SITES:
        value = normalized_screener[name]
        _require(isinstance(value, str) and bool(value.strip()),
                 f'{name} Shariah screener result must be a non-empty string')

    parsed_sections = report['whitepaper_parsing']
    _require(REQUIRED_WHITEPAPER_SECTIONS <= set(parsed_sections),
             'GREEN requires all ten named whitepaper/website parsing sections')
    for name in REQUIRED_WHITEPAPER_SECTIONS:
        section = parsed_sections.get(name)
        _require(isinstance(section, dict), f'{name} must be an object')
        state = str(section.get('status', '')).upper()
        _require(state in {'FOUND', 'NOT_FOUND', 'PARTIAL'},
                 f'{name}.status must be FOUND, NOT_FOUND, or PARTIAL')
        if state in {'FOUND', 'PARTIAL'}:
            _require(len(str(section.get('quote', '')).split()) >= 3,
                     f'{name} requires an exact supporting quote')
    utility = parsed_sections['S2_TOKEN_UTILITY']
    _require(str(utility.get('status', '')).upper() == 'FOUND' and
             len(str(utility.get('quote', '')).split()) >= 5,
             'GREEN requires an official quote supporting real token utility')

    keyword_results = report['keyword_scan_results']
    _require(KEYWORD_CATEGORIES <= set(keyword_results),
             'GREEN requires every named controller keyword category to be scanned')
    for name in KEYWORD_CATEGORIES:
        result = keyword_results.get(name)
        _require(isinstance(result, dict), f'{name} keyword result must be an object')
        hits = result.get('hits')
        _require(isinstance(hits, int) and not isinstance(hits, bool) and hits >= 0,
                 f'{name}.hits must be a non-negative integer')
        quotes = result.get('quotes')
        _require(isinstance(quotes, list), f'{name}.quotes must be an array')
        _require(all(isinstance(quote, str) and len(quote.split()) >= 3
                     for quote in quotes),
                 f'{name}.quotes must contain non-empty exact quote strings')
        if hits:
            _require(bool(quotes), f'{name} hits require supporting quotes')

    for contradiction in report['contradictions_found']:
        _require(isinstance(contradiction, dict),
                 'GREEN contradictions must be structured objects')
        _require(contradiction.get('material') is False or contradiction.get('resolved') is True,
                 'GREEN cannot contain an unresolved material contradiction')
    resolution = report['contradiction_resolution'].lower()
    _require('unresolved' not in resolution,
             'GREEN contradiction resolution cannot be unresolved')
    _require(report['human_escalation_required'] is False,
             'GREEN cannot require human escalation')

    green = report['green_proof_card']
    _require(GREEN_PROOF_CHECKS <= set(green),
             'GREEN proof card is missing named controller checks')
    _require(all(green.get(name) is True for name in GREEN_PROOF_CHECKS),
             'every named GREEN proof check must be true')


def validate_result(report: dict, *, expected_base: str) -> dict:
    """Strictly validate one V19.1 screening report against OUTPUT_SCHEMA.

    Returns the report when valid. Raises ResultValidationError otherwise —
    the caller must treat any failure as fail-closed NO_TRADE_INFO and must
    never let it authorize a trade.
    """
    _require(isinstance(report, dict), 'report must be a JSON object')

    required_strings = ['coin_name', 'ticker', 'main_framework', 'runner_controller',
                        'token_type', 'mal_status', 'sub_framework_applied',
                        'contradiction_resolution', 'final_code', 'direct_result',
                        'haram_narrative_code', 'haram_narrative_name', 'tech_stop_trigger',
                        'purification_required', 'human_escalation_reason',
                        'next_rescreen_date', 'shariah_result', 'user_personal_action',
                        'confidence_level']
    for field in required_strings:
        _require(isinstance(report.get(field), str) and report.get(field) != '',
                 f'missing or empty required field: {field}')
    for field in ['shariah_screener_check', 'whitepaper_parsing', 'keyword_scan_results',
                  'haram_proof_card', 'green_proof_card', 'tool_evidence']:
        _require(isinstance(report.get(field), dict), f'{field} must be an object')
    for field in ['contradictions_found', 'sources_opened', 'sources_failed', 'tool_access_limits']:
        _require(isinstance(report.get(field), list), f'{field} must be an array')
    _require(isinstance(report.get('human_escalation_required'), bool),
             'human_escalation_required must be a boolean')

    _require(report['main_framework'] == V19_MAIN_FRAMEWORK, 'main_framework mismatch')
    _require(report['runner_controller'] == V19_RUNNER_CONTROLLER, 'runner_controller mismatch')
    _require(report['token_type'] in TOKEN_TYPES, f'invalid token_type {report["token_type"]!r}')
    _require(report['mal_status'] in MAL_STATUSES, f'invalid mal_status {report["mal_status"]!r}')
    _require(report['sub_framework_applied'] in SUB_FRAMEWORKS,
             f'invalid sub_framework_applied {report["sub_framework_applied"]!r}')
    _require(report['confidence_level'] in CONFIDENCE_LEVELS,
             f'invalid confidence_level {report["confidence_level"]!r}')
    _require(report['tech_stop_trigger'] in TECH_STOP_TRIGGERS,
             f'invalid tech_stop_trigger {report["tech_stop_trigger"]!r}')
    _require(bool(_PURIFICATION_RE.match(report['purification_required'])),
             f'invalid purification_required {report["purification_required"]!r}')

    final_code = report['final_code']
    _require(final_code in FINAL_CODES, f'invalid final_code {final_code!r}')
    _require(report['direct_result'] == DIRECT_RESULT_BY_CODE[final_code],
             f'direct_result {report["direct_result"]!r} inconsistent with final_code {final_code!r}')
    _require(final_code != 'TOOL_ACCESS_LIMIT', 'TOOL_ACCESS_LIMIT is metadata, never a verdict')

    try:
        rescreen = datetime.strptime(report['next_rescreen_date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except Exception as exc:
        raise ResultValidationError('next_rescreen_date must be YYYY-MM-DD') from exc
    _require(rescreen > datetime.now(timezone.utc) - __import__('datetime').timedelta(days=1),
             'next_rescreen_date is in the past')

    ticker = str(report['ticker']).upper().replace('/', '').removesuffix('USDT')
    _require(ticker == str(expected_base).upper(),
             f'result identity mismatch: ticker {report["ticker"]!r} != expected base {expected_base!r}')

    proof = report['haram_proof_card']
    if final_code == 'HARAM':
        _require(report['haram_narrative_code'] in NARRATIVE_CODES,
                 'HARAM requires a named narrative N1-N11')
        _require(report['haram_narrative_name'] not in ('', 'NOT_PROVEN'),
                 'HARAM requires a narrative name')
        for condition in ('C1', 'C2', 'C3', 'C4', 'C5'):
            _require(proof.get(condition) is True,
                     f'HARAM proof card condition {condition} not proven')
        quote = str(proof.get('quote') or '')
        _require(len(quote.split()) >= MIN_HARAM_QUOTE_WORDS,
                 f'HARAM verbatim quote must be at least {MIN_HARAM_QUOTE_WORDS} words')
        proof_url = normalize_evidence_url(proof.get('url'))
        _require(bool(proof_url), 'HARAM proof card requires a valid HTTPS source URL')
        _require(bool(str(proof.get('tier') or '').strip()), 'HARAM proof card requires a source tier')
        _provider_urls, evidentiary_urls = _provider_tool_evidence_urls(report)
        _require(proof_url in evidentiary_urls,
                 'HARAM proof URL lacks provider citation/open-page evidence')
        _require(any(
            isinstance(source, dict)
            and source.get('opened') is True
            and source.get('identity_match') is True
            and normalize_evidence_url(source.get('url')) == proof_url
            for source in report['sources_opened']
        ), 'HARAM proof URL must match an opened identity-bound source')
    else:
        _require(report['haram_narrative_code'] in NARRATIVE_CODES | {'NOT_PROVEN'},
                 'haram_narrative_code must be N1-N11 or NOT_PROVEN')

    if final_code in TRADE_ELIGIBLE_CODES:
        _require(report['haram_narrative_code'] == 'NOT_PROVEN',
                 'GREEN result cannot carry a proven haram narrative')
        _require(report['tech_stop_trigger'] == 'NONE', 'GREEN result cannot carry a TECH_STOP trigger')
        _validate_green_evidence(report)

    if final_code == 'TECH_STOP':
        _require(report['tech_stop_trigger'] in {'T1', 'T2', 'T3'},
                 'TECH_STOP requires trigger T1, T2 or T3')

    return report


def fail_closed_report(base: str, *, reason: str, sources_failed=None) -> dict:
    """An internal, schema-complete fail-closed NO_TRADE_INFO record.

    Produced locally when screening could not run or its output was invalid
    (tool failure, quota, malformed response, timeout). It follows the V19.1
    rule that tool failure is never HARAM and TOOL_ACCESS_LIMIT is never a
    verdict; it can never authorize a trade.
    """
    today = datetime.now(timezone.utc).date()
    return {
        'coin_name': base.upper(), 'ticker': base.upper(),
        'main_framework': V19_MAIN_FRAMEWORK, 'runner_controller': V19_RUNNER_CONTROLLER,
        'token_type': 'UNKNOWN', 'mal_status': 'DOUBTFUL', 'sub_framework_applied': 'NONE',
        'shariah_screener_check': {}, 'whitepaper_parsing': {}, 'keyword_scan_results': {},
        'contradictions_found': [], 'contradiction_resolution': 'not-run',
        'sources_opened': [], 'sources_failed': list(sources_failed or []),
        'tool_access_limits': [reason],
        'tool_evidence': {'provider': 'local-fail-closed',
                          'completed_web_search_calls': [], 'url_citations': []},
        'final_code': FAIL_CLOSED_CODE, 'direct_result': 'NO TRADE',
        'haram_narrative_code': 'NOT_PROVEN', 'haram_narrative_name': 'NOT_PROVEN',
        'haram_proof_card': {'C1': False, 'C2': False, 'C3': False, 'C4': False, 'C5': False,
                             'quote': None, 'url': None, 'tier': None,
                             'note': 'HARAM gate not triggered or not applicable'},
        'green_proof_card': {},
        'tech_stop_trigger': 'NONE', 'purification_required': 'NO',
        'human_escalation_required': False,
        'human_escalation_reason': 'none — fail-closed local record, screening did not complete',
        'next_rescreen_date': (today + __import__('datetime').timedelta(days=1)).isoformat(),
        'shariah_result': f'NO TRADE under this screening — screening unavailable: {reason}',
        'user_personal_action': 'Do not trade this asset until a valid V19.1 screening completes.',
        'confidence_level': 'LOW',
        'fail_closed': True, 'fail_closed_reason': reason,
    }
