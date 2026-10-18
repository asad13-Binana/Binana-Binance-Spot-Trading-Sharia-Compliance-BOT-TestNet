# Monitor execution architecture

The default `MONITOR_EXECUTION_OWNER=SIDECAR` preserves the legacy sidecar
database, signal-only Freqtrade, and P/L collectors, including LIVE deployments.
Neither Testnet mode nor the presence of retained database files selects an owner.

For a deployment whose sole order owner is Freqtrade, explicitly set these
monitor-only environment values (example Testnet paths):

```dotenv
MONITOR_EXECUTION_OWNER=FREQTRADE
OWNER_DATABASE_PATH=/var/lib/binana-testnet/shared/freqtrade/binana-owner.sqlite
OWNER_EXTENSION_DATABASE_PATH=/var/lib/binana-testnet/shared/freqtrade/binana-extension.sqlite
OWNER_READINESS_PATH=/var/lib/binana-testnet/shared/runtime/owner_monitor_readiness.json
```

The owner paths above are defaults relative to `MONITOR_SHARED_ROOT`.
Existing `EXECUTION_DATABASE_PATH`, `SIGNAL_DATABASE_PATH`, and
`EXECUTION_PNL_LEDGER_PATH` apply only to the SIDECAR collectors.

FREQTRADE mode labels the configured owner and extension databases separately.
Checks remain read-only; unreadable private databases report unavailable integrity.
Their permissions must not be broadened for monitoring.

Owner health requires the root-published readiness summary to identify FREQTRADE,
match `BOT_ENVIRONMENT`, contain a boolean `release_approved` and string-list
`blockers`, and remain within its original `generated_at`/`valid_until` interval.
The accepted interval is at most 60 seconds. The publisher must preserve source
expiry and refresh often enough for that interval. Missing, malformed, future,
expired, blocked, or unapproved owner readiness cannot produce healthy owner
status. The execution sidecar is advisory in this mode.

Legacy execution-state, performance, recent-trade, signal-performance, and
order-quality collectors explicitly return `unsupported_for_execution_owner`
until owner-specific adapters are implemented. They never substitute archived
sidecar records or present signal-only results as owner execution.

The monitoring installer renders and installs the instance-specific
`owner-monitor-snapshot.service` and `.timer` units. It starts the five-second
owner-readiness timer only when the private monitoring environment explicitly
selects `MONITOR_EXECUTION_OWNER=FREQTRADE` and `MONITOR_ENABLED=true`. Rerun the
installer after changing either selection; SIDECAR or disabled monitoring stops
and disables the owner timer and stops any active publication service.

These units call the root-owned snapshot helper with `--owner-readiness-only`,
without Docker or permission-update operations, and preserve the source expiry.
The legacy 30-second Docker status timer remains unchanged. The monitor remains
unprivileged and receives no exchange credentials.
