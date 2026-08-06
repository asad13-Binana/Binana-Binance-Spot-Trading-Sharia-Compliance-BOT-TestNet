# Container image digest pinning (deep-audit F-08)

The service image base (`python:3.12-slim`) and the Freqtrade runtime
(`freqtradeorg/freqtrade:2026.6`) are currently **tag-pinned**, not
**digest-pinned**. A tag can be repointed registry-side, which would change
the built or pulled image without any change to this repository — a
reproducibility and supply-chain weakness.

This is a hardening item (P1), not a runtime defect: the pinned tags are
correct and the release is byte-reproducible from source. It requires network
access to a registry to resolve digests, so it cannot be completed on an
offline build host and is done as a deliberate, reviewed step before live
promotion.

## Procedure (run on a networked host, then review and commit)

1. Resolve the current multi-arch digests:

   ```bash
   bash scripts/resolve_image_digests.sh
   ```

   It prints the `name@sha256:...` digest references for both images.

2. Pin them:
   - `Dockerfile.services`: change `FROM python:3.12-slim` to
     `FROM python:3.12-slim@sha256:<digest>`.
   - `docker-compose.yml`: change `image: freqtradeorg/freqtrade:2026.6` to
     `image: freqtradeorg/freqtrade:2026.6@sha256:<digest>`.

3. Confirm the digest resolves on BOTH architectures you deploy to — Oracle
   Always-Free is commonly `linux/arm64` (Ampere A1) but may be `linux/amd64`;
   verify with `docker manifest inspect` before pinning a single-arch digest.

4. Rebuild, run the full gate, regenerate the manifest and audit ledgers, and
   commit the change with the resolved digests recorded in the release notes.

5. Document the update cadence: re-resolve digests on a schedule (e.g. monthly)
   with a review gate, so security patches to the base images are adopted
   deliberately rather than silently.

## Why this is not auto-applied here

Pinning a digest that only exists for one architecture would break the other
architecture's build. The correct digest set depends on the exact Oracle
instance shape the operator provisions, so it is resolved and verified at
deployment-preparation time, not baked in blindly at source-package time.
