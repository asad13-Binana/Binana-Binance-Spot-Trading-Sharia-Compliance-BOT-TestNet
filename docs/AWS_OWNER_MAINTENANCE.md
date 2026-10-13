# Recovered AWS Testnet owner maintenance

The recovered owner uses the existing AWS Compose overlay chain in
`/usr/local/sbin/binana-stage1-start`. The root repository's legacy Compose
configuration alone is not the recovered-owner installer. LIVE is outside this
procedure. Strategy, excluded assets, 1,000-USDT allocation, 250-USDT nominal
entry and four-position maximum are unchanged.

## Qualification and activation

Use `scripts/recovery/run_candidate_acceptance.py --owner-stopped --image
sha256:...` on private SQLite copies. One- and four-position checks add
`--slot-soak 1` and `--slot-soak 4`. Each requires ten uninterrupted minutes with
the requested positions protected, followed by restart, exit and closed restart.
A native stop that closes a position is valid trading behavior but fails this
particular constant-position duration check. Keep its failed receipt.

The acceptance transport permits authenticated requests only to Spot Testnet,
bounds every sale to the rehearsal's immutable fills, journals mutations before
transmission and rejects replay. It validates every configured rehearsal pair
before the first order. These manual admissions establish execution behavior,
not organic strategy signals or profitability.

After the final lifecycle, run `verify_candidate_restore.py --image sha256:...
--run /var/backups/binana-testnet/controlled-owner-...` before further account
mutations. The restore check copies both databases through SQLite's backup API,
compares logical contents and starts a read-only exchange reconciliation on the
restored closed position. Active-position restarts are covered separately by
the lifecycle and position soaks; this is not an active-position disaster test.

`deploy_candidate_owner.py` requires a stopped canonical owner, fresh flat
Testnet GETs, matching configuration and successful acceptance receipts. It
backs up both canonical databases and deployment configuration, then checks
candidate, rollback and candidate boots. Trading remains stopped. Trade/order
economics must remain identical; startup timestamp and appended wallet
telemetry are the only permitted changes to existing tables.

`issue_candidate_release.py` validates the accepted image and all six receipts
before issuing one atomic Ed25519 certificate. Runtime code and dependencies,
effective public configuration, account, Testnet epoch, Docker image, mounts,
environment fingerprint and entrypoint are bound. The private issuer key stays
outside container mounts. Only the public key and signed certificate are
readable by the owner. Read-only code, shared root and release mounts, a
dedicated writable owner-state mount, dropped capabilities and an init process
are required. Resume through the normal owner API only after fresh
`release_approved=true` and an empty blocker list.

## Renewal

Run `issue_candidate_release.py --evidence <private-evidence-directory>
--rollout <successful-rollout-receipt> --renew` from a root-owned installation.
The seven-day certificate is renewed only within two days of expiry. Renewal
requires the previous signature, identical bindings, matching acceptance,
current reconciliation and ten fresh isolated offline runs. It never submits
orders, changes bot state or approves changed source/configuration. Failed
renewal leaves the previous certificate intact. A daily systemd timer can run
this command; verify its installed unit and next trigger in the deployment
receipt. A code/configuration/account/epoch change requires new qualification.

## Backups and cleanup

`compact_daily_backups.py` runs before the existing daily state-backup service.
It compresses completed timestamped BINANA backups and compares every regular
file's hash and size against the archive, rechecking that originals did not
change before deleting duplicate directories. Canonical databases, current and
rollback images, configuration and private credentials are excluded from
cleanup. Failed compaction stops that backup invocation without deleting the
source copy. Compression receipts and archives remain private under
`/var/backups/binana-testnet`.

For an archived daily backup, first verify the archive SHA-256 from its
compression receipt and extract into a separate private directory; then use
the existing restore validator. An archived directory is not directly usable
by tools that require an uncompressed backup directory. Off-host backup is a
separate configuration and must not be reported as working from local backup
success alone. Archive retention and disk headroom still require monitoring.

Never prune a container merely because it is stopped: the two historical
validation containers contain unique database-repair evidence. Retain the
recovered base image; the owner build currently depends on that local image.
