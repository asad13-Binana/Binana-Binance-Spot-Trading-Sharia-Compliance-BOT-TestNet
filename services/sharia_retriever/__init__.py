"""Self-hosted source retrieval for Sharia screening.

Fetches the sources the V19.1 controller requires — official site, docs,
whitepaper, tokenomics, official GitHub, and the seven named public Sharia
screener sites — and records exactly what was received: the canonical URL,
the retrieval time, the HTTP status, the SHA-256 of the retrieved bytes, and
the extracted text.

There is no AI here and no API key. The point of the content digest is that
every quote the rules engine produces can be re-verified offline against the
stored bytes, rather than trusted because some model reported it.

``AI_ENDPOINT_HOSTS`` is a hard structural block: this module refuses to
fetch from a model-inference host at all, so the screening path cannot
silently regain a paid dependency.
"""
from services.sharia_retriever.retriever import (  # noqa: F401
    AI_ENDPOINT_HOSTS,
    FetchResult,
    Retriever,
    RetrievalBlocked,
    classify_tier,
    html_to_text,
)
