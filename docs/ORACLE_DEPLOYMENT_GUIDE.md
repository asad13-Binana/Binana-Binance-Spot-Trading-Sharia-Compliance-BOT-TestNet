# Oracle Cloud — Freqtrade-only deployment preparation

The current deployment source is the versioned owner bundle documented in
[ORACLE_OWNER_BUNDLE.md](ORACLE_OWNER_BUNDLE.md): Freqtrade **2026.9**, the
`BinanaNfiSpot` five-minute owner, four 250-USDT slots, and the existing Sharia,
freshness, protection and release gates. Freqtrade is the sole order owner.
The monitor cannot execute orders. Startup remains stopped.

## Prepare the public bundle

From the reviewed repository checkout, materialize a new output directory:

```sh
python runtime_owner/deployment/materialize_oracle_bundle.py /tmp/binana-owner-bundle
python /tmp/binana-owner-bundle/oracle_preflight.py --bundle /tmp/binana-owner-bundle
```

The owner guide specifies the pinned upstream archive, exact image inputs,
private configuration, state restoration, certificate issuance and renewal,
and the later host acceptance procedure. Its manifest binds every packaged
file. Packaging and offline checks do not deploy or resume the bot.

## Architecture boundary

Root `docker-compose.yml`, `deploy/install_artifact.sh` and
`deploy/oracle_setup.sh` are retained **legacy sidecar** tooling. They are not
the Freqtrade-owner installer. The legacy bootstrap now requires explicit
`--legacy-sidecar-recovery` selection before probing the host. Historical
procedures remain in [LEGACY_ORACLE_DEPLOYMENT_GUIDE.md](LEGACY_ORACLE_DEPLOYMENT_GUIDE.md)
for recovery of that architecture only. Never use the legacy artifact installer
to upgrade or roll back an owner bundle, or combine their state and containers.

## Oracle acceptance remains separate

The planning target is Ubuntu 24.04 ARM64 on an A1 allocation of 2 OCPUs and
12 GiB memory. Retained legacy bootstrap checks require 80 GiB free build
storage and 4 GiB swap; these do not establish measured capacity for the new
owner. Verify the tenancy's current allowance and aggregate workloads, native
ARM64 image behavior, resources, clock, network, state restore, single-owner
exclusion, certificate renewal, rollback and soak on the actual host before
trading. Local AMD64 or emulated ARM64 checks do not satisfy those gates.

No Oracle host or real account is certified by this guide. Keep private keys
and acceptance evidence outside source bundles and containers. Do not copy
a historical AWS certificate or account epoch into a new deployment.

Official version-matched references:

- [Freqtrade 2026.9 Docker deployment](https://www.freqtrade.io/en/2026.9/docker_quickstart/)
- [Freqtrade 2026.9 ARM64 installation guidance](https://www.freqtrade.io/en/2026.9/installation/)
- [Oracle Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
