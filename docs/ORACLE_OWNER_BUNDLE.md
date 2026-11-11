# Portable Oracle bundle for the recovered Spot Testnet owner

This is the deployment source package for `BinanaNfiSpot` on Freqtrade **2026.10**.
It starts **stopped**. Building it does not certify an Oracle host, an exchange
account, a strategy return, or a release certificate. The same source may be
mirrored in the LIVE repository for review; this bundle remains **Testnet-only**.

The legacy root `docker-compose.yml`, `deploy/install_artifact.sh`, and AWS
`binana-stage1-start` are different deployments. Do not combine their files,
Compose projects, containers, state directories, or old image tags with this
bundle. No AWS host image, `/home/ubuntu` path, Docker socket, or legacy
order-owning sidecar is used here.

## Build and verify the immutable public package

The final images use a minimal Debian runtime with pinned patched native
libraries. Runtime pip and shells are absent; rebuild through the supplied
Dockerfiles for dependency updates. Follow the offline image gates and
remaining host acceptance requirements in [the image security review](IMAGE_SECURITY_20261112.md).

From the reviewed repository checkout, with Python 3.10 or later:

```sh
python runtime_owner/deployment/materialize_oracle_bundle.py /tmp/binana-owner-bundle
python /tmp/binana-owner-bundle/oracle_preflight.py --bundle /tmp/binana-owner-bundle
```

For offline packaging, add `--archive /path/to/freqtrade-2026.10.tar.gz` to the
first command. The archive must match the existing pinned upstream commit
`4cf0351e08d26e96a6429ef48103b726bb230cf8` and SHA-256
`e7e83e139f0d7b01f337dfb2e0c4ff13d895431615886b3c8570e99bb139476a`.
A different archive, a symlink source, or an existing output directory is
rejected. The output manifest records every file's exact size and SHA-256.
Generated contexts are byte-identical across output locations.

The services context includes the dependency closure used by the manual
registry, scanner, market collector and panel. The captured scanner permits
`UNIVERSE_LIMIT=0` for all eligible pairs, or an explicit cap up to 200; the
example and Compose default preserve the approved all-eligible policy. Market availability, exclusions, Sharia
approval, liquidity and freshness still determine eligibility. The owner
retains four occupied slots, nominal 250 USDT each, total allocation 1000 USDT,
the five-minute NFI signal and its existing 52% normal flow threshold.

The default services and optional research services are:

| Service | Responsibility | Trading credentials |
| --- | --- | --- |
| `freqtrade` | Sole order owner; original BINANA native protection and recovery logic | Dedicated Binance **Spot Testnet** key only |
| `universe` | Public Spot scanner and sequence-aware market collector; optional advisory CoinGecko/CMC | Optional provider keys; no Binance trading key |
| `sharia-screener` | Existing networkless projector for the owner's manual registry | Registry signing material only |
| `telegram-broker` | Existing current owner control panel and durable notifications | Telegram token and internal Freqtrade API credentials |
| `owner-monitor` | Read-only accounting observer; writes compatibility health/state files | None; network disabled |
| `sharia-research` (profile `research`) | Existing source-based research worker; writes only the separate research tree | Research bus/result signing material and optional provider keys |
| `sharia-egress-proxy` (profile `research`) | Existing restricted HTTPS CONNECT proxy for that worker | None |

`owner-monitor` is the old monitor-only function under an accurate service
name. It cannot submit orders. The old executable engine is excluded from the
build contexts. Historical repository snapshots remain evidence because
source indexes and some health transformations depend on specific files.
Removing them from the source tree requires a separate reference migration.

Manual registry maintenance and existing reviewed records remain the operational
gate. The optional research worker has a separate state tree and cannot write
the operational registry. Research results still require the existing owner
review; a completed scan never grants trading permission by itself.

Research is disabled by default. Telegram rejects a scan or decision visibly
before enqueueing it if research is disabled, its heartbeat is stale, or its
backend is not ready. Enabling the optional profile requires the private
research inputs below. The packaged controller is the existing hash-verified
V19.3 definition; no replacement model, evidence, or Sharia rule is invented.

## Oracle capacity and private inputs

Oracle's current Always Free documentation describes an A1 allowance totaling
**2 OCPUs and 12 GB memory**, 1,500 OCPU-hours and 9,000 GB-hours monthly.
Choose an available A1 instance in the tenancy's home region and Ubuntu 24.04
ARM64. The E2 micro's 1 GB memory is unsuitable. A 100 GB boot volume fits
within the 200 GB combined Always Free boot/block allowance. Capacity is not
guaranteed, and idle instances can be reclaimed.

Sources, retrieved 2026-10-20:

- [Oracle Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Oracle Free Tier and trial boundaries](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm)

Install Docker/Compose on the future host through the platform's reviewed
administration process, verify the clock and DNS, and size swap/build disk
space before building. The existing owner clean-build workflow has separate
image tests; this bundle does not claim an Oracle reboot, OOM, network-loss,
restore or sustained resource test has happened.
One default owner stack has aggregate limits of 1.8 CPUs and 6,272 MiB; enabling
research raises these to 2 CPUs and 7,168 MiB. These are limits, not measured
native capacity or build headroom. Choose one Testnet OR one LIVE owner stack
for this planning envelope. Both default stacks total 3.6 CPUs and 12,544 MiB
before the OS and Docker; they exceed one 2-OCPU/12-GB Always Free allowance.
Do not reduce safety or memory budgets to claim that both fit.

Keep the public bundle immutable. Put the private environment **outside it**,
for example `/etc/binana-oracle-testnet/runtime.env`, mode `600`, using
`oracle.env.example` as the field list. Never paste credentials into GitHub,
the public config, a Dockerfile, or a command line. Compose passes each secret
only to its intended service. The preflight parser accepts literal unquoted
values and rejects interpolation.

Prepare these host paths, owned so UID `999` / GID `987` can read the config and
write only the intended state. The image deliberately uses fixed identities;
an arbitrary `BOT_UID` override is unsupported.

| Path | Required content |
| --- | --- |
| `BINANA_CONFIG_DIR` | `config.json` copied from `config.example.json`, plus the raw 32-byte `release-authority.ed25519.pub`; no other files |
| `BINANA_STATE_DIR` | `freqtrade/`, `freqtrade/logs/`, `sharia/`, `universe/`, `runtime/`; owner-maintained `sharia/halal_coins.json` and its required report evidence |
| `BINANA_RELEASE_DIR` | Initially empty; later only `deployment.json` and the correctly issued `certificate.json` |
| Separate unmounted authority storage | Private release issuer key and private acceptance receipts; never mount these into a container |

For optional research, also prepare `sharia_research/`,
`runtime/sharia_research/`, and `audit/` under `BINANA_STATE_DIR`.
`sharia_research/source_registry.json` must contain the owner's reviewed
source registry and its referenced evidence. The public
`research-source-registry.example.json` has no approved coin evidence and is
only a format starting point. Preserve the worker's existing proof, quota,
identity, and source checks. Set `BINANA_RESEARCH_ENABLED=true` only when this
private setup is ready, then include `--profile research` in Compose commands.
The worker accesses the network only through the packaged restricted proxy;
it has no Binance trading key or operational-registry write mount.

The config example intentionally contains
`REPLACE_WITH_RECONCILED_TESTNET_EPOCH`. Replace it with the real reconciled
account epoch before startup. Do not invent a reset or reuse the captured AWS
epoch as if it were a fresh reconciliation. An existing account needs its
consistent `binana-owner.sqlite` and `binana-extension.sqlite`, retained
inventory baseline, fills, plans, intents and notification state restored
from a verified backup. Starting with empty databases while an account has
orders or inventory is not a valid migration. No private database or
acceptance receipt is included in this public source bundle.

The public config contains no credentials because Telegram mounts it for
read-only inspection. Exchange and API credentials come from the scoped
private environment. Keep native Freqtrade Telegram disabled; the broker is
the sole update poller. Preserve the existing strategy, allocation and
protection contract. Custom CCXT URL overrides are rejected by the offline
preflight because they could replace the Testnet transport.

Check private inputs without a network or Docker command:

```sh
python /opt/binana-owner-bundle/oracle_preflight.py \
  --bundle /opt/binana-owner-bundle \
  --env-file /etc/binana-oracle-testnet/runtime.env
```

Success means files are prepared, **not** that any key works or a certificate
is valid. Failures name the missing field/path and never print its value.

## Build, stopped startup, acceptance and recovery

Only on the intended future host, after the preceding input checks:

```sh
cd /opt/binana-owner-bundle
docker compose --env-file /etc/binana-oracle-testnet/runtime.env -f compose.json config -q
docker compose --env-file /etc/binana-oracle-testnet/runtime.env -f compose.json build
docker compose --env-file /etc/binana-oracle-testnet/runtime.env -f compose.json up -d
```

To validate the support image independently of private inputs, before starting
services:

```sh
docker build -f services/Dockerfile -t binana-support-offline services
docker run --rm --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777 \
  --entrypoint python binana-support-offline -B /app/validate_support.py
```

This gate imports the actual packaged entrypoints, verifies the panel/broker
interface, controller integrity and all-eligible scanner settings, and rejects
any credentials. Network and subprocess calls are denied inside the checker.
It does not call a trading or research service's main loop.

The Compose `up` command starts services and can perform authenticated reconciliation.
It does not authorize a new entry: the owner is explicitly stopped at every
container startup, and its unchanged release gate rejects missing, expired,
incorrectly signed or mismatched acceptance evidence. This guide does not
instruct automatic Resume. A deliberate later operator action is separate.
The owner waits for healthy manual-registry and universe services, allowing
the first pairlist to exist before Freqtrade loads it.

The owner API binds only `127.0.0.1:8090` on the host. Access it through an SSH
tunnel if needed. Do not expose it in Oracle security lists or NSGs.

Use the exact built image digest, config, account-key hash, exchange epoch,
deployment binding and dependency binding for the existing release-authority
procedure. It requires actual controlled lifecycle, restart, restore, offline,
one-slot and four-slot receipts. Do not copy the AWS certificate or set its
flags manually. Its seven-day lifetime and renewal checks remain intact.
The issuer module is included in the owner context as source; host acceptance
or renewal still requires private evidence and a host-only private key.

Before retiring any old runtime, verify: one order owner and one Telegram
poller; accurate private state; independent off-host backup plus restore drill;
image/runtime version; ARM64 image behavior; DNS, clock and Binance endpoints;
market freshness; native partial-fill/replacement recovery; resource limits;
restart behavior; and rollback to the previously verified image and state.
Use SQLite online backup for a running database, or copy a consistently
stopped state directory. Never copy only a live WAL database file.

Rolling back a new-owner deployment must restore that owner's image and
bindings, not start the root legacy sidecar. Keep known-good images and state
until the new host passes acceptance. The bundle builder deliberately performs
no deletion of host containers, databases, private keys or backups.

The exact-version references supporting the build and backtest distinction:

- [Freqtrade 2026.10 Docker deployment](https://www.freqtrade.io/en/2026.10/docker_quickstart/)
- [Freqtrade 2026.10 backtesting assumptions](https://www.freqtrade.io/en/2026.10/backtesting/)
- [Exact 2026.10 backtesting callback implementation](https://github.com/freqtrade/freqtrade/blob/2026.10/freqtrade/optimize/backtesting.py)
- [Binance official documentation index](https://developers.binance.com/en/docs/llms.txt)

An ordinary candle backtest does not implement BINANA's native order-list
engine. `IctSmcStrategy` deliberately refuses Freqtrade entries; the current
owner wrapper deliberately refuses normal Freqtrade exits to avoid a second
SELL beside native protection. Neither may be presented as native execution
or profitability proof by simply renaming the old backtest command.

## Host certificate maintenance

The bundle includes maintenance/README.md and a non-secret Oracle host profile. Issuance and renewal require explicit host paths and Compose identity; no historical AWS path is used for the selected Oracle profile. The tools retain the existing signed acceptance and immutable-image requirements. They run only after separately authorized host acceptance. Local packaging tests do not certify a deployed host.
