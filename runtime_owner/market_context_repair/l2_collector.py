"""One bounded public REST snapshot worker for multiplexed Spot depth streams."""
import threading
import time
from .l2_book import DepthBook


class DepthCollector:
    def __init__(self):
        self.books={};self.lock=threading.RLock();self.stop_event=threading.Event()
        self.thread=None;self.requests=0;self.errors=0

    def set_symbols(self,symbols):
        with self.lock:
            for symbol in symbols:
                self.books.setdefault(symbol,DepthBook(max_levels=512,max_buffer=100))
            for symbol in set(self.books)-set(symbols):del self.books[symbol]

    def reset(self):
        with self.lock:
            for book in self.books.values():book.reset('CONNECTION_RESET')

    def ingest(self,payload,now):
        with self.lock:
            book=self.books.get(payload.get('s'))
            if book is None:return False
            try:
                age=time.time()*1000-int(payload['E'])
                if not -1000<=age<=5000:raise ValueError('depth event stale')
            except (ValueError,TypeError,KeyError):
                book.reset('DEPTH_EVENT_TIME_INVALID');return False
            return book.delta(payload,now)

    def view(self,symbol,now,max_age_ms):
        with self.lock:
            book=self.books.get(symbol)
            return book.view(now,max_age_ms) if book else {'status':'unavailable','sequence_verified':False}

    def _run(self):
        import requests
        session=requests.Session()
        try:
            while not self.stop_event.is_set():
                job=None
                with self.lock:
                    for symbol,book in self.books.items():
                        if book.sequence is None and book.buffer and not book.pending and time.monotonic()>=book.retry_at:
                            book.pending=True;job=(symbol,book,book.epoch);break
                if job is None:
                    self.stop_event.wait(0.1);continue
                symbol,book,epoch=job
                try:
                    self.requests+=1
                    response=session.get('https://data-api.binance.vision/api/v3/depth',params={'symbol':symbol,'limit':100},timeout=5)
                    response.raise_for_status();snapshot=response.json()
                    with self.lock:
                        if self.books.get(symbol) is book and book.epoch==epoch:
                            book.snapshot(snapshot,epoch);book.retry_at=time.monotonic()+1
                except Exception:
                    self.errors+=1
                    with self.lock:
                        if self.books.get(symbol) is book and book.epoch==epoch:
                            book.pending=False;book.reason='SNAPSHOT_UNAVAILABLE';book.retry_at=time.monotonic()+10
                self.stop_event.wait(0.25)
        finally:
            session.close()

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear();self.thread=threading.Thread(target=self._run,daemon=True,name='binana-depth-snapshots');self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=6)
