"""Retained inventory releases a slot only with scoped, replayable evidence."""
import copy
import unittest

from freqtrade.persistence import Trade
from binana_tests import test_exact_commissions as helpers


class RetainedSlotEvidence(unittest.TestCase):
    setUp = helpers.ExactCommissions.setUp
    tearDown = helpers.ExactCommissions.tearDown
    enter = helpers.ExactCommissions.enter
    update = helpers.ExactCommissions.update
    order = helpers.ExactCommissions.order

    def seed(self):
        self.enter(self.order(1, 'buy', 10, 10, .001, 'ADA'))
        self.update(self.order(2, 'sell', 9.99, 11))
        exact = self.trade.binana_economics()
        self.assertTrue(self.trade.is_open)
        return {'phase': 'retained', 'scope': self.manager.scope_id,
            'quantity': str(exact['quantity']), 'cost_basis': str(exact['basis']),
            'classification': 'NON_EXECUTABLE_DUST_MIN_QTY', 'non_executable': True,
            'exchange_open_orders': 0, 'price': '11',
            'filters': [{'filterType': 'LOT_SIZE', 'minQty': '.01', 'stepSize': '.01', 'maxQty': '100000'},
                        {'filterType': 'MIN_NOTIONAL', 'minNotional': '5'}]}

    def store(self, evidence):
        self.trade.set_custom_data(key='binana_retained_dust', value=evidence)
        Trade.commit()
        identifier = self.trade.id
        Trade.session.remove()
        self.trade = Trade.session.get(Trade, identifier)

    def test_valid_dust_releases_slot_but_keeps_inventory_and_invested_capital(self):
        self.store(self.seed())
        self.assertEqual(Trade.get_open_trade_count(), 0)
        self.assertEqual(len(Trade.get_trades([Trade.is_open.is_(True)]).all()), 1)
        self.assertAlmostEqual(Trade.total_open_trades_stakes(), self.trade.stake_amount)

    def test_incomplete_or_changed_evidence_keeps_slot_and_reconciliation_visible(self):
        evidence = self.seed()
        corruptions = [
            {'phase': 'prepared'}, {'scope': 'foreign'}, {'quantity': '10'},
            {'cost_basis': '900'}, {'classification': 'EXECUTABLE'}, {'filters': []},
            {'non_executable': False}, {'exchange_open_orders': 1}, {'quantity': 'NaN'},
        ]
        for change in corruptions:
            with self.subTest(change=change):
                self.store({**copy.deepcopy(evidence), **change})
                self.assertEqual(Trade.get_open_trade_count(), 1)
                self.assertEqual([t.id for t in Trade.get_open_trades()], [self.trade.id])
        self.store({'phase': 'retained'})
        self.assertEqual(Trade.get_open_trade_count(), 1)

    def test_open_canonical_order_prevents_slot_release(self):
        evidence = self.seed()
        self.trade.orders[-1].ft_is_open = True
        self.trade.orders[-1].status = 'open'
        self.store(evidence)
        self.assertEqual(Trade.get_open_trade_count(), 1)
