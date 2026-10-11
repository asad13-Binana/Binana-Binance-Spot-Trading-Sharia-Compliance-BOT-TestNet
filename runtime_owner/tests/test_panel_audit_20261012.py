import ast
import json
import math
from pathlib import Path
from types import SimpleNamespace
import time
import unittest

PANEL=Path(__file__).resolve().parents[1]/'runtime-fixes/telegram/runtime_stable_panel.py'


def panel(**overrides):
    names={'_pretty_open_trades','_pretty_protection','_pretty_health','_project_settlement',
           '_current_protection','_pretty_profit'}
    tree=ast.parse(PANEL.read_text(encoding='utf-8'))
    ns={'json':json,'math':math,'time':time,
        '_ft_data':lambda *a:[],
        '_binana_db_rows':lambda:{'available':True,'protection':[],'intents':[],'incidents':[]},
        '_owner_release_evidence':lambda:{'release_approved':True},
        'base':SimpleNamespace(ft_call=lambda *a:{'ok':False},
            read_json=lambda *a:{'ok':False},RUNTIME=Path('.'))}
    exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef)
        and n.name in names],type_ignores=[]),str(PANEL),'exec'),ns)
    ns.update(overrides)
    return ns


class PanelAudit(unittest.TestCase):
    def test_unavailable_positions_are_unknown_in_both_views(self):
        for status in (None,{},'failure'):
            ns=panel(_ft_data=lambda *a:status)
            for name in ('_pretty_open_trades','_pretty_protection'):
                with self.subTest(status=status,view=name):
                    result=ns[name]()
                    self.assertIn('UNKNOWN',result)
                    self.assertNotIn('No Freqtrade-owned open positions',result)

    def test_current_trade_never_borrows_previous_trade_protection(self):
        ext={'available':True,'incidents':[],
             'intents':[{'intent_id':'old','freqtrade_trade_id':98},
                        {'intent_id':'new','freqtrade_trade_id':99}],
             'protection':[{'intent_id':'old','pair':'ADA/USDT','generation':10,'status':'FIXED_ACTIVE','mode':'FIXED_OCO'},
                           {'intent_id':'new','pair':'ADA/USDT','generation':1,'status':'VERIFICATION_UNKNOWN','mode':'FIXED_OCO'}]}
        ns=panel(_ft_data=lambda *a:[{'pair':'ADA/USDT','trade_id':99}],_binana_db_rows=lambda:ext)
        for view in ('_pretty_open_trades','_pretty_protection'):
            result=ns[view]()
            self.assertIn('VERIFICATION_UNKNOWN',result)
            self.assertNotIn('FIXED_ACTIVE',result)

    def test_health_never_ready_with_unhealthy_displayed_dependencies(self):
        ns=panel(_ft_data=lambda *a:{'state':'running'})
        self.assertNotIn('readiness: READY',ns['_pretty_health']())

    def test_profit_unavailable_never_becomes_zero(self):
        def unavailable(*args): raise OSError('locked')
        result=panel(_owner_settled_trades=unavailable)['_pretty_profit']()
        self.assertIn('UNAVAILABLE',result)
        self.assertNotIn('Refreshed UTC',result)

    def test_closed_zero_remains_zero(self):
        result=panel()['_project_settlement']({'trade_id':1,'pair':'ADA/USDT','is_open':False,
            'close_date':'2026-09-14T00:00:00Z','close_profit_abs':0,'realized_profit':99})
        self.assertEqual(result['profit_abs'],0)

    def test_retained_missing_settlement_timestamp_is_not_metadata_date(self):
        data={'phase':'retained','quantity':'1','cost_basis':'0.01','scope':'a',
              'classification':'NON_EXECUTABLE_DUST_MIN_NOTIONAL','non_executable':True,
              'exchange_open_orders':0,'verified_at':time.time()}
        row={'trade_id':1,'pair':'ADA/USDT','is_open':False,'realized_profit':0,
             'close_date':'2026-10-12','dust_created_at':'2026-10-12','dust_json':json.dumps(data)}
        with self.assertRaises(ValueError): panel()['_project_settlement'](row)
