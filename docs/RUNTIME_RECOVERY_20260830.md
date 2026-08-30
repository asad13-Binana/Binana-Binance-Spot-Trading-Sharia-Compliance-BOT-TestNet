# AWS experiment and Oracle deployment: recovery repair

This update repairs execution-support and deployment defects. It does not
change the immutable strategy, its configuration, legacy trading core, V19.1
controller or religious decision rules. LIVE remains disarmed by default.
No authenticated Binance orders or host changes were performed for this update.

## Choose the host policy explicitly

The default remains `DEPLOYMENT_PROFILE=four-bot-oracle`: Ubuntu 24.04,
2 CPUs, at least 11264 MiB physical memory and 14336 MiB RAM plus swap,
80 GiB free at bootstrap. ARM64 remains the intended Oracle architecture;
AMD64 still requires the existing explicit review switch. These are admission
floors, not a performance or free-tier eligibility guarantee.

For a dedicated temporary Binana TestNet experiment only, the owner may select
`DEPLOYMENT_PROFILE=single-bot-testnet-experiment` in both the bootstrap
environment and the external private configuration. This profile requires
2 CPUs, 7168 MiB physical memory, 11264 MiB RAM plus swap and 20 GiB free at
bootstrap. It refuses LIVE and any other running container project, including
Bitcoin. Do not select it for a shared two/four-bot host. A small machine is
not made suitable by lowering the shared-host thresholds.

Both bootstrap and artifact installation apply the same profile. A server
with less capacity, another OS, unavailable architecture images or failing
time synchronisation remains unsupported until independently validated.
Nothing in this repository guarantees an AWS/Oracle resource is free.

## Migrate the manual AWS experiment without mixing identities

1. Inspect only the intended Compose project's service names, image IDs,
   restart policy and status. Do not dump container environments, resolved
   Compose configurations, tokens, chat IDs or unrestricted logs into chats.
2. Disarm entries through the existing authenticated owner interface. Verify
   account orders and positions read-only. Stop only this bot's stack after
   reconciliation; leave the Bitcoin stack untouched.
3. Preserve an offline copy of the manual persistent state and the exact
   deployed source/artifact identity. If release metadata is missing, label
   this an unverified state export, not a recovery-ready release backup.
4. Use the official bootstrap and approved immutable artifact workflow from
   `GITHUB_ORACLE_DEPLOYMENT.md`. Keep credentials only in the canonical
   `/etc/binana-testnet/.env` or `/etc/binana-live/.env`, root:root 0600.
   Do not add `.env.external`, caches, audit output or changed file modes to
   the immutable source tree. Never use recursive chmod 777.
5. After a reviewed backup, map only this bot's persistent data to its
   dedicated account and canonical directory. Do not blindly chown an old
   shared directory. The installer does not automatically migrate or delete
   legacy containers, names or volumes. Review collisions first.
6. Install monitoring and verify its enabled timers, local backup, disk guard
   and read-only monitor account. A manual `docker compose up` alone does not
   install these host protections. Retain the previous immutable artifact for
   rollback; never start both releases against the same state/account.
7. Verify provider access, Telegram delivery, the external monitoring API and
   owner-evidence workflow before enabling TestNet entries. LIVE stays in
   simulation, with live-money credentials and promotion out of this repair.

## Market-data transport

`FREQTRADE_ENABLE_WS=true` preserves the existing default. If a host rejects
Freqtrade's market WebSocket with code 1008, an explicit
`FREQTRADE_ENABLE_WS=false` selects the documented REST candle transport.
This does not alter the strategy, indicators, pair selection, Binance
user-data stream or Spot microstructure collector. Verify candle freshness and
shared-IP request budgets on that host; REST is not permission to bypass
Binance location/account restrictions. The exact reason for the supplied 1008
remains unproven. The malformed tmpfs list has been corrected to one mount.

## Sharia is operational only when evidence is complete

Discovery remains non-authorising. Follow the existing source-review workflow:
identity-bound candidate -> owner-confirmed sources -> proposed complete
quotes -> owner-reviewed claim values and all seven screener requirements ->
exact-byte revalidation -> signed owner decision -> current eligible cache.
No registry, quote, screener result or religious approval is fabricated here.

The proposal tool recognises storage/compute/network fees and prioritises
those complete blocks over corporate fundraising prose. It does not infer
that a fee model is permissible. Failed retrieval, ambiguous identity,
conflicting evidence and incomplete review continue to deny trading.

Telegram's existing Data Readiness screen now separates backend liveness from
`sharia_trade_ready`, eligible count and the eligibility blocker. Zero eligible
assets may legitimately coexist with a healthy process. The signal path uses
cached signed evidence; the historical fresh-screening seam is not enabled.

## Restart and uncertain submissions

Docker is enabled at boot by bootstrap; the existing services use
`restart: unless-stopped`. This covers process exits/daemon restarts after a
successful deployment, but a deliberately stopped container stays stopped.
An unhealthy but still-running container is not automatically repaired by a
restart policy. Keep the existing host health timers and alerting enabled.

Graceful termination now disarms and attempts portfolio persistence even if
another cleanup step fails. Abrupt OOM/SIGKILL cannot run Python cleanup.
Durable SQLite and startup reconciliation remain necessary. The simulator
does not claim that empty RAM after restart proves no outstanding position;
it latches recovery and avoids reusing old simulated order IDs.

Submission acknowledgement uses an atomic conditional SQLite update, so an
already-applied fill/protection event cannot be overwritten with submitted.
Uncertain outcomes are durable reconciliation incidents, not rejections.
Neither a retry nor a restart clears them. New entries remain blocked until
the existing verified-reconciliation/owner-resume procedure proves safety.

The sidecar also checks actual runtime disk capacity before processing signals,
independent of a writable status file. The host disk guard queues its signed
pause before optional status/logging; logging failures cannot skip that action.
A completely full/unwritable disk can still prevent persistence: shutdown is
fail-closed, not a claim that the failed write was durable.

## Backups and restoration

SQLite online backup connections are explicitly closed; only the copy is
converted to a standalone DELETE-journal database. Auxiliary WAL/SHM files
are not treated as primary databases. The validator rejects unexpected
auxiliary files, empty database inventories, unlisted files, duplicate or
unsafe checksum paths, wrong-mode metadata and missing release provenance.
Retention and off-host discovery now use the actual 8-digit-date timestamp.
Local retention is newest timestamp first, not mutable directory mtime.

Local backups on the same root volume do not protect against host/volume loss.
OCI remains the default encrypted off-host provider and uses its pinned CLI
container plus instance-principal authentication. An optional
`OFFHOST_PROVIDER=aws-s3` uses an operator-installed AWS CLI v2, EC2 instance
role credentials with IMDSv1 disabled, an existing private S3 bucket and the
expected bucket owner. No access keys, profiles or custom endpoint overrides
are inherited. Objects are create-only; checksums and a downloaded copy are
verified before success. A duplicate name fails rather than overwriting.

The owner must configure IAM/bucket policy, retention and costs externally.
Give only necessary bucket-read and object put/get permissions for the bot's
prefix, enforce HTTPS and block public access. The age public recipient can
be on the VM; the decryption identity must stay offline. Test preflight first,
then a real encrypted upload/readback. Neither provider is auto-enabled.

`stage_offhost_restore.sh` supports either provider, decrypts into a separate
staging backup and validates it without touching runtime state. Compare the
release hash/commit against the independently approved immutable artifact
before restoring; checksums alone are not cryptographic proof of publisher
identity. Prove staged restore, rollback and restart with entries disarmed.

## Still external, not certified by unit tests

Actual AWS/Oracle reboot, sidecar OOM, disk exhaustion, outage recovery,
encrypted off-host restore, Telegram phone delivery, account-specific filter
acceptance, authenticated TestNet lifecycle, multi-day soak and financial
performance remain separate evidence gates. No LIVE certification follows
from this patch. The 85% coverage target must be reported from fresh measured
coverage; passing a lower non-regression floor is not meeting that target.

Official references checked during this repair:
- https://docs.docker.com/engine/containers/start-containers-automatically/
- https://docs.freqtrade.io/en/latest/configuration/
- https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-metadata.html
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html
