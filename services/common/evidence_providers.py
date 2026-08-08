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
    # Remote hosted-web-search provider, retained ONLY so previously signed
    # historical evidence stays verifiable.
    #
    # This MUST stay empty. An earlier version required a top-level 'url'
    # here, which the real runner never emits -- it produces 'source_urls'
    # and carries the URL inside 'action'. That invented requirement rejected
    # previously valid reports, converted validated GREEN results to
    # NO_TRADE_INFO in the bridge, and broke 6 authoritative tests. It passed
    # my own check only because that check used a hand-built fixture instead
    # of the real producer's output. Requirements for this provider are
    # frozen at what the generic validator already enforced.
    'openai-responses': frozenset(),
}

DEFAULT_PROVIDER = 'local-oracle-v1'

# Providers a NEWLY produced report may declare. Historical records may still
# be verified under any registered provider, but a fresh result must not be
# able to select the weaker legacy schema and thereby skip the content-digest
# requirements that local-oracle-v1 imposes.
ACTIVE_PROVIDERS = frozenset({'local-oracle-v1'})


def is_selectable_for_new_result(provider: object) -> bool:
    """True when a freshly produced report may declare this provider."""
    return isinstance(provider, str) and provider in ACTIVE_PROVIDERS


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
