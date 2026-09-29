from registry_fixture import registry_document
import json, unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from freqtrade.binana.execution_manager import BinanaExecutionManager, AdmissionRejected
from freqtrade.binana.market_data import MarketDataService
from binana_tests.test_partial_promotion_recovery import Exchange, cfg

class AdmissionMarket:
    def __init__(self): self.budgets=[]
    def refresh(self,pair,quote_budget=None):
        self.budgets.append(quote_budget)
        return SimpleNamespace(reason='EXECUTION_HEADROOM',state='GOOD',flow_status='fresh',
            taker_buy_ratio_60s='0.70',cvd_quote_60s='1000',sequence_book_status='fresh',
            book_imbalance_20='0.60',to_dict=lambda:{'state':'GOOD'})

class BookExchange:
    def fetch_l2_order_book(self,pair,limit):
        return {'bids':[[1.0,20.0] for _ in range(20)],
                'asks':[[1.001,20.0] for _ in range(20)]}
    def get_tickers(self,symbols,cached=False):
        return {symbols[0]:{'quoteVolume':10000000}}

class R18Regressions(unittest.TestCase):
    def manager(self,td,stake=250,allocation=1000,max_open=4):
        registry=Path(td)/'halal.json'; registry.write_text(json.dumps(registry_document(['ADAUSDT'])))
        conf=cfg(td,registry); conf['stake_amount']=stake; conf['max_open_trades']=max_open
        conf['binana']['allocation_usdt']=allocation; conf['binana']['confluence_policy']={'enabled':False}
        ex=Exchange(); ex.get_min_pair_stake_amount=lambda *a,**k:5.0
        m=BinanaExecutionManager(conf,ex); m.market_data=AdmissionMarket()
        return m

    def test_allocation_is_enforced_for_manual_trade_size_override(self):
        with TemporaryDirectory() as td:
            m=self.manager(td,allocation=1000)
            m._nominal_usdt=lambda:'600'
            m._prepare_candidate(pair='ADA/USDT',enter_tag='a',candle_date='1')
            with self.assertRaisesRegex(AdmissionRejected,'ALLOCATION_LIMIT_EXCEEDED'):
                m._prepare_candidate(pair='ADA/USDT',enter_tag='b',candle_date='2')

    def test_terminal_signal_cannot_be_reopened(self):
        with TemporaryDirectory() as td:
            m=self.manager(td); iid=m._intent_id('ADA/USDT','a','1')
            m.store.put_intent(intent_id=iid,pair='ADA/USDT',signal_id='a',halal_allowed=True,
                registry_sha256='h',nominal_usdt='250',state='CLOSED_NO_FILL')
            with self.assertRaisesRegex(AdmissionRejected,'SIGNAL_ALREADY_TERMINAL'):
                m._prepare_candidate(pair='ADA/USDT',enter_tag='a',candle_date='1')

    def test_pre_submit_abort_releases_reservation(self):
        with TemporaryDirectory() as td:
            m=self.manager(td); p=m._prepare_candidate(pair='ADA/USDT',enter_tag='a',candle_date='1')
            self.assertEqual(m.store.occupied_count(),1); self.assertTrue(m.abort_prepared(pair='ADA/USDT',reason='test'))
            self.assertEqual(m.store.get_intent(p.intent_id)['state'],'CLOSED_NO_FILL'); self.assertEqual(m.store.occupied_count(),0)

    def test_bind_trade_clears_ephemeral_prepared_state(self):
        with TemporaryDirectory() as td:
            m=self.manager(td); m._prepare_candidate(pair='ADA/USDT',enter_tag='a',candle_date='1')
            m.bind_trade(pair='ADA/USDT',trade_id=7)
            self.assertNotIn('ADA/USDT',m.prepared)

    def test_order_notional_cannot_exceed_admitted_nominal(self):
        with TemporaryDirectory() as td:
            m=self.manager(td); p=m._prepare_candidate(pair='ADA/USDT',enter_tag='a',candle_date='1')
            with self.assertRaisesRegex(AdmissionRejected,'ORDER_NOTIONAL_EXCEEDS_ADMITTED'):
                m.submit_entry(pair='ADA/USDT',amount=300,rate=1,enter_tag='a')
            self.assertEqual(m.store.get_intent(p.intent_id)['state'],'CLOSED_NO_FILL')
            self.assertFalse(m.store.has_generation(p.intent_id))

    def test_slippage_budget_tracks_requested_notional(self):
        with TemporaryDirectory() as td:
            now=datetime.now(timezone.utc); ctx=Path(td)/'ctx.json'
            ctx.write_text(json.dumps({'generated_at':now.isoformat().replace('+00:00','Z'),'symbols':{'ADAUSDT':{
                'spot_aggressive_flow':{'status':'fresh','continuous_coverage_seconds':120,
                    'last_agg_trade_received_at':now.isoformat().replace('+00:00','Z'),
                    'last_agg_trade_event_time_ms':now.timestamp()*1000,'taker_buy_ratio_60s':'0.7','cvd_quote_60s':'1000'},
                'l2_order_book':{'status':'unavailable','sequence_verified':False}}}}))
            policy={'max_context_age_seconds':30,'hard_max_spread_bps':100,'hard_min_depth_quote':1,
                'hard_max_slippage_bps':1000,'min_quote_volume_24h':1,'good_max_spread_bps':100,
                'good_min_depth_quote':1,'good_max_slippage_bps':1000}
            svc=MarketDataService(BookExchange(),ctx,policy)
            self.assertNotEqual(svc.refresh('ADA/USDT',quote_budget=250).state,'DANGEROUS')
            big=svc.refresh('ADA/USDT',quote_budget=600)
            self.assertEqual((big.state,big.reason,big.slippage_quote_budget),('DANGEROUS','INSUFFICIENT_OBSERVED_DEPTH','600'))

if __name__=='__main__': unittest.main()
