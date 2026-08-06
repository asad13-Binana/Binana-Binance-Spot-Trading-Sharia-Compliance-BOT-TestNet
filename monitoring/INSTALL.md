# Monitoring installation

The immutable artifact installer calls `deploy/install_monitoring.sh` after all
five bot containers pass their health gate. It creates the `botmon` system user,
installs an exact hash-versioned Python environment, creates a root-owned
monitor-only env file, installs hardened systemd units, and starts only the
units whose enable flags are true.

Testnet first install automatically generates a 64-hex-character monitor token
without printing it. Inspect it only when configuring a local MCP client:

```bash
sudo grep '^MONITOR_TOKEN=' /etc/binance-freqtrade-v101/testnet-monitor.env
curl -H 'Authorization: Bearer TOKEN' \
  http://127.0.0.1:8090/api/v1/health
```

Telegram reporting uses a separate bot token in the monitor env. Set
`TELEGRAM_REPORTS_ENABLED=true` only after those values are populated, then:

```bash
sudo systemctl enable --now binance-bot-monitor-report-testnet.timer
```

For live, edit `/etc/binance-freqtrade-v101/live-monitor.env` only after the
exact live artifact completes promotion. Its template has
`MONITOR_ENABLED=false`, port 8091, a separate audit file, and reporting off.

Never copy `/etc/binance-freqtrade-v101/.env` into the monitor service. The
monitor units intentionally cannot read that trading-credential file.
