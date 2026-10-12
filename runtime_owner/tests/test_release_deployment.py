import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from freqtrade.binana import release_gate

authority_path=Path(__file__).resolve().parents[1]/'deployment/release_authority.py'
if not authority_path.exists(): authority_path=Path('/freqtrade/recovery_validation/release_authority.py')
spec=importlib.util.spec_from_file_location('authority_test',authority_path)
authority=importlib.util.module_from_spec(spec);spec.loader.exec_module(authority)


class CompletePolicyBinding(unittest.TestCase):
    def test_exchange_and_admission_configuration_changes_invalidate_binding(self):
        config={'exchange':{'name':'binance','binana_testnet':True,'key':'secret',
            'ccxt_config':{'timeout':5000}},'pairlists':[{'method':'StaticPairList'}],
            'binana':{'testnet_epoch_id':'epoch'},'stake_amount':250}
        # The pre-existing config hash omitted exchange configuration entirely.
        with patch.object(release_gate.Path,'read_bytes',return_value=b'code'):
            before=release_gate.runtime_bindings(config)['config_sha256']
            changed=copy.deepcopy(config);changed['exchange']['binana_testnet']=False
            self.assertNotEqual(before,release_gate.runtime_bindings(changed)['config_sha256'])

    def test_missing_immutable_deployment_record_cannot_match_certificate(self):
        with patch.object(release_gate.Path,'read_bytes',return_value=b'code'):
            result=release_gate.runtime_bindings({'binana':{'testnet_epoch_id':'epoch'}})
        self.assertIn('deployment_sha256',result)
        self.assertFalse(result['deployment_sha256'])


class ReleaseReceiptValidation(unittest.TestCase):
    def receipts(self):
        result={}
        for name,slots in [('lifecycle',None),('one_slot',1),('four_slot',4)]:
            phases=['entry','restart','exit','closed_restart'] if slots else ['entry','restart','restart_again','exit','closed_restart']
            result[name]={'image':'sha256:'+'a'*64,'passed':True,'slot_soak':slots,
                'phases':[{'phase':p,'passed':True} for p in phases],
                'source_preserved':True,'source_config_sha256':'c'*64,'live_writable_mounts':False,
                'open_exchange_orders':0,'open_exchange_lists':0,'soak_seconds_observed':601}
        result['restore']={'image':'sha256:'+'a'*64,'passed':True,'logical_equality':True}
        result['offline']={'image':'sha256:'+'a'*64,'passed':True,
            'runs':[{'successful':True,'skipped':[],'tests':216} for _ in range(10)]}
        result['rollout']={'image':'sha256:'+'a'*64,'passed':True,'read_only':True,'init':True,
            'rollback_verified':True,'accounting_preserved':True,'source_config_sha256':'c'*64}
        return result

    def test_every_required_acceptance_phase_and_image_is_checked(self):
        receipts=self.receipts();image='sha256:'+'a'*64
        authority.validate_receipts(image,receipts)
        for name in receipts:
            changed=copy.deepcopy(receipts);changed[name]['passed']=False
            with self.subTest(name=name),self.assertRaises(ValueError):authority.validate_receipts(image,changed)
        receipts['four_slot']['soak_seconds_observed']=599
        with self.assertRaises(ValueError):authority.validate_receipts(image,receipts)

    def test_missing_or_stale_reconciliation_cannot_be_signed(self):
        status={k:'b'*64 for k in authority.BINDINGS}
        status.update(generated_at=100,blockers=['release_evidence_unavailable'],
            authenticated_reconciliation={'ok':True,'checked_at':100},retained_inventory={'ok':True,'checked_at':100})
        payload=authority.build_payload(status,'sha256:'+'a'*64,self.receipts(),now=101)
        self.assertEqual(payload['valid_until']-payload['generated_at'],7*86400)
        for key in ('authenticated_reconciliation','retained_inventory'):
            changed=copy.deepcopy(status);changed[key]['checked_at']=0
            with self.subTest(key=key),self.assertRaises(ValueError):
                authority.build_payload(changed,'sha256:'+'a'*64,self.receipts(),now=101)
        status['blockers']=['PRIVATE_USER_STREAM_UNAVAILABLE']
        with self.assertRaises(ValueError):authority.build_payload(status,'sha256:'+'a'*64,self.receipts(),now=101)
