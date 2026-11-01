"""Credential-free package isolation checks against the real pinned CCXT client.

The expected package is a test-runner input, never a runtime mode override.
All HTTP transport is intercepted and the runner disables container networking.
"""
import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import ccxt
from freqtrade.binana.environment import BinanaConfigError, validate_runtime_contract
from freqtrade.binana.release_gate import runtime_bindings, certificate_blockers
from freqtrade.binana.user_stream import PrivateUserStream, health_is_fresh
from freqtrade.exchange.binance import Binance, BinanaBinance
from freqtrade.exceptions import OperationalException
from freqtrade.binana.execution_manager import BinanaExecutionManager
from freqtrade.freqtradebot import FreqtradeBot

MODE = os.environ.get('BINANA_TEST_EXPECTED_MODE', 'testnet')
assert MODE in {'testnet', 'live'}
REST = ('https://api.binance.com/api/v3' if MODE == 'live'
        else 'https://testnet.binance.vision/api/v3')
WS = ('wss://ws-api.binance.com:443/ws-api/v3' if MODE == 'live'
      else 'wss://ws-api.testnet.binance.vision/ws-api/v3')


def config(mode=MODE):
    policy = dict(enabled=True, environment=mode, spot_only=True,
                  allow_live=mode == 'live', allocation_usdt=1000,
                  single_halal_decision=True, account_scope='fixture-account-42')
    policy['exchange_epoch_id' if mode == 'live' else 'testnet_epoch_id'] = 'fixture-epoch-42'
    if mode == 'live':
        policy['live_activation'] = 'I_AUTHORIZE_BINANA_LIVE_SPOT'
    return dict(binana=policy, dry_run=False, initial_state='stopped',
                trading_mode='spot', margin_mode=None, stake_currency='USDT',
                stake_amount=250, max_open_trades=4, timeframe='5m',
                force_entry_enable=False, position_adjustment_enable=False,
                exchange={'name':'binance', 'binana_testnet':mode == 'testnet'})


def client(configuration=None):
    owner = BinanaBinance.__new__(BinanaBinance)
    owner.close = lambda: None  # The fixture never owns an event loop or opened exchange.
    owner._config = config() if configuration is None else configuration
    # No load_markets() / exchange account API is called by this constructor.
    api = ccxt.binance({'apiKey':'offline-fixture-key', 'secret':'offline-fixture-secret'})
    with patch.object(Binance, '_init_ccxt', return_value=api):
        return owner._init_ccxt({}, True, {})


class PackageEnvironment(unittest.TestCase):
    def test_matching_package_accepts_explicit_selected_policy(self):
        try:
            validate_runtime_contract(config())
        except BinanaConfigError as exc:
            self.fail('matching selected package rejected: '+str(exc))

    def test_foreign_environment_rejected_before_ccxt_construction(self):
        owner = BinanaBinance.__new__(BinanaBinance)
        owner.close = lambda: None
        owner._config = config('live' if MODE == 'testnet' else 'testnet')
        with patch.object(Binance, '_init_ccxt', side_effect=AssertionError('constructed foreign client')):
            with self.assertRaises(BinanaConfigError):
                owner._init_ccxt({}, True, {})

    def test_disabled_live_or_testnet_live_override_is_rejected(self):
        changed=config()
        changed['binana']['allow_live'] = MODE != 'live'
        with self.assertRaises(BinanaConfigError):
            validate_runtime_contract(changed)
        if MODE == 'live':
            changed=config(); changed['binana']['live_activation']=''
            with self.assertRaises(BinanaConfigError): validate_runtime_contract(changed)

    def test_live_placeholder_identity_cannot_construct_owner(self):
        if MODE == 'live':
            for key in ('account_scope', 'exchange_epoch_id'):
                for value in ('', 'REPLACE_WITH_RECONCILED_LIVE_IDENTITY'):
                    changed=config(); changed['binana'][key]=value
                    with self.subTest(key=key,value=value), self.assertRaises(BinanaConfigError):
                        validate_runtime_contract(changed)
        else:
            changed=config(); changed['exchange']['binana_testnet']=False
            with self.assertRaises(BinanaConfigError): validate_runtime_contract(changed)

    def test_private_and_execution_public_clients_use_selected_venue(self):
        api=client()
        self.assertEqual(api.urls['api']['private'], REST)
        public=getattr(api, 'binana_execution_public', getattr(api, 'binana_testnet_public', None))
        self.assertIsNotNone(public)
        self.assertEqual(public.urls['api']['public'], REST)
        self.assertFalse(public.apiKey or public.secret)
        self.assertEqual(api.urls['api']['public'], 'https://api.binance.com/api/v3')
        self.assertEqual(api.options['defaultType'], 'spot')

    def test_endpoint_tampering_rejects_post_before_http_transport(self):
        api=client(); requests=[]
        api.fetch=lambda *args,**kwargs: requests.append((args,kwargs)) or {}
        api.urls['api']['private']='https://wrong-environment.invalid/api/v3'
        with self.assertRaises(OperationalException):
            api.privatePostOrderListOco({'symbol':'ADAUSDT'})
        self.assertEqual(requests, [])

    def test_derivative_and_wallet_routes_are_rejected_before_http_transport(self):
        api=client(); requests=[]
        api.fetch=lambda *args,**kwargs: requests.append((args,kwargs)) or {}
        for route in ('sapi', 'fapiPrivate', 'dapiPrivate'):
            with self.subTest(route=route), self.assertRaises(OperationalException):
                api.request('order', route, 'POST', {'symbol':'ADAUSDT'})
        self.assertEqual(requests, [])

    def test_certificate_binds_environment_account_and_epoch(self):
        bindings=runtime_bindings(config())
        self.assertEqual(bindings.get('execution_environment'), MODE.upper())
        self.assertEqual(bindings.get('account_scope'), 'fixture-account-42')
        self.assertEqual(bindings['exchange_epoch_id'], 'fixture-epoch-42')
        valid={**bindings,'generated_at':100,'valid_until':200,
               **{key:True for key in ('verified','controlled_lifecycle_verified','restart_verified',
                   'backup_restore_verified','critical_suite_10x_verified','one_slot_soak_verified',
                   'four_slot_soak_verified')}}
        # Deployment files intentionally do not exist in this networkless test.
        valid['deployment_sha256']=bindings['deployment_sha256']='deployment-fixture'
        self.assertEqual(certificate_blockers(valid,bindings,now=150),[])
        for key,value in [('execution_environment','OTHER'),('account_scope','other-account'),
                          ('exchange_epoch_id','other-epoch')]:
            self.assertIn(key+'_mismatch',certificate_blockers({**valid,key:value},bindings,now=150))

    def test_private_stream_never_accepts_other_network_health(self):
        stream=PrivateUserStream('offline-fixture-key','offline-fixture-secret',
                                 scope='fixture-scope',runtime='/tmp')
        health=stream.health()
        self.assertEqual(health['endpoint'],WS)
        health.update(ok=True,subscribed=True,last_verified_at=100,valid_until=135)
        self.assertTrue(health_is_fresh(health,now=101))
        health['endpoint']='wss://wrong-environment.invalid/ws-api/v3'
        self.assertFalse(health_is_fresh(health,now=101))

    def test_wrong_release_scope_blocks_real_freqtrade_entry_before_submission(self):
        bindings={'execution_environment':MODE.upper(), 'account_scope':'fixture-account-42',
                  'exchange_epoch_id':'fixture-epoch-42'}
        valid={**bindings,'generated_at':100,'valid_until':200,
               **{key:True for key in ('verified','controlled_lifecycle_verified','restart_verified',
                   'backup_restore_verified','critical_suite_10x_verified','one_slot_soak_verified',
                   'four_slot_soak_verified')}}
        for key in bindings:
            with self.subTest(key=key):
                manager=object.__new__(BinanaExecutionManager)
                manager.config={'binana':{'state_db_path':'/tmp/offline-owner.sqlite'}}
                manager.release_status=lambda: {'blockers': certificate_blockers(
                    {**valid,key:'wrong-scope'},bindings,now=150)}
                manager.entry_parameters=lambda pair,stake: (1.0,0.01,0.0001)
                submitted=[]
                manager.submit_entry=lambda **kwargs: submitted.append(kwargs)
                bot=SimpleNamespace(binana=manager,
                    strategy=SimpleNamespace(order_time_in_force={'entry':'FOK'},
                        order_types={'entry':'limit'},confirm_trade_entry=lambda **kwargs:True),
                    get_valid_enter_price_and_stake=lambda *args:(1.0,250.0,1.0))
                with patch('freqtrade.binana.persistence_readiness.persistence_readiness',
                           return_value={'ok':True,'blockers':[]}):
                    self.assertFalse(FreqtradeBot.execute_entry(bot,'ADA/USDT',250,enter_tag='1'))
                self.assertEqual(submitted,[])

    def test_async_ccxt_request_keeps_awaitable_contract_and_endpoint_fence(self):
        async def exercise():
            import ccxt.async_support as async_ccxt
            owner=BinanaBinance.__new__(BinanaBinance)
            owner.close=lambda:None
            owner._config=config()
            api=async_ccxt.binance({'apiKey':'offline-fixture-key','secret':'offline-fixture-secret'})
            with patch.object(Binance,'_init_ccxt',return_value=api):
                api=owner._init_ccxt({},False,{})
            requests=[]
            async def transport(url,method='GET',headers=None,body=None):
                requests.append((url,method))
                return {'fixture':True}
            api.fetch=transport
            try:
                self.assertEqual(await api.privateGetAccount({}),{'fixture':True})
                self.assertTrue(requests[0][0].startswith(REST+'/account?'))
                api.urls['api']['private']='https://wrong-environment.invalid/api/v3'
                with self.assertRaises(OperationalException):
                    await api.privatePostOrderListOco({'symbol':'ADAUSDT'})
                self.assertEqual(len(requests),1)
            finally:
                await api.close()
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
