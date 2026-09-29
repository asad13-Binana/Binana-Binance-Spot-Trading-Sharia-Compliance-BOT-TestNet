"""SQLite owns lifecycle events; JSONL is a recoverable, idempotent projection."""
from pathlib import Path
import hashlib, json, os, shutil, sqlite3, time, math

def _clean(x):
    if isinstance(x,float) and not math.isfinite(x): return None
    if isinstance(x,dict):return {str(k):_clean(v) for k,v in x.items()}
    if isinstance(x,(list,tuple)):return [_clean(v) for v in x]
    return x

def identity(record):
    typ=str(record.get("type") or "").upper()
    scope=str(record.get("scope") or "binana-20260910-owner-cutover-1")
    pair=str(record.get("pair") or "").upper()
    if typ in {"STRATEGY_SIGNAL","RAW_STRATEGY_CANDIDATE"}:
        key=(scope,"RAW_STRATEGY_CANDIDATE",pair,str(record.get("candle_time")),str(record.get("entry_tag")))
    elif typ in {"ENTRY_FILL","EXIT_FILL"} and record.get("order_id"):
        from decimal import Decimal
        qty=str(Decimal(str(record.get("amount") or "0")).normalize())
        key=(scope,typ,pair,str(record.get("trade_id")),str(record["order_id"]),qty)
    elif typ in {"ADMISSION_ACCEPTED","ADMISSION_REJECTED"}:
        key=(scope,typ,pair,str(record.get("candle_time")),str(record.get("entry_tag")),str(record.get("reason")))
    else:
        stable={k:v for k,v in record.items() if k not in {"ts","source","event_id","_new_event"}}
        if not record.get("order_id") and not record.get("event_time") and not record.get("candle_time"):
            stable["observation_bucket"]=int(float(record.get("ts") or 0)//300)
        key=(scope,typ,pair,stable)
    return hashlib.sha256(json.dumps(key,sort_keys=True,default=str,separators=(",",":")).encode()).hexdigest()

def _connect(path):
    con=sqlite3.connect(str(path),timeout=10,isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("CREATE TABLE IF NOT EXISTS lifecycle_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS projection(singleton INTEGER PRIMARY KEY CHECK(singleton=1),seq INTEGER NOT NULL,offset INTEGER NOT NULL)")
    return con

def _encode(record):
    payload=_clean(dict(record));payload.pop("_new_event",None)
    payload.setdefault("ts",time.time())
    payload["event_id"]=identity(payload)
    return payload,json.dumps(payload,default=str,sort_keys=True,separators=(",",":"))

def _initialize(con,path):
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT 1 FROM projection").fetchone():
            con.commit();return
        if path.exists() and path.stat().st_size:
            raw=path.read_bytes()
            digest=hashlib.sha256(raw).hexdigest()[:16]
            backup=path.with_name(path.name+".legacy-"+digest)
            if not backup.exists():
                with backup.open("xb") as out:
                    out.write(raw);out.flush();os.fsync(out.fileno())
            for line in raw.splitlines():
                try:record=json.loads(line)
                except (ValueError,UnicodeError):continue
                if not isinstance(record,dict):continue
                if record.get("type") in {"ENTRY_FILL","EXIT_FILL"} and not record.get("order_id"):
                    record["type"]="LEGACY_"+record["type"]
                payload,encoded=_encode(record)
                con.execute("INSERT OR IGNORE INTO lifecycle_events(event_id,payload) VALUES(?,?)",(payload["event_id"],encoded))
        con.execute("INSERT INTO projection VALUES(1,0,0)")
        con.commit()
    except BaseException:
        con.rollback();raise

def project(con,path,after_append=None):
    while True:
        con.execute("BEGIN IMMEDIATE")
        try:
            seq,offset=con.execute("SELECT seq,offset FROM projection WHERE singleton=1").fetchone()
            rows=con.execute("SELECT seq,payload FROM lifecycle_events WHERE seq>? ORDER BY seq LIMIT 500",(seq,)).fetchall()
            if not rows:
                con.commit();return
            data=("".join(r[1]+"\n" for r in rows)).encode()
            with path.open("r+b" if path.exists() else "w+b") as fh:
                fh.seek(0,2)
                if fh.tell()<offset:
                    # Rebuild a removed/truncated projection from canonical SQLite.
                    con.execute("UPDATE projection SET seq=0,offset=0 WHERE singleton=1")
                    con.commit();continue
                fh.seek(offset)
                if fh.read(len(data))!=data:
                    fh.seek(offset);fh.write(data)
                fh.truncate(offset+len(data));fh.flush();os.fsync(fh.fileno())
            if after_append:after_append()
            con.execute("UPDATE projection SET seq=?,offset=? WHERE singleton=1",(rows[-1][0],offset+len(data)))
            con.commit()
        except BaseException:
            con.rollback();raise

def append_event(path,record,after_append=None):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    payload,encoded=_encode(record)
    con=_connect(path.with_suffix(".sqlite"))
    try:
        _initialize(con,path)
        con.execute("BEGIN IMMEDIATE")
        cursor=con.execute("INSERT OR IGNORE INTO lifecycle_events(event_id,payload) VALUES(?,?)",(payload["event_id"],encoded))
        inserted=bool(cursor.rowcount);con.commit()
        project(con,path,after_append)
        return {**payload,"_new_event":inserted}
    finally:con.close()

