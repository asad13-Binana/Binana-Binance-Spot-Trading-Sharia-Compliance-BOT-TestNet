import importlib.util
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('outbox_test',Path(__file__).resolve().parents[1]/'runtime-fixes/telegram/durable_outbox.py')
outbox=importlib.util.module_from_spec(spec);spec.loader.exec_module(outbox)


class DurableNotifications(unittest.TestCase):
    def test_transport_failure_survives_restart_and_ack_deduplicates(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'notifications.sqlite';box=outbox.Outbox(path)
            self.assertTrue(box.enqueue('event','a trade','owner'))
            self.assertFalse(box.deliver_one(lambda *a:None,now=10))
            box=outbox.Outbox(path)
            self.assertFalse(box.enqueue('event','a trade','owner'))
            calls=[]
            def send(*a): calls.append(a);return {'ok':True,'result':{'message_id':1}}
            self.assertFalse(box.deliver_one(send,now=11))
            self.assertTrue(box.deliver_one(send,now=12))
            self.assertFalse(box.deliver_one(send,now=20))
            self.assertEqual(calls,[('a trade','owner')])

    def test_chat_budget_is_shared_across_instances_and_restarts(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'notifications.sqlite'
            self.assertEqual(outbox.Outbox(path).reserve_chat('a',now=10),0)
            self.assertAlmostEqual(outbox.Outbox(path).reserve_chat('a',now=10),1.1)
            self.assertEqual(outbox.Outbox(path).reserve_chat('b',now=10),0)
