# Retired root engines and historical evidence

The standalone Oracle bundle is the supported route. Root ICT strategy, legacy
all-in-one engine, order-owning sidecar and their Docker/deployment launchers are
removed. Their exact source bytes remain in the immutable Git baseline recorded
in `retired_sources.json`; nothing from that registry is copied into a new image.

`python scripts/resolve_retired_source.py --path PATH --expected-sha256 SHA256`
returns only verified historical bytes to standard output. It checks the exact
baseline commit, path, Git blob, byte length and SHA-256. It performs no network
fetch, restore, imports, code execution, host action or order operation. A shallow
checkout missing the baseline must obtain that Git history explicitly before
historical lookup can succeed. New Oracle builds do not require these old objects.

The historical deployed source indexes and all 54 captured overrides are kept.
Eleven root sidecar paths referenced by the service index now use this optional
historical lookup; only the selected captured universe, market and monitor bytes
are active support materializer inputs. An index's expected SHA must match the
requested revision: a different historical SHA fails closed and is not silently
substituted with newer bytes.

Legacy-only test cases were retired with their engine. Mixed test modules retain
independent support/Sharia/Telegram checks. Current execution coverage is provided
by the owner tests for entry submission once, unknown ACK recovery, restart and
partial fills, order-list children, protection, zero-fill cleanup, user-stream
health, canonical fees/receipts, retained inventory, release admission and operator
exit. The retained baseline preserves the earlier tests as historical evidence.

No source deletion is authority to delete databases, WAL files, private environment
files, accepted certificates, backups, previous images or deployment checkpoints.

The AWS `binana-stage1-start` wrapper and its three dated Compose overlays are
retired with verified baseline identities. Inert AWS policy, topology and local
image records remain provenance. `verify_local_images.py` remains a generic
identity verifier, not an install or startup route. The historical four-instance
namespace registry is not a current co-hosting capacity approval.
