"""Durable, at-least-once Telegram delivery; no exchange access."""
import json
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import contextmanager


class Outbox:
    def __init__(self,path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS notification_outbox (id TEXT PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT "PENDING",attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL DEFAULT 0,lease TEXT,lease_until REAL NOT NULL DEFAULT 0,delivered_at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS telegram_chat_rate (chat TEXT PRIMARY KEY,next_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS notification_settings (name TEXT PRIMARY KEY,value REAL NOT NULL)')
            db.execute('INSERT OR IGNORE INTO notification_settings VALUES ("cutover",?)',(time.time(),))
            self.cutover=db.execute('SELECT value FROM notification_settings WHERE name="cutover"').fetchone()[0]

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db: yield db
        finally:
            db.close()

    def enqueue(self,key,text,chat):
        with self.connect() as db:
            # Preserve historical delivered identifiers during migration.
            legacy=db.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_notification_delivery'").fetchone()
            if legacy and db.execute('SELECT 1 FROM telegram_notification_delivery WHERE event_key=?',(key,)).fetchone():
                return False
            return db.execute('INSERT OR IGNORE INTO notification_outbox(id,payload) VALUES (?,?)',
                (key,json.dumps({'text':str(text),'chat':str(chat)}))).rowcount==1

    def reserve_chat(self,chat,*,now=None,interval=1.1):
        now=time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT next_at FROM telegram_chat_rate WHERE chat=?',(str(chat),)).fetchone()
            at=max(now,previous[0] if previous else now)
            db.execute('INSERT OR REPLACE INTO telegram_chat_rate VALUES (?,?)',(str(chat),at+interval))
        return max(0,at-now)

    def defer_chat(self,chat,seconds):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT next_at FROM telegram_chat_rate WHERE chat=?',(str(chat),)).fetchone()
            db.execute('INSERT OR REPLACE INTO telegram_chat_rate VALUES (?,?)',
                (str(chat),max(time.time()+max(0,float(seconds)),row[0] if row else 0)))

    def deliver_one(self,send,*,now=None):
        now=time.time() if now is None else now
        lease=uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM notification_outbox WHERE state!="DELIVERED" AND next_at<=? AND lease_until<=? ORDER BY rowid LIMIT 1',(now,now)).fetchone()
            if row is None: return False
            db.execute('UPDATE notification_outbox SET lease=?,lease_until=?,attempts=attempts+1 WHERE id=?',
                (lease,now+300,row['id']))
        try:
            payload=json.loads(row['payload']);response=send(payload['text'],payload['chat'])
            body=response.json() if hasattr(response,'json') else response
            if not isinstance(body,dict) or body.get('ok') is not True:
                raise RuntimeError('TELEGRAM_ACK_UNAVAILABLE')
        except Exception:
            with self.connect() as db:
                db.execute('UPDATE notification_outbox SET lease=NULL,lease_until=0,next_at=? WHERE id=? AND lease=?',
                    (now+min(60,2**min(row['attempts']+1,6)),row['id'],lease))
            return False
        with self.connect() as db:
            db.execute('UPDATE notification_outbox SET state="DELIVERED",delivered_at=?,lease=NULL,lease_until=0 WHERE id=? AND lease=?',
                (time.time(),row['id'],lease))
        return True
