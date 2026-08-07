#!/usr/bin/env bash
# Resolve immutable multi-arch digests for the release's container images
# (deep-audit F-08). Run on a networked host with Docker available, then pin
# the printed name@sha256 references per docs/IMAGE_DIGEST_PINNING.md.
set -euo pipefail

images=(
  "python:3.12-slim"
  "freqtradeorg/freqtrade:2026.6"
)

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required to resolve digests" >&2
  exit 2
fi

for image in "${images[@]}"; do
  echo "== ${image} =="
  # Index (multi-arch) digest:
  if digest=$(docker buildx imagetools inspect "${image}" --format '{{json .Manifest.Digest}}' 2>/dev/null); then
    echo "  index digest: ${image}@$(echo "${digest}" | tr -d '"')"
  fi
  # Per-architecture digests for the platforms Oracle Free Tier may use:
  docker manifest inspect "${image}" 2>/dev/null \
    | python3 -c 'import sys,json
d=json.load(sys.stdin)
for m in d.get("manifests", []):
    p=m.get("platform",{})
    if p.get("os")=="linux" and p.get("architecture") in ("amd64","arm64"):
        print(f"  linux/{p[\"architecture\"]}: {m[\"digest\"]}")' || true
done

echo
echo "Pin these per docs/IMAGE_DIGEST_PINNING.md, then rebuild and re-run the gate."
