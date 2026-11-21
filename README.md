# BINANA Oracle Freqtrade owner

The supported installation is the standalone Oracle owner bundle described in
[docs/ORACLE_OWNER_BUNDLE.md](docs/ORACLE_OWNER_BUNDLE.md).

Materialize the bundle from the pinned official Freqtrade source in a new output
directory with `python runtime_owner/deployment/materialize_oracle_bundle.py OUTPUT`.
This creates source and configuration templates; it does not install or start a bot.

`BinanaNfiSpot` is the only order owner. The reviewed strategy remains 5m, four
250 USDT slots, Spot-only, and uses the owner-maintained Sharia registry. Startup
is stopped; LIVE remains disabled until its separate acceptance and authorization.
Telegram, the monitor and research services do not acquire order ownership.

GitHub Actions validates source and builds AMD64 and native ARM64 images. It
creates an Oracle bundle artifact and has no deployment job or cloud credentials.
Host state, accepted releases, database/WAL files and credentials are not source
cleanup targets. Native host acceptance and authenticated exchange validation
remain separate release gates.

The root ICT/sidecar Docker topology and launchers were retired. Dated audit and
recovery documents are historical evidence. See [docs/retirement/README.md](docs/retirement/README.md)
for exact baseline source lookup and test coverage migration.
