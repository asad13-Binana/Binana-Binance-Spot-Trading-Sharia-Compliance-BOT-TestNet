import asyncio
import unittest

from freqtrade.persistence import Trade
from freqtrade.persistence.custom_data import _CustomData
from freqtrade.rpc.api_server import deps
from freqtrade.rpc.api_server.webserver import ApiServer


class _FakeSession:
    def __init__(self):
        self.rollback_calls = 0
        self.remove_calls = 0

    def rollback(self):
        self.rollback_calls += 1

    def remove(self):
        self.remove_calls += 1


class RpcSessionCleanup(unittest.TestCase):
    def test_get_rpc_releases_trade_and_custom_data_scoped_sessions(self):
        trade_session = _FakeSession()
        custom_session = _FakeSession()
        had_trade_session = hasattr(Trade, 'session')
        had_custom_session = hasattr(_CustomData, 'session')
        old_trade_session = getattr(Trade, 'session', None)
        old_custom_session = getattr(_CustomData, 'session', None)
        had_has_rpc = hasattr(ApiServer, '_has_rpc')
        had_rpc = hasattr(ApiServer, '_rpc')
        old_has_rpc = getattr(ApiServer, '_has_rpc', None)
        old_rpc = getattr(ApiServer, '_rpc', None)

        async def exercise():
            generator = deps.get_rpc()
            await generator.__anext__()
            await generator.aclose()

        try:
            Trade.session = trade_session
            _CustomData.session = custom_session
            ApiServer._has_rpc = True
            ApiServer._rpc = object()
            asyncio.run(exercise())
            self.assertEqual(trade_session.remove_calls, 1)
            self.assertEqual(custom_session.remove_calls, 1)
        finally:
            if had_trade_session:
                Trade.session = old_trade_session
            else:
                delattr(Trade, 'session')
            if had_custom_session:
                _CustomData.session = old_custom_session
            else:
                delattr(_CustomData, 'session')
            if had_has_rpc:
                ApiServer._has_rpc = old_has_rpc
            else:
                delattr(ApiServer, '_has_rpc')
            if had_rpc:
                ApiServer._rpc = old_rpc
            else:
                delattr(ApiServer, '_rpc')


if __name__ == '__main__':
    unittest.main()
