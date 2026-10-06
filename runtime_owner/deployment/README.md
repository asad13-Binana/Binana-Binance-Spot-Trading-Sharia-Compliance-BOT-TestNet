# Isolated AWS owner recovery build

This package builds the reviewed owner candidate from the retained AWS r17 image and the exact files in `../PROVENANCE.json`. The default entry point verifies source hashes and runs the offline owner suite. It cannot start Freqtrade or place orders. No credentials, runtime databases or effective private configuration belong in its build context.

`AWS_POLICY_20261007.json` records the non-secret trading policy observed on AWS. Its source digest identifies the private configuration, which remains on the server. It is evidence, not a complete launch configuration.

From the repository root, create a new context outside the checkout:

```sh
python3 runtime_owner/deployment/materialize_context.py /absolute/path/to/new-context
docker build --network=none --pull=false -t binana-testnet-freqtrade:recovery-validation-20261007 /absolute/path/to/new-context
docker run --rm --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --memory=768m --cpus=1 --pids-limit=128 \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777 \
  binana-testnet-freqtrade:recovery-validation-20261007
```

The digest-pinned r17 base must already exist on the AWS host. No clean-host reconstruction of that base is claimed. A successful offline run is not authenticated exchange acceptance, deployment approval, a lifecycle certificate or permission to clear incidents. Keep the running owner paused until the remaining recovery and execution cases have been demonstrated. The root Compose package still describes the older execution architecture and must not replace the current AWS owner.

## Preserving the existing AWS startup

The checked-in `binana-stage1-start` is the repaired host script. `compose.running-images-20261007.json` pins the seven image digests that were running after the research upgrade. These digests refer to retained local AWS images, not a published registry. The script depends on the host's existing private environment, base Compose file and overlays. It is an exact recovery record, not a standalone installer.

`compose.research-v193.yml` records the research-only image/controller override. The installed host filename is `compose.sharia-v193.yml`; both express the same override. The final image overlay takes precedence and pins the built research image.

The pre-upgrade topology was captured at 21:31 UTC on October 6 (October 7 in the owner's timezone). The upgrade completed at 21:37 UTC. The latest image overlay and verification receipt supersede that snapshot for the research image. Credentials and runtime database copies must remain on AWS.
