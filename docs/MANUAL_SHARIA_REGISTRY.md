# Manual Sharia Asset Registry

The trading bot does not perform Sharia research. It enforces the
owner-maintained `shared/sharia/halal_coins.json` file before a pair can enter
the executable universe and again at signal execution.

This is an operational allowlist, not a fatwa. The owner is responsible for
the review and for the contents of the file.

## File format

```json
{
  "_comment": "Owner-maintained operational Sharia allowlist.",
  "schema_version": 1,
  "version": "1.1",
  "last_reviewed": "2026-09-02",
  "next_review": "2026-12-02",
  "symbols": [
    "ETHUSDT",
    "LINKUSDT",
    "SOLUSDT"
  ]
}
```

Rules:

- Use exact uppercase Binance Spot/USDT symbols.
- Keep `symbols` sorted and free of duplicates.
- Increase `version` whenever the list changes.
- `last_reviewed` cannot be in the future.
- `next_review` must be after `last_reviewed`, still current, and no more than
  366 days later.
- An empty `symbols` list is a valid deny-all state and may use `null` dates.
- Missing, malformed, changed-before-signing, or expired data blocks every new
  entry. It never authorises a coin by default.

## Runtime flow

```text
Manual owner review
        |
        v
halal_coins.json
        |
        v
network-isolated signed registry projector
        |
        v
sharia_status.json
        |
        +--> universe filter
        +--> Freqtrade signal gate
        +--> execution-sidecar gate
```

The projector has no network and no Binance credentials. It never edits
`halal_coins.json`; it only signs a report-bound execution projection. When a
symbol is added or removed, the existing durable Telegram alert outbox informs
the owner. Protective exits remain governed by the existing execution safety
logic; this allowlist controls new entries.

After an update, confirm the Telegram registry notification and check the
Sharia status screen before enabling entries. Do not put API keys or personal
information in the registry file.
