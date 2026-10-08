"""Ten-minute one/four-position Testnet execution soak on private database copies.

This exercises protection/recovery and allocation accounting using explicit
manual test admissions. It does not claim strategy profitability or NFI signals.
"""
import glob
import json
import logging
from pathlib import Path
import sys
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP

import probe_base as base

ROOT = Path('/probe')
PAIRS = ('LTC/USDT','LINK/USDT','ADA/USDT','NEAR/USDT')
SOAK_SECONDS = 600


class SlotGuard(base.TransportGuard):
    def __init__(self, root, state, intents, phase):
        super().__init__(root, state, 'read-only-default', phase, mutation_budget=20)
        self.intents = intents

    def validate(self, url, method, headers=None, body=None):
        if method == 'GET':
            return super().validate(url,method,headers,body)
        params = base.request_values(url,body)
        pair = next((pair for pair in self.intents if pair.replace('/','') == params.get('symbol')), None)
        if pair is None:
            raise RuntimeError('SOAK_FOREIGN_PAIR_DENIED')
        guard = base.TransportGuard(self.path.parent,self.state,self.intents[pair],self.phase,
                                    pair=pair,mutation_budget=20)
        guard.validate(url,method,headers,body)
        self.journal = guard.journal


def states(bot, intents):
    records, trades = {}, {}
    for pair,intent in intents.items():
        records[pair], matches = base.snapshot(bot,intent,pair=pair)
        if len(matches) == 1:
            trades[pair] = matches[0]
    return records,trades


def normalized(records):
    return {pair:base.canonical_signature({'canonical':record}) for pair,record in records.items()}


def unexpected(bot):
    return [{'id':row['incident_id'],'code':row['code']} for row in bot.binana.store.unresolved()
            if row['incident_id'] != 'repair-economic-settlement-replay-20260920']


def protected(bot, intents, trades):
    if len(trades) != len(intents) or not bot.binana.canonical_reconciliation_ready:
        return False
    for pair,intent in intents.items():
        generation = bot.binana.store.latest_generation(intent)
        if (not trades[pair].is_open or not generation
                or generation['status'] not in {'FIXED_ACTIVE','TRAILING_ACTIVE'}):
            return False
    return True


def settle(bot, intents, *, exit_requested=False):
    from freqtrade.binana.user_stream import health_is_fresh
    for _ in range(25):
        bot.process_stopped()
        records,trades = states(bot,intents)
        if exit_requested:
            for pair,trade in trades.items():
                if trade.is_open:
                    latest = bot.binana.store.latest_generation(intents[pair])
                    if latest['mode'] != 'OPERATOR_EXIT':
                        bot.binana.request_operator_exit(trade,reason='controlled_slot_soak_cleanup')
            ready = len(trades) == len(intents) and all(not trade.is_open for trade in trades.values())
        else:
            ready = protected(bot,intents,trades)
        if ready and bot.binana.canonical_reconciliation_ready and not unexpected(bot) and health_is_fresh(bot.binana.user_stream.health()):
            return records,trades
        time.sleep(3)
    raise RuntimeError('SOAK_SETTLEMENT_NOT_VERIFIED')


def main():
    sys.path[:0] = ['/freqtrade', *glob.glob('/home/ftuser/.local/lib/python*/site-packages')]
    from freqtrade.configuration import Configuration
    from freqtrade.enums import RunMode, State
    from freqtrade.freqtradebot import FreqtradeBot
    from freqtrade.persistence import Trade
    from freqtrade.binana.execution_manager import PreparedAdmission
    from freqtrade.binana.environment import assert_spot_pair
    from freqtrade.binana.user_stream import health_is_fresh
    phase = sys.argv[1]
    if phase not in {'entry','restart','exit','closed_restart'}:
        raise RuntimeError('UNKNOWN_SOAK_PHASE')
    options = json.loads((ROOT/'soak.json').read_text())
    if options != {'slots':options.get('slots'),'seconds':SOAK_SECONDS} or options['slots'] not in {1,4}:
        raise RuntimeError('UNEXPECTED_SOAK_POLICY')
    pairs = PAIRS[:options['slots']]
    intents = {pair:'soak-'+(ROOT/'run_id').read_text().strip()+'-'+pair.split('/')[0] for pair in pairs}
    assets = tuple(pair.split('/')[0] for pair in pairs)+('USDT',)
    report_path = ROOT/(phase+'.json')
    with report_path.open('x') as stream:
        json.dump({'started_at':time.time(),'passed':False},stream)
    report = {'started_at':time.time(),'phase':phase,'slots':len(pairs),'soak_seconds_required':SOAK_SECONDS,
              'intents':intents,'passed':False,'strategy_admission_verified':False,
              'release_certificate_created':False,'samples':[]}
    guard = SlotGuard(ROOT,base.STATE,intents,phase)
    guard.install()
    bot = None
    try:
        config = Configuration({'config':['/probe/config.json'],'strategy':'BinanaNfiSpot',
                                'strategy_path':'/freqtrade/binana-strategies'},RunMode.LIVE).get_config()
        if (config.get('dry_run') is not False or config.get('initial_state') != 'stopped'
                or config['db_url'] != 'sqlite:////freqtrade/shared/freqtrade/binana-owner.sqlite'
                or config['binana']['state_db_path'] != str(base.STATE)
                or config.get('cancel_open_orders_on_exit') is not True
                or Decimal(str(config['stake_amount'])) != 250 or config['max_open_trades'] != 4
                or Decimal(str(config['binana']['allocation_usdt'])) != 1000
                or any(config.get(name,{}).get('enabled') for name in ['telegram','api_server','webhook'])):
            raise RuntimeError('SOAK_CONFIGURATION_MISMATCH')
        bot = FreqtradeBot(config)
        if bot.state != State.STOPPED:
            raise RuntimeError('SOAK_OWNER_MUST_REMAIN_STOPPED')
        bot.startup()
        report['account_before'] = base.account_totals(bot,assets)
        if phase == 'entry':
            if (Trade.get_open_trades() or bot.exchange._api.privateGetOpenOrders()
                    or bot.exchange._api.privateGetOpenOrderList() or bot.binana.store.occupied_count()):
                raise RuntimeError('SOAK_REQUIRES_EMPTY_OPEN_OBLIGATIONS')
            # Run the candidate's full order-filter/OTOCO preflight for every
            # bounded plan before the first exchange mutation.
            from freqtrade.binana.protection_plan import provisional_plan
            planned = {}
            report['all_pair_preflight'] = {}
            for pair,intent in intents.items():
                assert_spot_pair(pair,side='long',leverage=1)
                decision = bot.binana.registry.decide(intent_id=intent,pair=pair)
                if not decision.allowed or pair not in bot.active_pair_whitelist:
                    raise RuntimeError('SOAK_PAIR_NOT_APPROVED_AND_LISTED')
                info = bot.binana.order_lists.symbol_info(pair)
                filters = {row['filterType']:row for row in info['filters']}
                tick,step = Decimal(filters['PRICE_FILTER']['tickSize']),Decimal(filters['LOT_SIZE']['stepSize'])
                book = bot.binana.order_lists.public.publicGetTickerBookTicker({'symbol':pair.replace('/','')})
                price = (Decimal(book['askPrice'])*Decimal('1.002')/tick).to_integral_value(rounding=ROUND_UP)*tick
                amount = (Decimal('250')/price/step).to_integral_value(rounding=ROUND_DOWN)*step
                fee = bot.binana.entry_commission_reserve(pair)
                protected_quantity = (amount*(1-fee)/step).to_integral_value(rounding=ROUND_DOWN)*step
                policy = config['binana']
                plan = provisional_plan(intent_id=intent,pair=pair,quantity=amount,entry_limit=price,tick_size=tick,
                    target_fraction=policy['fixed_target_fraction'],stop_fraction=policy['normal_stop_fraction'],
                    stop_limit_buffer_fraction=policy['stop_limit_buffer_fraction'],
                    estimated_exit_fee_fraction=policy['estimated_exit_fee_fraction'])
                report['all_pair_preflight'][pair] = bot.binana.order_lists.preflight(plan,pending_quantity=protected_quantity,otoco=True)
                planned[pair] = (amount,price)
            base.durable(report_path,report)
            report['entries'] = {}
            for pair,intent in intents.items():
                if bot.binana.store.get_intent(intent):
                    raise RuntimeError('SOAK_ENTRY_ALREADY_EXISTS')
                decision = bot.binana.registry.decide(intent_id=intent,pair=pair)
                if not decision.allowed or pair not in bot.active_pair_whitelist:
                    raise RuntimeError('SOAK_PAIR_NOT_APPROVED_AND_LISTED')
                market = bot.binana.market_data.refresh(pair,Decimal('250'))
                bot.binana.store.put_intent(intent_id=intent,pair=pair,signal_id='controlled_slot_soak',
                    halal_allowed=True,registry_sha256=decision.registry_sha256,nominal_usdt='250',
                    admission={'kind':'controlled_testnet_slot_soak','strategy_signal':False})
                bot.binana.prepared[pair] = PreparedAdmission(intent,pair,'controlled_slot_soak','NORMAL',
                    market,decision.registry_sha256,Decimal('250'))
                amount,price = planned[pair]
                if bot.binana.store.occupied_count() > len(pairs) or bot.binana.store.occupied_nominal_usdt() > 1000:
                    raise RuntimeError('SOAK_ALLOCATION_BOUND_EXCEEDED')
                report['entries'][pair] = {'quantity':str(amount),'limit':str(price),'nominal':str(amount*price)}
                base.durable(report_path,report)
                report['entries'][pair]['response'] = bot.binana.submit_entry(pair=pair,amount=float(amount),
                    rate=float(price),enter_tag='controlled_slot_soak')
                base.durable(report_path,report)
                # Bind each real working fill before submitting another pair.
                admitted = {p:i for p,i in intents.items() if p in report['entries']}
                settle(bot,admitted)
        records,trades = settle(bot,intents,exit_requested=phase in {'exit','closed_restart'})
        if phase == 'entry':
            started = time.monotonic()
            baseline = normalized(records)
            while True:
                bot.process_stopped()
                records,trades = states(bot,intents)
                sample = {'at':time.time(),'elapsed_seconds':time.monotonic()-started,
                    'positions':len(trades),'occupied_slots':bot.binana.store.occupied_count(),
                    'reserved_nominal':str(bot.binana.store.occupied_nominal_usdt()),
                    'protected':protected(bot,intents,trades),'canonical_ready':bot.binana.canonical_reconciliation_ready,
                    'rest_ok':bot.binana._rest_health.get('ok'),
                    'private_stream_fresh':health_is_fresh(bot.binana.user_stream.health()),
                    'unexpected_incidents':unexpected(bot)}
                report['samples'].append(sample)
                base.durable(report_path,report)
                if (not all(sample[key] for key in ['protected','canonical_ready','rest_ok','private_stream_fresh'])
                        or sample['unexpected_incidents'] or sample['occupied_slots'] != len(pairs)
                        or Decimal(sample['reserved_nominal']) != Decimal(250*len(pairs))
                        or normalized(records) != baseline):
                    raise RuntimeError('SOAK_INVARIANT_FAILED')
                if sample['elapsed_seconds'] >= SOAK_SECONDS:
                    break
                time.sleep(min(10,SOAK_SECONDS-sample['elapsed_seconds']))
            report['soak_seconds_observed'] = sample['elapsed_seconds']
        if phase in {'restart','closed_restart'}:
            prior = json.loads((ROOT/('exit.json' if phase=='closed_restart' else 'entry.json')).read_text())
            if not prior.get('passed') or normalized(records) != normalized(prior['canonical']):
                raise RuntimeError('SOAK_RESTART_ACCOUNTING_CHANGED')
            report['restart_accounting_unchanged'] = True
        report['canonical'] = records
        report['unexpected_incidents'] = unexpected(bot)
        report['open_exchange_orders'] = bot.exchange._api.privateGetOpenOrders()
        report['open_exchange_lists'] = bot.exchange._api.privateGetOpenOrderList()
        report['account_after'] = base.account_totals(bot,assets)
        if phase in {'exit','closed_restart'} and (report['open_exchange_orders'] or report['open_exchange_lists']):
            raise RuntimeError('SOAK_OBLIGATIONS_REMAIN')
        if phase == 'closed_restart':
            prior = json.loads((ROOT/'entry.json').read_text())
            report['account_delta'] = {asset:str(Decimal(value)-Decimal(prior['account_before'][asset]))
                                      for asset,value in report['account_after'].items()}
            realized = sum((Decimal(record['trades'][0]['economics']['realized']) for record in records.values()),Decimal('0'))
            if (any(Decimal(report['account_delta'][asset]) != 0 for asset in assets if asset != 'USDT')
                    or Decimal(report['account_delta']['USDT']) != realized):
                raise RuntimeError('SOAK_ACCOUNT_DELTA_RECEIPTS_MISMATCH')
            report['account_delta_matches_receipts'] = True
        if guard.denials or any(row['open_canonical_orders'] for record in records.values() for row in record['trades']):
            raise RuntimeError('SOAK_TRANSPORT_OR_CANONICAL_BOUNDARY_FAILED')
        report['passed'] = True
    except Exception as exc:
        report['error'] = base.safe_error(exc)
        if bot is not None:
            try:
                report['canonical'],_ = states(bot,intents)
            except Exception as inspect_exc:
                report['inspection_error'] = base.safe_error(inspect_exc)
    finally:
        if bot is not None:
            try:
                before_cleanup,_ = states(bot,intents)
                bot.cleanup()
                after_cleanup,_ = states(bot,intents)
                if normalized(before_cleanup) != normalized(after_cleanup) or guard.denials:
                    raise RuntimeError('SOAK_CLEANUP_CHANGED_CANONICAL_STATE')
                report['cleanup_unchanged'] = True
            except Exception as exc:
                report['cleanup_error'] = base.safe_error(exc)
                report['passed'] = False
        report.update(completed_at=time.time(),mutations=guard.journal,transport_denials=guard.denials)
        base.durable(report_path,report)
        print(json.dumps({key:report.get(key) for key in ['phase','slots','passed','error','soak_seconds_observed']}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    raise SystemExit(main())
