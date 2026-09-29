import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from freqtrade.binana.user_stream import PrivateUserStream


class PrivateEventOrdering(unittest.TestCase):
    def test_duplicate_and_out_of_order_events_are_recorded_once_without_economic_writes(self):
        with TemporaryDirectory() as directory:
            stream=PrivateUserStream('unused','unused',scope='testnet|account|epoch',runtime=directory)
            event={'e':'executionReport','s':'ADAUSDT','i':10,'I':102,'E':2000,'z':'250','x':'TRADE'}
            first=stream._record_event({'subscriptionId':1,'event':event},1)
            duplicate=stream._record_event({'subscriptionId':1,'event':{**event,'E':2001}},1)
            older=stream._record_event({'subscriptionId':1,'event':{**event,'I':101,'E':1000,'z':'0','x':'NEW'}},1)
            self.assertTrue(first['_new_event']);self.assertFalse(duplicate['_new_event']);self.assertTrue(older['_new_event'])
            with sqlite3.connect(str(Path(directory)/'freqtrade_lifecycle_events.sqlite')) as connection:
                rows=[json.loads(row[0]) for row in connection.execute('SELECT payload FROM lifecycle_events ORDER BY seq')]
                tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual([row['execution_id'] for row in rows],[102,101])
            self.assertNotIn('trades',tables)
            self.assertNotIn('orders',tables)

    def test_wrong_subscription_or_missing_execution_identity_is_rejected(self):
        with TemporaryDirectory() as directory:
            stream=PrivateUserStream('unused','unused',scope='scope',runtime=directory)
            for message in [{'subscriptionId':2,'event':{}},
                            {'subscriptionId':1,'event':{'e':'executionReport','i':10}}]:
                with self.assertRaises(RuntimeError):stream._record_event(message,1)
