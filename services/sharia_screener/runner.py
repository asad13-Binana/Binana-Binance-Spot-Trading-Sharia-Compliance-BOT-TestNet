from __future__ import annotations
"""AI screening runner for the immutable V19.1 controller.

Executes the complete, byte-exact controller through an external AI model
with hosted web search (OpenAI Responses API wire format; the base URL and
model are configurable). Every failure mode — missing key, missing model,
quota exhaustion, rate limit, network fault, malformed output — raises
ScreeningUnavailable and therefore fails closed to NO_TRADE_INFO upstream.

Cost/billing note (master protocol 8.8): API usage is billed separately from
any ChatGPT Plus or Claude Pro subscription. No API key ships with this
repository; SHARIA_OPENAI_API_KEY must be provided through the private
deployment environment. The model name must be configured explicitly via
SHARIA_MODEL — the runner never silently substitutes a model.
"""
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from services.common.config_bounds import env_int
from services.common.sharia_v19 import normalize_evidence_url

log = logging.getLogger('sharia-runner')

WRAPPER_PROMPT = (
    'Apply the following immutable screening controller to exactly one Binance Spot asset. '
    'Use live web search, actually open sources, follow every mandate, and return only one '
    'JSON object matching OUTPUT_SCHEMA. If evidence is incomplete, use the controller\'s '
    'fail-closed non-HARAM code. Asset: {base}; Binance pair: {pair}.\n\nCONTROLLER:\n{controller}'
)


class ScreeningUnavailable(RuntimeError):
    """Screening could not produce a valid result. Callers must fail closed."""


PACKAGE_MODE_FILE = Path(__file__).resolve().parents[2] / 'RELEASE_MODE'
LIVE_POLICY_FILE = Path(__file__).resolve().parents[2] / 'VALIDATION_STATUS.json'


def enforce_live_screening_policy(*, package_mode_path: str | Path | None = None,
                                  policy_path: str | Path | None = None,
                                  execution_mode: str | None = None) -> None:
    """Enforce the package/execution contract and bind live AI provenance.

    Both packages may screen in simulation, and the testnet package may also
    screen in testnet mode.  Only ``EXECUTION_MODE=live`` in the immutable live
    package requires an approved ``sharia_live_policy``.  This preserves the
    safe simulation default without allowing environment variables to create
    live authority or to widen an approved live host/model policy.
    """
    mode_file = Path(package_mode_path or PACKAGE_MODE_FILE)
    policy_file = Path(policy_path or LIVE_POLICY_FILE)
    try:
        package_mode = mode_file.read_text(encoding='utf-8').strip().lower()
    except OSError as exc:
        raise ScreeningUnavailable(f'package mode unavailable for screening policy: {exc}') from exc
    if package_mode not in {'live', 'testnet'}:
        raise ScreeningUnavailable(f'invalid immutable package mode {package_mode!r}')
    execution = str(
        execution_mode if execution_mode is not None
        else os.getenv('EXECUTION_MODE', 'simulation')
    ).strip().lower()
    allowed_execution_modes = {
        'testnet': {'testnet', 'simulation'},
        'live': {'live', 'simulation'},
    }
    if execution not in allowed_execution_modes[package_mode]:
        raise ScreeningUnavailable(
            f'the {package_mode} package does not permit '
            f'EXECUTION_MODE={execution!r} for screening')
    if package_mode != 'live' or execution != 'live':
        return
    try:
        metadata = json.loads(policy_file.read_text(encoding='utf-8'))
        policy = metadata['sharia_live_policy']
    except Exception as exc:
        raise ScreeningUnavailable(
            'live screening policy is absent from immutable VALIDATION_STATUS.json') from exc
    if not isinstance(policy, dict) or policy.get('approved') is not True:
        raise ScreeningUnavailable('live screening policy is not explicitly approved')
    approved_base = str(policy.get('base_url', '')).rstrip('/')
    approved_model = str(policy.get('model', '')).strip()
    parsed = urlparse(approved_base)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment or not approved_model):
        raise ScreeningUnavailable('immutable live screening host/model policy is malformed')
    configured_base = os.getenv('SHARIA_OPENAI_BASE', '').rstrip('/')
    configured_model = os.getenv('SHARIA_MODEL', '').strip()
    configured_hosts = {x.strip().lower() for x in
                        os.getenv('SHARIA_ALLOWED_OPENAI_HOSTS', '').split(',') if x.strip()}
    if configured_base != approved_base:
        raise ScreeningUnavailable('live SHARIA_OPENAI_BASE differs from immutable approval')
    if configured_model != approved_model:
        raise ScreeningUnavailable('live SHARIA_MODEL differs from immutable approval')
    if configured_hosts != {parsed.hostname.lower()}:
        raise ScreeningUnavailable('live host allowlist differs from immutable single-host approval')


class ScreeningRunner:
    def __init__(self, controller_bytes: bytes):
        # Keep the immutable live policy at the network-capable class boundary,
        # not only in the normal service entrypoint. Direct construction must
        # not turn environment variables into live authority.
        enforce_live_screening_policy()
        self.controller_text = controller_bytes.decode('utf-8')
        self.base_url = os.getenv('SHARIA_OPENAI_BASE', 'https://api.openai.com/v1').rstrip('/')
        parsed = urlparse(self.base_url)
        allowed_hosts = {x.strip().lower() for x in
                         os.getenv('SHARIA_ALLOWED_OPENAI_HOSTS', 'api.openai.com').split(',')
                         if x.strip()}
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                parsed.password or parsed.hostname.lower() not in allowed_hosts):
            raise ScreeningUnavailable(
                'SHARIA_OPENAI_BASE must be HTTPS and its host explicitly allowlisted')
        self.model = os.getenv('SHARIA_MODEL', '').strip()
        self.api_key = os.getenv('SHARIA_OPENAI_API_KEY', '').strip()
        self.timeout = env_int('SHARIA_REQUEST_TIMEOUT_SECONDS', 300, 30, 900)
        self.max_attempts = env_int('SHARIA_MAX_ATTEMPTS', 2, 1, 5)
        self.max_output_tokens = env_int('SHARIA_MAX_OUTPUT_TOKENS', 16000, 1000, 128000)

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, 'SHARIA_OPENAI_API_KEY is not configured (separately billed API credit required)'
        if not self.model:
            return False, 'SHARIA_MODEL is not configured (no model is ever silently substituted)'
        return True, 'configured'

    @staticmethod
    def _extract_output_text(data: dict) -> str:
        if isinstance(data.get('output_text'), str) and data['output_text'].strip():
            return data['output_text']
        chunks: list[str] = []
        for item in data.get('output') or []:
            if not isinstance(item, dict):
                continue
            for content in item.get('content') or []:
                if isinstance(content, dict) and content.get('type') in ('output_text', 'text'):
                    text = content.get('text')
                    if isinstance(text, str):
                        chunks.append(text)
        return '\n'.join(chunks)

    @staticmethod
    def _extract_tool_evidence(data: dict) -> dict:
        """Extract only provider-owned search calls and URL annotations."""
        calls: list[dict] = []
        citations: list[dict] = []
        for item in data.get('output') or []:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'web_search_call' and item.get('status') == 'completed':
                action = item.get('action') if isinstance(item.get('action'), dict) else {}
                urls: set[str] = set()
                action_url = normalize_evidence_url(action.get('url'))
                if action_url:
                    urls.add(action_url)
                for source in action.get('sources') or []:
                    if isinstance(source, dict) and source.get('type') == 'url':
                        normalized = normalize_evidence_url(source.get('url'))
                        if normalized:
                            urls.add(normalized)
                calls.append({
                    'id': str(item.get('id', '')),
                    'status': 'completed',
                    'action_type': str(action.get('type', '')),
                    'source_urls': sorted(urls),
                })
            if item.get('type') != 'message':
                continue
            for content in item.get('content') or []:
                if not isinstance(content, dict) or content.get('type') != 'output_text':
                    continue
                for annotation in content.get('annotations') or []:
                    if not isinstance(annotation, dict) or annotation.get('type') != 'url_citation':
                        continue
                    normalized = normalize_evidence_url(annotation.get('url'))
                    start, end = annotation.get('start_index'), annotation.get('end_index')
                    if (not normalized or not isinstance(start, int) or isinstance(start, bool) or
                            not isinstance(end, int) or isinstance(end, bool) or
                            not 0 <= start <= end):
                        continue
                    citations.append({
                        'url': normalized,
                        'title': str(annotation.get('title', '')),
                        'start_index': start,
                        'end_index': end,
                    })
        return {
            'provider': 'openai-responses',
            'completed_web_search_calls': calls,
            'url_citations': citations,
        }

    @staticmethod
    def _parse_single_json_object(text: str) -> dict:
        cleaned = text.strip()
        if cleaned.startswith('```'):
            cleaned = cleaned.strip('`')
            if cleaned.lower().startswith('json'):
                cleaned = cleaned[4:]
        start = cleaned.find('{')
        if start < 0:
            raise ScreeningUnavailable('model output contained no JSON object')
        decoder = json.JSONDecoder()
        try:
            obj, end = decoder.raw_decode(cleaned[start:])
        except Exception as exc:
            raise ScreeningUnavailable(f'model output JSON parse failed: {exc}') from exc
        trailing = cleaned[start + end:].strip().strip('`').strip()
        if trailing:
            raise ScreeningUnavailable('model output contained data after the JSON object')
        if not isinstance(obj, dict):
            raise ScreeningUnavailable('model output was not a JSON object')
        return obj

    def run(self, base: str, pair: str) -> tuple[dict, dict]:
        """Return (report, usage_meta). Raises ScreeningUnavailable on failure."""
        ok, reason = self.available()
        if not ok:
            raise ScreeningUnavailable(reason)
        prompt = WRAPPER_PROMPT.format(base=base, pair=pair, controller=self.controller_text)
        # Hosted web search is incompatible with forced-JSON response mode
        # (documented provider behavior); the controller itself mandates
        # JSON-only output and the strict parser + validator enforce it.
        body = {
            'model': self.model,
            'tools': [{'type': 'web_search'}],
            'include': ['web_search_call.action.sources'],
            'input': prompt,
            'max_output_tokens': self.max_output_tokens,
        }
        headers = {'Authorization': f'Bearer {self.api_key}',
                   'Content-Type': 'application/json'}
        last_error = 'unknown'
        for attempt in range(self.max_attempts):
            try:
                response = requests.post(
                    self.base_url + '/responses', json=body, headers=headers,
                    timeout=self.timeout, allow_redirects=False)
            except requests.RequestException as exc:
                last_error = f'network error: {exc}'
            else:
                if response.status_code in (401, 403):
                    raise ScreeningUnavailable('API authentication failed (key rejected)')
                if response.status_code == 429:
                    detail = ''
                    try:
                        detail = str(response.json().get('error', {}).get('code', ''))
                    except Exception:
                        pass
                    if 'insufficient_quota' in detail or 'insufficient_quota' in response.text:
                        raise ScreeningUnavailable(
                            'insufficient_quota: separately billed API credit is exhausted or absent')
                    last_error = 'rate limited (429)'
                elif 500 <= response.status_code < 600:
                    last_error = f'provider error HTTP {response.status_code}'
                elif response.status_code != 200:
                    raise ScreeningUnavailable(
                        f'provider rejected the request (HTTP {response.status_code}): '
                        + response.text[:300])
                else:
                    data = response.json()
                    text = self._extract_output_text(data)
                    if not text.strip():
                        raise ScreeningUnavailable('model returned no output text')
                    report = self._parse_single_json_object(text)
                    # Model JSON is untrusted provenance. Replace any claim
                    # with evidence extracted from provider-owned response data.
                    report['tool_evidence'] = self._extract_tool_evidence(data)
                    usage = data.get('usage') or {}
                    log.info('screening completed for %s (input=%s output=%s tokens)',
                             pair, usage.get('input_tokens'), usage.get('output_tokens'))
                    return report, {
                        'usage': usage, 'model': self.model,
                        'completed_web_search_calls': len(
                            report['tool_evidence']['completed_web_search_calls']),
                        'url_citations': len(report['tool_evidence']['url_citations']),
                    }
            if attempt < self.max_attempts - 1:
                time.sleep(min(10.0 * (attempt + 1), 30.0))
        raise ScreeningUnavailable(f'screening failed after {self.max_attempts} attempts: {last_error}')
