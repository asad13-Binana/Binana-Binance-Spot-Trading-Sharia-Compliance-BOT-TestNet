# Codex revision 2026-08-01J release notes

This is the testnet package for revision `2026-08-01J`; its internal release
identifier is `V10.2-CODEX-2026-08-01J`. The canonical revision 01I artifacts
remain unchanged. This package is structurally testnet-only and cannot be
promoted to live execution by editing `.env`.

## Protected behavior

- `IctSmcStrategy.py` was not modified. Its required SHA-256 remains
  `9f6bafc78c8cd0d9b9cbde615ddce89e304ab09738584b88d05bfdf92ff4e830`.
- The preserved all-in-one legacy core was not modified. Its required
  SHA-256 remains
  `70b1d67cc0092b5b8db4a68b343cf893641bde1aae580e9ef51e2adec1062459`.

## Engineering repairs

- Universe consumers now reopen and validate a complete immutable snapshot,
  including its full semantic hash, filename binding, ranking, configuration,
  freshness, and safe path. Consumer mounts are read-only; the universe
  service remains the only writer.
- Automated Sharia screening now requires controller-bound, report-bound,
  Ed25519-attested records. Search-result URLs are discovery hints only;
  evidentiary citations must come from opened sources. Fresh screening is
  mandatory in the live-capable package. This is automated research logic,
  not a fatwa.
- Screening attempts have a durable, transactional, globally bounded quota,
  including urgent requests, retries, spacing, reservation, and restart
  recovery.
- Binance exchange metadata and order-list preflight validation now reject
  duplicate or malformed filters, wrong symbol identity, absent Spot
  permission, invalid boolean/range fields, unsafe OCO relationships, and
  uncertain capacity data.
- Entry, protection, re-protection, emergency-exit, partial-fill, timeout,
  expiry, event-journal, and restart paths now fail closed into durable
  reconciliation/safety state. The final trailing-order payload is validated
  at the broker call boundary.
- Oracle artifact installation now activates monitoring inside the same
  transaction as the application release. Deployment success is recorded only
  after monitoring succeeds; rollback restores the previous monitoring release
  or disables all monitoring units after a failed first installation.
- Telegram derives trade eligibility from the verified Sharia gate, displays
  NOT READY when service health is not current, exposes protected-position
  status, and redacts callback/network errors. Each Telegram update is now
  durably claimed before command dispatch, preventing a failed response or
  restart from replaying side effects such as a manual Sharia scan.
- The exact dependency locks now require `aiohttp==3.14.3` and
  `cryptography==50.0.0`.  A fresh isolated install passed `pip check` and
  strict service/monitoring vulnerability audits with no known vulnerable
  installed packages reported by the audit tool.
- Fresh working-tree, CI-style tar, Git round-trip, and launcher simulations
  passed.  The package gate ran 346 core tests (342 passed and 4 skipped),
  50 monitoring tests, and 33/33 preserved legacy self-tests.  One known
  Starlette/httpx deprecation warning remains disclosed.

## Telegram configuration

Follow `docs/TELEGRAM_BOT_SETUP.md`. Put the real values only in the private,
mode-600 deployment `.env` file:

```dotenv
TELEGRAM_BOT_TOKEN=REPLACE_WITH_THE_PRIVATE_TOKEN_FROM_BOTFATHER
TELEGRAM_OWNER_CHAT_ID=123456789
```

Never put either secret in source code, `.env.example`, Compose YAML, Git, or
a release ZIP.

## Remaining external gates

No authenticated Binance order, real Telegram credential test, Docker image
run, Oracle deployment, or real-money trade was performed. Mutable container
tags require digest hardening before Oracle. GitHub CI is still authoritative
for the first real Docker and exact dependency run. Authenticated Spot Testnet
lifecycle evidence, Oracle soak and rollback drills, fee/slippage backtesting,
and reconciliation evidence remain mandatory before live promotion.
