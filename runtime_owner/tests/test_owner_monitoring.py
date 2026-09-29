import ast
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from threading import Lock
from unittest.mock import Mock
from freqtrade.binana.execution_manager import BinanaExecutionManager


def method(name):
    root=Path(__file__).resolve().parents[1]
    path=root/'owner/freqtrade/freqtradebot.py'
    if not path.exists():path=Path('/freqtrade/freqtrade/freqtradebot.py')
    tree=ast.parse(path.read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='FreqtradeBot')
    return next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name==name)


class OwnerMonitoring(unittest.TestCase):
    def test_import_incident_closes_only_after_verified_recovery(self):
        for state in ('UNKNOWN', 'EXIT_PENDING', 'OPEN', 'EXIT_FILLED', 'DUST_RETAINED'):
            manager = BinanaExecutionManager.__new__(BinanaExecutionManager)
            manager.store = Mock()
            manager.store.get_intent.return_value = {'state': state}
            manager.store.latest_generation.return_value = None
            manager.intent_for_trade = lambda trade_id: 'intent-7'
            manager.acknowledge_canonical_reconciliation(SimpleNamespace(id=7))
            with self.subTest(state=state):
                self.assertEqual(manager.store.close_incident.called,
                                 state in {'OPEN', 'EXIT_FILLED', 'DUST_RETAINED'})

    def test_stopped_mode_keeps_owner_monitoring_and_evidence_live(self):
        events=[]
        bot=SimpleNamespace(config={},_exit_lock=Lock(),binana=SimpleNamespace(
            start_user_stream=lambda:events.append('stream'),
            publish_release_status=lambda:events.append('evidence')),
            _binana_reconcile_external_exits=lambda trades:events.append('reconcile'))
        ns={'Trade':SimpleNamespace(get_open_trades=lambda:[])}
        exec(compile(ast.Module(body=[method('process_stopped')],type_ignores=[]),'test','exec'),ns)
        ns['process_stopped'](bot)
        self.assertEqual(events,['stream','reconcile','evidence'])

    def test_shutdown_failure_does_not_skip_resource_cleanup(self):
        events=[]
        def failed_stop():raise RuntimeError('PRIVATE_STREAM_SHUTDOWN_INCOMPLETE')
        bot=SimpleNamespace(config={},binana=SimpleNamespace(stop_user_stream=failed_stop),
            check_for_open_trades=lambda:None,
            strategy=SimpleNamespace(ft_bot_cleanup=lambda:events.append('strategy')),
            rpc=SimpleNamespace(cleanup=lambda:events.append('rpc')),
            exchange=SimpleNamespace(close=lambda:events.append('exchange')))
        ns={'logger':logging.getLogger('test'),'Trade':SimpleNamespace(session=True,commit=lambda:events.append('commit'))}
        exec(compile(ast.Module(body=[method('cleanup')],type_ignores=[]),'test','exec'),ns)
        with self.assertRaisesRegex(RuntimeError,'SHUTDOWN'):
            ns['cleanup'](bot)
        self.assertEqual(events,['strategy','rpc','exchange','commit'])

    def test_unmapped_canonical_trade_creates_durable_blocker(self):
        owner=SimpleNamespace(store=Mock(),recover_closed_acknowledgments=lambda:True,
            reconcile_trade=Mock(side_effect=RuntimeError('missing intent')))
        bot=SimpleNamespace(binana=owner,_binana_recover_unbound_entries=lambda:[])
        ns={'logger':logging.getLogger('test'),'Trade':SimpleNamespace(session=Mock()),'Order':object}
        exec(compile(ast.Module(body=[method('_binana_reconcile_external_exits')],type_ignores=[]),'test','exec'),ns)
        ns['_binana_reconcile_external_exits'](bot,[SimpleNamespace(id=7,pair='ADA/USDT')])
        self.assertFalse(owner.canonical_reconciliation_ready)
        self.assertEqual(owner.store.incident.call_args.kwargs['code'],'CANONICAL_RECONCILIATION_FAILED')
