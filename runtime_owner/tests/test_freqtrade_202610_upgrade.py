"""Behavioral checks at the BINANA overlay / Freqtrade 2026.10 boundary."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import tempfile
from threading import Event, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from freqtrade.enums import RPCMessageType, RunMode
from freqtrade.exceptions import ConfigurationError
from freqtrade.exchange.binance import BinanaBinance
from freqtrade.persistence import Trade
from freqtrade.persistence.custom_data import _CustomData
from freqtrade.persistence.models import _request_id_ctx_var
from freqtrade.resolvers import ExchangeResolver, StrategyResolver
from freqtrade.rpc.api_server import deps
from freqtrade.rpc.api_server.webserver import ApiServer
from freqtrade.rpc.rpc import RPC, RPCHandler
from freqtrade.rpc.rpc_manager import RPCManager
from freqtrade.strategy import IStrategy, IntParameter
from freqtrade.strategy.parameters import BaseParameter

from binana_tests.test_package_environment import config as package_config


class HistoryParameterProbe(IStrategy):
    """Expose a parameter through analysed data, without an exchange dependency."""

    timeframe = '5m'
    startup_candle_count = 0
    stoploss = -0.1
    buy_cutoff = IntParameter(1, 10, default=3, space='buy')

    def populate_indicators(self, dataframe, metadata):
        dataframe['cutoff'] = self.buy_cutoff.value
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe['enter_long'] = 0
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe['exit_long'] = 0
        return dataframe


class Upstream202610OverlayAdmission(unittest.TestCase):
    def test_resolver_constructs_fenced_owner_without_public_websocket_workers(self):
        configuration = package_config()
        configuration['runmode'] = RunMode.LIVE
        # Exercise the real resolver, CCXT constructors and upstream capability
        # consumers. validate=False prevents market/account network discovery.
        try:
            exchange = ExchangeResolver.load_exchange(configuration, validate=False)
        except (TypeError, AttributeError) as exc:
            self.fail(f'BINANA transport cannot consume the upstream capability contract: {exc}')
        try:
            self.assertIsInstance(exchange, BinanaBinance)
            self.assertFalse(exchange._has_watch_ohlcv)
            self.assertFalse(exchange._has_watch_orderbook)
            self.assertIsNone(exchange._exchange_ws)
            self.assertIsNone(exchange._ws_async)
            self.assertEqual(exchange._api.options['fetchMarkets']['types'], ['spot'])
        finally:
            exchange.close()

    def test_history_api_applies_instance_overrides_to_returned_analysis(self):
        configuration = {
            'strategy': 'HistoryParameterProbe', 'timeframe': '5m',
            'runmode': RunMode.WEBSERVER, 'stake_currency': 'USDT',
            'datadir': Path('/tmp'), 'dataformat_ohlcv': 'feather',
            'exchange': {'pair_whitelist': ['ADA/USDT']},
        }
        candles = pd.DataFrame({
            'date': pd.to_datetime(['2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z']),
            'open': [1.0, 1.1], 'high': [1.2, 1.3], 'low': [0.9, 1.0],
            'close': [1.1, 1.2], 'volume': [100.0, 120.0],
        })
        instances = []

        def resolve(config):
            strategy = HistoryParameterProbe(copy.deepcopy(config))
            instances.append(strategy)
            return strategy

        def history(**kwargs):
            return {'ADA/USDT': candles.copy(deep=True)}

        with patch.object(StrategyResolver, 'load_strategy', side_effect=resolve), \
                patch('freqtrade.rpc.rpc.load_data', side_effect=history):
            try:
                changed = RPC._rpc_analysed_history_full(
                    configuration, 'ADA/USDT', '5m', None, ['cutoff'], False,
                    strategy_params=[{'name': 'buy_cutoff',
                                      'param_type': HistoryParameterProbe.buy_cutoff.param_type,
                                      'value': 7}],
                )
            except TypeError as exc:
                self.fail(f'History API cannot accept the upstream strategy override: {exc}')
            default = RPC._rpc_analysed_history_full(
                configuration, 'ADA/USDT', '5m', None, ['cutoff'], False,
                strategy_params=None,
            )
        self.assertEqual([row[changed['columns'].index('cutoff')] for row in changed['data']], [7, 7])
        self.assertEqual([row[default['columns'].index('cutoff')] for row in default['data']], [3, 3])
        self.assertEqual(instances[0].buy_cutoff.value, 7)
        self.assertEqual(instances[1].buy_cutoff.value, 3)

    def test_rpc_handler_registers_and_delivers_off_the_owner_thread(self):
        received = []
        delivered = Event()

        class ReceiptSink(RPCHandler):
            def send_msg(self, message):
                received.append((dict(message), get_ident()))
                delivered.set()

            def cleanup(self):
                pass

        manager = RPCManager(SimpleNamespace(config={}))
        try:
            try:
                manager._register(ReceiptSink(manager._rpc, {}))
            except AttributeError as exc:
                self.fail(f'Overlay RPCHandler cannot register with the upstream worker manager: {exc}')
            message = {'type': RPCMessageType.STATUS, 'status': 'offline-owner-receipt'}
            manager.send_msg(message)
            self.assertTrue(delivered.wait(2), 'queued owner receipt was not delivered')
            self.assertEqual(received[0][0], message)
            self.assertNotEqual(received[0][1], get_ident())
        finally:
            manager.cleanup()

    def test_rpc_cleanup_releases_custom_session_when_trade_cleanup_raises(self):
        removed = []

        class TradeSession:
            def rollback(self):
                pass

            def remove(self):
                raise RuntimeError('fixture-trade-cleanup-failure')

        custom_session = SimpleNamespace(remove=lambda: removed.append('custom'))
        prior_scope = _request_id_ctx_var.get()

        async def exercise():
            generator = deps.get_rpc()
            await generator.__anext__()
            with self.assertRaisesRegex(RuntimeError, 'fixture-trade-cleanup-failure'):
                await generator.aclose()
            self.assertEqual(_request_id_ctx_var.get(), prior_scope)

        with patch.object(Trade, 'session', TradeSession(), create=True), \
                patch.object(_CustomData, 'session', custom_session, create=True), \
                patch.object(ApiServer, '_has_rpc', True, create=True), \
                patch.object(ApiServer, '_rpc', object(), create=True):
            asyncio.run(exercise())
        self.assertEqual(removed, ['custom'])

    def test_real_nfi_instances_reject_api_policy_override_without_state_leak(self):
        root = Path(__file__).resolve().parents[1]
        candidates = [root / 'binana-strategies/BinanaNfiSpot.py',
                      root / 'owner/binana-strategies/BinanaNfiSpot.py',
                      Path('/freqtrade/binana-strategies/BinanaNfiSpot.py')]
        source = next(path for path in candidates if path.is_file())
        spec = importlib.util.spec_from_file_location('binana_202610_real_strategy', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            configuration = package_config()
            configuration.update(runmode=RunMode.WEBSERVER, user_data_dir=Path(directory))
            configuration['binana'].update(logical_emergency_stop_fraction=0.99,
                                           logical_roi_fraction=0.99)
            first = module.BinanaNfiSpot(copy.deepcopy(configuration))
            second = module.BinanaNfiSpot(copy.deepcopy(configuration))
            # This pinned NFI uses ordinary attributes, not hyperopt Parameter
            # objects. A supplied API parameter must not rewrite the owner policy.
            with self.assertRaisesRegex(ConfigurationError, 'not a strategy parameter'):
                first.ft_set_api_params([{'name': 'stake_amount', 'param_type': 'int', 'value': 1}])
            for strategy in (first, second):
                strategy.ft_set_api_params(None)
                strategy.ft_load_hyper_params()
                self.assertEqual(strategy.custom_stake_amount(
                    'ADA/USDT', datetime.now(timezone.utc), 1, 250, 5, 1000, 1, '1', 'long'), 250)
                self.assertFalse(strategy.confirm_trade_exit(
                    'ADA/USDT', None, 'market', 1, 1, 'GTC', 'stop_loss', None))
            parameter_names = [name for name in dir(type(first))
                               if isinstance(getattr(type(first), name, None), BaseParameter)]
            if parameter_names:
                for name in parameter_names:
                    self.assertIsNot(getattr(first, name), getattr(second, name))


if __name__ == '__main__':
    unittest.main()
