# Clean-host deployment repair — 31 August 2026

Scope: host installation and monitoring only. No strategy, signals, risk sizing,
Sharia decisions, execution core, exchange credentials or LIVE activation changes.

## Confirmed failure chain

The supplied AWS transcript stopped at the installer, not in the strategy:

1. Clean Ubuntu provides python3, not necessarily python.
2. Host Sharia schema validation imported Crypto before its dependency existed.
3. The Compose helper reassigned the immutable COMPOSE_PROJECT_NAME.

The repaired installer uses Python 3 for standard-library checks and creates a
root-owned, isolated Python 3.12 host-validation venv with the exact reviewed
pycryptodome pin/hashes from the services lock. It does not install application
packages globally or bypass Sharia validation. Compose receives the fixed project
name as an argument and the canonical private env explicitly.

Related fixes: precreate shared bind paths with bot ownership; initialise Sharia
files as bot-readable/writable without replacing existing decisions; make signed
pause/reconcile requests readable by their consumer; require two valid command
IDs and both acknowledgements; catch partial startup/status-write failures in
rollback; disable instance timers on failed first installation; render backup
paths and monitoring snapshot identity for the actual instance; create backup
roots before systemd uses them; create monitoring venvs at their final path and
quarantine incomplete builds for retry.

## Host preparation

Use Ubuntu 24.04. Oracle ARM64 remains the default. For a reviewed AWS AMD64
experiment, explicitly pass ALLOW_REVIEWED_AMD64=true, HOST_PROVIDER=aws,
and DEPLOYMENT_PROFILE=single-bot-testnet-experiment to the root-owned setup.
The AWS choice selects Amazon's local NTP address; Oracle keeps its own address.
Do not change a running host's global Docker settings without reviewing the
effect on other bots. Do not delete or restart Bitcoin as a side effect.

The experiment profile still needs two CPUs, at least 7168 MiB physical RAM,
11264 MiB RAM+swap, and 20 GiB free at host bootstrap. It refuses another running
container project. The default four-bot profile and LIVE restrictions are unchanged.
The supplied 29-GiB disk with 16–17 GiB free does not meet the bootstrap free-space
check: preserve backups and resolve capacity explicitly, never bypass the guard.

Run setup first; then configure the private env, validate the NEW immutable
artifact/digest, approve that digest, and invoke the instance deployment wrapper.
No manual python-is-python3, global pip or custom PATH repair is required.
Do not reuse the failing old artifact. Never wipe SQLite, safety history or
Sharia evidence to make deployment appear successful. Reconcile any outstanding
exchange state before replacement. A stale current symlink or missing command
acknowledgement requires recovery review, not a forced install.

## Validation boundaries

The clean-host-installer job starts a disposable pinned Ubuntu 24.04 container.
It executes the actual approval/wrapper/installer, manifest/security checks,
locked venv installation, file ownership and monitoring rendering. It tests
failed first-start cleanup/retry and upgrade rollback, without globally installed
Crypto or a bare Python alias. Docker, systemd, capacity and account
acknowledgements are explicit test doubles; no credentials, exchange orders or
host socket enter the test. This job is required before release-artifact generation.

Real Docker builds/health remain covered separately by integration-simulation.
Real systemd, cloud firewall, API credentials, Telegram delivery, Binance
reconciliation, reboot/restore and soak tests must still be validated on the
actual host. Passing source/CI tests is not a claim of zero crashes, profitable
signals, religious certification or permission to activate LIVE.

Sharia schema validity is not coin eligibility. Empty/unverified/expired sources
continue to block trading. Market conditions still determine whether the
unchanged strategy produces a signal.

References: [Python virtual environments](https://docs.python.org/3.12/library/venv.html),
[Amazon local time synchronisation](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configure-ec2-ntp.html).
