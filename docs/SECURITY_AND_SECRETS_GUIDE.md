# Security & Secrets Guide

## Never in the repository or the release ZIP
Binance API key/secret, Telegram token, Freqtrade API password/JWT/WS token, the
HMAC bus keys, the Sharia Ed25519 signing private key, the live-evidence key, SSH private keys, `.env`, databases, logs. The release ships only
`.env.example` placeholders. A secret scanner (`tests/secret_scan.py`) runs in the release verifier and CI
and fails the build on any populated sensitive assignment or token-shaped literal.

## Where secrets live
Only in the Oracle host's private env file `/etc/binance-freqtrade-v101/.env`, mode `600`, owned by the
bot user. One secret-free public source repository + Oracle-only secrets is the approved topology
(V101-NEW-012). Do not keep a second divergent code copy; use one auditable codebase and environment-based
private configuration.

## Inter-service authentication (V101-NEW-001)
Every signal, command, Sharia request/result, and live-evidence file is an HMAC-SHA-256 envelope bound to
producer, purpose, nonce, timestamps, expiry, and the installed release hash. A Sharia result or status
is additionally authorized by an Ed25519 signature whose private key is held only by the screener.
Generate each bus key as
≥ 32 random characters:

```
python -c "import secrets; print(secrets.token_hex(24))"
```

- `SIGNAL_HMAC_KEY` — Freqtrade (producer) + sidecar (consumer)
- `COMMAND_HMAC_KEY` — Telegram (producer) + sidecar (consumer)
- `SHARIA_HMAC_KEY` — Telegram/sidecar request producers + screener request consumer
- `SHARIA_APPROVAL_HMAC_KEY` — Telegram owner-decision producer + screener only
- `SHARIA_RESULT_HMAC_KEY` — screener result producer + sidecar/Telegram result consumers
- `SHARIA_RESULT_SIGNING_PRIVATE_KEY_B64` — screener only; Ed25519 private DER, base64 encoded
- `SHARIA_RESULT_VERIFY_PUBLIC_KEY_B64` — public Ed25519 DER distributed to all result/status consumers
- `LIVE_EVIDENCE_KEY` — operator/release-certifier only; needed solely to promote to live

The canonical Sharia directory is writable by the screener alone; all other containers mount it read-only.
`no-new-privileges`, read-only root filesystems, and per-service memory limits are set in Compose.

## Least privilege
- Binance key: **Spot only, withdrawals disabled, IP-restricted**, on a dedicated sub-account.
- The Sharia screener holds no Binance key and no external AI/model API key.
- Freqtrade holds no Binance credentials (signal-only).

## Live promotion (C-003 / H-007)
Live mode requires ALL of: `EXECUTION_MODE=live`, installed `RELEASE_SHA256.txt` == `SIDECAR_RELEASE_HASH`
== `SIDECAR_LIVE_OK`, `AUTO_CONFIRM=false`, and a valid signed `LIVE_EVIDENCE.json` envelope binding this
release hash, the V19.1 controller hash, the protected strategy fingerprints, an exact-strategy Freqtrade
backtest artifact (≥ 100 trades), and Testnet/Oracle/clean-pass assertions. Presence-only marker files and
the legacy backtest gate can no longer unlock live on their own.

## Supply chain
GitHub Actions are commit-SHA pinned; `requirements.services.lock` is the resolved dependency graph
(`pip-audit`: no known vulnerabilities on this host). The Python and Freqtrade base images are pinned by
immutable registry digest. REMAINING EXTERNAL WORK: enable branch protection with the CODEOWNERS reviewer
and retain signed CI image/artifact provenance.
