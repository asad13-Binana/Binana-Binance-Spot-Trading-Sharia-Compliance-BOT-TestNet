# Codex revision 2026-08-10A release notes

`V10.3-LOCAL-SHARIA-2026-08-10A` hardens the local Sharia evidence and
network-transport boundary without modifying the immutable V19.1 controller,
the Sharia rules executor, the Binance strategy, legacy execution, order and
reconciliation logic, or risk controls.

## Evidence integrity

- Positive screener evidence must be one complete extracted HTML/PDF block.
- The reviewed occurrence and its surrounding blocks are bound by exact
  offsets, raw-response SHA-256, extracted-text SHA-256, context SHA-256 and
  extractor version.
- Runtime verification replays those offsets; it never relocates a substring
  or guesses an English sentence boundary.
- The owner review report and Telegram card contain every bound context, and
  the existing owner signature remains bound to the complete report hash.

## Network isolation

- The Sharia screener has only an internal Docker network.
- A secretless HTTPS CONNECT proxy is the sole egress bridge. It resolves once,
  rejects mixed/private/link-local/loopback DNS answers, connects to a validated
  numeric address, and leaves TLS hostname verification end-to-end.
- The registry helper refuses direct host execution and is shipped for use
  only inside that isolated container.

## Release correction

- LIVE and TestNet retain their separate package identities and interlocks.
- Six-service health and rollback checks include the egress proxy.
- GitHub Actions includes a container test proving proxied HTTPS succeeds while
  direct and Oracle-metadata egress are refused.

This remains research screening, not a fatwa, and does not establish trading
profitability or live-money readiness.
