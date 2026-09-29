from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from decimal import Decimal
from freqtrade.binana.state_store import StateStore
from freqtrade.binana.execution_manager import BinanaExecutionManager, AdmissionRejected


class FillIdentity(unittest.TestCase):
    def test_duplicate_fill_is_idempotent_but_conflicting_economics_block(self):
        with tempfile.TemporaryDirectory() as directory:
            store=StateStore(Path(directory)/'state.sqlite')
            store.put_intent(intent_id='i',pair='ADA/USDT',signal_id='s',halal_allowed=True,
                             registry_sha256='sha',nominal_usdt='250')
            fill=dict(fill_key='key',scope_id='epoch',intent_id='i',pair='ADA/USDT',
                      order_id='100',side='BUY',base_qty='1',quote_qty='2',average_price='2',
                      exchange_trade_id='10',fee_asset='ADA',fee_amount='0.001')
            store.record_fill(**fill)
            store.record_fill(**{**fill,'base_qty':'1.000'})
            self.assertEqual(len(store.fill_rows('i')),1)
            for change in [{'base_qty':'2'},{'fee_amount':'0'},{'fee_asset':'USDT'},
                           {'fill_key':'another','order_id':'101'}]:
                with self.subTest(change=change), self.assertRaises(sqlite3.IntegrityError):
                    store.record_fill(**{**fill,**change})
            self.assertEqual(store.fill_rows('i')[0]['base_qty'],'1')
            self.assertTrue(store.has_blockers())

    def test_authenticated_trade_zero_is_valid_and_duplicate_conflict_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            manager=BinanaExecutionManager.__new__(BinanaExecutionManager)
            manager.store=StateStore(Path(directory)/'state.sqlite')
            manager.scope_id='epoch';manager.legacy_scope_ids=set()
            manager.store.put_intent(intent_id='i',pair='ADA/USDT',signal_id='s',halal_allowed=True,
                registry_sha256='sha',nominal_usdt='250')
            fill={'id':0,'order':'100','symbol':'ADA/USDT','side':'buy','amount':1,'cost':2,
                'fee':{'currency':'ADA','cost':.001},'timestamp':1790160000000}
            fetch=Mock(return_value=[fill,dict(fill,amount='1.00')])
            manager.exchange=SimpleNamespace(_api=SimpleNamespace(fetch_my_trades=fetch))
            args=dict(intent_id='i',pair='ADA/USDT',order_id='100',side='BUY')
            self.assertEqual(manager._record_order_trades(**args),Decimal(1))
            self.assertEqual(len(manager._verified_order_fills),1)
            self.assertEqual(manager.store.fill_rows('i')[0]['exchange_trade_id'],'0')
            fetch.return_value=[fill,dict(fill,fee={'currency':'ADA','cost':.002})]
            with self.assertRaisesRegex(AdmissionRejected,'CONFLICTING_EXECUTION_REPLAY'):
                manager._record_order_trades(**args)
            fetch.return_value=[dict(fill,side='sell')]
            with self.assertRaisesRegex(AdmissionRejected,'PAIR_OR_SIDE_MISMATCH'):
                manager._record_order_trades(**args)
