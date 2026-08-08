"""Registered screening-evidence provider schemas (LOCAL-BACKEND-001).

The V19.1 result validator originally hard-coded
``evidence.get('provider') == 'openai-responses'``, which made a separately
billed external API structurally mandatory: no other backend could ever
produce an accepted verdict.  This module replaces that single literal with an
explicit registry so a self-hosted backend can be validated just as strictly.

This is deliberately a registry and not a free-text field.  An unregistered
provider name is rejected, so a forged report cannot invent its own evidence
format to bypass the per-provider structural requirements.

``local-oracle-v1`` is the shipped default and requires *stronger* evidence
than the remote provider did: every evidentiary URL must carry the SHA-256 of
the exact bytes that were retrieved, so a quote can be re-verified offline
against the stored content rather than trusted because a model reported it.
"""
from __future__ import annotations

# Provider name -> required keys on each evidentiary retrieval record.
_REGISTERED_PROVIDERS: dict[str, frozenset[str]] = {
    # Self-hosted retriever: URL, fetch time, HTTP status and content digest.
    'local-oracle-v1': frozenset({
        'url', 'retrieved_utc', 'http_status', 'content_sha256', 'source_tier',
    }),
    # Remote hosted-web-search provider. Retained so previously signed
    # historical evidence stays verifiable; it is NOT selectable by the
    # shipped configuration and requires no API credentials to validate.
    'openai-responses': frozenset({'url'}),
}

DEFAULT_PROVIDER = 'local-oracle-v1'


def is_registered(provider: object) -> bool:
    return isinstance(provider, str) and provider in _REGISTERED_PROVIDERS


def registered_providers() -> frozenset[str]:
    return frozenset(_REGISTERED_PROVIDERS)


def required_record_keys(provider: str) -> frozenset[str]:
    """Keys every evidentiary retrieval record must carry for this provider."""
    try:
        return _REGISTERED_PROVIDERS[provider]
    except KeyError as exc:
        raise KeyError(f'unregistered evidence provider {provider!r}') from exc
