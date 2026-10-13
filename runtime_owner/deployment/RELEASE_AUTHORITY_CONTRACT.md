# Candidate release-authority contract

The October 12 candidate refuses legacy unsigned JSON certificates. This is a deliberate compatibility break: existing booleans and expiry timestamps cannot authenticate an issuer. The accepted AWS deployment now has an independent authority and a daily renewal timer; private keys remain host-only.

The runtime reads one envelope at the existing owner certificate path. It requires exactly `schema`, `payload`, and `signature`, with schema `binana-release-ed25519-v1`. The payload is a JSON object containing the existing acceptance flags, times and runtime bindings. The signature is standard base64 encoding of a 64-byte Ed25519 signature over:

```python
json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
```

The independent trust anchor is the raw 32-byte Ed25519 public key at `/freqtrade/binana-config/release-authority.ed25519.pub`. The certificate cannot choose its own key or algorithm. Duplicate JSON members, unexpected envelope fields, malformed signatures, missing keys and signatures from other issuers are rejected. Existing expiry, maximum-seven-day TTL, binding and lifecycle policy checks run after signature verification.

The public key and its parent mount must be read-only to the trading owner. The verifier code also needs an immutable image/read-only root filesystem. The private key must remain outside the trading container and runtime-writable shared directories. A signature authenticates the issuer's statement; it does not prove the statement is true or make an incomplete runtime binding complete.

Renewal is implemented in `scripts/recovery/issue_candidate_release.py --renew` and installed by `scripts/recovery/install_release_renewal.py`. The root-owned timer validates unchanged image, effective configuration, dependency/code/deployment/account/epoch bindings, accepted lifecycle/soak/restore receipts and fresh exchange reconciliation before atomically replacing the signed envelope. It does not renew changed or unqualified releases and does not resume trading. The AWS timer was installed and forced successfully on October 14, 2026 (Asia/Karachi). The historical audit's missing-renewal finding is superseded by that deployment evidence.

Do not convert old flags into a signed certificate to bypass new acceptance. No runtime is automatically started when a certificate file appears. The remaining complete-environment binding and activation design is tracked by audit findings F15-F17.
