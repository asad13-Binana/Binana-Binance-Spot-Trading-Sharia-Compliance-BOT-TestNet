from __future__ import annotations
"""Ed25519 attestations for automated Sharia screening artifacts.

The request bus deliberately uses a shared HMAC because several services may
request a screen.  A result, however, is authoritative only when the screener
attests it with a private key that is never mounted into requesters or
consumers.  Consumers receive only the public verification key.

This module authenticates software artifacts produced by the automated
research screener.  It does not make or certify a religious determination.
"""
import base64
import hashlib
import json
import os

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from services.common.envelope import installed_release_hash


ATTESTATION_VERSION = 1
RESULT_PURPOSE = "sharia-screening-result"
STATUS_PURPOSE = "sharia-status-record"
PRIVATE_KEY_ENV = "SHARIA_RESULT_SIGNING_PRIVATE_KEY_B64"
PUBLIC_KEY_ENV = "SHARIA_RESULT_VERIFY_PUBLIC_KEY_B64"


class ShariaAttestationError(ValueError):
    """The artifact is unsigned, forged, malformed, or release-mismatched."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _decode_key(env_name: str, *, private: bool):
    encoded = os.getenv(env_name, "").strip()
    if not encoded:
        raise ShariaAttestationError(f"{env_name} is required")
    try:
        key = ECC.import_key(base64.b64decode(encoded, validate=True))
    except Exception as exc:
        raise ShariaAttestationError(f"{env_name} is not a valid base64 DER Ed25519 key") from exc
    if key.curve != "Ed25519":
        raise ShariaAttestationError(f"{env_name} must be an Ed25519 key")
    if private and not key.has_private():
        raise ShariaAttestationError(f"{env_name} must contain the screener private key")
    if not private and key.has_private():
        # A verifier must never silently accept deployment of the private key.
        raise ShariaAttestationError(f"{env_name} must be public-only")
    return key


def load_private_key():
    return _decode_key(PRIVATE_KEY_ENV, private=True)


def load_public_key():
    return _decode_key(PUBLIC_KEY_ENV, private=False)


def _key_id(key) -> str:
    public_der = key.public_key().export_key(format="DER")
    return hashlib.sha256(public_der).hexdigest()[:24]


def _body(payload: dict, *, purpose: str, release_hash: str) -> dict:
    return {
        "attestation_version": ATTESTATION_VERSION,
        "algorithm": "Ed25519",
        "purpose": purpose,
        "release_hash": release_hash,
        "payload": payload,
    }


def sign_payload(payload: dict, *, purpose: str) -> dict:
    if not isinstance(payload, dict):
        raise ShariaAttestationError("attested payload must be an object")
    release_hash = installed_release_hash()
    if not release_hash:
        raise ShariaAttestationError("installed release hash unavailable")
    key = load_private_key()
    body = _body(payload, purpose=purpose, release_hash=release_hash)
    signature = eddsa.new(key, "rfc8032").sign(_canonical_bytes(body))
    return {
        "attestation_version": ATTESTATION_VERSION,
        "algorithm": "Ed25519",
        "purpose": purpose,
        "release_hash": release_hash,
        "key_id": _key_id(key),
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def verify_payload(payload: dict, attestation: dict, *, purpose: str) -> None:
    if not isinstance(payload, dict) or not isinstance(attestation, dict):
        raise ShariaAttestationError("payload and attestation must be objects")
    if attestation.get("attestation_version") != ATTESTATION_VERSION:
        raise ShariaAttestationError("unsupported Sharia attestation version")
    if attestation.get("algorithm") != "Ed25519":
        raise ShariaAttestationError("Sharia attestation algorithm must be Ed25519")
    if attestation.get("purpose") != purpose:
        raise ShariaAttestationError("Sharia attestation purpose mismatch")
    release_hash = installed_release_hash()
    if not release_hash or attestation.get("release_hash") != release_hash:
        raise ShariaAttestationError("Sharia attestation release binding failed")
    key = load_public_key()
    if attestation.get("key_id") != _key_id(key):
        raise ShariaAttestationError("Sharia attestation key id mismatch")
    try:
        signature = base64.b64decode(str(attestation.get("signature", "")), validate=True)
    except Exception as exc:
        raise ShariaAttestationError("Sharia attestation signature is not valid base64") from exc
    body = _body(payload, purpose=purpose, release_hash=release_hash)
    try:
        eddsa.new(key, "rfc8032").verify(_canonical_bytes(body), signature)
    except (ValueError, TypeError) as exc:
        raise ShariaAttestationError("Sharia attestation signature verification failed") from exc


def attach(payload: dict, *, purpose: str) -> dict:
    """Return a copy with an Ed25519 ``attestation`` field."""
    unsigned = dict(payload)
    unsigned.pop("attestation", None)
    return dict(unsigned, attestation=sign_payload(unsigned, purpose=purpose))


def verify_attached(payload: dict, *, purpose: str) -> dict:
    """Verify and return a copy without its ``attestation`` field."""
    if not isinstance(payload, dict):
        raise ShariaAttestationError("attested artifact must be an object")
    unsigned = dict(payload)
    attestation = unsigned.pop("attestation", None)
    verify_payload(unsigned, attestation, purpose=purpose)
    return unsigned
