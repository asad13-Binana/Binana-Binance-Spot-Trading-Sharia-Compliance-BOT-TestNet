# Owner builds and deployment records

## Portable Oracle owner package

Use [the Oracle owner bundle guide](../../docs/ORACLE_OWNER_BUNDLE.md) for the
separate stopped, Testnet-only `BinanaNfiSpot` topology. Its materializer uses
the current manifest-verified owner patches and current research/broker
sources, and includes host-only issuance and renewal tools outside both image
contexts. The root Compose installer and alternative order-owning engines
are retired. New installations use only the Oracle owner bundle.

Historical AWS evidence records `clean-20261016-20269` (`960d789f...`), Freqtrade 2026.9, and its October 18 acceptance. The retained image map and Compose overlays describe that captured host only; they do not attest the current candidate, a running service, or a new Oracle installation. See [the dated acceptance record](../../docs/recovery/AWS_2026_9_RELEASE_20261018.md).

## Independent public-source build

This procedure needs Docker and Python 3.10+ on a fresh build host. It does not need any AWS image, database, credential or private configuration. The default image command only runs offline validation.

```sh
python3 runtime_owner/deployment/materialize_clean_context.py /absolute/new-owner-context
docker build --pull -f /absolute/new-owner-context/Dockerfile.clean \
  -t binana-owner-clean:reviewed /absolute/new-owner-context
docker run --rm --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --memory=2g --cpus=2 --pids-limit=256 \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1777 binana-owner-clean:reviewed
```

For ARM64 builders, use `docker buildx build --platform linux/arm64 --load` with the same Dockerfile/context and `docker run --platform linux/arm64` with the same validation limits. The historical October 18 2026.9 image passed 244 offline tests with zero skips under Docker Desktop ARM64 emulation. That receipt does not cover subsequent source or dependency changes. Build and validate the exact current candidate separately; actual Oracle-host installation and account-bound acceptance remain separate.

The source builder selects official Freqtrade 2026.10 commit `4cf0351e08d26e96a6429ef48103b726bb230cf8`, archive SHA256 `e7e83e139f0d7b01f337dfb2e0c4ff13d895431615886b3c8570e99bb139476a`. Only its application/client source and packaging metadata are materialized. The upstream license symlink becomes regular verified license bytes. The seven manifest-verified BINANA overlays preserve the custom exchange and owner contracts. Python 3.14.10 is pinned by an official multi-platform OCI digest; CCXT remains locked at 4.5.87, which satisfies this release's declared requirements. The final image must pass dependency consistency and every owner regression. Historical 2026.9 receipts do not validate this image.

A clean image build is not a complete live deployment: supply private configuration and keys separately, restore or initialize account-scoped durable state, use the current owner topology, and qualify/sign the new image before enabling entries. Never copy an old image-bound certificate to a new image. The supported execution contract remains Binance Spot Testnet.

## Service health overlays

`python3 runtime_owner/deployment/materialize_service_health.py /absolute/new-health-context`
produces the four public files referenced by the deployed service overlays.
The builder verifies the retained broker, market collector and manual Sharia source hashes,
allows exactly six derived-health publication call changes, and copies the shared atomic
writer. Private queues, offsets, credentials and accounting keep their existing
permissions. Install these files read-only at the paths recorded in the Compose
overlays; this procedure does not rebuild or restart the order owner.

The separate read-only monitor must explicitly select the owner architecture;
see [monitor setup](../../monitoring/OWNER_ARCHITECTURE.md).

## Historical recovery evidence

The retained AWS image builder and legacy startup path are retired. Their
original bytes remain in Git history; use the retirement inventory and
`scripts/resolve_retired_source.py` for an explicit, read-only historical lookup.
`PROVENANCE.json`, `AWS_POLICY_20261007.json`, dated image receipts and source
indexes remain evidence of earlier recovery work. They do not select the
current build, renew a certificate or authorize a new installation.
