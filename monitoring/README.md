# V10.1 read-only monitoring

This is the single canonical monitor. It exposes 11 authenticated GET routes
under `/api/v1`, 12 read-only MCP tools, an optional Telegram report, and
mode-specific systemd units. It has no Binance credentials and no order,
cancel, buy, sell, transfer, or configuration-write route.

## Data-source contract

- Real order lifecycle/open positions: `shared/runtime/execution_state.sqlite`.
- Real realised P/L: `shared/legacy_runtime/logs/pnl_ledger.jsonl`.
- Freqtrade: reported separately as **signal-only**, never as real execution.
- Service/WebSocket/Sharia state: bot-produced health JSON files.
- Docker status: a root-owned fixed helper writes a sanitized snapshot. The
  `botmon` API user never receives Docker-socket access.
- Deployment/validation: installer-produced status files bound to the exact
  release hash.

Every returned string is recursively secret-redacted. Authentication uses a
constant-time Bearer comparison, loopback source allow-list, per-client rate
limit after authentication, request IDs, and mandatory audit logging.

Routes: `health`, `status`, `performance`, `trades`, `errors`, `crashes`,
`latency`, `system`, `deployment`, `sharia`, and `report`.

Testnet defaults to port 8090. Live uses 8091 and is programmatically disabled
until the operator explicitly enables the live monitor after formal promotion.
See `INSTALL.md`, `SECURITY.md`, and `../docs/OFFICIAL_DEPLOYMENT_REFERENCES.md`.
