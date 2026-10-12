# Candidate release-authority contract

The October 12 candidate refuses legacy unsigned JSON certificates. This is a deliberate compatibility break: existing booleans and expiry timestamps cannot authenticate an issuer. No authority key or renewed certificate is installed by this branch.

The runtime reads one envelope at the existing owner certificate path. It requires exactly `schema`, `payload`, and `signature`, with schema `binana-release-ed25519-v1`. The payload is a JSON object containing the existing acceptance flags, times and runtime bindings. The signature is standard base64 encoding of a 64-byte Ed25519 signature over:

```python
json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
```

The independent trust anchor is the raw 32-byte Ed25519 public key at `/freqtrade/binana-config/release-authority.ed25519.pub`. The certificate cannot choose its own key or algorithm. Duplicate JSON members, unexpected envelope fields, malformed signatures, missing keys and signatures from other issuers are rejected. Existing expiry, maximum-seven-day TTL, binding and lifecycle policy checks run after signature verification.

The public key and its parent mount must be read-only to the trading owner. The verifier code also needs an immutable image/read-only root filesystem. The private key must remain outside the trading container and runtime-writable shared directories. A signature authenticates the issuer's statement; it does not prove the statement is true or make an incomplete runtime binding complete.

Renewal is still an unimplemented deployment workflow. A qualifying issuer must independently validate receipts for the exact candidate image/configuration/account/epoch, controlled lifecycles, restart/restore and soaks; obtain fresh exchange reconciliation; reject missing, stale or changed evidence; and activate a fully signed envelope with one atomic durable replacement. Runtime readiness should derive from this single object. Historical scripts that write two unsigned markers are not a renewal procedure for this candidate.

Do not convert old flags into a signed certificate to bypass new acceptance. No runtime is automatically started when a certificate file appears. The remaining complete-environment binding and activation design is tracked by audit findings F15-F17.
