# Migration from monitoring_addon_SEPARATE.zip (original preserved as evidence)
CONSOLIDATED: monitor_server.py (x-token) + bot_monitor/monitor_api.py (Bearer)
  -> monitoring/api/* (ONE canonical Bearer API). Both originals superseded.
REPLACED: mcp_monitor_bridge.py -> mcp/monitor_mcp_server.py (12 tools, clamps,
  audit, Bearer). telegram_reporter.py -> telegram/telegram_reporter.py
  (hardened). monitor.service/-report.timer -> systemd/* (per-mode pairs, the
  missing report .service added, sandboxing, dedicated botmon user, separate
  monitoring env file instead of the bot's .env).
ADDED: redaction, constant-time auth, rate limit, audit log, request IDs,
  bounds, /api/v1 versioning, docs-off default, system/latency/sharia/
  deployment metrics, tests, requirements, compose, guides.
