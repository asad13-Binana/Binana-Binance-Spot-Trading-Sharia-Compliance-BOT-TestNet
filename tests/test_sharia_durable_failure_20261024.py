"""Exercise durable worker failures, including the captured deployed service."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from tests import _harness  # noqa: F401
from services.sharia_screener import service
from services.sharia_screener import bridge
from services.common import envelope
from services.sharia_screener.queue_store import QueueStore


@pytest.mark.parametrize('failure', [
    'ineligible', 'backend_unavailable', 'provider_error',
    'invalid_result', 'validation_error',
])
def test_failure_publication_error_does_not_strand_running(failure, tmp_path):
    module = service
    queue = QueueStore(tmp_path / 'queue.sqlite')
    assert queue.enqueue('manual-ETH-test', 'ETH', 'ETH/USDT', 'manual', 'owner')
    row = queue.next_request()
    worker = module.ShariaScreenerService.__new__(module.ShariaScreenerService)
    worker.queue = queue
    worker.source_discovery = None
    worker.max_scans_per_day = 100
    worker.max_scans_per_base_per_day = 4
    worker.max_scans_per_actor_per_day = 100
    worker.min_between_scans = 1
    worker.idle_retry_base_seconds = 300
    worker.idle_retry_max_seconds = 300
    worker.idle_retry_max_attempts = 3
    worker._last_scan_at = 0
    report = {'final_code': 'NO_TRADE_INFO'}

    def run(*args, **kwargs):
        if failure == 'backend_unavailable':
            raise module.ScreeningUnavailable('backend unavailable')
        if failure == 'provider_error':
            raise ValueError('malformed provider response')
        return report, {}

    worker.runner = SimpleNamespace(run=run)
    validation_error = (
        module.ResultValidationError('incomplete evidence')
        if failure == 'invalid_result' else RuntimeError('validation failed')
        if failure == 'validation_error' else None)
    with mock.patch.object(worker, '_pair_eligible', return_value=(
            failure != 'ineligible', 'symbol unavailable', {})), \
            mock.patch.object(module, 'audit'), \
            mock.patch.object(module, 'validate_result', side_effect=validation_error), \
            mock.patch.object(module, 'write_screening_outcome',
                              side_effect=PermissionError('read-only result directory')):
        worker.process_request(row)

    # The real SQLite queue must retain the request and a meaningful terminal
    # failure even when neither reports nor signed results could be published.
    reopened = QueueStore(queue.db_path)
    assert reopened.counts() == {'FAILED': 1}
    assert reopened.last('FAILED')['request_id'] == 'manual-ETH-test'
    assert 'read-only result directory' in reopened.last('FAILED')['error']
    assert reopened.cost_today() == 1
    assert reopened.requeue_running() == 0


def _inbox_request(inbox, request_id, base='ETH', priority='manual'):
    inbox.mkdir(exist_ok=True)
    path = inbox / f'request_{request_id}.json'
    path.write_text(json.dumps(envelope.sign_envelope(
        producer='telegram-broker', purpose=envelope.BUS_SHARIA_REQUEST,
        payload={'request_id': request_id, 'base': base,
                 'pair': f'{base}/USDT', 'priority': priority,
                 'requested_by': 'telegram-owner'}, ttl_seconds=600)))
    return path


def test_failed_bulk_expansion_preserves_authenticated_inbox_for_retry(tmp_path):
    inbox, processed = tmp_path / 'inbox', tmp_path / 'processed'
    request = _inbox_request(inbox, 'bulk-all-test', '*', 'bulk')
    worker = service.ShariaScreenerService.__new__(service.ShariaScreenerService)
    worker.queue = QueueStore(tmp_path / 'queue.sqlite')
    with mock.patch.object(service, 'SHARIA_QUEUE_INBOX', inbox), \
            mock.patch.object(service, 'SHARIA_QUEUE_PROCESSED', processed), \
            mock.patch.object(service, 'audit'), \
            mock.patch.object(worker, '_spot_usdt_bases',
                              side_effect=RuntimeError('metadata unavailable')):
        with pytest.raises(RuntimeError, match='metadata unavailable'):
            worker.ingest_requests()
    assert request.exists()
    assert not list(processed.glob('*.json'))
    assert worker.queue.counts() == {}


def test_suppressed_request_has_its_own_signed_failure_without_approval_mutation(tmp_path):
    inbox, processed = tmp_path / 'inbox', tmp_path / 'processed'
    reports, results = tmp_path / 'reports', tmp_path / 'results'
    status = tmp_path / 'sharia_status.json'
    status.write_bytes(b'{"private":"existing approval projection"}')
    worker = service.ShariaScreenerService.__new__(service.ShariaScreenerService)
    worker.queue = QueueStore(tmp_path / 'queue.sqlite')
    assert worker.queue.enqueue('manual-ETH-first', 'ETH', 'ETH/USDT', 'manual', 'owner')
    _inbox_request(inbox, 'manual-ETH-second')
    controller = Path(__file__).resolve().parents[1] / 'shared/sharia' / _harness.V19_CONTROLLER_FILENAME
    with mock.patch.object(service, 'SHARIA_QUEUE_INBOX', inbox), \
            mock.patch.object(service, 'SHARIA_QUEUE_PROCESSED', processed), \
            mock.patch.object(service, 'SHARIA_RESULTS_DIR', results), \
            mock.patch.object(service, 'audit'), \
            mock.patch.object(bridge, 'audit'), \
            mock.patch.object(bridge, 'SHARIA_REPORTS_DIR', reports), \
            mock.patch.object(bridge, 'SHARIA_RESULTS_DIR', results), \
            mock.patch.object(bridge, 'SHARIA_FILE', status), \
            mock.patch.object(bridge, 'SHARIA_CONTROLLER_FILE', controller):
        worker.ingest_requests()
        # A replay retries no research and cannot overwrite its terminal result.
        result_file = results / 'result_manual-ETH-second.json'
        before = result_file.read_bytes()
        _inbox_request(inbox, 'manual-ETH-second')
        worker.ingest_requests()
        assert result_file.read_bytes() == before
    reopened = QueueStore(worker.queue.db_path)
    assert reopened.counts() == {'QUEUED': 1, 'REJECTED': 1}
    failure = reopened.last('REJECTED')
    assert failure['request_id'] == 'manual-ETH-second'
    assert 'manual-ETH-first' in failure['error']
    payload = envelope.read_verified_file(
        result_file, purpose=envelope.BUS_SHARIA_RESULT,
        expected_producers={'sharia-screener'})
    assert payload['request_id'] == 'manual-ETH-second'
    assert payload['validated'] is False
    assert payload['final_code'] == 'NO_TRADE_INFO'
    assert status.read_bytes() == b'{"private":"existing approval projection"}'
    assert reopened.cost_today() == 0


def test_concurrent_same_base_requests_keep_one_job_and_both_correlations(tmp_path):
    queue = QueueStore(tmp_path / 'queue.sqlite')
    barrier = threading.Barrier(2)

    def enqueue(request_id):
        barrier.wait(timeout=5)
        return queue.enqueue(request_id, 'ETH', 'ETH/USDT', 'manual', 'owner')

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue, ['manual-ETH-a', 'manual-ETH-b']))
    assert sorted(results) == [False, True]
    reopened = QueueStore(queue.db_path)
    assert reopened.counts() == {'QUEUED': 1, 'REJECTED': 1}
    assert {reopened.request(request)['status']
            for request in ('manual-ETH-a', 'manual-ETH-b')} == {'QUEUED', 'REJECTED'}
    assert reopened.cost_today() == 0
