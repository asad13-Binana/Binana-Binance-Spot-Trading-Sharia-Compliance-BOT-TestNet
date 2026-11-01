# Portable release issuance and renewal

These host-only tools maintain the signed BINANA Spot Testnet release certificate. Host installation and first issuance require separate deployment authorization and new exact-image acceptance on that host. Neither command resumes trading.

Both scripts require an explicit --host profile. Start with runtime_owner/deployment/release-host.oracle.example.json; use the actual Compose project/service and the same configuration, shared-state and release directories as the deployed bundle. An older AWS host must provide its own profile. The archived AWS rollout command remains a separate historical tool and must not deploy the Oracle bundle.

The archived `deploy_candidate_owner.py` command requires
`--host-profile aws-historical` before any host inspection. It rejects Oracle
and unknown profiles. Its imported helper functions remain available to the
portable issuer after the explicit JSON host profile has been validated.

The profile must be root-owned, mode 0600, with absolute paths and no symlinks. Keep the issuer in a root-private directory outside every container mount. The public verifier stays in the read-only configuration directory. No issuer material belongs in a source artifact or Docker context.

Use host Python with the pinned cryptography dependency installed. The acceptance directory must be root-owned and private, under evidence_root. It must contain real lifecycle, one-slot, four-slot, restore and ten-run offline receipts for the same immutable image, configuration and Testnet account, plus a successful rollout receipt. Historical AWS receipts cannot certify an Oracle deployment.

Before the separately authorized host installation, prepare an isolated,
root-owned Python 3.12 environment outside all container mounts. Use
`python3 -m venv --copies /usr/local/lib/binana-release-maintenance/PROJECT/venv`
and install only the shipped host renewal closure with
`/usr/local/lib/binana-release-maintenance/PROJECT/venv/bin/python -m pip install --require-hashes -r requirements.renewal.lock`.
Replace `PROJECT` with the validated project name. Keep system site packages
disabled. The renewal lock contains cryptography and its small CFFI closure;
the owner trading dependency set is not a host installation prerequisite.
Pass this regular executable explicitly as `--python /absolute/venv/bin/python`
to `install_release_renewal.py`. The installer validates root ownership,
non-writable parent paths, no symlinks, Python 3.12 and the pinned cryptography
version before changing units. The generated unit uses that exact interpreter.

After separately approved host acceptance, invoke scripts/recovery/issue_candidate_release.py with --host /path/to/host.json --evidence /path/to/acceptance --rollout /path/to/rollout.json as root. First issuance still requires stopped/paused Spot readiness and fresh reconciliation. Adding --renew requires the existing verified signature and identical code, configuration, account, exchange epoch, deployment and dependency bindings. The issuer verifies the new signature before atomically replacing the certificate. Changed bindings require new acceptance.

Install the daily timer with scripts/recovery/install_release_renewal.py and the same three arguments, only after host approval. It installs project-specific tools and a private profile under /usr/local/lib/binana-release-maintenance/<project>. The unit writes only to that profile's release and evidence directories. The 2400-second service timeout accommodates ten 180-second validators. Missing units are handled on first install. Failed installation restores the preceding tooling, certificate and enabled/active timer state. An active renewal must finish before installation can proceed.

Local verification uses disposable fixture keys and stateful command adapters. Oracle systemd, authenticated reconciliation, reboot, restoration and expiry-crossing renewal still require host acceptance. This document describes a future operator procedure; no host installation or trading authorization is implied.

The renewal budget is ten validation passes at 180 seconds each, plus two
owner identity checks containing two Docker queries each at 30 seconds. That
leaves 480 seconds of the 2400-second systemd budget for preparation, signing,
atomic publication and scheduling margin. A failed pass aborts renewal; there
are no unbounded retries. A timed-out pass records its partial output and
elapsed time, then attempts a 30-second cleanup of its uniquely named,
credential-free validation container. Cleanup failure is explicit and never
publishes fresh acceptance. Host filesystem or engine failures remain host
acceptance gates; the timeout budget cannot guarantee progress through them.
