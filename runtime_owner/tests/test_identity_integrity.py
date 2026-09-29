import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from freqtrade.binana.state_store import StateStore


class IdentityIntegrity(unittest.TestCase):
    def test_pair_and_role_conflicts_block_entries(self):
        for mismatch in ['pair', 'role']:
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as directory:
                store = StateStore(Path(directory) / 'state.sqlite')
                store.put_intent(intent_id='i', pair='ADA/USDT', signal_id='s',
                                 halal_allowed=True, registry_sha256='h', nominal_usdt='250')
                if mismatch == 'pair':
                    generation = dict(intent_id='i', generation=1, mode='FIXED_OCO', status='PENDING')
                    store.put_generation(**generation, pair='ADA/USDT', ids={'tp_order_id':'101'})
                    with self.assertRaises(sqlite3.IntegrityError):
                        store.put_generation(**generation, pair='ETH/USDT', ids={'sl_order_id':'102'})
                else:
                    identity = dict(scope_id='scope', intent_id='i', generation=1, pair='ADA/USDT', client_id='a')
                    store.bind_order_identity(**identity, role='ENTRY', order_id='1')
                    with self.assertRaises(sqlite3.IntegrityError):
                        store.bind_order_identity(**identity, role='SL', order_id='1')
                self.assertTrue(store.has_blockers())

    def test_protection_child_binding_survives_restart_and_conflicting_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite'
            store = StateStore(path)
            store.put_intent(intent_id='i', pair='ADA/USDT', signal_id='s',
                             halal_allowed=True, registry_sha256='h', nominal_usdt='250')
            generation = dict(intent_id='i', generation=1, pair='ADA/USDT',
                              mode='FIXED_OCO', status='PENDING')
            store.put_generation(**generation, ids={'tp_order_id': '101'})
            store = StateStore(path)
            store.put_generation(**generation, ids={'tp_order_id': '101', 'sl_order_id': '102'})
            with self.assertRaises(sqlite3.IntegrityError):
                store.put_generation(**generation, ids={'tp_order_id': '201'})
            self.assertEqual(store.generation('i', 1)['tp_order_id'], '101')

    def test_trade_binding_is_unique_and_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.sqlite')
            for intent in ['a', 'b']:
                store.put_intent(intent_id=intent, pair='ADA/USDT', signal_id=intent,
                                 halal_allowed=True, registry_sha256='registry', nominal_usdt='250')
            store.set_trade_id('a', 7)
            store.set_trade_id('a', 7)
            with self.assertRaises(sqlite3.IntegrityError):
                store.set_trade_id('a', 8)
            with self.assertRaises(sqlite3.IntegrityError):
                store.set_trade_id('b', 7)

    def test_bound_exchange_order_cannot_be_replaced_on_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.sqlite')
            store.put_intent(intent_id='i', pair='ADA/USDT', signal_id='signal',
                             halal_allowed=True, registry_sha256='registry', nominal_usdt='250')
            binding = dict(scope_id='testnet|account|epoch', intent_id='i', generation=1,
                           pair='ADA/USDT', role='ENTRY', client_id='entry-client')
            store.bind_order_identity(**binding, order_id='101')
            store.bind_order_identity(**binding, order_id='101')
            with self.assertRaises((ValueError, sqlite3.IntegrityError)):
                store.bind_order_identity(**binding, order_id='202')
            self.assertTrue(store.has_blockers())
            with closing(sqlite3.connect(store.path)) as database:
                self.assertEqual(database.execute('SELECT order_id FROM order_identity').fetchone()[0], '101')


if __name__ == '__main__':
    unittest.main()
