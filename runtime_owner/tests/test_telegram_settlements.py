import ast
import json
from pathlib import Path
import unittest


def projection():
    path=Path(__file__).resolve().parents[1]/'runtime-fixes/telegram/runtime_stable_panel.py'
    tree=ast.parse(path.read_text(encoding='utf-8'))
    function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='_project_settlement')
    namespace={'json':json}
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),'exec'),namespace)
    return namespace['_project_settlement']


class TelegramSettlements(unittest.TestCase):
    def row(self):
        return {'trade_id':20,'pair':'MINA/USDT','is_open':False,
                'realized_profit':11.4463,'close_profit_abs':9.9792,'close_profit':0.045,
                'max_stake_amount':249.99086,'close_date':'2026-09-13 01:02:03',
                'dust_created_at':'2026-09-20 01:00:00','dust_json':json.dumps({
                    'phase':'retained','non_executable':True,'classification':'NON_EXECUTABLE_DUST_MIN_NOTIONAL',
                    'quantity':'3.6','cost_basis':'0.37836','exchange_open_orders':0,'scope':'testnet|account|epoch',
                    'settled_at':'2026-09-13 01:02:03'})}

    def test_closed_retained_trade_uses_cumulative_profit_and_actual_settlement_day(self):
        result=projection()(self.row())
        self.assertEqual(result['kind'],'DUST_RETAINED')
        self.assertEqual(result['profit_abs'],11.4463)
        self.assertEqual(result['settled_at'],'2026-09-13T01:02:03+00:00')
        self.assertEqual(result['retained_cost_basis'],'0.37836')

    def test_zero_and_negative_realized_profit_do_not_fall_back_to_last_exit(self):
        for value in [0,-2]:
            row=self.row();row['realized_profit']=value
            self.assertEqual(projection()(row)['profit_abs'],value)

    def test_fully_sold_marker_is_a_closed_settlement(self):
        row=self.row();row['dust_json']=json.dumps({'phase':'fully_sold'})
        self.assertEqual(projection()(row)['kind'],'CLOSED')

    def test_unverified_executable_nonfinite_or_open_obligations_are_not_settled(self):
        for update in [{'non_executable':False},{'exchange_open_orders':1},{'quantity':'NaN'},
                       {'cost_basis':'-1'},{'scope':''},{'classification':'EXECUTABLE'}]:
            row=self.row();data=json.loads(row['dust_json']);data.update(update);row['dust_json']=json.dumps(data)
            with self.subTest(update=update),self.assertRaises(ValueError):
                projection()(row)

    def test_nonretained_open_trade_is_not_a_settlement(self):
        row=self.row();row.update(is_open=True,dust_json=None)
        self.assertIsNone(projection()(row))
