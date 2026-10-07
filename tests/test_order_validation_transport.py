"""Ensure the networked recovery probe cannot submit or cancel matching orders."""
import importlib.util
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('order_validation',
    Path(__file__).resolve().parents[1] / 'scripts/recovery/probe_testnet_order_validation.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
runner_spec = importlib.util.spec_from_file_location('order_validation_runner',
    Path(__file__).resolve().parents[1] / 'scripts/recovery/run_testnet_order_validation.py')
runner = importlib.util.module_from_spec(runner_spec)
runner_spec.loader.exec_module(runner)


class OrderValidationTransportTests(unittest.TestCase):
    def test_requests_layer_disables_and_rejects_redirects(self):
        original = Mock(return_value=SimpleNamespace(status_code=302))
        session = SimpleNamespace(send=original)
        probe.restrict_session(session)
        request = SimpleNamespace(url='https://testnet.binance.vision/api/v3/order/test', method='POST')
        with self.assertRaisesRegex(RuntimeError, 'REDIRECT_DENIED'):
            session.send(request, allow_redirects=True)
        original.assert_called_once_with(request, allow_redirects=False)
        request.url = 'https://api.binance.com/api/v3/order/test'
        with self.assertRaisesRegex(RuntimeError, 'TRANSPORT_DENIED'):
            session.send(request)
        self.assertEqual(original.call_count, 1)

    def test_success_requires_probe_and_every_host_preservation_check(self):
        receipt = {'exit_code': 0, 'owner_container_unchanged': True,
                   'owner_image_unchanged': True,
                   'trade_order_counts_and_open_incidents_unchanged': True}
        self.assertTrue(runner.verified_result(receipt, {'passed': True}))
        for key in receipt:
            changed = {**receipt, key: 1 if key == 'exit_code' else False}
            self.assertFalse(runner.verified_result(changed, {'passed': True}))
        for report in [{}, {'passed': False}, {'passed': 1}]:
            self.assertFalse(runner.verified_result(receipt, report))

    def test_accepts_testnet_validation_and_scoped_reads(self):
        for method, path in [('POST', '/api/v3/order/test'), ('GET', '/api/v3/exchangeInfo'),
                             ('GET', '/api/v3/account/commission')]:
            self.assertEqual(probe.check_transport('https://testnet.binance.vision'+path, method)['path'], path)

    def test_rejects_trading_mutations_and_credentials_to_other_hosts(self):
        for url, method in [
            ('https://testnet.binance.vision/api/v3/order', 'POST'),
            ('https://testnet.binance.vision/api/v3/orderList/otoco', 'POST'),
            ('https://testnet.binance.vision/api/v3/orderList/oco', 'POST'),
            ('https://testnet.binance.vision/api/v3/orderList', 'DELETE'),
            ('https://api.binance.com/api/v3/order/test', 'POST'),
            ('https://testnet.binance.vision.evil.invalid/api/v3/order/test', 'POST'),
            ('http://testnet.binance.vision/api/v3/order/test', 'POST'),
            ('https://testnet.binance.vision:8443/api/v3/order/test', 'POST'),
            ('https://user@testnet.binance.vision/api/v3/order/test', 'POST'),
            ('https://testnet.binance.vision/api/v3/order/test#fragment', 'POST'),
        ]:
            with self.subTest(url=url, method=method), self.assertRaises(RuntimeError):
                probe.check_transport(url, method)


if __name__ == '__main__':
    unittest.main()
