"""Issuer authenticity is independent of the certificate's acceptance flags."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from freqtrade.binana.release_gate import load_certificate


class ReleaseSignature(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'certificate.json'
        self.authority=Path(self.tmp.name)/'authority.pub'
        self.key=Ed25519PrivateKey.generate()
        self.authority.write_bytes(self.key.public_key().public_bytes_raw())
        self.payload={'verified':True,'generated_at':100,'valid_until':200}

    def write(self,payload=None,signer=None):
        payload=self.payload if payload is None else payload
        canonical=json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        envelope={'schema':'binana-release-ed25519-v1','payload':payload,
                  'signature':base64.b64encode((signer or self.key).sign(canonical)).decode()}
        self.path.write_text(json.dumps(envelope));return envelope

    def test_unsigned_flags_are_rejected(self):
        self.path.write_text(json.dumps(self.payload))
        self.assertIsNone(load_certificate(self.path))

    def test_valid_signature_returns_payload_for_policy_validation(self):
        self.write()
        self.assertEqual(load_certificate(self.path,authority_path=self.authority),self.payload)

    def test_payload_tampering_is_rejected(self):
        envelope=self.write();envelope['payload']['valid_until']=999
        self.path.write_text(json.dumps(envelope))
        self.assertIsNone(load_certificate(self.path,authority_path=self.authority))

    def test_other_issuer_and_missing_anchor_are_rejected(self):
        self.write(signer=Ed25519PrivateKey.generate())
        self.assertIsNone(load_certificate(self.path,authority_path=self.authority))
        self.authority.unlink()
        self.assertIsNone(load_certificate(self.path,authority_path=self.authority))

    def test_unsupported_schema_or_malformed_signature_is_rejected(self):
        for change in ({'schema':'future'},{'signature':'not-base64'},{'issuer_key':'untrusted'}):
            envelope=self.write();envelope.update(change);self.path.write_text(json.dumps(envelope))
            with self.subTest(change=change):
                self.assertIsNone(load_certificate(self.path,authority_path=self.authority))

    def test_duplicate_json_members_are_rejected(self):
        self.write();text=self.path.read_text().replace('"verified": true','"verified": false, "verified": true')
        self.path.write_text(text)
        self.assertIsNone(load_certificate(self.path,authority_path=self.authority))
