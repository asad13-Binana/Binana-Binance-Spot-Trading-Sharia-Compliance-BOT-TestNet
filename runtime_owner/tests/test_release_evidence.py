import unittest
from freqtrade.binana.release_gate import certificate_blockers


class ReleaseEvidence(unittest.TestCase):
    def test_expired_unverified_or_changed_release_never_authorizes_resume(self):
        bindings={'code_sha256':'code', 'config_sha256':'config', 'exchange_epoch_id':'epoch'}
        valid={**bindings, 'generated_at':100, 'valid_until':200, 'verified':True,
               'controlled_lifecycle_verified':True, 'restart_verified':True,
               'backup_restore_verified':True, 'critical_suite_10x_verified':True,
               'one_slot_soak_verified':True, 'four_slot_soak_verified':True}
        self.assertEqual(certificate_blockers(valid, bindings, now=150), [])
        for change in [{'controlled_lifecycle_verified':False}, {'valid_until':149},
                       {'code_sha256':'other'}, {'config_sha256':'other'},
                       {'exchange_epoch_id':'other'}, {'generated_at':float('nan')},
                       {'one_slot_soak_verified':False},{'four_slot_soak_verified':False}]:
            with self.subTest(change=change):
                self.assertTrue(certificate_blockers({**valid, **change}, bindings, now=150))
        self.assertTrue(certificate_blockers(None, bindings, now=150))


if __name__ == '__main__':
    unittest.main()
