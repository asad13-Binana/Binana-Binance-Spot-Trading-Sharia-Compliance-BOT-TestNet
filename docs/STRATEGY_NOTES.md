# Current Oracle owner route

Use [ORACLE_OWNER_BUNDLE.md](ORACLE_OWNER_BUNDLE.md) for the supported installation.
The candidate uses Freqtrade **2026.10** with `BinanaNfiSpot`, 5m, four 250 USDT
slots, unchanged Spot/Sharia/risk gates, and stopped startup. Source preparation
does not establish host, exchange or strategy-performance acceptance.

CI validates and packages the standalone owner/support bundle; it does not SSH
to a host or deploy. The previous root ICT/sidecar installation is retired.
Historical reports and recovery records remain evidence for their stated release.
Current owner behavioral regressions live in `runtime_owner/tests/`; support,
Sharia, Telegram, packaging and monitoring regressions remain in `tests/` and
`monitoring/tests/`.
