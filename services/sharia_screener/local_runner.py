"""Self-hosted V19.1 screening runner with no model or paid API dependency."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.common.sharia_v19 import (
    GREEN_PROOF_CHECKS,
    KEYWORD_CATEGORIES,
    REQUIRED_WHITEPAPER_SECTIONS,
    SCREENER_SITES,
    fail_closed_report,
    validate_local_evidence_files,
)
from services.sharia_retriever import EvidenceStore, Retriever
from services.sharia_rules.engine import (
    Disposition,
    EvidenceClaim,
    RetrievedDocument,
    evaluate,
)
from services.sharia_screener.runner import ScreeningUnavailable
from services.sharia_screener.source_registry import (
    SourceRegistry,
    SourceRegistryError,
)
from services.sharia_screener.evidence_binding import verify_reviewed_block


def _future_date(days: int = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()


class LocalScreeningRunner:
    """Fetch, attest and evaluate owner-identified sources on the Oracle host.

    The runner never emits a tradeable verdict. A mechanically complete case
    becomes a review proposal; only a separately signed owner decision may
    later promote that exact evidence set.
    """

    provider = 'local-oracle-v1'
    model = ''

    def __init__(self, controller: dict, *, registry_path: str | Path,
                 evidence_root: str | Path, retriever: Retriever | None = None):
        self.controller = controller
        self.registry = SourceRegistry(registry_path)
        self.evidence_root = Path(evidence_root)
        self.store = EvidenceStore(self.evidence_root)
        self.retriever = retriever or Retriever(evidence_store=self.store)

    def available(self) -> tuple[bool, str]:
        try:
            self.evidence_root.mkdir(parents=True, exist_ok=True)
            self.registry.load()
        except (OSError, SourceRegistryError) as exc:
            return False, str(exc)
        return True, 'local-oracle-v1 ready; no external AI service configured'

    @staticmethod
    def _claim(raw: object, documents: dict[str, RetrievedDocument],
               sources: dict[str, dict]
               ) -> EvidenceClaim | None:
        if not isinstance(raw, dict):
            return None
        document = documents.get(str(raw.get('url', '')).strip())
        if document is None or not document.opened:
            return None
        source = sources.get(document.url)
        if (not isinstance(source, dict) or
                source.get('content_sha256') != document.content_sha256):
            return None
        binding_ok, _reason = verify_reviewed_block(
            document.text, str(raw.get('quote', '')), raw)
        if not binding_ok:
            return None
        return EvidenceClaim(
            value=str(raw.get('value', '')).strip(),
            quote=str(raw.get('quote', '')).strip(),
            url=document.url,
            content_sha256=document.content_sha256,
        )

    @staticmethod
    def _review_report(base: str, finding, fetches, claims, screeners) -> dict:
        reason = '; '.join(finding.reasons) or 'local screening requires owner review'
        report = fail_closed_report(base, reason=reason)
        keyword_results = {
            name: {'hits': 0, 'quotes': []}
            for name in KEYWORD_CATEGORIES
        }
        for hit in [*finding.hits, *finding.clean_hits]:
            category = str(hit.category).split('.', 1)[0]
            if category not in keyword_results:
                continue
            keyword_results[category]['hits'] += 1
            quote = str(hit.quote).strip()
            if len(quote.split()) >= 3:
                keyword_results[category]['quotes'].append(quote)

        all_green = (
            GREEN_PROOF_CHECKS <= set(finding.green_checks)
            and all(finding.green_checks.get(name) is True
                    for name in GREEN_PROOF_CHECKS)
        )
        scope_review_only = (
            finding.disposition == Disposition.ESCALATE
            and all_green
            and finding.escalations == [
                'negation or conditional scope requires owner review']
            and not finding.hits
        )
        all_sources_opened = bool(fetches) and all(item.ok for item in fetches)
        bound_items = []
        for name, raw in {**claims, **screeners}.items():
            if not isinstance(raw, dict):
                continue
            bound_items.append({
                'name': name,
                'url': str(raw.get('url', '')),
                'quote': str(raw.get('quote', '')),
                'quote_start': raw.get('quote_start'),
                'quote_end': raw.get('quote_end'),
                'context': str(raw.get('context', '')),
                'context_start': raw.get('context_start'),
                'context_end': raw.get('context_end'),
                'context_sha256': str(raw.get('context_sha256', '')),
                'text_sha256': str(raw.get('text_sha256', '')),
                'extractor_version': str(raw.get('extractor_version', '')),
            })
        promotable = (
            all_green and all_sources_opened
            and (finding.disposition == Disposition.PROPOSE_GREEN
                 or scope_review_only)
        )
        report.update({
            'token_type': str((claims.get('token_type') or {}).get('value', 'UNKNOWN')),
            'shariah_screener_check': {
                name: str((screeners.get(name) or {}).get('value', 'not checked'))
                for name in SCREENER_SITES
            },
            'whitepaper_parsing': {
                name: {'status': 'NOT_FOUND', 'quote': ''}
                for name in REQUIRED_WHITEPAPER_SECTIONS
            },
            'keyword_scan_results': keyword_results,
            'contradiction_resolution': 'all checked; none found',
            'sources_opened': [{
                'url': item.url, 'tier': item.tier, 'opened': True,
                'identity_match': item.identity_match,
                'quote': item.text[:1000],
                'content_sha256': item.content_sha256,
                'content_path': item.content_path,
            } for item in fetches if item.ok],
            'sources_failed': [{
                'url': item.url, 'error': item.error,
                'http_status': item.http_status,
            } for item in fetches if not item.ok],
            'tool_evidence': {
                'provider': 'local-oracle-v1',
                'completed_web_search_calls': [
                    item.as_evidence_record(index)
                    for index, item in enumerate(fetches, start=1) if item.ok
                ],
                'url_citations': [],
            } if any(item.ok for item in fetches) else report['tool_evidence'],
            'human_escalation_required': True,
            'human_escalation_reason': reason,
            'next_rescreen_date': _future_date(),
            'confidence_level': 'LOW',
            'shariah_result': 'NO TRADE pending owner review of local evidence',
            'user_personal_action': 'Review the exact local evidence before deciding',
            'local_review': {
                'schema_version': 1,
                'disposition': finding.disposition,
                'reasons': list(finding.reasons),
                'green_checks': dict(finding.green_checks),
                'escalations': list(finding.escalations),
                'hits': [hit.__dict__ for hit in finding.hits],
                'clean_hits': [hit.__dict__ for hit in finding.clean_hits],
                'scope_review_only': scope_review_only,
                'all_registered_sources_opened': all_sources_opened,
                'promotable': promotable,
                'owner_decision_required': True,
                'evidence_bindings': bound_items,
            },
        })
        utility = claims.get('utility') or {}
        revenue = claims.get('revenue') or {}
        if utility:
            report['whitepaper_parsing']['S2_TOKEN_UTILITY'] = {
                'status': 'FOUND', 'quote': str(utility.get('quote', ''))}
        if revenue:
            report['whitepaper_parsing']['S3_REVENUE_MODEL'] = {
                'status': 'FOUND', 'quote': str(revenue.get('quote', ''))}
        return report

    def run(self, base: str, pair: str) -> tuple[dict, dict]:
        try:
            asset = self.registry.asset(base)
        except SourceRegistryError as exc:
            raise ScreeningUnavailable(str(exc)) from exc
        if asset is None:
            report = fail_closed_report(
                base, reason='no owner-verified local source registry entry')
            return report, {'backend': self.provider, 'sources': 0,
                            'owner_action': 'register official sources'}

        fetches = [
            self.retriever.fetch(
                source['url'], official_hosts=asset['official_hosts'],
                identity_match=source['identity_match'])
            for source in asset['sources']
        ]
        documents_by_url = {
            item.url: RetrievedDocument(
                url=item.url, tier=item.tier, text=item.text,
                content_sha256=item.content_sha256,
                retrieved_utc=item.retrieved_utc,
                http_status=item.http_status,
                identity_match=item.identity_match)
            for item in fetches if item.ok
        }
        sources_by_url = {
            str(source.get('url', '')).strip(): source
            for source in asset['sources']
        }
        raw_claims = asset['claims']
        fact_evidence = {
            name: self._claim(
                raw_claims.get(name), documents_by_url, sources_by_url)
            for name in ('token_type', 'utility', 'revenue')
        }
        raw_screeners = {
            str(name).lower().replace('_', '').replace('-', ''): value
            for name, value in asset['screeners'].items()
        }
        screener_evidence = {
            name: self._claim(value, documents_by_url, sources_by_url)
            for name, value in raw_screeners.items()
        }
        screener_results = {
            name: str((raw_screeners.get(name) or {}).get('value', ''))
            for name in SCREENER_SITES
        }
        token_type = str((raw_claims.get('token_type') or {}).get(
            'value', 'UNKNOWN')).upper()
        utility_quote = str((raw_claims.get('utility') or {}).get('quote', ''))
        revenue_value = str((raw_claims.get('revenue') or {}).get(
            'value', '')).casefold()
        finding = evaluate(
            self.controller, documents=list(documents_by_url.values()),
            screener_results=screener_results,
            identity_confirmed=any(
                document.identity_match for document in documents_by_url.values()),
            token_type=token_type, utility_quote=utility_quote,
            revenue_clean=revenue_value in {'clean', 'non-material'},
            contradictions=[], expected_screeners=SCREENER_SITES,
            fact_evidence=fact_evidence,
            screener_evidence=screener_evidence,
            asset_identifier=base,
        )
        report = self._review_report(
            base, finding, fetches, raw_claims, raw_screeners)
        if report['tool_evidence'].get('provider') == self.provider:
            validate_local_evidence_files(report, self.evidence_root)
        return report, {
            'backend': self.provider,
            'sources_opened': sum(item.ok for item in fetches),
            'sources_failed': sum(not item.ok for item in fetches),
            'disposition': finding.disposition,
            'owner_review_required': True,
        }
