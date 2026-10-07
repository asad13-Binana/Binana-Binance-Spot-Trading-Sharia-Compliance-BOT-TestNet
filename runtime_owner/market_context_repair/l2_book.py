"""Bounded Binance Spot depth book with snapshot bridging and gap invalidation.

Protocol reference: Binance Spot WebSocket documentation and Cryptofeed's
Binance sequence-checking design. No credentials or order methods.
"""
from collections import deque
from decimal import Decimal
import time


def integer(value):
    if isinstance(value, bool):
        raise ValueError('Boolean sequence')
    result = int(value)
    if result < 0 or Decimal(str(value)) != result:
        raise ValueError('Invalid sequence')
    return result


def levels(values):
    parsed = []
    for row in values:
        p, q = Decimal(str(row[0])), Decimal(str(row[1]))
        if not p.is_finite() or not q.is_finite() or p <= 0 or q < 0:
            raise ValueError('Invalid depth level')
        parsed.append((p, q))
    return parsed


class DepthBook:
    def __init__(self, *, max_levels=1000, max_buffer=300, min_levels=20):
        self.max_levels, self.max_buffer, self.min_levels = max_levels, max_buffer, min_levels
        self.epoch = 0
        self.resets = 0
        self.pending = False
        self.retry_at = 0.0
        self.reset('NOT_INITIALISED')

    def reset(self, reason):
        self.epoch += 1
        self.resets += 1
        self.bids, self.asks = {}, {}
        self.sequence = None
        self.buffer = deque()
        self.ready = False
        self.reason = reason
        self.received_mono = None
        self.pending = False

    def delta(self, payload, now=None):
        now = time.monotonic() if now is None else now
        try:
            first, last = integer(payload['U']), integer(payload['u'])
            if first > last:
                raise ValueError('Reversed sequence interval')
            update = (first, last, levels(payload['b']), levels(payload['a']), now)
            if self.sequence is None:
                if len(self.buffer) >= self.max_buffer:
                    self.reset('BUFFER_OVERFLOW')
                self.buffer.append(update)
                return False
            return self._apply(update)
        except (ValueError, TypeError, KeyError, ArithmeticError, IndexError):
            self.reset('MALFORMED_DEPTH')
            return False

    def _apply(self, update):
        first, last, bids, asks, now = update
        if last <= self.sequence:
            return False
        if not first <= self.sequence + 1 <= last:
            self.reset('SEQUENCE_GAP')
            self.buffer.append(update)
            return False
        for changes, book in [(bids, self.bids), (asks, self.asks)]:
            for price, quantity in changes:
                if quantity:
                    book[price] = quantity
                else:
                    book.pop(price, None)
        if (min(len(self.bids), len(self.asks)) < self.min_levels or
                max(len(self.bids), len(self.asks)) > self.max_levels):
            self.reset('DEPTH_COVERAGE_RESET')
            return False
        if (sorted(self.bids,reverse=True)[self.min_levels-1] < self.bid_floor or
                sorted(self.asks)[self.min_levels-1] > self.ask_ceiling):
            self.reset('SNAPSHOT_COVERAGE_EXHAUSTED')
            return False
        if max(self.bids) >= min(self.asks):
            self.reset('CROSSED_BOOK')
            return False
        self.sequence = last
        self.ready, self.reason, self.received_mono = True, 'SEQUENCE_VERIFIED', now
        return True

    def snapshot(self, payload, epoch):
        if epoch != self.epoch:
            return False
        try:
            seq = integer(payload['lastUpdateId'])
            bids = {p: q for p, q in levels(payload['bids']) if q}
            asks = {p: q for p, q in levels(payload['asks']) if q}
            if (min(len(bids), len(asks)) < self.min_levels or
                    max(len(bids), len(asks)) > self.max_levels or max(bids) >= min(asks)):
                raise ValueError('Invalid snapshot')
            buffered = list(self.buffer)
            self.bid_floor, self.ask_ceiling = min(bids), max(asks)
            self.bids, self.asks, self.sequence = bids, asks, seq
            self.buffer.clear()
            self.pending = False
            self.ready, self.reason = False, 'WAITING_FOR_SEQUENCE_BRIDGE'
            for update in buffered:
                if self.sequence is None:
                    break
                self._apply(update)
            return self.ready
        except (ValueError, TypeError, KeyError, ArithmeticError, IndexError):
            self.reset('INVALID_SNAPSHOT')
            return False

    def view(self, now=None, max_age_ms=15000):
        now = time.monotonic() if now is None else now
        age = None if self.received_mono is None else max(0, (now - self.received_mono) * 1000)
        valid = self.ready and age is not None and age <= max_age_ms
        result = {'status': 'fresh' if valid else 'unavailable', 'reason': self.reason,
            'sequence_verified': valid, 'last_update_id': self.sequence,
            'depth_age_ms': age, 'reset_count': self.resets, 'update_interval_ms': 100}
        if valid:
            bids = sorted(self.bids.items(), reverse=True)[:20]
            asks = sorted(self.asks.items())[:20]
            b = sum(p*q for p, q in bids)
            a = sum(p*q for p, q in asks)
            result.update(bid_depth_20_quote=str(b), ask_depth_20_quote=str(a),
                imbalance_20_quote=str((b-a)/(b+a)), best_bid=str(bids[0][0]), best_ask=str(asks[0][0]))
        return result
