# Codex revision 2026-08-09A release notes

This TestNet candidate is `V10.3-LOCAL-SHARIA-2026-08-09A`. It replaces the
externally billed Sharia model runner with a self-hosted Oracle backend while
preserving the protected strategy and legacy trading-core bytes.

## Local Sharia backend

- The active and only selectable backend is `local-oracle-v1`; the former
  callable OpenAI runner and all active `SHARIA_OPENAI_*` configuration were
  removed.
- Owner-registered public HTTPS sources are retrieved directly with HTTPS-only,
  public-address, same-origin redirect and AI-inference-host restrictions.
- Exact response bytes are written atomically to content-addressed SHA-256
  paths and re-hashed before approval.
- HTML and text-bearing PDFs are parsed locally. Encrypted, malformed,
  image-only, oversized-page-count and oversized-text PDFs fail closed.
- Positive claims and all seven named screener results must match verbatim
  quotes in the retained source bytes and the screener's real domain.
- The deterministic engine never emits a tradeable verdict. Telegram shows
  the owner the proof status, report SHA-256 and adverse/disclaimer quotes.
  APPROVE/REJECT uses a dedicated HMAC key and is bound to the exact proposal,
  asset, request and evidence hashes.
- A GREEN projection is possible only after all 12 proof checks pass and the
  exact owner decision is revalidated. It expires from entry eligibility after
  seven days and must be re-screened.

## Oracle and supply chain

- `python:3.12-slim` and `freqtradeorg/freqtrade:2026.6` are pinned by immutable
  multi-architecture registry digest; both indexes include linux/arm64.
- Runtime Python requirements remain fully hash-locked. `pypdf==6.15.0` is a
  pure-Python direct dependency used only for bounded local PDF extraction.
- The installer seeds but never overwrites the persistent owner source
  registry and creates persistent evidence and decision-bus directories.
- Compose grants the separate approval key only to Telegram and the screener;
  the execution sidecar cannot mint an owner approval.

## Deliberate operational boundary

Ticker identity and religious judgement are not guessed. The shipped source
registry is empty and therefore fail-closed. The owner must independently
populate and review a registry entry before that asset can reach a Telegram
approval proposal. See `docs/SHARIA_LOCAL_SCREENING.md`.

This revision is not a profitability claim, a fatwa, Oracle soak evidence or
live-trading certification. GitHub CI, authenticated Binance Spot TestNet
lifecycle tests, Oracle fault/rollback drills, fees-and-slippage backtesting
and signed live-promotion evidence remain external gates.
