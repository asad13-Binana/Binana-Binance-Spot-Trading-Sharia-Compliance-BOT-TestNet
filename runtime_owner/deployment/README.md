# Owner builds and deployment records

The current accepted AWS owner is `clean-20261015-r2` (`b38bdf55...`), resumed October 15 at 18:29:12 Asia/Karachi. The image map and Compose image overlay in this directory are byte-identical to the deployed host records. See [acceptance and runtime evidence](../../docs/recovery/AWS_CLEAN_RELEASE_20261015.md).

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

For ARM64 builders, use `docker buildx build --platform linux/arm64 --load` with the same Dockerfile/context and `docker run --platform linux/arm64` with the same validation limits. The previous 2026.8 source passed 222 offline tests and the AWS named-user/executable startup contract under Docker Desktop emulation on October 15. Those results do not qualify the new 2026.9 image. Actual Oracle-host installation and account-bound acceptance remain separate.

The source builder now selects upstream commit `1f394eaebc2f46a83d26971388628707802f8602` (Freqtrade 2026.9), archive SHA256 `a453638c8ad59f8655036eaabd0858cb432331e70caa259976299e5c59108c89`. Only its application/client source and packaging metadata are materialized. The one upstream license symlink becomes regular verified license bytes. The manifest-verified BINANA patches include the custom exchange export required by the resolver. Python 3.14.7 is pinned by an official OCI digest. The dependency closure preserves the reviewed security updates and CCXT 4.5.87, adding upstream-required cloudpickle 3.1.2. The new AMD64 image passes 244 offline tests with zero skips. See [2026.9 repair evidence](../../docs/FREQTRADE_2026_9_ADMISSION_REPAIR_2026-10-16.md); source preparation alone does not change the accepted AWS deployment above.

A clean image build is not a complete live deployment: supply private configuration and keys separately, restore or initialize account-scoped durable state, use the current owner topology, and qualify/sign the new image before enabling entries. Never copy an old image-bound certificate to a new image. The supported execution contract remains Binance Spot Testnet.

## Historical retained-image recovery build

This package builds the reviewed owner candidate from the retained AWS r17 image and the exact files in `../PROVENANCE.json`. The default entry point verifies source hashes and runs the offline owner suite. It cannot start Freqtrade or place orders. No credentials, runtime databases or effective private configuration belong in its build context.

`AWS_POLICY_20261007.json` records the non-secret trading policy observed on AWS. Its source digest identifies the private configuration, which remains on the server. It is evidence, not a complete launch configuration.

From the repository root, create a new context outside the checkout:

```sh
python3 runtime_owner/deployment/materialize_context.py /absolute/path/to/new-context
python3 runtime_owner/deployment/build_verified_owner.py /absolute/path/to/new-context --tag binana-testnet-freqtrade:recovery-validation-20261007
docker run --rm --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --memory=768m --cpus=1 --pids-limit=128 \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777 \
  binana-testnet-freqtrade:recovery-validation-20261007
```

The retained r17 base is pinned separately in `retained-owner-base.json` and must already exist on the AWS host with that exact image ID. `local-images-20261007.json` describes currently deployed services and cannot select or change the historical build base. The build wrapper checks that identity before and after building; the Dockerfile alone is not the verified build procedure. No clean-host reconstruction of that base is claimed. A successful offline run is not authenticated exchange acceptance, deployment approval, a lifecycle certificate or permission to clear incidents. That recovery acceptance was subsequently completed for image `7b12004b…`; this historical paragraph is not an instruction to pause the accepted running owner. The root Compose package still describes the older execution architecture and must not replace the current AWS owner.

## Preserving the existing AWS startup

The checked-in `binana-stage1-start` is the repaired host script. `compose.running-images-20261007.json` uses the seven retained local tags observed after the research upgrade. Before Compose runs, verify_local_images.py checks every tag against the separate Docker image ID in local-images-20261007.json and checks that Compose names the same tags. Missing or retagged images stop startup. Docker image IDs are not presented as registry manifest digests. The script depends on the host's existing private environment, base Compose file and overlays. It is an exact recovery record, not a standalone installer.

`compose.research-v193.yml` records the research-only image/controller override. The installed host filename is `compose.sharia-v193.yml`; both express the same override. The final image overlay takes precedence and pins the built research image.

The pre-upgrade topology was captured at 21:31 UTC on October 6 (October 7 in the owner's timezone). The upgrade completed at 21:37 UTC. The latest image overlay and verification receipt supersede that snapshot for the research image. Credentials and runtime database copies must remain on AWS.

The final research overlay pins the subsequent dependency-security build b74ef9a… from source commit 0c4a9d8. The earlier research image remains a rollback artifact. See the verification record for both deployment events.

The October 7 market recovery overlay changes only universe CPU (0.07 to 0.75), memory (180 to 512 MiB), and three collector source mounts. It preserves the existing 96-process limit. The original AWS files remain archived under deployed_services; market_context_repair records the deployed repairs and their source hashes.

## October 8 accepted AWS owner

At that historical rollout, the running image overlay and local-image manifest pinned recovery-validation-20261007-r4 (a90b7f66...). The October 15 records above supersede those image references. The actual stopped rollout and r17 rollback rehearsal passed before the existing acceptance certificate was issued and the broker Resume path enabled entries. All accounting and order data remained unchanged; only startup_time and the four exact StateStore identity constraints were accepted as startup changes. Retain r17 and the verified backups for recovery. The candidate image defaults to an offline validator; the existing AWS Compose owner overlay explicitly selects the real Freqtrade entrypoint and command.
