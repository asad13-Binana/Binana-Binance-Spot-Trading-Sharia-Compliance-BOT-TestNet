"""Issuer renewal rejects drift and forged prior authorization offline."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts/recovery'))
import issue_candidate_release as issuer


class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.key=Ed25519PrivateKey.generate()
        (self.root/'release-authority.ed25519.pub').write_bytes(self.key.public_key().public_bytes(
            serialization.Encoding.Raw,serialization.PublicFormat.Raw))
        (self.root/'private').write_bytes(self.key.private_bytes(serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,serialization.NoEncryption()))
        self.status={k:'a'*64 for k in issuer.deploy.authority.BINDINGS}
        self.payload={**self.status,**{k:True for k in issuer.deploy.authority.FLAGS},'image':'sha256:'+'b'*64}
        self.config=patch.object(issuer.deploy,'CONFIG',self.root/'config.json');self.config.start()
    def tearDown(self):
        self.config.stop();self.temp.cleanup()
    def signed(self):return issuer.deploy.authority.sign(self.payload,self.root/'private')
    def test_same_accepted_bindings_can_renew(self):
        self.assertEqual(issuer.previous_authority(self.signed(),self.status,self.payload['image']),self.payload)
    def test_every_changed_binding_is_rejected(self):
        for name in self.status:
            with self.subTest(binding=name),self.assertRaisesRegex(RuntimeError,'BINDING_DRIFT'):
                issuer.previous_authority(self.signed(),{**self.status,name:'changed'},self.payload['image'])
    def test_tampered_payload_cannot_renew(self):
        raw=json.loads(self.signed());raw['payload']['verified']=False
        with self.assertRaises(InvalidSignature):
            issuer.previous_authority(json.dumps(raw).encode(),self.status,self.payload['image'])
    def test_signed_but_incomplete_acceptance_is_rejected(self):
        self.payload['four_slot_soak_verified']=False
        with self.assertRaisesRegex(RuntimeError,'ACCEPTANCE_INCOMPLETE'):
            issuer.previous_authority(self.signed(),self.status,self.payload['image'])


if __name__=='__main__':unittest.main()
