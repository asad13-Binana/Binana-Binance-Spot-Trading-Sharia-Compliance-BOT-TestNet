"""Operator-workflow regressions for the non-core Sharia/Telegram layer.

These tests deliberately do not execute or import the protected strategy.  A
discovered website remains an untrusted reading-list candidate until the owner
completes the existing evidence review and signed decision workflow.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from services.sharia_operator import source_review
from services.telegram_broker import bot


def _candidate(base: str = 'EXP') -> dict:
    payload = {
        'schema_version': 1,
        'base': base,
        'pair': f'{base}/USDT',
        'status': 'VERIFIED_CANDIDATE',
        'discovered_at': '2026-08-28T00:00:00+00:00',
        'refresh_due_at': '2026-09-04T00:00:00+00:00',
        'retention_days': 90,
        'binance': {
            'symbol': base + 'USDT',
            'status': 'TRADING',
            'base_asset': base,
            'quote_asset': 'USDT',
            'spot_trading_allowed': True,
            'identity_basis': 'documented Binance Spot exchangeInfo',
        },
        'provider_identity': {
            'provider': 'coingecko',
            'provider_asset_id': 'example-project',
            'name': 'Example Project',
            'symbol': base,
            'identity_basis': 'exact Binance market binding',
        },
        'official_hosts_candidates': ['exampleproject.org'],
        'source_candidates': [
            {'role': 'official_website',
             'url': 'https://exampleproject.org/'},
            {'role': 'whitepaper',
             'url': 'https://exampleproject.org/whitepaper.pdf'},
        ],
        'errors': [],
        'owner_verified': False,
        'trade_permission': False,
        'owner_action': 'review required',
    }
    payload['record_sha256'] = source_review.record_digest(payload)
    return payload


class DiscoveryBridgeTests(unittest.TestCase):
    def test_recomputed_digest_cannot_hide_a_ticker_substitution(self):
        payload = _candidate()
        payload['binance']['symbol'] = 'OTHERUSDT'
        payload['record_sha256'] = source_review.record_digest(payload)
        with self.assertRaisesRegex(ValueError, 'Binance identity binding'):
            source_review.validated_candidate_record(payload)

    def test_provider_symbol_and_official_host_are_exactly_bound(self):
        for mutate, reason in (
                (lambda value: value['provider_identity'].__setitem__(
                    'symbol', 'OTHER'), 'provider symbol binding'),
                (lambda value: value['official_hosts_candidates'].__setitem__(
                    0, 'attacker.example'), 'official website host binding')):
            with self.subTest(reason=reason):
                payload = _candidate()
                mutate(payload)
                payload['record_sha256'] = source_review.record_digest(
                    payload)
                with self.assertRaisesRegex(ValueError, reason):
                    source_review.validated_candidate_record(payload)

    def test_candidate_index_ignores_ambiguous_and_tampered_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            valid = _candidate('EXP')
            (root / 'EXP.json').write_text(json.dumps(valid), encoding='utf-8')
            ambiguous = _candidate('AMB')
            ambiguous['status'] = 'AMBIGUOUS'
            ambiguous['record_sha256'] = source_review.record_digest(
                ambiguous)
            (root / 'AMB.json').write_text(
                json.dumps(ambiguous), encoding='utf-8')
            tampered = _candidate('BAD')
            tampered['trade_permission'] = True
            tampered['record_sha256'] = source_review.record_digest(
                tampered)
            (root / 'BAD.json').write_text(
                json.dumps(tampered), encoding='utf-8')
            self.assertEqual(source_review.candidate_bases(root), {'EXP'})


class _Response:
    def __init__(self, *, status_code=200, text='ok', error=None):
        self.status_code = status_code
        self.text = text
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error


class TelegramOperatorTests(unittest.TestCase):
    def test_top_level_menu_is_bounded_and_every_category_is_reachable(self):
        rows = bot.menu()
        callbacks = {
            button['callback_data'] for row in rows for button in row}
        self.assertLessEqual(len(rows), 6)
        self.assertTrue({
            'do|menu_dashboard', 'do|menu_sharia', 'do|menu_signals',
            'do|menu_research', 'do|menu_trading', 'do|menu_protection',
            'do|menu_alerts', 'do|menu_system', 'do|menu_emergency',
            'do|menu_help',
        }.issubset(callbacks))
        sharia_callbacks = {
            button['callback_data']
            for row in bot.sharia_menu() for button in row}
        for limit in bot.BULK_SCAN_LIMITS:
            self.assertIn(f'do|scan_bulk_{limit}_confirm', sharia_callbacks)
        self.assertIn('do|scan_bulk_help', sharia_callbacks)

    def test_research_menu_is_spot_only_and_never_offers_futures(self):
        labels = ' '.join(
            button['text'] for row in bot.research_menu() for button in row
        ).lower()
        self.assertIn('spot', labels)
        self.assertNotIn('future', labels)
        self.assertNotIn('funding', labels)
        self.assertNotIn('open interest', labels)

    def test_edit_failure_falls_back_to_presentation_only_send(self):
        with mock.patch.object(bot, 'TOKEN', '123456:' + 'T' * 32), \
                mock.patch.object(bot.requests, 'post', return_value=_Response(
                    error=RuntimeError('edit unavailable'))) as post, \
                mock.patch.object(bot, 'send') as send:
            bot.edit_or_send('menu', '123', 77, bot.menu())
        self.assertIn('/editMessageText', post.call_args.args[0])
        send.assert_called_once_with('menu', '123', bot.menu())

    def test_callback_routes_with_the_existing_message_id(self):
        callback = {
            'id': 'callback', 'from': {'id': 1},
            'message': {'message_id': 77, 'chat': {'id': 123}},
            'data': 'do|menu_sharia',
        }
        with mock.patch.object(bot, 'is_owner', return_value=True), \
                mock.patch.object(bot.requests, 'post', return_value=_Response()), \
                mock.patch.object(bot, 'route') as route:
            bot.handle_callback(callback)
        route.assert_called_once_with('menu_sharia', '123', 77)

    def test_low_priority_exact_pair_payload_is_signed_and_strict(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            captured = {}

            def sign(**kwargs):
                captured.update(kwargs)
                return {'signed': kwargs['payload']}

            with mock.patch.object(bot, 'SHARIA_QUEUE_INBOX', root), \
                    mock.patch.object(bot.envelope, 'sign_envelope', sign), \
                    mock.patch.object(bot, 'audit'):
                outcome = bot.sharia_scan_request('EXP', priority='bulk')
            self.assertTrue(outcome['request_id'].startswith('bulk-EXP-'))
            self.assertEqual(captured['payload']['base'], 'EXP')
            self.assertEqual(captured['payload']['pair'], 'EXP/USDT')
            self.assertEqual(captured['payload']['priority'], 'bulk')
            written = list(root.glob('*.json'))
            self.assertEqual(len(written), 1)
            self.assertEqual(json.loads(written[0].read_text(
                encoding='utf-8'))['signed']['base'], 'EXP')
        with self.assertRaises(ValueError):
            bot.sharia_scan_request('EXP', priority='urgent')

    def test_bounded_scan_uses_validated_universe_and_exact_requests(self):
        pairs = [{'pair': f'Z{index:02d}/USDT'} for index in range(1, 31)]
        snapshot = {'pairs': pairs, 'snapshot_hash': 'a' * 64}
        with mock.patch.object(bot, 'load_current', return_value=snapshot), \
                mock.patch.object(
                    bot, 'sharia_scan_request',
                    side_effect=lambda base, priority: {
                        'request_id': f'{priority}-{base}'}) as request:
            outcome = bot.sharia_bounded_scan_requests(25)
        self.assertEqual(outcome['queued_count'], 25)
        self.assertEqual(outcome['bases'][0], 'Z01')
        self.assertEqual(outcome['bases'][-1], 'Z25')
        self.assertEqual(outcome['snapshot_hash'], 'a' * 64)
        self.assertEqual(request.call_count, 25)
        with mock.patch.object(bot, 'load_current', return_value=snapshot), \
                mock.patch.object(
                    bot, 'sharia_scan_request',
                    side_effect=lambda base, priority: {
                        'request_id': f'{priority}-{base}'}):
            custom = bot.sharia_bounded_scan_requests(11)
        self.assertEqual(custom['queued_count'], 11)
        for bad in (True, 0, 101, '25'):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                bot.sharia_bounded_scan_requests(bad)

    def test_scanbulk_command_accepts_any_integer_from_one_to_one_hundred(self):
        message = {
            'chat': {'id': 123}, 'from': {'id': 1}, 'text': '/scanbulk 17'}
        with mock.patch.object(bot, 'is_owner', return_value=True), \
                mock.patch.object(bot, 'audit'), \
                mock.patch.object(bot, '_ask_confirm') as confirm:
            bot.handle_message(message)
        self.assertEqual(confirm.call_args.args[2], 'scan_bulk')
        self.assertEqual(confirm.call_args.args[3], {'limit': 17})

    def test_market_context_summary_exposes_spot_advisory_freshness_only(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            current = root / 'current.json'
            health = root / 'health.json'
            current.write_text(json.dumps({
                'schema_version': 1, 'spot_only': True,
                'advisory_only': True, 'can_trade': False,
                'generated_at': '2026-08-29T00:00:00Z',
                'symbol_count': 2, 'fresh_symbol_count': 1,
                'universe_snapshot_hash': 'a' * 64,
                'symbols': {
                    'ETHUSDT': {'status': 'fresh'},
                    'SOLUSDT': {'status': 'stale'},
                },
            }), encoding='utf-8')
            health.write_text(json.dumps({
                'ok': True, 'status': 'fresh', 'ts': time.time(),
                'stream': {'subscription_ready': True},
            }), encoding='utf-8')
            with mock.patch.object(bot, 'MARKET_CONTEXT_FILE', current), \
                    mock.patch.object(bot, 'MARKET_CONTEXT_HEALTH_FILE', health):
                rendered = bot._market_context_status()
        self.assertIn('Spot market context', rendered)
        self.assertIn('advisory_only=True', rendered)
        self.assertIn('can_trade=False', rendered)
        self.assertIn('fresh symbols=1/2', rendered)

    def test_provider_status_does_not_expose_credentials(self):
        with tempfile.TemporaryDirectory() as raw:
            status = Path(raw) / 'external_signals.json'
            status.write_text(json.dumps({
                'generated_at': 123,
                'coingecko': {'enabled': True, 'keyless': True,
                              'cache_age_seconds': 5,
                              'breaker': {'state': 'closed'}},
                'cmc': {'enabled': True, 'cache_age_seconds': 10,
                        'breaker': {'state': 'closed'},
                        'api_key': 'must-not-render'},
                'config': {'role': 'advisory-annotation-only'},
            }), encoding='utf-8')
            with mock.patch.object(bot, 'EXTERNAL_SIGNALS_STATUS', status):
                rendered = bot._external_provider_status()
        self.assertIn('CoinGecko', rendered)
        self.assertIn('CoinMarketCap', rendered)
        self.assertNotIn('must-not-render', rendered)
        self.assertNotIn('api_key', rendered)

    def test_empty_registry_status_cannot_look_ready(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw)
            registry = runtime / 'source_registry.json'
            discovery = runtime / 'discovery'
            discovery.mkdir()
            registry.write_text(
                '{"schema_version":1,"assets":{}}', encoding='utf-8')
            (discovery / 'EXP.json').write_text(
                json.dumps(_candidate('EXP')), encoding='utf-8')
            (runtime / 'health.json').write_text(json.dumps({
                'ok': True,
                'ready_for_screening': True,
                'ts': time.time(),
                'queue': {},
                'backend': {'name': 'local-oracle-v1', 'available': True,
                            'external_ai': False, 'reason': ''},
            }), encoding='utf-8')
            with mock.patch.object(bot, 'SHARIA_RUNTIME_DIR', runtime), \
                    mock.patch.object(bot, 'SHARIA_FILE', runtime / 'missing'), \
                    mock.patch.object(bot, 'SHARIA_SOURCE_REGISTRY', registry), \
                    mock.patch.object(
                        bot, 'SHARIA_DISCOVERY_CURRENT_DIR', discovery):
                text = bot._sharia_service_status()
        self.assertIn('health: READY', text)
        self.assertIn('registry is empty', text)
        self.assertIn('owner review pending=1', text)
        self.assertNotIn('operational screening: READY', text)


if __name__ == '__main__':
    unittest.main()
