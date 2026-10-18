# Container image digest pinning (deep-audit F-08)

The service image base and Freqtrade runtime are pinned to immutable
multi-architecture index digests. The human-readable tags remain alongside
the digests for reviewability, but the registry cannot silently change the
resolved bytes without a source change.

## Current reviewed pins

The Python index was reviewed on 2026-08-09. The Freqtrade index was
updated from the official 2026.9 release and verified on 2026-10-16:

```text
python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
freqtradeorg/freqtrade:2026.9@sha256:989aff5636d8f0213ed44fe334c3314a194a51c0c755d16866960247aafd2eae
```

Both indexes were verified to contain `linux/amd64` and `linux/arm64`
manifests. The SHA-256 calculated over each re-fetched index matched the
registry's `Docker-Content-Digest` header exactly.

Never replace these values with a child-manifest digest unless the release is
being made deliberately single-architecture. The pinned index digests preserve
both supported Oracle shapes while keeping registry resolution immutable.

## Review and update procedure

1. Resolve the current multi-architecture digests on a networked host:

   ```bash
   bash scripts/resolve_image_digests.sh
   ```

2. Re-fetch each index by digest and verify its bytes hash to that digest.

3. Confirm both intended deployment platforms are present. Oracle Always-Free
   A1 uses `linux/arm64`; other Oracle shapes may use `linux/amd64`.

4. Review upstream release notes and vulnerability information before adopting
   a newer image. A changed digest is a dependency update, not a formatting
   change.

5. Update the two source references, rebuild, run the complete release and
   supply-chain gates, then regenerate the audit ledgers and release manifest.

6. Re-resolve on a defined maintenance cadence so security updates are adopted
   deliberately instead of through a mutable tag.
