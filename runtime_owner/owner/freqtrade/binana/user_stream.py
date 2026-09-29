"""Read-only authenticated Testnet events inside the Freqtrade owner.

Events are durably journalled. Freqtrade's normal REST reconciliation applies
fills; this thread never creates orders or changes Trade economics.
"""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import random
import threading
import time

from .durable_journal import append_event

ENDPOINT = 'wss://ws-api.testnet.binance.vision/ws-api/v3'


def health_is_fresh(health, *, now=None):
    now = time.time() if now is None else now
    try:
        return (health.get('owner') == 'FREQTRADE' and health.get('endpoint') == ENDPOINT
                and health.get('subscribed') is True and health.get('ok') is True
                and 0 <= now - float(health['last_verified_at']) <= 35
                and now < float(health['valid_until']))
    except (AttributeError, KeyError, ValueError, TypeError, OverflowError):
        return False


class PrivateUserStream:
    def __init__(self, api_key, secret, *, scope, runtime):
        self._api_key, self._secret = api_key, secret
        self.scope, self.runtime = scope, Path(runtime)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread = None
        self._loop = None
        self._task = None
        self._state = {'owner':'FREQTRADE', 'endpoint':ENDPOINT, 'scope':scope,
                       'ok':False, 'subscribed':False, 'reconnect_count':0}

    def _publish(self, **changes):
        with self._lock:
            self._state.update(changes, generated_at=time.time(), valid_until=time.time()+35)
            target = self.runtime / 'owner_user_stream_health.json'
            temporary = target.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(self._state, sort_keys=True))
            os.replace(temporary, target)

    def health(self):
        with self._lock:
            return dict(self._state)

    def _subscription(self):
        params = {'apiKey':self._api_key, 'timestamp':int(time.time()*1000), 'recvWindow':5000}
        payload = '&'.join(f'{key}={params[key]}' for key in sorted(params))
        params['signature'] = hmac.new(self._secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return {'id':'binana-owner-subscribe', 'method':'userDataStream.subscribe.signature', 'params':params}

    def _record_event(self, message, subscription):
        if message.get('subscriptionId') != subscription:
            raise RuntimeError('PRIVATE_SUBSCRIPTION_ID_MISMATCH')
        event = message.get('event') or {}
        if event.get('e') in {'eventStreamTerminated', 'serverShutdown'}:
            raise RuntimeError('PRIVATE_STREAM_TERMINATED')
        if event.get('e') == 'executionReport' and (event.get('i') is None or event.get('I') is None):
            raise RuntimeError('PRIVATE_EXECUTION_ID_MISSING')
        symbol = str(event.get('s') or '')
        pair = symbol[:-4]+'/USDT' if symbol.endswith('USDT') else symbol
        return append_event(self.runtime/'freqtrade_lifecycle_events.jsonl',
            {'scope':self.scope, 'type':'EXCHANGE_'+str(event.get('e','UNKNOWN')),
             'pair':pair, 'order_id':event.get('i'), 'event_time':event.get('E'),
             'execution_id':event.get('I'), 'event':event})

    async def _session(self):
        import websockets
        async with websockets.connect(ENDPOINT, open_timeout=10, close_timeout=5,
                                      ping_interval=20, ping_timeout=10, max_queue=256) as socket:
            await socket.send(json.dumps(self._subscription()))
            reply = json.loads(await asyncio.wait_for(socket.recv(),15))
            if reply.get('status') != 200 or 'subscriptionId' not in (reply.get('result') or {}):
                raise RuntimeError('PRIVATE_SUBSCRIPTION_REJECTED')
            subscription = reply['result']['subscriptionId']
            self._publish(ok=True, subscribed=True, last_verified_at=time.time(),
                          subscription_id=subscription, last_error_type=None)
            started = time.monotonic()
            while not self._stop.is_set() and time.monotonic()-started < 23*3600:
                try:
                    message = json.loads(await asyncio.wait_for(socket.recv(),15))
                except asyncio.TimeoutError:
                    pong = await socket.ping()
                    await asyncio.wait_for(pong,10)
                    self._publish(last_verified_at=time.time(), last_pong_at=time.time())
                    continue
                self._record_event(message, subscription)
                self._publish(last_verified_at=time.time(), last_event_at=time.time())

    async def _supervise(self):
        failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._session()
            except Exception as exc:
                self._publish(ok=False, subscribed=False, last_error_type=type(exc).__name__)
            if self._stop.is_set():
                break
            failures = 0 if time.monotonic()-started >= 60 else min(failures+1,6)
            self._publish(ok=False, subscribed=False,
                          reconnect_count=self._state['reconnect_count']+1)
            deadline = time.monotonic()+random.uniform(1,min(60,2**failures))
            while not self._stop.is_set() and time.monotonic()<deadline:
                await asyncio.sleep(0.25)

    async def _run(self):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        try:
            await self._supervise()
        except asyncio.CancelledError:
            pass
        finally:
            self._publish(ok=False, subscribed=False)
            self._task = None
            self._loop = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        if not self._api_key or not self._secret:
            raise ValueError('PRIVATE_STREAM_CREDENTIALS_UNAVAILABLE')
        self.runtime.mkdir(parents=True,exist_ok=True)
        self._stop.clear()
        self._thread=threading.Thread(target=lambda:asyncio.run(self._run()),
                                      name='binana-owner-user-stream',daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        if self._thread:
            self._thread.join(timeout=12)
            if self._thread.is_alive():
                raise RuntimeError('PRIVATE_STREAM_SHUTDOWN_INCOMPLETE')
