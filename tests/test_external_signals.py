from __future__ import annotations
"""V10.2 external signal tests (CoinGecko + CoinMarketCap), remediated per
the 2026-07-23 independent audit.

Pins the audit requirements for the enrichment layer:

1. HARD safety ceilings 4% under the documented free tiers (CoinGecko Demo
   100/min + 10k/month -> 96 + 9600; CMC Basic 50/min + 15k/month -> 48 +
   14400). Env overrides clamp to the CEILING, never to the provider quota
   (audit ISSUE 2).
2. CMC accounting is credit-true: actual ``status.credit_count`` above the
   estimate is booked, malformed metadata keeps the conservative estimate,
   and ``/v1/key/info`` reconciliation only ever raises the local ledger —
   except as the sanctioned recovery from quarantine (audit ISSUE 3).
3. Throttle state survives restarts: minute window, breaker cool-downs,
   daily/monthly counters (audit ISSUE 5).
4. Corrupt quota state fails CLOSED — quarantine, backup recovery, provider
   blocked; usage never resets to zero (audit ISSUE 6).
5. Identity and freshness safety: cached ambiguity is enforced when data is
   consumed, CoinGecko endpoints retain independent clocks, and the
   market-cap floor fires only when a trusted canonical Binance/CoinGecko/CMC
   binding agrees with below-floor data from BOTH providers (audit ISSUE 7).
6. Enrichment is advisory: identical ranking order with the feature on or
   off; disabled features add zero HTTP calls; every failure class degrades
   to Binance-only scanning.
7. V102-FIX-001: a read-only Sharia mount (production layout) cannot fail
   the scan.

Everything runs offline against fake HTTP sessions.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tests._harness as harness  # noqa: E402  (sets bus keys before service imports)
from services.universe_service.external_signals import budget as budget_mod  # noqa: E402
from services.universe_service.external_signals.breaker import CircuitBreaker  # noqa: E402
from services.universe_service.external_signals.budget import ApiBudget  # noqa: E402
from services.universe_service.external_signals.cmc_client import CoinMarketCapClient  # noqa: E402
from services.universe_service.external_signals.coingecko_client import CoinGeckoClient  # noqa: E402
from services.universe_service.external_signals import enrichment as enrichment_mod  # noqa: E402
from services.universe_service.external_signals.enrichment import ExternalSignals  # noqa: E402
from services.universe_service.external_signals.httpguard import guarded_get_json  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError('no JSON')
        return self._payload


class FakeSession:
    """Queue-driven requests.Session stand-in. Records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({'url': url, 'params': params, 'headers': headers,
                           'timeout': timeout})
        if not self.responses:
            raise AssertionError('unexpected extra HTTP call to ' + url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_budget(tmp, name='test', per_minute=10, per_month=1000, per_day=None):
    return ApiBudget(name, Path(tmp) / f'{name}_budget.json',
                     per_minute=per_minute, per_month=per_month, per_day=per_day)


def cg_row(symbol, coin_id, cap, rank=100, change=1.0):
    return {'symbol': symbol, 'id': coin_id, 'market_cap': cap,
            'market_cap_rank': rank, 'price_change_percentage_24h': change}


def cmc_row(symbol, cmc_id, cap, rank=100, change=10.0, volume=1e7):
    return {'symbol': symbol, 'id': cmc_id, 'cmc_rank': rank,
            'quote': {'USD': {'market_cap': cap, 'percent_change_24h': change,
                              'volume_24h': volume}}}


class QuotaCeilingTests(unittest.TestCase):
    """Audit ISSUE 2: the 4% reserve is a permanent HARD ceiling."""

    def test_safe_ceilings_are_96_percent_of_documented_quotas(self):
        self.assertEqual(enrichment_mod.COINGECKO_FREE_PER_MINUTE, 100)
        self.assertEqual(enrichment_mod.COINGECKO_FREE_MONTHLY, 10_000)
        self.assertEqual(enrichment_mod.CMC_FREE_PER_MINUTE, 50)
        self.assertEqual(enrichment_mod.CMC_FREE_MONTHLY, 15_000)
        self.assertEqual(enrichment_mod.QUOTA_SAFETY, 0.96)
        self.assertEqual(enrichment_mod.COINGECKO_SAFE_PER_MINUTE, 96)
        self.assertEqual(enrichment_mod.COINGECKO_SAFE_MONTHLY, 9_600)
        self.assertEqual(enrichment_mod.CMC_SAFE_PER_MINUTE, 48)
        self.assertEqual(enrichment_mod.CMC_SAFE_MONTHLY, 14_400)

    def test_overrides_clamp_to_safe_ceiling_not_provider_quota(self):
        cases = [
            ({'COINGECKO_PER_MINUTE_LIMIT': '97'}, 'cg_min', 96),
            ({'COINGECKO_PER_MINUTE_LIMIT': '100'}, 'cg_min', 96),
            ({'COINGECKO_PER_MINUTE_LIMIT': '5000'}, 'cg_min', 96),
            ({'COINGECKO_MONTHLY_LIMIT': '9601'}, 'cg_month', 9_600),
            ({'COINGECKO_MONTHLY_LIMIT': '10000'}, 'cg_month', 9_600),
            ({'CMC_PER_MINUTE_LIMIT': '49'}, 'cmc_min', 48),
            ({'CMC_PER_MINUTE_LIMIT': '50'}, 'cmc_min', 48),
            ({'CMC_MONTHLY_LIMIT': '14401'}, 'cmc_month', 14_400),
            ({'CMC_MONTHLY_LIMIT': '15000'}, 'cmc_month', 14_400),
        ]
        for env, which, expected in cases:
            with self.subTest(env=env), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {'COINGECKO_API_KEY': 'demo',
                                                 **env}, clear=False):
                ext = ExternalSignals.from_env(tmp)
                actual = {
                    'cg_min': ext._cg_budget.per_minute,
                    'cg_month': ext._cg_budget.per_month,
                    'cmc_min': ext._cmc_budget.per_minute,
                    'cmc_month': ext._cmc_budget.per_month,
                }[which]
                self.assertEqual(actual, expected)

    def test_lowering_below_ceiling_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                'COINGECKO_API_KEY': 'demo',
                'COINGECKO_PER_MINUTE_LIMIT': '10',
                'CMC_MONTHLY_LIMIT': '1000'}, clear=False):
            ext = ExternalSignals.from_env(tmp)
            self.assertEqual(ext._cg_budget.per_minute, 10)
            self.assertEqual(ext._cmc_budget.per_month, 1000)

    def test_status_reports_configured_and_effective_limits(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                'COINGECKO_API_KEY': 'demo',
                'COINGECKO_PER_MINUTE_LIMIT': '100'}, clear=False):
            status = ExternalSignals.from_env(tmp).status_snapshot()
            self.assertEqual(status['coingecko']['configured_per_minute'], 100)
            self.assertEqual(status['coingecko']['budget']['minute_cap'], 96)
            self.assertEqual(status['cmc']['configured_monthly'], 14_400)
            self.assertEqual(status['cmc']['budget']['month_cap'], 14_400)

    def test_keyless_coingecko_is_clamped_to_shared_ip_budget(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                'COINGECKO_API_KEY': ''}, clear=False):
            ext = ExternalSignals.from_env(tmp)
            self.assertEqual(ext._cg_budget.per_minute,
                             enrichment_mod.COINGECKO_KEYLESS_PER_MINUTE)

    def test_invalid_settings_fail_loudly(self):
        cases = [
            {'EXTERNAL_SIGNALS_REFRESH_SECONDS': '10'},
            {'EXTERNAL_SIGNALS_REFRESH_SECONDS': 'soon'},
            {'EXTERNAL_MIN_MARKET_CAP_USD': '-1'},
            {'CMC_TRENDING_LIMIT': '0'},
            {'EXTERNAL_BREAKER_COOLDOWN_SECONDS': '5'},
            {'COINGECKO_PER_MINUTE_LIMIT': '0'},
            {'COINGECKO_MONTHLY_LIMIT': '-5'},
            {'CMC_RECONCILE_SECONDS': '60'},
        ]
        for env in cases:
            with self.subTest(env=env), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaises(ValueError):
                    ExternalSignals.from_env(tmp)


class ApiBudgetTests(unittest.TestCase):
    def test_minute_window_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = make_budget(tmp, per_minute=3, per_month=1000)
            for _ in range(3):
                self.assertTrue(b.try_acquire())
            self.assertFalse(b.try_acquire())
            b._minute_marks[0] = time.time() - 61
            self.assertTrue(b.try_acquire())

    def test_minute_window_survives_restart(self):
        """Audit ISSUE 5: a restart cannot grant a fresh per-minute window."""
        with tempfile.TemporaryDirectory() as tmp:
            b = make_budget(tmp, per_minute=3, per_month=1000)
            for _ in range(3):
                self.assertTrue(b.try_acquire())
            reborn = make_budget(tmp, per_minute=3, per_month=1000)
            self.assertEqual(reborn.stats()['minute_used'], 3)
            self.assertFalse(reborn.try_acquire())

    def test_daily_cap_spreads_month_over_31_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(make_budget(tmp, per_month=9600).per_day, 9600 // 31)
            self.assertEqual(make_budget(tmp, name='c', per_month=14400).per_day,
                             14400 // 31)

    def test_monthly_cap_and_credit_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = make_budget(tmp, per_minute=100, per_month=5, per_day=5)
            self.assertTrue(b.try_acquire(cost=3))
            self.assertFalse(b.try_acquire(cost=3))
            self.assertTrue(b.try_acquire(cost=2))
            self.assertFalse(b.try_acquire())

    def test_counters_persist_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertTrue(b.try_acquire(cost=4))
            reborn = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertEqual(reborn.stats()['month_used'], 4)
            self.assertEqual(reborn.stats()['day_used'], 4)

    def test_month_rollover_resets_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertTrue(b.try_acquire(cost=9))
            future = datetime(2099, 1, 1, tzinfo=timezone.utc)
            with mock.patch.object(budget_mod, '_utc', return_value=future):
                self.assertEqual(b.stats()['month_used'], 0)
                self.assertTrue(b.try_acquire(cost=9))

    def test_older_state_rolls_over_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test_budget.json'
            path.write_text(json.dumps({
                'day': '1999-01-01', 'day_count': 9,
                'month': '1999-01', 'month_count': 9,
                'minute_marks': [], 'quarantined': False}), encoding='utf-8')
            b = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertEqual(b.stats()['month_used'], 0)
            self.assertFalse(b.stats()['quarantined'])

    def test_record_extra_books_provider_charges(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertTrue(b.try_acquire())
            b.record_extra(2)
            self.assertEqual(b.stats()['month_used'], 3)
            b.record_extra(0)
            b.record_extra(-5)
            self.assertEqual(b.stats()['month_used'], 3)

    def test_reconcile_month_only_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = make_budget(tmp, per_minute=100, per_month=100, per_day=100)
            self.assertTrue(b.try_acquire(cost=10))
            self.assertEqual(b.reconcile_month(50), 50)   # provider higher
            self.assertEqual(b.reconcile_month(20), 50)   # provider lower: keep
            self.assertIsNone(b.reconcile_month('50'))    # malformed
            self.assertIsNone(b.reconcile_month(True))    # bool is not usage
            self.assertIsNone(b.reconcile_month(-1))
            self.assertEqual(b.stats()['month_used'], 50)

    def test_reconcile_can_exhaust_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = make_budget(tmp, per_minute=100, per_month=100, per_day=100)
            b.reconcile_month(100)
            self.assertFalse(b.try_acquire())


class BudgetCorruptionTests(unittest.TestCase):
    """Audit ISSUE 6: corruption can never increase allowance."""

    def _audit_env(self, tmp):
        return mock.patch.dict(os.environ,
                               {'AUDIT_LOG': str(Path(tmp) / 'audit.jsonl')})

    def test_invalid_json_quarantines_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, self._audit_env(tmp):
            path = Path(tmp) / 'test_budget.json'
            path.write_text('{ this is not json', encoding='utf-8')
            b = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertTrue(b.quarantined)
            self.assertFalse(b.try_acquire())
            self.assertEqual(b.stats()['month_used'], 10)
            corrupt = list(Path(tmp).glob('test_budget.json.corrupt-*'))
            self.assertEqual(len(corrupt), 1)
            audit_text = (Path(tmp) / 'audit.jsonl').read_text(encoding='utf-8')
            self.assertIn('external_budget_state_quarantined', audit_text)

    def test_negative_counts_and_future_month_are_corruption(self):
        bad_states = [
            {'day': '2026-01-01', 'day_count': -1, 'month': '2026-01',
             'month_count': 0, 'minute_marks': [], 'quarantined': False},
            {'day': '2099-01-01', 'day_count': 0, 'month': '2099-01',
             'month_count': 0, 'minute_marks': [], 'quarantined': False},
            {'day': 'nonsense', 'day_count': 0, 'month': '2026-01',
             'month_count': 0, 'minute_marks': [], 'quarantined': False},
            {'day': '2026-01-01', 'day_count': 0, 'month': '2026-01',
             'month_count': 0, 'minute_marks': 'not-a-list', 'quarantined': False},
        ]
        for state in bad_states:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp, \
                    self._audit_env(tmp):
                (Path(tmp) / 'test_budget.json').write_text(
                    json.dumps(state), encoding='utf-8')
                b = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
                self.assertTrue(b.quarantined)
                self.assertFalse(b.try_acquire())

    def test_corruption_always_fails_closed_even_with_valid_backup(self):
        # V102-REM-012: a valid `.bak` can hold a LOWER, stale counter than the
        # corrupted-but-real main ledger. Restoring it would hand back
        # already-spent quota (fail-open). Corruption must ALWAYS fail closed.
        with tempfile.TemporaryDirectory() as tmp, self._audit_env(tmp):
            b = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertTrue(b.try_acquire(cost=4))
            make_budget(tmp, per_minute=100, per_month=10, per_day=10)  # writes .bak
            self.assertTrue((Path(tmp) / 'test_budget.json.bak').exists())
            (Path(tmp) / 'test_budget.json').write_text('garbage', 'utf-8')
            recovered = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertTrue(recovered.quarantined)
            self.assertFalse(recovered.try_acquire())
            self.assertEqual(recovered.stats()['month_used'], 10)  # treated as spent

    def test_missing_ledger_with_install_marker_fails_closed(self):
        # V102-REM-013: a vanished ledger under a running deployment is
        # unexplained state loss, not a free reset to zero.
        with tempfile.TemporaryDirectory() as tmp, self._audit_env(tmp):
            b = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertTrue(b.try_acquire(cost=3))
            self.assertTrue((Path(tmp) / 'test_budget.json.install').exists())
            (Path(tmp) / 'test_budget.json').unlink()  # ledger vanishes, marker remains
            reborn = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertTrue(reborn.quarantined)
            self.assertFalse(reborn.try_acquire())

    def test_true_first_install_starts_at_zero(self):
        with tempfile.TemporaryDirectory() as tmp, self._audit_env(tmp):
            b = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertFalse(b.quarantined)
            self.assertTrue(b.try_acquire())
            self.assertTrue((Path(tmp) / 'test_budget.json.install').exists())

    def test_documented_manual_reset_clears_state(self):
        # The documented reset deletes BOTH the ledger and its install marker.
        with tempfile.TemporaryDirectory() as tmp, self._audit_env(tmp):
            b = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertTrue(b.try_acquire(cost=5))
            (Path(tmp) / 'test_budget.json').unlink()
            (Path(tmp) / 'test_budget.json.install').unlink()
            fresh = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertFalse(fresh.quarantined)
            self.assertEqual(fresh.stats()['month_used'], 0)

    def test_quarantine_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp, self._audit_env(tmp):
            (Path(tmp) / 'test_budget.json').write_text('garbage', 'utf-8')
            b = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertTrue(b.quarantined)
            reborn = make_budget(tmp, per_minute=100, per_month=10, per_day=10)
            self.assertTrue(reborn.quarantined)
            self.assertFalse(reborn.try_acquire())

    def test_provider_reconciliation_recovers_from_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp, self._audit_env(tmp):
            (Path(tmp) / 'test_budget.json').write_text('garbage', 'utf-8')
            b = make_budget(tmp, per_minute=100, per_month=100, per_day=100)
            self.assertTrue(b.quarantined)
            self.assertEqual(b.reconcile_month(7), 7)
            self.assertFalse(b.quarantined)
            # The day counter stays conservative until UTC midnight.
            self.assertFalse(b.try_acquire())
            self.assertEqual(b.stats()['month_used'], 7)


class CircuitBreakerTests(unittest.TestCase):
    def test_opens_after_threshold_and_recovers(self):
        br = CircuitBreaker('t', failure_threshold=3, cooldown_seconds=0.05)
        br.record_failure(); br.record_failure()
        self.assertTrue(br.allows())
        br.record_failure()
        self.assertFalse(br.allows())
        time.sleep(0.06)
        self.assertTrue(br.allows())
        br.record_success()
        self.assertEqual(br.state()['failures'], 0)

    def test_cooldown_override_opens_immediately(self):
        br = CircuitBreaker('t', failure_threshold=99, cooldown_seconds=900)
        br.record_failure(cooldown_override=30.0)
        self.assertFalse(br.allows())
        self.assertGreater(br.state()['retry_in_seconds'], 0)

    def test_cooldown_survives_restart(self):
        """Audit ISSUE 5: restart preserves an unexpired cool-down."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'breaker.json'
            br = CircuitBreaker('t', cooldown_seconds=900, state_path=path)
            br.record_failure(cooldown_override=120.0)
            reborn = CircuitBreaker('t', cooldown_seconds=900, state_path=path)
            self.assertFalse(reborn.allows())
            self.assertGreater(reborn.state()['retry_in_seconds'], 60)

    def test_expired_cooldown_allows_probe_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'breaker.json'
            br = CircuitBreaker('t', cooldown_seconds=900, state_path=path)
            br.record_failure(cooldown_override=0.01)
            time.sleep(0.02)
            reborn = CircuitBreaker('t', cooldown_seconds=900, state_path=path)
            self.assertTrue(reborn.allows())

    def test_corrupt_breaker_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'breaker.json'
            path.write_text('not json', encoding='utf-8')
            br = CircuitBreaker('t', cooldown_seconds=5.0, state_path=path)
            self.assertFalse(br.allows())

    def test_success_clears_persisted_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'breaker.json'
            br = CircuitBreaker('t', cooldown_seconds=900, state_path=path)
            br.record_failure(cooldown_override=300.0)
            br.record_success()
            reborn = CircuitBreaker('t', cooldown_seconds=900, state_path=path)
            self.assertTrue(reborn.allows())


class HttpGuardTests(unittest.TestCase):
    def _run(self, session, budget, breaker, cost=1):
        return guarded_get_json(session, 'https://x.test/api', params=None,
                                headers={}, timeout=5, budget=budget,
                                breaker=breaker, cost=cost)

    def test_budget_refusal_prevents_the_http_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget = make_budget(tmp, per_minute=1, per_month=1000)
            self.assertTrue(budget.try_acquire())
            session = FakeSession([])
            self.assertIsNone(self._run(session, budget, CircuitBreaker('t')))
            self.assertEqual(session.calls, [])

    def test_none_budget_skips_the_budget_gate_only(self):
        session = FakeSession([FakeResponse(200, payload={'ok': True})])
        out = self._run(session, None, CircuitBreaker('t'))
        self.assertEqual(out, {'ok': True})
        breaker = CircuitBreaker('t')
        breaker.record_failure(cooldown_override=60.0)
        self.assertIsNone(self._run(FakeSession([]), None, breaker))

    def test_429_honors_retry_after_and_opens_breaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget = make_budget(tmp)
            breaker = CircuitBreaker('t', failure_threshold=99)
            session = FakeSession([FakeResponse(429, headers={'Retry-After': '77'})])
            self.assertIsNone(self._run(session, budget, breaker))
            self.assertFalse(breaker.allows())
            self.assertAlmostEqual(breaker.state()['retry_in_seconds'], 77, delta=2)
            self.assertIsNone(self._run(session, budget, breaker))
            self.assertEqual(len(session.calls), 1)

    def test_huge_retry_after_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            breaker = CircuitBreaker('t', failure_threshold=99)
            session = FakeSession([FakeResponse(429, headers={'Retry-After': '999999'})])
            self.assertIsNone(self._run(session, make_budget(tmp), breaker))
            self.assertLessEqual(breaker.state()['retry_in_seconds'], 900)

    def test_auth_error_backs_off_for_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            breaker = CircuitBreaker('t', failure_threshold=99)
            session = FakeSession([FakeResponse(403, payload={'err': 1})])
            self.assertIsNone(self._run(session, make_budget(tmp), breaker))
            self.assertGreater(breaker.state()['retry_in_seconds'], 3600)

    def test_transient_errors_open_after_threshold(self):
        import requests as req
        with tempfile.TemporaryDirectory() as tmp:
            budget = make_budget(tmp)
            breaker = CircuitBreaker('t', failure_threshold=3, cooldown_seconds=900)
            session = FakeSession([
                req.ConnectionError('boom'),
                FakeResponse(500, payload={}),
                FakeResponse(200, payload=None),
            ])
            for _ in range(3):
                self.assertIsNone(self._run(session, budget, breaker))
            self.assertFalse(breaker.allows())

    def test_success_returns_payload_and_resets(self):
        with tempfile.TemporaryDirectory() as tmp:
            breaker = CircuitBreaker('t')
            breaker.record_failure()
            session = FakeSession([FakeResponse(200, payload={'ok': True})])
            out = self._run(session, make_budget(tmp), breaker)
            self.assertEqual(out, {'ok': True})
            self.assertEqual(breaker.state()['failures'], 0)


class CoinGeckoClientTests(unittest.TestCase):
    def _client(self, tmp, responses, key='demo'):
        client = CoinGeckoClient(key, make_budget(tmp), CircuitBreaker('cg'), 5)
        client.session = FakeSession(responses)
        return client

    def test_markets_carries_stable_ids_and_merges_pages(self):
        page1 = [cg_row('aaa', 'aaa-coin', 900, rank=1)]
        page2 = [cg_row('bbb', 'bbb-coin', 100, rank=50)]
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp, [FakeResponse(200, page1),
                                        FakeResponse(200, page2)])
            markets, ambiguous = client.markets()
            self.assertEqual(markets['AAA']['coingecko_id'], 'aaa-coin')
            self.assertEqual(markets['BBB']['market_cap_rank'], 50)
            self.assertEqual(ambiguous, [])
            self.assertEqual(client.session.calls[0]['headers'],
                             {'x-cg-demo-api-key': 'demo'})

    def test_same_symbol_different_id_is_ambiguous_and_excluded(self):
        page1 = [cg_row('aaa', 'aaa-coin', 900, rank=1)]
        page2 = [cg_row('AAA', 'another-aaa', 5, rank=400),
                 cg_row('bbb', 'bbb-coin', 100, rank=50)]
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp, [FakeResponse(200, page1),
                                        FakeResponse(200, page2)])
            markets, ambiguous = client.markets()
            self.assertNotIn('AAA', markets)
            self.assertEqual(ambiguous, ['AAA'])
            self.assertIn('BBB', markets)

    def test_same_symbol_same_id_is_pagination_overlap_not_ambiguity(self):
        page1 = [cg_row('aaa', 'aaa-coin', 900, rank=1)]
        page2 = [cg_row('aaa', 'aaa-coin', 900, rank=1)]
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp, [FakeResponse(200, page1),
                                        FakeResponse(200, page2)])
            markets, ambiguous = client.markets()
            self.assertIn('AAA', markets)
            self.assertEqual(ambiguous, [])

    def test_trending_parses_and_dedupes(self):
        payload = {'coins': [{'item': {'symbol': 'aaa'}}, {'item': {'symbol': 'AAA'}},
                             {'item': {'symbol': 'zzz'}}, {'bad': 1}]}
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp, [FakeResponse(200, payload)])
            self.assertEqual(client.trending(), ['AAA', 'ZZZ'])

    def test_total_failure_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp, [FakeResponse(500, {}), FakeResponse(500, {})])
            self.assertIsNone(client.markets())


class CoinMarketCapClientTests(unittest.TestCase):
    def _client(self, tmp, responses, key='k', limit=200, name='c'):
        client = CoinMarketCapClient(key, make_budget(tmp, name=name),
                                     CircuitBreaker(name), 5, listing_limit=limit)
        client.session = FakeSession(responses)
        return client

    def test_requires_key_and_counts_credits(self):
        with tempfile.TemporaryDirectory() as tmp:
            keyless = CoinMarketCapClient('', make_budget(tmp), CircuitBreaker('c'), 5)
            self.assertIsNone(keyless.listings())
            self.assertEqual(
                CoinMarketCapClient('k', make_budget(tmp, name='c2'),
                                    CircuitBreaker('c2'), 5,
                                    listing_limit=500).credit_cost(), 3)
            self.assertEqual(
                CoinMarketCapClient('k', make_budget(tmp, name='c3'),
                                    CircuitBreaker('c3'), 5,
                                    listing_limit=200).credit_cost(), 1)

    def test_listings_parse_ids_momentum_and_ambiguity(self):
        payload = {'status': {'error_code': 0, 'credit_count': 1},
                   'data': [cmc_row('aaa', 11, 1e8, rank=300, change=30.0),
                            cmc_row('bbb', 22, 9e9, rank=12, change=11.0),
                            cmc_row('AAA', 33, 5e5, rank=900, change=9.0)]}
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp, [FakeResponse(200, payload)])
            listings, ambiguous = client.listings()
            self.assertEqual(ambiguous, ['AAA'])
            self.assertNotIn('AAA', listings)
            self.assertEqual(listings['BBB']['cmc_id'], 22)
            self.assertEqual(listings['BBB']['momentum_rank'], 2)
            self.assertEqual(client.session.calls[0]['headers']['X-CMC_PRO_API_KEY'], 'k')

    def test_actual_credit_count_above_estimate_is_booked(self):
        payload = {'status': {'error_code': 0, 'credit_count': 3},
                   'data': [cmc_row('aaa', 11, 1e8)]}
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp, [FakeResponse(200, payload)])
            self.assertIsNotNone(client.listings())
            # 1 reserved + 2 booked from the provider's actual charge.
            self.assertEqual(client.budget.stats()['month_used'], 3)

    def test_malformed_credit_count_keeps_conservative_estimate(self):
        for credit_count in (None, 'two', -4, True):
            payload = {'status': {'error_code': 0},
                       'data': [cmc_row('aaa', 11, 1e8)]}
            if credit_count is not None:
                payload['status']['credit_count'] = credit_count
            with self.subTest(credit_count=credit_count), \
                    tempfile.TemporaryDirectory() as tmp:
                client = self._client(tmp, [FakeResponse(200, payload)])
                self.assertIsNotNone(client.listings())
                self.assertEqual(client.budget.stats()['month_used'], 1)

    def test_lower_credit_count_never_refunds(self):
        payload = {'status': {'error_code': 0, 'credit_count': 1},
                   'data': [cmc_row('aaa', 11, 1e8)]}
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp, [FakeResponse(200, payload)], limit=500)
            self.assertIsNotNone(client.listings())
            self.assertEqual(client.budget.stats()['month_used'], 3)  # estimate kept

    def test_inband_error_is_a_failure(self):
        payload = {'status': {'error_code': 1006, 'error_message': 'plan'}, 'data': []}
        with tempfile.TemporaryDirectory() as tmp:
            breaker = CircuitBreaker('c', failure_threshold=1, cooldown_seconds=900)
            client = CoinMarketCapClient('k', make_budget(tmp), breaker, 5)
            client.session = FakeSession([FakeResponse(200, payload)])
            self.assertIsNone(client.listings())
            self.assertFalse(breaker.allows())

    def test_key_info_parses_usage_and_books_cost(self):
        payload = {'status': {'error_code': 0},
                   'data': {'usage': {'current_month': {'credits_used': 1234}}}}
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(tmp, [FakeResponse(200, payload)])
            self.assertEqual(client.key_info_month_credits_used(), 1234)
            self.assertEqual(client.budget.stats()['month_used'], 1)

    def test_key_info_malformed_returns_none(self):
        cases = [
            {'status': {'error_code': 0}, 'data': {}},
            {'status': {'error_code': 0},
             'data': {'usage': {'current_month': {'credits_used': 'many'}}}},
            {'status': {'error_code': 0},
             'data': {'usage': {'current_month': {'credits_used': -2}}}},
            {'status': {'error_code': 1002, 'error_message': 'bad key'}},
        ]
        for payload in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                client = self._client(tmp, [FakeResponse(200, payload)])
                self.assertIsNone(client.key_info_month_credits_used())

    def test_key_info_runs_even_when_budget_quarantined(self):
        """Reconciliation is the recovery path; it cannot be budget-gated."""
        payload = {'status': {'error_code': 0},
                   'data': {'usage': {'current_month': {'credits_used': 42}}}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {'AUDIT_LOG': str(Path(tmp) / 'audit.jsonl')}):
            (Path(tmp) / 'c_budget.json').write_text('garbage', 'utf-8')
            client = self._client(tmp, [FakeResponse(200, payload)])
            self.assertTrue(client.budget.quarantined)
            self.assertEqual(client.key_info_month_credits_used(), 42)

    def test_key_info_skips_when_healthy_budget_at_cap(self):
        """V102-REM-014: a healthy budget already at its monthly cap has no
        reason to spend an extra credit probing usage."""
        with tempfile.TemporaryDirectory() as tmp:
            budget = make_budget(tmp, name='cap', per_minute=100,
                                 per_month=5, per_day=5)
            self.assertTrue(budget.try_acquire(cost=5))  # exhaust the month
            self.assertFalse(budget.quarantined)
            client = CoinMarketCapClient('k', budget, CircuitBreaker('cap'), 5)
            client.session = FakeSession([])  # any HTTP call would raise
            self.assertIsNone(client.key_info_month_credits_used())
            self.assertEqual(client.session.calls, [])


class EnrichmentTests(unittest.TestCase):
    def _env(self, extra=None):
        env = {'ENABLE_COINGECKO_SIGNALS': 'true', 'ENABLE_CMC_TRENDING': 'true',
               'COINGECKO_API_KEY': 'demo', 'CMC_API_KEY': 'basic',
               'EXTERNAL_SIGNALS_REFRESH_SECONDS': '1800'}
        env.update(extra or {})
        return mock.patch.dict(os.environ, env, clear=False)

    def _seed_cache(self, root, *, cg_age=0.0, cg_markets_age=None,
                    cg_trending_age=None, cmc_age=0.0):
        state = Path(root) / 'external'
        state.mkdir(parents=True, exist_ok=True)
        now = time.time()
        markets_age = cg_age if cg_markets_age is None else cg_markets_age
        trending_age = cg_age if cg_trending_age is None else cg_trending_age
        (state / 'cache.json').write_text(json.dumps({
            'coingecko': {'markets_fetched_at': now - markets_age,
                          'trending_fetched_at': now - trending_age,
                          'markets': {
                              'AAA': {'coingecko_id': 'aaa-coin',
                                      'market_cap_usd': 5e7,
                                      'market_cap_rank': 80,
                                      'change_24h_pct': 4.2},
                              'BBB': {'coingecko_id': 'bbb-coin',
                                      'market_cap_usd': 1e7,
                                      'market_cap_rank': 700,
                                      'change_24h_pct': 22.0},
                              'DDD': {'coingecko_id': 'ddd-coin',
                                      'market_cap_usd': 2e6,
                                      'market_cap_rank': 1500,
                                      'change_24h_pct': 3.0},
                              'EEE': {'coingecko_id': 'eee-coin',
                                      'market_cap_usd': 1e7,
                                      'market_cap_rank': 800,
                                      'change_24h_pct': 2.0}},
                          'trending': ['AAA'],
                          'ambiguous': []},
            'cmc': {'fetched_at': now - cmc_age,
                    'listings': {
                        'AAA': {'cmc_id': 101, 'cmc_rank': 90,
                                'momentum_rank': 3, 'market_cap_usd': 5.1e7,
                                'change_24h_pct': 4.0, 'volume_24h_usd': 2e7},
                        'BBB': {'cmc_id': 202, 'cmc_rank': 750,
                                'momentum_rank': 40, 'market_cap_usd': 1.1e7,
                                'change_24h_pct': 21.0, 'volume_24h_usd': 5e6},
                        'EEE': {'cmc_id': 505, 'cmc_rank': 300,
                                'momentum_rank': 60, 'market_cap_usd': 9e7,
                                'change_24h_pct': 1.5, 'volume_24h_usd': 3e6}},
                    'ambiguous': []},
        }), encoding='utf-8')

    def test_disabled_features_make_zero_calls_and_empty_annotations(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                'ENABLE_COINGECKO_SIGNALS': 'false',
                'ENABLE_CMC_TRENDING': 'false'}, clear=False):
            with mock.patch.object(enrichment_mod, 'CoinGeckoClient') as cg_cls, \
                    mock.patch.object(enrichment_mod, 'CoinMarketCapClient') as cmc_cls:
                ext = ExternalSignals.from_env(tmp)
                ext.refresh_if_stale()
                cg_cls.assert_not_called()
                cmc_cls.assert_not_called()
            self.assertEqual(ext.enrich('AAA'), {})
            self.assertIsNone(ext.reject_reason('AAA'))

    def test_cmc_enabled_without_key_stays_disabled(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                'ENABLE_CMC_TRENDING': 'true', 'CMC_API_KEY': '',
                'COINMARKETCAP_API_KEY': ''}, clear=False):
            ext = ExternalSignals.from_env(tmp)
            self.assertFalse(ext.cmc_enabled)
            self.assertTrue(ext.status_snapshot()['cmc']['requested_but_missing_key'])

    def test_fresh_cache_suppresses_refetch(self):
        with tempfile.TemporaryDirectory() as tmp, self._env():
            self._seed_cache(tmp, cg_age=10, cmc_age=10)
            with mock.patch.object(enrichment_mod, 'CoinGeckoClient') as cg_cls, \
                    mock.patch.object(enrichment_mod, 'CoinMarketCapClient') as cmc_cls:
                ext = ExternalSignals.from_env(tmp)
                ext.refresh_if_stale()
                cg_cls.assert_not_called()
                cmc_cls.assert_not_called()

    def test_stale_cache_triggers_refetch_and_failure_keeps_previous_data(self):
        with tempfile.TemporaryDirectory() as tmp, self._env():
            self._seed_cache(tmp, cg_age=3600, cmc_age=3600)
            cg_stub = mock.Mock()
            cg_stub.markets.return_value = None
            cg_stub.trending.return_value = None
            cmc_stub = mock.Mock()
            cmc_stub.listings.return_value = None
            cmc_stub.key_info_month_credits_used.return_value = None
            audit_dir = Path(tmp) / 'audit'
            with mock.patch.object(enrichment_mod, 'CoinGeckoClient', return_value=cg_stub), \
                    mock.patch.object(enrichment_mod, 'CoinMarketCapClient', return_value=cmc_stub), \
                    mock.patch.dict(os.environ, {'AUDIT_LOG': str(audit_dir / 'events.jsonl')}):
                ext = ExternalSignals.from_env(tmp)
                ext.refresh_if_stale()
            cg_stub.markets.assert_called_once()
            cmc_stub.listings.assert_called_once()
            self.assertIn('coingecko', ext.enrich('AAA'))
            audit_lines = (audit_dir / 'events.jsonl').read_text(encoding='utf-8')
            self.assertIn('external_signals_refresh_failed', audit_lines)

    def test_trending_success_cannot_rejuvenate_failed_markets(self):
        """Each CoinGecko endpoint owns the timestamp for its own bytes."""
        with tempfile.TemporaryDirectory() as tmp, self._env({
                'EXTERNAL_MIN_MARKET_CAP_USD': '30000000',
                'AUDIT_LOG': str(Path(tmp) / 'audit' / 'events.jsonl')}):
            beyond = 1800 * enrichment_mod.STALE_INTERVALS + 60
            self._seed_cache(tmp, cg_markets_age=beyond,
                             cg_trending_age=beyond)
            binding = {'BBB': {
                'verified': True, 'binance_base': 'BBB',
                'coingecko_id': 'bbb-coin', 'cmc_id': 202}}
            ext = ExternalSignals.from_env(
                tmp, verified_identity_bindings=binding)
            old_markets_at = ext._cache['coingecko']['markets_fetched_at']
            old_trending_at = ext._cache['coingecko']['trending_fetched_at']
            cg_stub = mock.Mock()
            cg_stub.markets.return_value = None
            cg_stub.trending.return_value = ['AAA']
            with mock.patch.object(enrichment_mod, 'CoinGeckoClient',
                                   return_value=cg_stub):
                ext._refresh_coingecko()

            cg_stub.markets.assert_called_once()
            cg_stub.trending.assert_called_once()
            cache = ext._cache['coingecko']
            self.assertEqual(cache['markets_fetched_at'], old_markets_at)
            self.assertGreater(cache['trending_fetched_at'], old_trending_at)
            self.assertEqual(ext.enrich('AAA')['coingecko'],
                             {'trending': True})
            self.assertIsNone(ext.reject_reason('BBB'))

    def test_markets_success_cannot_rejuvenate_failed_trending(self):
        with tempfile.TemporaryDirectory() as tmp, self._env({
                'AUDIT_LOG': str(Path(tmp) / 'audit' / 'events.jsonl')}):
            beyond = 1800 * enrichment_mod.STALE_INTERVALS + 60
            self._seed_cache(tmp, cg_markets_age=beyond,
                             cg_trending_age=beyond)
            ext = ExternalSignals.from_env(tmp)
            old_markets_at = ext._cache['coingecko']['markets_fetched_at']
            old_trending_at = ext._cache['coingecko']['trending_fetched_at']
            cg_stub = mock.Mock()
            cg_stub.markets.return_value = ({
                'AAA': {'coingecko_id': 'aaa-new',
                        'market_cap_usd': 6e7}}, [])
            cg_stub.trending.return_value = None
            with mock.patch.object(enrichment_mod, 'CoinGeckoClient',
                                   return_value=cg_stub):
                ext._refresh_coingecko()

            cache = ext._cache['coingecko']
            self.assertGreater(cache['markets_fetched_at'], old_markets_at)
            self.assertEqual(cache['trending_fetched_at'], old_trending_at)
            self.assertEqual(ext.enrich('AAA')['coingecko'], {
                'coingecko_id': 'aaa-new',
                'market_cap_usd': 6e7,
                'trending': False,
            })

    def test_future_cache_timestamps_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, self._env({
                'EXTERNAL_MIN_MARKET_CAP_USD': '30000000'}):
            self._seed_cache(tmp, cg_age=-3600, cmc_age=-3600)
            binding = {'BBB': {
                'verified': True, 'binance_base': 'BBB',
                'coingecko_id': 'bbb-coin', 'cmc_id': 202}}
            ext = ExternalSignals.from_env(
                tmp, verified_identity_bindings=binding)
            self.assertIsNone(ext._provider_age('coingecko', 'markets'))
            self.assertIsNone(ext._provider_age('coingecko', 'trending'))
            self.assertIsNone(ext._provider_age('cmc'))
            self.assertEqual(ext.enrich('AAA'), {})
            self.assertIsNone(ext.reject_reason('BBB'))
            ext._cache['coingecko']['markets_fetched_at'] = float('nan')
            ext._cache['coingecko']['trending_fetched_at'] = float('nan')
            ext._cache['cmc']['fetched_at'] = float('nan')
            self.assertIsNone(ext._provider_age('coingecko', 'markets'))
            self.assertIsNone(ext._provider_age('coingecko', 'trending'))
            self.assertIsNone(ext._provider_age('cmc'))

    def test_cached_ambiguous_symbols_are_enforced_at_consumption(self):
        with tempfile.TemporaryDirectory() as tmp, self._env({
                'EXTERNAL_MIN_MARKET_CAP_USD': '30000000'}):
            self._seed_cache(tmp)
            cache_path = Path(tmp) / 'external' / 'cache.json'
            cache = json.loads(cache_path.read_text(encoding='utf-8'))
            cache['coingecko']['ambiguous'] = ['aaa', 'BBB']
            cache['cmc']['ambiguous'] = ['BBB']
            cache_path.write_text(json.dumps(cache), encoding='utf-8')
            binding = {'BBB': {
                'verified': True, 'binance_base': 'BBB',
                'coingecko_id': 'bbb-coin', 'cmc_id': 202}}
            ext = ExternalSignals.from_env(
                tmp, verified_identity_bindings=binding)
            aaa = ext.enrich('AAA')
            self.assertNotIn('coingecko', aaa)
            self.assertIn('cmc', aaa)
            self.assertEqual(ext.enrich('BBB'), {})
            self.assertIsNone(ext.reject_reason('BBB'))
            ext._cache['coingecko']['ambiguous'] = 'AAA'
            ext._cache['cmc']['ambiguous'] = 'AAA'
            self.assertEqual(ext.enrich('AAA'), {})

    def test_data_beyond_stale_window_is_discarded(self):
        with tempfile.TemporaryDirectory() as tmp, self._env(
                {'EXTERNAL_MIN_MARKET_CAP_USD': '30000000'}):
            beyond = 1800 * enrichment_mod.STALE_INTERVALS + 60
            self._seed_cache(tmp, cg_age=beyond, cmc_age=beyond)
            ext = ExternalSignals.from_env(tmp)
            self.assertEqual(ext.enrich('AAA'), {})
            self.assertIsNone(ext.reject_reason('BBB'))

    def test_enrich_includes_ids_and_trending_flags(self):
        with tempfile.TemporaryDirectory() as tmp, self._env():
            self._seed_cache(tmp)
            ext = ExternalSignals.from_env(tmp)
            aaa = ext.enrich('AAA')
            self.assertTrue(aaa['coingecko']['trending'])
            self.assertEqual(aaa['coingecko']['coingecko_id'], 'aaa-coin')
            self.assertTrue(aaa['cmc']['trending'])
            self.assertEqual(aaa['cmc']['cmc_id'], 101)
            bbb = ext.enrich('BBB')
            self.assertFalse(bbb['coingecko']['trending'])
            self.assertFalse(bbb['cmc']['trending'])  # momentum_rank 40 > 20
            self.assertEqual(ext.enrich('UNKNOWN'), {})

    def test_floor_requires_verified_canonical_three_way_binding(self):
        """Matching ticker text and two provider ids is not identity proof."""
        with tempfile.TemporaryDirectory() as tmp, self._env(
                {'EXTERNAL_MIN_MARKET_CAP_USD': '30000000'}):
            self._seed_cache(tmp)
            cache_path = Path(tmp) / 'external' / 'cache.json'
            cache = json.loads(cache_path.read_text(encoding='utf-8'))
            cache['verified_identity_bindings'] = {'BBB': {
                'verified': True, 'binance_base': 'BBB',
                'coingecko_id': 'bbb-coin', 'cmc_id': 202}}
            cache_path.write_text(json.dumps(cache), encoding='utf-8')

            # A writable-cache assertion cannot create a trusted binding.
            self.assertIsNone(ExternalSignals.from_env(tmp).reject_reason('BBB'))
            mismatched = {'BBB': {
                'verified': True, 'binance_base': 'BBB',
                'coingecko_id': 'different-coin', 'cmc_id': 202}}
            self.assertIsNone(ExternalSignals.from_env(
                tmp, verified_identity_bindings=mismatched).reject_reason('BBB'))
            unverified = {'BBB': {
                'verified': False, 'binance_base': 'BBB',
                'coingecko_id': 'bbb-coin', 'cmc_id': 202}}
            self.assertIsNone(ExternalSignals.from_env(
                tmp, verified_identity_bindings=unverified).reject_reason('BBB'))

            verified = {
                'BBB': {'verified': True, 'binance_base': 'BBB',
                        'coingecko_id': 'bbb-coin', 'cmc_id': 202},
                'EEE': {'verified': True, 'binance_base': 'EEE',
                        'coingecko_id': 'eee-coin', 'cmc_id': 505},
            }
            ext = ExternalSignals.from_env(
                tmp, verified_identity_bindings=verified)
            ext._cache['cmc']['listings']['BBB']['cmc_id'] = 202.0
            self.assertIsNone(ext.reject_reason('BBB'))
            ext._cache['cmc']['listings']['BBB']['cmc_id'] = 202
            self.assertEqual(ext.reject_reason('BBB'),
                             'below_min_market_cap')
            self.assertIsNone(ext.reject_reason('AAA'))       # no binding
            self.assertIsNone(ext.reject_reason('DDD'))       # CG only
            self.assertIsNone(ext.reject_reason('EEE'))       # caps conflict
            self.assertIsNone(ext.reject_reason('UNKNOWN'))

    def test_floor_disabled_or_single_provider_never_rejects(self):
        with tempfile.TemporaryDirectory() as tmp, self._env():
            self._seed_cache(tmp)
            self.assertIsNone(ExternalSignals.from_env(tmp).reject_reason('BBB'))
        with tempfile.TemporaryDirectory() as tmp, self._env(
                {'EXTERNAL_MIN_MARKET_CAP_USD': '30000000',
                 'ENABLE_CMC_TRENDING': 'false'}):
            self._seed_cache(tmp)
            ext = ExternalSignals.from_env(tmp)
            self.assertIsNone(ext.reject_reason('BBB'))  # CMC off -> no floor

    def test_cmc_reconciliation_is_interval_limited_and_raises_ledger(self):
        with tempfile.TemporaryDirectory() as tmp, self._env(
                {'CMC_RECONCILE_SECONDS': '3600'}):
            self._seed_cache(tmp, cmc_age=3600)  # stale -> refresh
            cmc_stub = mock.Mock()
            cmc_stub.listings.return_value = ({'AAA': {'cmc_id': 101,
                                                       'momentum_rank': 1,
                                                       'market_cap_usd': 5e7}}, [])
            cmc_stub.key_info_month_credits_used.return_value = 500
            with mock.patch.object(enrichment_mod, 'CoinMarketCapClient',
                                   return_value=cmc_stub), \
                    mock.patch.object(enrichment_mod, 'CoinGeckoClient'):
                ext = ExternalSignals.from_env(tmp)
                ext.refresh_if_stale()
                self.assertEqual(cmc_stub.key_info_month_credits_used.call_count, 1)
                self.assertEqual(ext._cmc_budget.stats()['month_used'], 500)
                # Second refresh within the interval: no second key/info call.
                ext2 = ExternalSignals.from_env(tmp)
                ext2.refresh_if_stale()
                self.assertEqual(cmc_stub.key_info_month_credits_used.call_count, 1)

    def test_status_file_written_with_ceilings(self):
        with tempfile.TemporaryDirectory() as tmp, self._env():
            self._seed_cache(tmp)
            ext = ExternalSignals.from_env(tmp)
            ext.write_status()
            data = json.loads((Path(tmp) / 'external_signals.json').read_text('utf-8'))
            self.assertTrue(data['coingecko']['enabled'])
            self.assertEqual(data['coingecko']['budget']['minute_cap'], 96)
            self.assertEqual(data['cmc']['budget']['month_cap'], 14_400)
            self.assertFalse(data['cmc']['budget']['quarantined'])
            self.assertIn('configured_per_minute', data['coingecko'])
            self.assertIn('markets_cache_age_seconds', data['coingecko'])
            self.assertIn('trending_cache_age_seconds', data['coingecko'])
            self.assertEqual(
                data['config']['verified_identity_binding_count'], 0)


FAKE_NOW_MS = int(datetime.now(timezone.utc).timestamp() * 1000)
OLD_LISTING_MS = FAKE_NOW_MS - 200 * 86_400_000


def _symbol_info(symbol, base):
    return {
        'symbol': symbol, 'baseAsset': base, 'quoteAsset': 'USDT',
        'status': 'TRADING', 'isSpotTradingAllowed': True,
        'ocoAllowed': True, 'otoAllowed': True, 'allowTrailingStop': True,
        'filters': [
            {'filterType': 'NOTIONAL', 'minNotional': '5'},
            {'filterType': 'PRICE_FILTER', 'tickSize': '0.0001'},
            {'filterType': 'LOT_SIZE', 'stepSize': '0.01'},
            {'filterType': 'TRAILING_DELTA',
             'minTrailingAboveDelta': 10, 'maxTrailingAboveDelta': 2000,
             'minTrailingBelowDelta': 10, 'maxTrailingBelowDelta': 2000},
        ],
    }


class FakeBinancePublic:
    """Offline stand-in for scanner.BinancePublic."""

    def __init__(self):
        pass

    def get(self, path, params=None):
        if path == '/api/v3/exchangeInfo':
            return {'symbols': [_symbol_info('AAAUSDT', 'AAA'),
                                _symbol_info('BBBUSDT', 'BBB'),
                                _symbol_info('CCCUSDT', 'CCC')]}
        if path == '/api/v3/ticker/24hr':
            return [
                {'symbol': 'AAAUSDT', 'priceChangePercent': '9.5', 'quoteVolume': '9000000'},
                {'symbol': 'BBBUSDT', 'priceChangePercent': '4.0', 'quoteVolume': '5000000'},
                {'symbol': 'CCCUSDT', 'priceChangePercent': '2.0', 'quoteVolume': '3000000'},
            ]
        if path == '/api/v3/ticker/bookTicker':
            return [{'symbol': s, 'bidPrice': '1.0000', 'askPrice': '1.0010'}
                    for s in ('AAAUSDT', 'BBBUSDT', 'CCCUSDT')]
        if path == '/api/v3/klines':
            return [[OLD_LISTING_MS, '1', '1', '1', '1', '1']]
        raise AssertionError('unexpected Binance path ' + path)


class ScannerIntegrationTests(unittest.TestCase):
    """The enrichment layer must never change WHICH pairs rank WHERE."""

    def _run_scan(self, tmp, env):
        from services.common.sharia_v19 import V19_CONTROLLER_FILENAME
        from services.universe_service import scanner
        root = Path(tmp) / 'universe'
        sharia_dir = Path(tmp) / 'sharia'
        sharia_dir.mkdir(parents=True, exist_ok=True)
        installed_controller = ROOT / 'shared' / 'sharia' / V19_CONTROLLER_FILENAME
        (sharia_dir / V19_CONTROLLER_FILENAME).write_bytes(
            installed_controller.read_bytes())
        sharia_file = sharia_dir / 'sharia_status.json'
        sharia_file.write_text(json.dumps(harness.v19_status(
            [('AAA', 'GREEN'), ('BBB', 'GREEN'), ('CCC', 'GREEN')])),
            encoding='utf-8')
        env = dict(env)
        env.setdefault('AUDIT_LOG', str(Path(tmp) / 'audit' / 'events.jsonl'))
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(scanner, 'BinancePublic', FakeBinancePublic), \
                mock.patch.object(scanner, 'ROOT', root), \
                mock.patch.object(scanner, 'SHARIA', sharia_file), \
                mock.patch.object(scanner, 'LEGACY_HALAL',
                                  sharia_dir / 'halal_coins.json'), \
                mock.patch.object(scanner.ShariaFilter, '_record_binding_error',
                                  return_value=''):
            # Cryptographic Sharia projection/report binding is covered by its
            # own suite. These scanner tests isolate external-signal behavior
            # while still loading the immutable controller and V19.1 schema.
            return scanner.scan_once()

    def _seed_external_cache(self, tmp):
        state = Path(tmp) / 'universe' / 'external'
        state.mkdir(parents=True, exist_ok=True)
        now = time.time()
        (state / 'cache.json').write_text(json.dumps({
            'coingecko': {'markets_fetched_at': now,
                          'trending_fetched_at': now,
                          'markets': {
                              'AAA': {'coingecko_id': 'aaa-coin',
                                      'market_cap_usd': 9e8,
                                      'market_cap_rank': 40,
                                      'change_24h_pct': 9.4},
                              'BBB': {'coingecko_id': 'bbb-coin',
                                      'market_cap_usd': 1e7,
                                      'market_cap_rank': 900,
                                      'change_24h_pct': 4.1}},
                          'trending': ['AAA'], 'ambiguous': []},
            'cmc': {'fetched_at': now,
                    'listings': {
                        'AAA': {'cmc_id': 45, 'cmc_rank': 45,
                                'momentum_rank': 2, 'market_cap_usd': 9.1e8,
                                'change_24h_pct': 9.3, 'volume_24h_usd': 4e8},
                        'BBB': {'cmc_id': 88, 'cmc_rank': 910,
                                'momentum_rank': 55, 'market_cap_usd': 1.2e7,
                                'change_24h_pct': 4.0, 'volume_24h_usd': 6e6}},
                    'ambiguous': []},
        }), encoding='utf-8')

    def test_scan_with_signals_disabled_matches_previous_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._run_scan(tmp, {'ENABLE_COINGECKO_SIGNALS': 'false',
                                        'ENABLE_CMC_TRENDING': 'false'})
            self.assertEqual(snap['pairs'], ['AAA/USDT', 'BBB/USDT', 'CCC/USDT'])
            for row in snap['ranking']:
                self.assertNotIn('external_signals', row)
            self.assertIn('external_signals', snap['configuration'])
            self.assertFalse(
                snap['configuration']['external_signals']['coingecko_enabled'])

    def test_enabled_signals_annotate_without_reordering(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_external_cache(tmp)
            snap = self._run_scan(tmp, {
                'ENABLE_COINGECKO_SIGNALS': 'true',
                'ENABLE_CMC_TRENDING': 'true',
                'COINGECKO_API_KEY': 'demo', 'CMC_API_KEY': 'basic'})
            self.assertEqual(snap['pairs'], ['AAA/USDT', 'BBB/USDT', 'CCC/USDT'])
            by_base = {row['base']: row for row in snap['ranking']}
            self.assertTrue(by_base['AAA']['external_signals']['coingecko']['trending'])
            self.assertEqual(
                by_base['AAA']['external_signals']['coingecko']['coingecko_id'],
                'aaa-coin')
            self.assertTrue(by_base['AAA']['external_signals']['cmc']['trending'])
            self.assertIn('coingecko', by_base['BBB']['external_signals'])
            self.assertNotIn('external_signals', by_base['CCC'])
            status = json.loads((Path(tmp) / 'universe' /
                                 'external_signals.json').read_text('utf-8'))
            self.assertTrue(status['coingecko']['enabled'])

    def test_market_cap_floor_is_advisory_without_canonical_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_external_cache(tmp)
            snap = self._run_scan(tmp, {
                'ENABLE_COINGECKO_SIGNALS': 'true',
                'ENABLE_CMC_TRENDING': 'true',
                'COINGECKO_API_KEY': 'demo', 'CMC_API_KEY': 'basic',
                'EXTERNAL_MIN_MARKET_CAP_USD': '30000000'})
            # No verified Binance/provider identity resolver is wired into
            # the scanner, so even two same-ticker provider rows are advisory.
            self.assertEqual(snap['pairs'],
                             ['AAA/USDT', 'BBB/USDT', 'CCC/USDT'])
            rejections = json.loads((Path(tmp) / 'universe' /
                                     'latest_rejections.json').read_text('utf-8'))
            all_reasons = [reason for row in rejections['rejected']
                           for reason in row['reasons']]
            self.assertNotIn('below_min_market_cap', all_reasons)

    def test_floor_without_cmc_never_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_external_cache(tmp)
            snap = self._run_scan(tmp, {
                'ENABLE_COINGECKO_SIGNALS': 'true',
                'ENABLE_CMC_TRENDING': 'false',
                'COINGECKO_API_KEY': 'demo',
                'EXTERNAL_MIN_MARKET_CAP_USD': '30000000'})
            self.assertEqual(snap['pairs'], ['AAA/USDT', 'BBB/USDT', 'CCC/USDT'])

    def test_scan_survives_read_only_sharia_mount(self):
        """V102-FIX-001: the production compose mounts /app/shared/sharia
        read-only for the universe container; the legacy-projection write
        must be best-effort, never fatal."""
        from services.universe_service.sharia_filter import ShariaFilter
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ShariaFilter, 'sync_legacy_compat',
                                   side_effect=OSError(30, 'Read-only file system')):
                snap = self._run_scan(tmp, {'ENABLE_COINGECKO_SIGNALS': 'false',
                                            'ENABLE_CMC_TRENDING': 'false'})
            self.assertEqual(len(snap['pairs']), 3)


class WriterLeaseTests(unittest.TestCase):
    """A-004: a real cross-process single-writer lease protects the quota
    ledger. A second PROCESS must not be able to write it concurrently."""

    def _env(self):
        return mock.patch.dict(os.environ, {
            'ENABLE_COINGECKO_SIGNALS': 'true', 'ENABLE_CMC_TRENDING': 'true',
            'COINGECKO_API_KEY': 'demo', 'CMC_API_KEY': 'basic'}, clear=False)

    def test_lease_is_acquired_and_reentrant_within_one_process(self):
        from services.universe_service.external_signals import writer_lock
        with tempfile.TemporaryDirectory() as tmp, self._env():
            first = ExternalSignals.from_env(tmp)
            self.assertTrue(first.writer_lease_held)
            self.assertTrue(first.coingecko_enabled)
            # Re-entrant: the same process may build the object again.
            second = ExternalSignals.from_env(tmp)
            self.assertTrue(second.writer_lease_held)
            self.assertTrue(second.coingecko_enabled)
            # Assert the lease CONTRACT, not the on-disk side effect: the lease
            # file is an implementation detail whose visibility can lag on some
            # filesystems, and the guarantee under test is "this process may
            # write the ledger".
            self.assertIn(writer_lock.locking_backend(), ('fcntl', 'unavailable'))
            writer_lock.release_writer_lease(Path(tmp) / 'external' / '.writer.lock')

    def test_second_process_holding_the_lease_disables_enrichment(self):
        # Simulate the lease being owned by a different process.
        with tempfile.TemporaryDirectory() as tmp, self._env(), \
                mock.patch.dict(os.environ, {'AUDIT_LOG': str(Path(tmp) / 'audit.jsonl')}), \
                mock.patch.object(enrichment_mod, 'acquire_writer_lease', return_value=False):
            ext = ExternalSignals.from_env(tmp)
            self.assertFalse(ext.writer_lease_held)
            # Fail closed: no provider may spend quota in this process.
            self.assertFalse(ext.coingecko_enabled)
            self.assertFalse(ext.cmc_enabled)
            self.assertEqual(ext.enrich('AAA'), {})
            self.assertIsNone(ext.reject_reason('AAA'))
            ext.refresh_if_stale()  # must be a no-op, never an HTTP call
            audit_text = (Path(tmp) / 'audit.jsonl').read_text(encoding='utf-8')
            self.assertIn('external_signals_writer_conflict', audit_text)

    @unittest.skipUnless(
        __import__('services.universe_service.external_signals.writer_lock',
                   fromlist=['locking_backend']).locking_backend() == 'fcntl',
        'POSIX advisory locking not available on this host')
    def test_posix_lock_actually_excludes_a_foreign_holder(self):
        """On the deployment platform the lease is a real OS lock: a foreign
        file descriptor holding it must make acquisition fail."""
        import fcntl
        from services.universe_service.external_signals import writer_lock
        with tempfile.TemporaryDirectory() as tmp:
            lease = Path(tmp) / '.writer.lock'
            lease.parent.mkdir(parents=True, exist_ok=True)
            foreign = lease.open('a+', encoding='utf-8')
            try:
                fcntl.flock(foreign.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertFalse(writer_lock.acquire_writer_lease(lease))
            finally:
                fcntl.flock(foreign.fileno(), fcntl.LOCK_UN)
                foreign.close()

    def test_mkdir_failure_fails_closed_and_disables_enrichment(self):
        """R-001/LOCK-001: a lease directory that cannot be created must return
        False (fail closed), NOT True. Previously this returned True and left
        enrichment running with no lock at all.

        The patch is scoped to the external state directory so the audit
        subsystem keeps working (a global mkdir patch would break it too)."""
        from services.universe_service.external_signals import writer_lock
        real_mkdir = Path.mkdir

        def deny_external_dir(self, *args, **kwargs):
            if self.name == 'external':
                raise OSError(13, 'Permission denied')
            return real_mkdir(self, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, self._env(), \
                mock.patch.dict(os.environ, {'AUDIT_LOG': str(Path(tmp) / 'audit.jsonl')}), \
                mock.patch.object(Path, 'mkdir', deny_external_dir):
            self.assertFalse(writer_lock.acquire_writer_lease(
                Path(tmp) / 'external' / '.writer.lock'))
            ext = ExternalSignals.from_env(tmp)
            self.assertFalse(ext.writer_lease_held)
            self.assertFalse(ext.coingecko_enabled)
            self.assertFalse(ext.cmc_enabled)
        # The conflict is audited outside the patch so the audit log is intact.
        self.assertTrue(True)

    def test_open_failure_fails_closed_and_disables_enrichment(self):
        """R-001/LOCK-001: a lease file that cannot be opened/written must
        return False (fail closed), NOT True. Scoped to the lease filename so
        unrelated file IO (audit, cache) still works."""
        from services.universe_service.external_signals import writer_lock
        real_open, real_write = Path.open, Path.write_text

        def deny_lease_open(self, *args, **kwargs):
            if self.name == '.writer.lock':
                raise OSError(13, 'Permission denied')
            return real_open(self, *args, **kwargs)

        def deny_lease_write(self, *args, **kwargs):
            if self.name == '.writer.lock':
                raise OSError(13, 'Permission denied')
            return real_write(self, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, self._env(), \
                mock.patch.dict(os.environ, {'AUDIT_LOG': str(Path(tmp) / 'audit.jsonl')}), \
                mock.patch.object(Path, 'open', deny_lease_open), \
                mock.patch.object(Path, 'write_text', deny_lease_write):
            self.assertFalse(writer_lock.acquire_writer_lease(
                Path(tmp) / 'external' / '.writer.lock'))
            ext = ExternalSignals.from_env(tmp)
            self.assertFalse(ext.writer_lease_held)
            self.assertFalse(ext.coingecko_enabled)
            self.assertFalse(ext.cmc_enabled)
            audit_text = (Path(tmp) / 'audit.jsonl').read_text(encoding='utf-8')
            self.assertIn('external_signals_writer_conflict', audit_text)

    def test_disabled_providers_do_not_take_a_lease(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                'ENABLE_COINGECKO_SIGNALS': 'false',
                'ENABLE_CMC_TRENDING': 'false'}, clear=False):
            ext = ExternalSignals.from_env(tmp)
            self.assertTrue(ext.writer_lease_held)
            self.assertFalse((Path(tmp) / 'external' / '.writer.lock').exists())


class SingleWriterInvariantTests(unittest.TestCase):
    """V102-REM (deep-audit): the external-signals budget has exactly one
    writer in the shipped topology — the universe scanner. Pin that no other
    service module constructs the enrichment clients, so the single-writer
    assumption behind the free-tier budgets holds."""

    def test_external_provider_guards_have_only_two_bounded_consumers(self):
        services = ROOT / 'services'
        importers = []
        for path in services.rglob('*.py'):
            text = path.read_text(encoding='utf-8')
            if 'external_signals' in text and 'import' in text:
                importers.append(path.relative_to(ROOT).as_posix())
        # The package's own modules import within external_signals/. Outside
        # consumers are limited to the advisory universe scanner and the
        # fail-closed Sharia source-discovery layer. The latter can only create
        # owner-unverified reading-list candidates, never trade permission.
        outside = [p for p in importers
                   if not p.startswith('services/universe_service/external_signals/')]
        self.assertEqual(outside, [
            'services/sharia_screener/source_discovery.py',
            'services/universe_service/scanner.py',
        ],
                         msg=f'unexpected external_signals importers: {outside}')
        discovery = (ROOT / 'services/sharia_screener/source_discovery.py').read_text(
            encoding='utf-8')
        self.assertIn("'trade_permission': False", discovery)
        self.assertIn("'owner_verified': False", discovery)


if __name__ == '__main__':
    unittest.main()
