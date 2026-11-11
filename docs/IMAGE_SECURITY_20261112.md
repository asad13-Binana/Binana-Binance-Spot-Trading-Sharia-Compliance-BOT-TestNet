# Final-image security repair, 12 November 2026

The previous owner and support images each reported **48 HIGH, zero CRITICAL**
findings in Trivy 0.76.1. These were 48 package/advisory matches covering 12
unique advisories, not 48 independently reproduced application exploits.
Updating Debian stable alone could not resolve them: its available package
versions were unchanged. Python lock audits alone did not cover this OS layer.

## Runtime changes

Both Dockerfiles now use a pinned, public `distroless/cc-debian13:nonroot`
runtime. The existing Python builders, hash-locked dependencies, Freqtrade
2026.10 source, UID 999/GID 987, entrypoints and package modes are retained.
No strategy, risk, Sharia, exchange or owner-recovery source changes are needed.

Only Python's extra shared libraries are copied from the Debian builders;
their actual Debian status records and copyright files accompany them.
Package metadata is not edited to conceal findings. Compilers, APT, shells,
Perl, systemd and administrative utilities are absent from the final runtime.
Pip is uninstalled after the build's `pip check`; its vendored setuptools
component is therefore physically absent too. Install dependencies in the
builder and rebuild the immutable image; runtime `pip` or shell maintenance
is intentionally unavailable.

Three libraries are built from pinned upstream archives because Debian stable
had no suitable update. `build_native_runtime.sh` checks each archive's SHA-256
before extraction. `/usr/local/share/binana-native-runtime.json` records URLs,
versions, archive hashes and the installed shared-library hashes. Ncurses and
libuuid license texts are included in `/usr/local/share/licenses`.

| Previous HIGH matches | Treatment | Primary evidence |
| --- | --- | --- |
| 36 util-linux matches: CVE-2026-76642, CVE-2026-78408/78409/78410 | Remove administrative programs and their unused libraries; compile the required UUID library from util-linux 2.42.4 | [Debian advisory](https://security-tracker.debian.org/tracker/CVE-2026-76642), [official source and checksums](https://www.kernel.org/pub/linux/utils/util-linux/v2.42/) |
| Three SQLite matches: CVE-2026-119373/119374/119379 | Replace SQLite 3.46.1 with 3.54.1; preserve the used FTS, session, metadata, threading and transaction features | [SQLite 3.54.1 release](https://sqlite.org/releaselog/3_54_1.html), [FTS5 advisory](https://security-tracker.debian.org/tracker/CVE-2026-119373), [secure-delete advisory](https://security-tracker.debian.org/tracker/CVE-2026-119374), [session advisory](https://security-tracker.debian.org/tracker/CVE-2026-119379) |
| Four ncurses matches: CVE-2025-69720 | Use ncurses 6.6 libraries; omit the affected infocmp utility | [Maintainer release](https://invisible-island.net/ncurses/announce.html), [Debian advisory](https://security-tracker.debian.org/tracker/CVE-2025-69720) |
| ACL: CVE-2026-54369 | ACL utilities and library are not part of the runtime closure | [Debian advisory](https://security-tracker.debian.org/tracker/CVE-2026-54369) |
| Two systemd matches: CVE-2026-16742 | systemd-homed, libsystemd and libudev are not part of this container runtime | [Debian advisory](https://security-tracker.debian.org/tracker/CVE-2026-16742) |
| Perl: CVE-2026-9538 | Perl is not part of the runtime | [Debian advisory](https://security-tracker.debian.org/tracker/CVE-2026-9538) |
| Pip's vendored setuptools: CVE-2025-47273 | Remove the build-only pip installation after dependency consistency checks | [Maintainer advisory](https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf) |

The public runtime supports AMD64 and ARM64 at the same pinned index digest.
See [distroless documentation](https://github.com/GoogleContainerTools/distroless/blob/main/README.md)
and [Docker multi-stage builds](https://docs.docker.com/build/building/multi-stage/).
No private registry login or additional runtime credential is introduced.

## Reproduction and acceptance

The original image fails the HIGH/CRITICAL scan gate and the new offline
`validate_runtime_security.py` gate. The latter checks the actual SQLite
version, FTS negative-rowid prefix queries, WAL commit/rollback/reopen, integrity,
native imports, TLS CA store, runtime ncurses version, random UUID generation,
absence of administrative/install programs, and native-library hash bindings.

For each built owner and support image, run:

```sh
docker run --rm --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,size=128m,mode=1777 \
  --entrypoint python IMAGE /usr/local/share/validate_runtime_security.py
trivy image --scanners vuln --severity HIGH,CRITICAL --exit-code 1 IMAGE
```

Also run the owner's complete default validation entrypoint, the support image's
`/app/validate_support.py`, and the existing source release checks. CI does this
on separate native AMD64 and ARM64 runners. CI downloads checksum-pinned Trivy
0.76.1, scans both final images, requires OS and Python scan results, rejects
every HIGH/CRITICAL result, and uploads the unfiltered JSON reports. It uses
no ignore-unfixed switch, ignore file, VEX filter or severity override.

The initial rebuilt Testnet AMD64 images passed all four runtime tests each,
287 owner regressions with zero skips, and support imports. Both full scans
reported **zero HIGH/CRITICAL**, with **35 MEDIUM, 12 LOW and five UNKNOWN**
findings each. Those remaining findings are not cleared or reclassified.
The successful GitHub run and its image-security artifacts for a particular
commit are the release evidence; this document is not a substitute for them.

Trivy's OS/Python package scan does not independently certify arbitrary
source-built native libraries. The upstream release/advisory checks, exact
source hashes, runtime versions, binary hashes and offline regression tests
are separate evidence for these three components. This work does not run the
complete upstream SQLite/util-linux/ncurses test suites or prove absence of
unknown vulnerabilities. Future upstream advisories require continued review.

## What still blocks Oracle trading deployment

Passing image/source CI does not create or accept an Oracle host. Before
activation, the operator still needs:

1. A correctly sized native ARM64 host, working Docker/Compose, clock, DNS,
   storage, permissions and private network/secret provisioning. The documented
   free-tier budget is for one stack; both complete stacks exceed that budget.
2. Verified account-bound state restoration and reconciliation, manual Sharia
   evidence, real account/epoch bindings, and exactly one order owner and one
   Telegram poller. An empty database is not a migration of existing inventory.
3. Actual host lifecycle/restart/restore/offline/one-slot/four-slot acceptance
   receipts bound to the final image, configuration and dependency identities.
4. A newly signed release certificate from the host-only issuer, then verified
   expiry handling, renewal, rollback and sustained resource/network behavior
   on that Oracle host. Old AWS acceptance cannot authorize a new image/host.
5. Separate deployment and activation authorization. Both packages start
   stopped; LIVE remains disabled by default. No trading, cloud changes or
   authenticated exchange acceptance is performed by these image tests.

Software verification is not evidence of strategy profitability or successful
exchange execution. Preserve the detailed acceptance procedure in
[the Oracle bundle guide](ORACLE_OWNER_BUNDLE.md).
