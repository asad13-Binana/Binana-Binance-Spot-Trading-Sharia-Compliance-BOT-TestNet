from __future__ import annotations

import json
import math
import re
import time
import os
import threading
import websocket

from services.telegram_broker import bot as base

PROVIDER_STATUS_FILE = base.Path('/app/shared/universe/external_signals.json')

ORIGINAL_SEND = base.send
ORIGINAL_HANDLE_MESSAGE = base.handle_message
ORIGINAL_ROUTE = base.route
ORIGINAL_CONFIRM_ACTION = base._confirm_action


def _tg_request(method, endpoint, *, data=None, timeout=15, attempts=4):
    last = None
    for attempt in range(max(1, int(attempts))):
        try:
            r = base.requests.request(method, base.BASE + endpoint, data=data, timeout=timeout)
            if r.status_code == 429:
                try:
                    retry_after = float((r.json().get('parameters') or {}).get('retry_after', 1))
                except Exception:
                    retry_after = 1.0
                time.sleep(min(8.0, max(0.5, retry_after)))
                last = RuntimeError('Telegram rate limited')
                continue
            if r.status_code in {500, 502, 503, 504}:
                time.sleep(min(4.0, 0.5 * (2 ** attempt)))
                last = RuntimeError(f'Telegram transient HTTP {r.status_code}')
                continue
            if 400 <= r.status_code < 500:
                try:
                    body = r.json()
                    description = str(body.get('description') or body.get('error_code') or 'client error')
                except Exception:
                    description = str(r.text or 'client error')
                raise RuntimeError(f'Telegram HTTP {r.status_code}: {description[:300]}')
            r.raise_for_status()
            return r
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(min(4.0, 0.5 * (2 ** attempt)))
    raise last or RuntimeError('Telegram request failed')

BUTTON_ACTIONS = {
    'Status': 'status',
    'Balance': 'balance',
    'Open Trades': 'open_trades',
    'Trade History': 'trade_history',
    'Last Signal': 'last_signal',
    'Recent Signals': 'signal_history',
    'Profit': 'profit',
    'Daily Report': 'daily_report',
    'Market Scanner': 'menu_market_scanner',
    'Halal List': 'menu_sharia',
    'Trading Universe': 'universe',
    'System Health': 'system_health',
    'Alerts': 'menu_alerts',
    'Protection': 'menu_protection',
    'Resume Entries': 'entries_on_confirm',
    'Pause Entries': 'entries_off_confirm',
    'Reconcile Status': 'reconcile',
    'Reconcile': 'reconcile',
    'System': 'menu_health',
    'Self-Test': 'selftest',
    'Logs': 'logs',
    'Emergency': 'menu_emergency',
    'Set Trade Amount': 'menu_sizing',
    'Check Coin Signal': 'check_signal_help',
    'Help': 'menu_help',
}

# ReplyKeyboard buttons arrive as ordinary message.text.  Telegram clients can
# retain an older persistent keyboard, so accept historical labels after
# normalization.  Sensitive legacy labels are deliberately routed to the
# existing confirmation screens, never to direct mutation actions.
LEGACY_TEXT_ACTIONS = {
    'status': 'status',
    'dashboard': 'status',
    'trading status': 'status',
    'current trading status': 'status',
    'private status': 'status',
    'check status': 'status',
    'balance': 'balance',
    'open trades': 'open_trades',
    'open orders': 'open_trades',
    'history': 'trade_history',
    'trade history': 'trade_history',
    'current signal': 'last_signal',
    'last signal': 'last_signal',
    'last strategy signal': 'last_signal',
    'latest signal': 'last_signal',
    'recent signals': 'signal_history',
    'rejected signals': 'signal_rejected',
    'profit': 'profit',
    'performance': 'profit',
    'pnl': 'profit',
    'p and l': 'profit',
    'pnl 24h': 'profit',
    'p and l 24h': 'profit',
    'daily report': 'daily_report',
    'market scanner': 'menu_market_scanner',
    'current spot universe': 'universe',
    'pair management': 'universe',
    'universe': 'universe',
    'halal list': 'menu_sharia',
    'view halal list': 'sharia',
    'sharia': 'menu_sharia',
    'sharia status': 'menu_sharia',
    'registry and scanner health': 'sharia_service',
    'check sharia': 'sharia_report_help',
    'health': 'system_health',
    'system health': 'system_health',
    'safety and health': 'system_health',
    'signal health': 'data_readiness',
    'alerts': 'menu_alerts',
    'delivery status': 'alert_status',
    'protection': 'menu_protection',
    'oco and trailing': 'protection_status',
    'resume entries': 'entries_on_confirm',
    'enable auto entries': 'entries_on_confirm',
    'resume signals': 'entries_on_confirm',
    'pause entries': 'entries_off_confirm',
    'pause auto entries': 'entries_off_confirm',
    'stop entries': 'entries_off_confirm',
    'stop new entries': 'entries_off_confirm',
    'reconcile': 'reconcile',
    'reconcile orders': 'reconcile',
    'system': 'menu_health',
    'bot controls': 'menu_controls',
    'controls': 'menu_controls',
    'auto trading': 'menu_autotrade',
    'self test': 'selftest',
    'release validation': 'selftest',
    'logs': 'logs',
    'recent audit': 'logs',
    'emergency': 'menu_emergency',
    'emergency stop': 'menu_emergency',
    'help': 'menu_help',
    'home': 'home',
    'coinmarketcap': 'provider_coinmarketcap',
    'coin gecko': 'provider_coingecko',
    'coingecko': 'provider_coingecko',
    'market context': 'market_context',
    'check market context': 'market_context',
    'spot context': 'market_context',
    'api readiness': 'data_readiness',
    'data freshness': 'data_readiness',
    'freshness': 'data_readiness',
    'deployment': 'deploy',
    'deployment info': 'deploy',
    'set trade amount': 'menu_sizing',
    'check coin signal': 'check_signal_help',
}



def normalize_button_text(text):
    value = str(text or '').casefold().replace('&', ' and ')
    return re.sub(r'[^a-z0-9]+', ' ', value).strip()


def resolve_button_action(text):
    exact = str(text or '').strip()
    return BUTTON_ACTIONS.get(exact) or LEGACY_TEXT_ACTIONS.get(
        normalize_button_text(exact)
    )

FIXED_KEYBOARD = [
    ['Status', 'Balance'],
    ['Open Trades', 'Trade History'],
    ['Last Signal', 'Recent Signals'],
    ['Profit', 'Daily Report'],
    ['Market Scanner', 'Halal List'],
    ['Trading Universe', 'System Health'],
    ['Alerts', 'Protection'],
    ['Resume Entries', 'Pause Entries'],
    ['Set Trade Amount', 'Check Coin Signal'],
    ['Reconcile Status', 'System'],
    ['Self-Test', 'Logs'],
    ['Emergency', 'Help'],
]
SHORTCUTS = {
    '/status': 'status',
    '/balance': 'balance',
    '/orders': 'open_trades',
    '/history': 'trade_history',
    '/lastsignal': 'last_signal',
    '/signals': 'signal_history',
    '/profit': 'profit',
    '/report': 'daily_report',
    '/universe': 'universe',
    '/sharia': 'menu_sharia',
    '/health': 'system_health',
    '/alerts': 'menu_alerts',
    '/protection': 'menu_protection',
    '/controls': 'menu_controls',
    '/autotrade': 'menu_autotrade',
    '/reconcile': 'reconcile',
    '/pause': 'entries_off_confirm',
    '/logs': 'logs',
    '/selftest': 'selftest',
    '/help': 'menu_help',
    '/daily': 'daily_report',
    '/stop': 'entries_off_confirm',
    '/rejected': 'signal_rejected',
    '/spotcontext': 'market_context',
    '/providers': 'provider_status',
    '/readiness': 'data_readiness',
    '/shariastatus': 'sharia_service',
    '/deploy': 'deploy',
    '/checksignal': 'check_signal_help',
}



def fixed_keyboard_markup():
    return {
        'keyboard': [[{'text': label} for label in row] for row in FIXED_KEYBOARD],
        'resize_keyboard': True,
        'is_persistent': True,
        'one_time_keyboard': False,
        'input_field_placeholder': 'BINANA TestNet controls',
    }


def send_fixed(text, chat_id=None, buttons=None):
    if not base.TOKEN:
        return None
    data = base._telegram_message_data(text, chat_id, buttons)
    if not buttons:
        data['reply_markup'] = json.dumps(fixed_keyboard_markup())
    return _tg_request('POST', '/sendMessage', data=data, timeout=15)


def show_panel(chat):
    base.send(
        'BINANA Spot TestNet control panel\n'
        'Persistent controls are active below the chat.\n'
        'Strategy and risk rules are unchanged.',
        chat,
    )

def register_commands():
    descriptions = {
        'menu':'Show BINANA TestNet controls','status':'Freqtrade trading status','balance':'TestNet account balance',
        'orders':'Open Freqtrade trades','history':'Trade history','lastsignal':'Latest strategy signal',
        'signals':'Recent strategy signals','profit':'Profit summary','report':'Daily report','daily':'Daily report',
        'universe':'Current eligible universe','sharia':'Halal list and research','shariastatus':'Sharia services status',
        'health':'System health','alerts':'Alert status','protection':'Protection status','controls':'Trading controls',
        'autotrade':'Fixed auto-trade settings','reconcile':'Canonical reconciliation','pause':'Pause new entries',
        'stop':'Pause new entries','logs':'Recent audit events','selftest':'Validation status','rejected':'Rejected signals',
        'spotcontext':'Spot market context','providers':'Market-data providers','readiness':'Data readiness','deploy':'Deployment info'
    }
    commands=[]
    for command in SHORTCUTS:
        name=command.lstrip('/')
        commands.append({'command':name,'description':descriptions.get(name, name.replace('_',' ').title())})
    response = _tg_request('POST','/setMyCommands',data={'commands':json.dumps(commands)},timeout=15)
    body=response.json()
    if body.get('ok') is not True:
        raise RuntimeError('Telegram setMyCommands returned failure')
    return commands


def _decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value




def _ft_data(method, path):
    raw = base.ft_call(method, path)
    if not isinstance(raw, dict) or raw.get('ok') is not True:
        return None
    return raw.get('data')

def _binana_db_rows():
    import sqlite3
    path='/app/shared/freqtrade/binana-extension.sqlite'
    active_states=('HALAL_DECIDED','RESERVED','ENTRY_PENDING','ENTRY_PARTIAL','OPEN','EXIT_PENDING','UNKNOWN')
    try:
        con=sqlite3.connect(f'file:{path}?mode=ro', uri=True); con.row_factory=sqlite3.Row
        marks=','.join('?' for _ in active_states)
        intents=[dict(r) for r in con.execute(
            f"SELECT * FROM intents WHERE state IN ({marks}) ORDER BY updated_ts DESC",
            active_states,
        )]
        prot=[dict(r) for r in con.execute(
            f"""SELECT p.* FROM protection p JOIN intents i ON i.intent_id=p.intent_id
                WHERE i.state IN ({marks}) ORDER BY p.updated_ts DESC""",
            active_states,
        )]
        inc=[dict(r) for r in con.execute(
            "SELECT * FROM incidents WHERE status='OPEN' ORDER BY updated_ts DESC"
        )]
        recent=[dict(r) for r in con.execute(
            "SELECT * FROM intents ORDER BY updated_ts DESC LIMIT 20"
        )]
        con.close()
        return {'available':True,'error_type':None,'protection':prot,'incidents':inc,
                'intents':intents,'recent_intents':recent}
    except Exception as exc:
        try:
            base.audit('canonical_state_read_failed', severity='CRITICAL', details={'error_type':type(exc).__name__})
        except Exception:
            pass
        return {'available':False,'error_type':type(exc).__name__,'protection':[],'incidents':[],'intents':[]}

def _owner_settled_trades(limit=1000):
    """Read economically settled trades without mutating Freqtrade persistence.

    Normal closed rows are settlements.  An open row is included only when
    verified recovery recorded non-executable retained dust with no exchange
    orders left open.
    """
    import sqlite3
    path='/app/shared/freqtrade/binana-owner.sqlite'
    rows=[]
    try:
        con=sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        con.row_factory=sqlite3.Row
        raw=con.execute("""
            SELECT t.id AS trade_id, t.pair, t.is_open, t.stake_amount,
                   t.max_stake_amount, t.realized_profit,
                   t.close_profit, t.close_profit_abs, t.close_date, t.open_date,
                   t.exit_reason,
                   d.cd_value AS dust_json, d.created_at AS dust_created_at,
                   d.updated_at AS dust_updated_at
            FROM trades t
            LEFT JOIN trade_custom_data d ON d.id=(
                SELECT x.id FROM trade_custom_data x
                WHERE x.ft_trade_id=t.id AND x.cd_key='binana_retained_dust'
                ORDER BY x.id DESC LIMIT 1
            )
            WHERE t.is_open=0 OR d.id IS NOT NULL
            ORDER BY COALESCE(d.updated_at,t.close_date,t.open_date) DESC, t.id DESC
            LIMIT ?
        """,(max(1,min(int(limit),5000)),)).fetchall()
        con.close()
    except Exception:
        return []
    for row in raw:
        item=dict(row)
        if int(item.get('is_open') or 0):
            try:
                dust=json.loads(item.get('dust_json') or '{}')
            except Exception:
                continue
            explicit_non_executable = (
                dust.get('non_executable') is True
                and str(dust.get('classification') or '').startswith('NON_EXECUTABLE_DUST')
            )
            legacy_below_min_notional = False
            if not explicit_non_executable:
                try:
                    quantity=float(dust.get('quantity') or 0)
                    price=float(dust.get('price') or 0)
                    sold=float(dust.get('sold_qty') or 0)
                    original=float(dust.get('original_protection_qty') or 0)
                    minima=[]
                    for flt in dust.get('filters') or []:
                        if not isinstance(flt,dict):
                            continue
                        if flt.get('filterType') in {'NOTIONAL','MIN_NOTIONAL'}:
                            raw_min=flt.get('minNotional')
                            if raw_min not in (None,''):
                                minima.append(float(raw_min))
                    min_notional=max(minima) if minima else 0.0
                    qty_matches = original > 0 and abs(sold-original) <= max(1e-12, original*1e-9)
                    legacy_below_min_notional = (
                        quantity > 0 and price > 0 and min_notional > 0
                        and quantity*price < min_notional and qty_matches
                    )
                except Exception:
                    legacy_below_min_notional = False
            if not (
                str(dust.get('phase') or '').lower()=='retained'
                and int(dust.get('exchange_open_orders') or 0)==0
                and (explicit_non_executable or legacy_below_min_notional)
            ):
                continue
            kind='DUST_RETAINED'
            settled_at=item.get('dust_updated_at') or item.get('dust_created_at')
        else:
            kind='CLOSED'
            settled_at=item.get('close_date')
        try:
            if kind == 'DUST_RETAINED':
                profit_abs=float(item.get('realized_profit') or item.get('close_profit_abs') or 0)
            else:
                profit_abs=float(item.get('close_profit_abs') or item.get('realized_profit') or 0)
        except Exception:
            profit_abs=0.0
        try:
            profit_ratio=float(item.get('close_profit') or 0)
        except Exception:
            profit_ratio=0.0
        try:
            capital=float(item.get('max_stake_amount') or item.get('stake_amount') or 0)
        except Exception:
            capital=0.0
        rows.append({
            'trade_id':item.get('trade_id'),'pair':item.get('pair'),
            'kind':kind,'settled_at':settled_at,
            'profit_abs':profit_abs,'profit_ratio':profit_ratio,
            'stake_amount':capital,
            'exit_reason':item.get('exit_reason'),
        })
    return rows

def _ft_state():
    status=_ft_data('GET','/status')
    cfg=_ft_data('GET','/show_config')
    bal=_ft_data('GET','/balance')
    return status,cfg,bal

def _sidecar_result(command):
    raw = base._sidecar_read(command)
    if not isinstance(raw, dict):
        return raw
    if raw.get('ok') is not True:
        raise RuntimeError(f'{command} backend unavailable')
    return _decode(raw.get('result'))



def _effective_trade_size():
    cfg=_ft_data('GET','/show_config') or {}
    fallback=cfg.get('stake_amount','n/a')
    try:
        raw=base.read_json(base.Path('/app/shared/runtime/trade_size.json'), {}) or {}
        value=float(raw.get('usdt'))
        if math.isfinite(value) and value > 0:
            return value
    except Exception:
        pass
    return fallback

def _pretty_status():
    status,cfg,bal=_ft_state(); ext=_binana_db_rows()
    trades=status if isinstance(status,list) else []
    state=(cfg or {}).get('state','unknown') if isinstance(cfg,dict) else 'unknown'
    strategy=(cfg or {}).get('strategy_version') or (cfg or {}).get('strategy') or 'BinanaNfiSpot'
    canonical_ok=ext.get('available') is True
    incidents=ext.get('incidents') or []
    active_states={'HALAL_DECIDED','RESERVED','ENTRY_PENDING','ENTRY_PARTIAL','OPEN','EXIT_PENDING','UNKNOWN'}
    occupied=sum(1 for row in (ext.get('intents') or []) if str(row.get('state') or '').upper() in active_states)
    running=str(state).lower()=='running'
    if not canonical_ok:
        effective='BLOCKED'; reason='canonical state unavailable'
    elif not running:
        effective='PAUSED'; reason='Freqtrade is stopped'
    elif incidents:
        effective='BLOCKED'; reason='open reconciliation/protection incident'
    elif occupied >= 4:
        effective='CAPACITY FULL'; reason='4/4 real occupied trades; waiting for a slot'
    else:
        effective='READY'; reason='none'
    lines=['📊 BINANA TESTNET STATUS','',
           'Order owner: FREQTRADE',
           f'Freqtrade state: {str(state).upper()}',
           f'Entry admission: {effective}',
           f'Blocker: {reason}',
           f'Strategy: {strategy}',
           f"Stake: {_effective_trade_size()} USDT",
           f'Occupied BINANA slots: {occupied}/4',
           f'Open Freqtrade trades: {len(trades)}',
           f"Canonical state: {'AVAILABLE' if canonical_ok else 'UNAVAILABLE'}",
           f'Open BINANA incidents: {len(incidents) if canonical_ok else "unknown"}',
           'Legacy execution sidecar: MONITOR-ONLY / NO BINANCE CREDENTIALS']
    return '\n'.join(lines)

def _pretty_balance():
    d=_ft_data('GET','/balance')
    rows=d.get('currencies',[]) if isinstance(d,dict) else []
    usdt=next((r for r in rows if str(r.get('currency')).upper()=='USDT'),{})
    return '\n'.join(['💰 TESTNET BALANCE','',
        f"USDT free: {usdt.get('free','n/a')}", f"USDT used/locked: {usdt.get('used','n/a')}",
        f"USDT total: {usdt.get('balance',usdt.get('total','n/a'))}",
        'Source: Freqtrade / Binance Spot Testnet'])

def _pretty_open_trades():
    status=_ft_data('GET','/status'); trades=status if isinstance(status,list) else []
    ext=_binana_db_rows(); prot=ext.get('protection') or []
    lines=['📋 OPEN BINANA TESTNET TRADES','',f'Freqtrade open trades: {len(trades)}']
    if not trades: lines += ['','No Freqtrade-owned open positions.']
    for t in trades[:8]:
        pair=t.get('pair','?'); tid=t.get('trade_id','?')
        p=next((x for x in prot if str(x.get('freqtrade_trade_id',''))==str(tid) or x.get('pair')==pair),None)
        lines += ['',f'• {pair}',f"  Trade ID: {tid}",f"  Amount: {t.get('amount','n/a')}",f"  Open rate: {t.get('open_rate','n/a')}",f"  Protection: {(p or {}).get('mode','pending/none')} / {(p or {}).get('status','unknown')}"]
    return '\n'.join(lines)[:3900]

def _pretty_history():
    trades=_owner_settled_trades(1000)
    lines=['📜 RECENT TRADE HISTORY','',f'Recorded settled trades: {len(trades)}']
    if not trades:
        lines += ['', 'No economically settled TestNet trades recorded yet.']
    for t in trades[:8]:
        pct=float(t.get('profit_ratio') or 0)*100.0
        kind='dust retained' if t.get('kind')=='DUST_RETAINED' else 'closed'
        lines.append(
            f"• {t.get('pair','?')} | P&L {float(t.get('profit_abs') or 0):+.8f} USDT "
            f"({pct:+.4f}%) | {kind} | {t.get('settled_at') or 'n/a'}"
        )
    lines.append('Source: canonical Freqtrade owner ledger; dust rows require verified non-executable retention.')
    return '\n'.join(lines)[:3900]


LIFECYCLE_JOURNAL = base.RUNTIME / 'freqtrade_lifecycle_events.jsonl'
LIFECYCLE_MONITOR_STATE = base.RUNTIME / 'freqtrade_lifecycle_monitor.json'
FT_WS_TOKEN = os.getenv('FREQTRADE_API_WS_TOKEN', '')
RPC_EVENT_TYPES = {'entry','entry_fill','entry_cancel','exit','exit_fill','exit_cancel',
                   'protection_trigger','protection_trigger_global','strategy_msg'}


FT_LOG_PATH = base.Path('/app/shared/freqtrade/logs/freqtrade-owner.log')
LOG_MONITOR_STATE = base.RUNTIME / 'freqtrade_log_monitor.json'
STRATEGY_NOTICE_STATE = base.RUNTIME / 'freqtrade_strategy_notice_state.json'
_LOG_NOTICE_LAST = {}
NOISY_ADMISSION_REASONS = {'REJECTED_MAX_SLOTS','MAX_SLOTS_REACHED','RECONCILIATION_BLOCKER_OPEN','REJECTED_FLOW','REJECTED_CONFLUENCE','REJECTED_EXECUTION','REJECTED_SEQUENCE_BOOK','PAIR_NOT_READY','BULLISH_FLOW_STALE','BULLISH_FLOW_NOT_CONFIRMED','CONFLUENCE_NOT_CONFIRMED','SEQUENCE_BOOK_BULLISH_NOT_CONFIRMED','VOLUME_HARD_LIMIT','UNSUPPORTED_NFI_MODE','RUNTIME_CONTRACT','HALAL_ALLOWED'}
NOTIFICATION_DB = base.RUNTIME / 'telegram-notifications.sqlite'
_CANONICAL_SEEN_ORDER_KEYS=set()

def _notification_key(record):
    typ=str(record.get('type') or '').upper()
    pair=str(record.get('pair') or '').upper()
    trade_id=str(record.get('trade_id') or '')
    order_id=str(record.get('order_id') or '')
    entry_tag=str(record.get('entry_tag') or '')
    event_time=str(record.get('event_time') or record.get('candle_time') or '')
    reason=str(record.get('reason') or record.get('exit_reason') or '')
    raw='|'.join([typ,pair,trade_id,order_id,entry_tag,event_time,reason])
    import hashlib
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def _claim_notification(record):
    key=_notification_key(record)
    NOTIFICATION_DB.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    conn=sqlite3.connect(str(NOTIFICATION_DB), timeout=5)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE IF NOT EXISTS telegram_notification_delivery (event_key TEXT PRIMARY KEY, delivered_at REAL NOT NULL)')
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT 1 FROM telegram_notification_delivery WHERE event_key=?',(key,)).fetchone()
        if row:
            conn.rollback(); return False
        conn.execute('INSERT INTO telegram_notification_delivery(event_key,delivered_at) VALUES (?,?)',(key,time.time()))
        conn.commit(); return True
    finally:
        conn.close()


def _parse_freqtrade_log_event(line):
    text=str(line or '')
    rejected=re.search(r'BINANA rejected ([A-Z0-9]{2,20}/USDT) before order callback:\s*(.+)$', text)
    if rejected:
        return {'type':'TRADE_REJECTED','pair':rejected.group(1),
                'reason':rejected.group(2).strip()[:300], 'source':'freqtrade_log'}
    expired=re.search(r'Long FOK order .* for ([A-Z0-9]{2,20}/USDT) is expired .*zero amount', text, re.I)
    if expired:
        return {'type':'ENTRY_TIMEOUT','pair':expired.group(1).upper(),
                'reason':'FOK expired without fill', 'source':'freqtrade_log'}
    timed=re.search(r'(?:Long )?order .* for ([A-Z0-9]{2,20}/USDT).*timed out', text, re.I)
    if timed:
        return {'type':'ENTRY_TIMEOUT','pair':timed.group(1).upper(),
                'reason':'entry order timed out', 'source':'freqtrade_log'}
    return None


def _append_normalized_lifecycle(record):
    if not isinstance(record,dict):
        return None
    import hashlib, sqlite3
    from decimal import Decimal
    payload=dict(record)
    payload.setdefault('ts', time.time())
    payload.setdefault('source','current_lifecycle')
    payload['type']=str(payload.get('type') or '').upper()
    payload['pair']=str(payload.get('pair') or '').upper()
    scope=str(payload.get('scope') or 'binana-20260910-owner-cutover-1')
    typ=payload['type']; pair=payload['pair']
    if typ in {'ENTRY_FILL','EXIT_FILL'} and payload.get('order_id'):
        qty=str(Decimal(str(payload.get('amount') or '0')).normalize())
        key=(scope,typ,pair,str(payload.get('trade_id')),str(payload.get('order_id')),qty)
    else:
        stable={k:v for k,v in payload.items() if k not in {'ts','source','event_id','_new_event'}}
        if not payload.get('order_id') and not payload.get('event_time') and not payload.get('candle_time'):
            stable['observation_bucket']=int(float(payload.get('ts') or 0)//300)
        key=(scope,typ,pair,stable)
    event_id=hashlib.sha256(json.dumps(key,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()
    payload['event_id']=event_id
    db=LIFECYCLE_JOURNAL.with_suffix('.sqlite')
    db.parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(str(db),timeout=10,isolation_level=None)
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=FULL')
        con.execute('CREATE TABLE IF NOT EXISTS lifecycle_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL)')
        encoded=json.dumps(payload,default=str,sort_keys=True,separators=(',',':'))
        con.execute('BEGIN IMMEDIATE')
        cur=con.execute('INSERT OR IGNORE INTO lifecycle_events(event_id,payload) VALUES(?,?)',(event_id,encoded))
        inserted=bool(cur.rowcount)
        con.commit()
    except BaseException:
        try: con.rollback()
        except Exception: pass
        raise
    finally:
        con.close()
    payload['_new_event']=inserted
    return payload


def _freqtrade_log_monitor_loop():
    offset=0
    try:
        state=base.read_json(LOG_MONITOR_STATE,{}) or {}
        offset=max(0,int(state.get('offset') or 0))
    except Exception:
        offset=0
    first=True
    while True:
        try:
            if not FT_LOG_PATH.exists():
                time.sleep(3); continue
            size=FT_LOG_PATH.stat().st_size
            if offset > size: offset=0
            if offset == 0 and first:
                offset=max(0,size-250000)
            with FT_LOG_PATH.open('r',encoding='utf-8',errors='replace') as fh:
                fh.seek(offset)
                lines=fh.readlines()
                offset=fh.tell()
            for line in lines:
                event=_parse_freqtrade_log_event(line)
                if not event: continue
                event['ts']=time.time()
                saved=_append_normalized_lifecycle(event)
                if isinstance(saved,dict) and saved.get('_new_event') is False:
                    continue
                if not first:
                    reason=str(saved.get('reason') or '').upper()
                    if saved.get('type')=='TRADE_REJECTED' and reason in NOISY_ADMISSION_REASONS:
                        continue
                    if not _claim_notification(saved):
                        continue
                    key=f"{saved.get('type')}|{saved.get('pair')}|{saved.get('reason')}"
                    now=time.time(); last=float(_LOG_NOTICE_LAST.get(key,0) or 0)
                    if now-last >= 60:
                        title='❌ TRADE REJECTED' if saved['type']=='TRADE_REJECTED' else '⚠️ ENTRY TIMEOUT'
                        base.send(f"{title}\n\nPair: {saved.get('pair','n/a')}\nReason: {saved.get('reason','n/a')}\nSource: Freqtrade execution lifecycle", base.OWNER)
                        _LOG_NOTICE_LAST[key]=now
                        base.audit('telegram_execution_rejection_notice_delivered',details={'type':saved.get('type'),'pair':saved.get('pair')})
            base.atomic_write_json(LOG_MONITOR_STATE,{'offset':offset,'ts':time.time()})
            first=False
        except Exception as exc:
            base.audit('freqtrade_log_monitor_error',severity='WARNING',details={'error_type':type(exc).__name__})
        time.sleep(2)

def _strategy_journal_notice_loop():
    offset=None
    try:
        state=base.read_json(STRATEGY_NOTICE_STATE,{}) or {}
        if isinstance(state.get('offset'),int): offset=max(0,state['offset'])
    except Exception:
        offset=None
    while True:
        try:
            if not LIFECYCLE_JOURNAL.exists():
                time.sleep(2); continue
            size=LIFECYCLE_JOURNAL.stat().st_size
            if offset is None:
                offset=size
                base.atomic_write_json(STRATEGY_NOTICE_STATE,{'offset':offset,'ts':time.time()})
                time.sleep(1); continue
            if offset > size:
                offset=size
            with LIFECYCLE_JOURNAL.open('r',encoding='utf-8',errors='replace') as fh:
                fh.seek(offset); lines=fh.readlines(); offset=fh.tell()
            for line in lines:
                try: row=json.loads(line)
                except Exception: continue
                if not isinstance(row,dict) or row.get('source')!='binana_wrapper': continue
                typ=str(row.get('type') or '').upper()
                if typ=='STRATEGY_SIGNAL':
                    # Candidate signals are high-frequency telemetry, not lifecycle events.
                    # Keep them available via Last Signal / Signal History without push spam.
                    continue
                elif typ=='TRADE_REJECTED':
                    reason=str(row.get('reason') or '').upper()
                    if reason in NOISY_ADMISSION_REASONS:
                        continue
                    if not _claim_notification(row):
                        continue
                    base.send('\n'.join([
                        '❌ TRADE REJECTED','',
                        f"Pair: {row.get('pair','n/a')}",
                        f"Tag: {row.get('entry_tag','n/a')}",
                        f"Reason: {row.get('reason','n/a')}",
                        'Source: BinanaNfiSpot confirmation lifecycle'
                    ]), base.OWNER)
                    base.audit('telegram_trade_rejected_notice_delivered',details={'pair':row.get('pair'),'reason':row.get('reason')})
            base.atomic_write_json(STRATEGY_NOTICE_STATE,{'offset':offset,'ts':time.time()})
        except Exception as exc:
            base.audit('strategy_notice_monitor_error',severity='WARNING',details={'error_type':type(exc).__name__})
        time.sleep(1)


def _is_real_trailing_promotion(old, new):
    if old is None or new is None:
        return False
    _, old_mode, old_status = old
    _, new_mode, new_status = new
    old_fixed='FIXED' in str(old_mode).upper() and 'ACTIVE' in str(old_status).upper()
    new_trail='TRAIL' in str(new_mode).upper() and 'ACTIVE' in str(new_status).upper()
    return bool(old_fixed and new_trail)

def _record_lifecycle_event(message):
    if not isinstance(message, dict):
        return None
    import hashlib, sqlite3
    raw_type = str(message.get('type') or '').lower()
    data = message.get('data') if isinstance(message.get('data'), dict) else {
        k: v for k, v in message.items() if k != 'type'
    }
    event_type = raw_type.upper()
    if raw_type == 'entry_cancel' and 'timeout' in str(data.get('reason','')).lower():
        event_type = 'ENTRY_TIMEOUT'
    record = {
        'ts': time.time(), 'type': event_type, 'source': 'freqtrade_rpc',
        'pair': str(data.get('pair') or '').upper(),
        'trade_id': data.get('trade_id'),
        'order_id': data.get('order_id') or data.get('ft_order_id'),
        'event_time': data.get('order_filled_date') or data.get('order_date') or data.get('close_date') or data.get('open_date') or data.get('date'),
        'entry_tag': data.get('enter_tag') or data.get('buy_tag'),
        'exit_reason': data.get('exit_reason'),
        'amount': data.get('amount'),
        'open_rate': data.get('open_rate'),
        'close_rate': data.get('close_rate') or data.get('close_rate_requested'),
        'profit_ratio': data.get('profit_ratio'),
        'reason': data.get('reason'),
    }
    stored=dict(record)
    if event_type in {'ENTRY_FILL','EXIT_FILL'}:
        stored['type']='RPC_'+event_type+'_OBSERVED'
    stable={k:v for k,v in stored.items() if k!='ts'}
    event_id=hashlib.sha256(json.dumps(stable,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()
    record['event_id']=event_id
    stored['event_id']=event_id
    db=LIFECYCLE_JOURNAL.with_suffix('.sqlite')
    db.parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(str(db),timeout=10,isolation_level=None)
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=FULL')
        con.execute('CREATE TABLE IF NOT EXISTS lifecycle_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL)')
        encoded=json.dumps(stored,default=str,sort_keys=True,separators=(',',':'))
        con.execute('BEGIN IMMEDIATE')
        cur=con.execute('INSERT OR IGNORE INTO lifecycle_events(event_id,payload) VALUES(?,?)',(event_id,encoded))
        inserted=bool(cur.rowcount)
        con.commit()
    except BaseException:
        try: con.rollback()
        except Exception: pass
        raise
    finally:
        con.close()
    record['_new_event']=inserted
    return record


def _lifecycle_rows(limit=200):
    import sqlite3
    from collections import deque
    n=max(1,min(int(limit),500))
    db=LIFECYCLE_JOURNAL.with_suffix('.sqlite')
    if db.exists():
        try:
            con=sqlite3.connect(f'file:{db}?mode=ro',uri=True,timeout=5)
            raw=con.execute('SELECT payload FROM lifecycle_events ORDER BY seq DESC LIMIT ?',(n,)).fetchall()
            con.close()
            rows=[]
            for (payload,) in reversed(raw):
                try:
                    row=json.loads(payload)
                    if isinstance(row,dict): rows.append(row)
                except Exception:
                    continue
            return rows
        except Exception as exc:
            try: base.audit('lifecycle_sqlite_read_failed',severity='WARNING',details={'error_type':type(exc).__name__})
            except Exception: pass
    try:
        with LIFECYCLE_JOURNAL.open('r',encoding='utf-8',errors='replace') as fh:
            lines=deque(fh,maxlen=n)
    except Exception:
        return []
    rows=[]
    for line in lines:
        try:
            row=json.loads(line)
            if isinstance(row,dict): rows.append(row)
        except Exception:
            continue
    return rows


def _current_signal_history(limit=10, rejected_only=False):
    rows=_lifecycle_rows(500)
    allowed={'TRADE_REJECTED','ENTRY_CANCEL','ENTRY_TIMEOUT'} if rejected_only else {'STRATEGY_SIGNAL'}
    rows=[r for r in rows if str(r.get('type','')).upper() in allowed]
    if rejected_only:
        rows=[r for r in rows if str(r.get('reason') or '').upper() not in NOISY_ADMISSION_REASONS]
    dedup=[]; seen=set()
    for row in reversed(rows):
        key=(str(row.get('type','')).upper(),row.get('pair'),row.get('entry_tag'),row.get('reason'))
        if key in seen: continue
        seen.add(key); dedup.append(row)
        if len(dedup) >= max(1,min(int(limit),20)): break
    rows=dedup
    lines=['🚫 REJECTED SIGNALS' if rejected_only else '🗂 RECENT CURRENT SIGNALS','',f'Count: {len(rows)}']
    if not rows:
        lines += ['', 'No current BinanaNfiSpot lifecycle evidence recorded yet.']
    for row in rows:
        typ=str(row.get('type') or '?').upper()
        pair=row.get('pair') or '?'
        tag=row.get('entry_tag') or 'none'
        reason=row.get('reason') or row.get('exit_reason') or ''
        lines.append(f'• {pair} — {typ} — {tag}' + (f' — {reason}' if reason else ''))
    return '\n'.join(lines)[:3900]


def _format_rpc_notice(record):
    typ=str(record.get('type') or '').upper(); pair=record.get('pair') or 'n/a'
    title={'ENTRY':'🟦 BUY SUBMITTED','ENTRY_FILL':'🟢 BUY FILLED',
           'ENTRY_CANCEL':'⚠️ BUY CANCELED','ENTRY_TIMEOUT':'⚠️ BUY TIMEOUT',
           'EXIT':'🟨 SELL SUBMITTED','EXIT_FILL':'🔴 SELL FILLED',
           'EXIT_CANCEL':'⚠️ SELL CANCELED'}.get(typ)
    if not title: return None
    lines=[title,'',f'Pair: {pair}']
    if record.get('trade_id') is not None: lines.append(f"Trade ID: {record.get('trade_id')}")
    if record.get('entry_tag'): lines.append(f"Signal: {record.get('entry_tag')}")
    if record.get('amount') is not None: lines.append(f"Amount: {record.get('amount')}")
    if record.get('open_rate') is not None: lines.append(f"Buy/entry price: {record.get('open_rate')}")
    if record.get('close_rate') is not None: lines.append(f"Sell/exit price: {record.get('close_rate')}")
    if typ=='EXIT_FILL' and record.get('profit_ratio') is not None:
        try:
            pct=float(record.get('profit_ratio'))*100.0
            lines.append(f"P/L: {pct:+.4f}%")
        except Exception:
            lines.append(f"P/L ratio: {record.get('profit_ratio')}")
    if record.get('exit_reason'): lines.append(f"Exit reason: {record.get('exit_reason')}")
    if record.get('reason'): lines.append(f"Reason: {record.get('reason')}")
    lines.append('Source: canonical Freqtrade filled-order ledger' if record.get('source') == 'canonical_freqtrade_order' else 'Source: Freqtrade RPC lifecycle')
    return '\n'.join(lines)


def _rpc_retry_delay(attempt, random_value=None):
    import random
    rv=random.random if random_value is None else random_value
    n=max(0,int(attempt))
    base_delay=min(60.0, 2.0 ** min(n, 6))
    multiplier=1.0 + ((float(rv()) * 2.0) - 1.0) * 0.20
    return max(0.5, min(60.0, base_delay * multiplier))


def _rpc_lifecycle_loop():
    if not FT_WS_TOKEN:
        base.audit('freqtrade_rpc_bridge_disabled', severity='ERROR', details={'reason':'ws token missing'})
        return
    url=base.FT_BASE.replace('http://','ws://').replace('https://','wss://') + '/message/ws?token=' + FT_WS_TOKEN
    health_path=base.RUNTIME/'freqtrade_rpc_health.json'
    attempt=0
    last_error_key=None
    last_error_audit_at=0.0
    last_connected_at=None
    while True:
        ws=None
        try:
            ws=websocket.create_connection(url, timeout=15)
            ws.settimeout(None)
            ws.send(json.dumps({'type':'subscribe','data':sorted(RPC_EVENT_TYPES)}))
            attempt=0
            last_connected_at=time.time()
            base.atomic_write_json(health_path,{'ok':True,'state':'connected','ts':last_connected_at,'last_connected_at':last_connected_at,'last_error_type':None,'retry_delay_seconds':0})
            base.audit('freqtrade_rpc_bridge_connected')
            while True:
                raw=ws.recv()
                if not raw: raise RuntimeError('Freqtrade websocket closed')
                msg=json.loads(raw)
                typ=str(msg.get('type') or '').lower() if isinstance(msg,dict) else ''
                if typ not in RPC_EVENT_TYPES: continue
                record=_record_lifecycle_event(msg)
                if isinstance(record,dict) and record.get('_new_event') is False:
                    continue
                if str((record or {}).get('type') or '').upper() in {'ENTRY_FILL','EXIT_FILL'}:
                    continue
                notice=_format_rpc_notice(record or {})
                if notice:
                    if not _claim_notification(record):
                        continue
                    base.send(notice, base.OWNER)
                    base.audit('telegram_freqtrade_rpc_notice_delivered',details={'type':record.get('type'),'pair':record.get('pair'),'trade_id':record.get('trade_id')})
        except Exception as exc:
            now=time.time()
            delay=_rpc_retry_delay(attempt)
            attempt=min(attempt+1,20)
            error_type=type(exc).__name__
            error_text=base._redact_secrets(exc)
            error_key=(error_type,str(error_text)[:160])
            base.atomic_write_json(health_path,{'ok':False,'state':'disconnected','ts':now,'last_connected_at':last_connected_at,'last_error_type':error_type,'retry_delay_seconds':round(delay,3)})
            if error_key != last_error_key or now-last_error_audit_at >= 60:
                base.audit('freqtrade_rpc_bridge_error', severity='WARNING', details={'error_type':error_type,'error':error_text,'retry_delay_seconds':round(delay,3)})
                last_error_key=error_key
                last_error_audit_at=now
            time.sleep(delay)
        finally:
            try:
                if ws: ws.close()
            except Exception: pass


def _protection_monitor_loop():
    previous=None
    while True:
        try:
            rows=_binana_db_rows().get('protection') or []
            latest={}
            for row in rows:
                pair=str(row.get('pair') or '')
                if pair and pair not in latest:
                    latest[pair]=row
            current={pair:(row.get('generation'),row.get('mode'),row.get('status'))
                     for pair,row in latest.items()}
            if previous is None:
                previous=current
                time.sleep(4)
                continue
            for pair,row in latest.items():
                snap=current[pair]; old=previous.get(pair)
                active=str(row.get('status') or '').upper() in {'FIXED_ACTIVE','TRAILING_ACTIVE','ACTIVE'}
                if old is None and active:
                    _append_normalized_lifecycle({'type':'PROTECTION_ACTIVE','source':'binana_extension_db',
                                                  'pair':pair,'reason':f"{row.get('mode')} / {row.get('status')}"})
                    base.send(f"🛡 PROTECTION ACTIVE\n\nPair: {pair}\nMode: {row.get('mode','n/a')}\nStatus: {row.get('status','n/a')}\nGeneration: {row.get('generation','n/a')}\nSource: BINANA extension DB", base.OWNER)
                elif _is_real_trailing_promotion(old,snap):
                    _append_normalized_lifecycle({'type':'PROTECTION_PROMOTED','source':'binana_extension_db',
                                                  'pair':pair,'reason':f"{row.get('mode')} / {row.get('status')}"})
                    base.send(f"📈 FIXED OCO → TRAILING OCO\n\nPair: {pair}\nMode: {row.get('mode','n/a')}\nStatus: {row.get('status','n/a')}\nGeneration: {row.get('generation','n/a')}\nSource: BINANA extension DB", base.OWNER)
            previous=current
        except Exception as exc:
            base.audit('telegram_protection_monitor_error', severity='WARNING', details={'error_type':type(exc).__name__})
        time.sleep(4)


def _seed_current_trade_history(notify=False):
    import sqlite3
    seen=globals().setdefault('_CANONICAL_SEEN_ORDER_KEYS',set())
    with sqlite3.connect("file:/app/shared/freqtrade/binana-owner.sqlite?mode=ro",uri=True,timeout=2) as con:
        con.row_factory=sqlite3.Row
        rows=con.execute("""SELECT o.*,t.pair,t.enter_tag,t.open_rate,t.realized_profit,t.fee_open,t.fee_close
            FROM orders o JOIN trades t ON t.id=o.ft_trade_id
            WHERE o.filled>0 AND o.ft_is_open=0 ORDER BY o.id""").fetchall()
    for raw in rows:
        row=dict(raw)
        if row.get('ft_order_side') not in {'buy','sell','stoploss'}:
            continue
        order_key=(str(row.get('pair') or ''),str(row.get('order_id') or ''),str(row.get('ft_order_side') or ''))
        if order_key in seen:
            continue
        typ='ENTRY_FILL' if row['ft_order_side']=='buy' else 'EXIT_FILL'
        record=_append_normalized_lifecycle({'type':typ,'source':'canonical_freqtrade_order',
            'pair':row['pair'],'trade_id':row['ft_trade_id'],'order_id':str(row['order_id']),
            'entry_tag':row.get('enter_tag'),'amount':row['filled'],
            'open_rate':row.get('open_rate'),'close_rate':(row.get('average') or row.get('price')) if typ=='EXIT_FILL' else None,
            'cost':row.get('cost'),'fee_open':row.get('fee_open'),'fee_close':row.get('fee_close'),
            'event_time':row.get('order_filled_date') or row.get('order_date')})
        if isinstance(record,dict):
            seen.add(order_key)
        if notify and isinstance(record,dict) and record.get('_new_event'):
            notice=_format_rpc_notice(record)
            if notice and _claim_notification(record):
                base.send(notice,base.OWNER)


def _canonical_lifecycle_loop():
    last_error_type=None
    last_audit_at=0.0
    while True:
        try:
            _seed_current_trade_history(notify=True)
            last_error_type=None
        except Exception as exc:
            now=time.time(); et=type(exc).__name__
            if et!=last_error_type or now-last_audit_at>=60:
                base.audit('canonical_lifecycle_monitor_error',severity='WARNING',details={'error_type':et})
                last_error_type=et; last_audit_at=now
        time.sleep(3)


def _pretty_last_signal():
    all_rows=_lifecycle_rows(500)
    rows=[r for r in all_rows if str(r.get('type','')).upper()=='STRATEGY_SIGNAL']
    if not rows:
        rows=[r for r in all_rows if str(r.get('type','')).upper() in {'ENTRY','ENTRY_FILL'}]
    if not rows:
        return '📈 LAST STRATEGY SIGNAL\n\nNo current BinanaNfiSpot lifecycle evidence available yet.'
    row=rows[-1]
    lines=['📈 LAST STRATEGY SIGNAL','',
           f"Pair: {row.get('pair','n/a')}",
           f"Event: {str(row.get('type','n/a')).upper()}",
           f"Tag: {row.get('entry_tag') or 'n/a'}",
           f"Candle: {row.get('candle_time') or 'n/a'}"]
    for label,key in [('Price','close'),('RSI','rsi'),('MFI','mfi'),('CMF','cmf')]:
        if row.get(key) is not None: lines.append(f"{label}: {row.get(key)}")
    lines.append(f"Source: {row.get('source','current lifecycle')}")
    return '\n'.join(lines)[:3900]


def _pretty_signals():
    return _current_signal_history(limit=10, rejected_only=False)

def _pretty_rejected():
    import sqlite3
    path='/app/shared/freqtrade/binana-extension.sqlite'
    try:
        con=sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        con.row_factory=sqlite3.Row
        rows=[dict(r) for r in con.execute(
            "SELECT pair,state,admission_json,updated_ts FROM intents "
            "WHERE state LIKE 'REJECTED_%' ORDER BY updated_ts DESC LIMIT 500"
        ).fetchall()]
        con.close()
    except Exception as exc:
        return f'🚫 REJECTED SIGNALS\n\nCanonical rejection ledger unavailable: {type(exc).__name__}'
    reasons={}
    recent=[]
    for row in rows:
        code=str(row.get('state') or 'REJECTED')
        try:
            payload=json.loads(row.get('admission_json') or '{}')
            code=str(payload.get('code') or code)
        except Exception:
            pass
        reasons[code]=reasons.get(code,0)+1
        if len(recent)<8:
            recent.append((row.get('pair') or '?',code))
    lines=['🚫 REJECTED SIGNALS','',
           f'Canonical recent rejections: {len(rows)}',
           'Aggregated reasons (latest 500):']
    for code,count in sorted(reasons.items(), key=lambda kv:(-kv[1],kv[0]))[:8]:
        lines.append(f'• {code}: {count}')
    if recent:
        lines += ['', 'Most recent:']
        lines += [f'• {pair} — {code}' for pair,code in recent]
    return '\n'.join(lines)[:3900]


def _pretty_profit():
    d=_ft_data('GET','/profit') or {}
    settled=_owner_settled_trades(5000)
    fully_closed=sum(1 for t in settled if t.get('kind')=='CLOSED')
    dust=sum(1 for t in settled if t.get('kind')=='DUST_RETAINED')
    realized=sum(float(t.get('profit_abs') or 0) for t in settled)
    stake=sum(float(t.get('stake_amount') or 0) for t in settled)
    pct=(realized/stake*100.0) if stake else 0.0
    return '\n'.join([
        '💵 TESTNET PROFIT','',
        f'Economically settled trades: {len(settled)}',
        f'Fully closed trades: {fully_closed}',
        f'Dust-retained settlements: {dust}',
        f'Settled realized P&L USDT: {realized:.8f}',
        f'Settled P&L vs stake: {pct:+.4f}%',
        f"Freqtrade total P&L incl. residual valuation: {d.get('profit_all_coin','n/a')} USDT",
        f"Refreshed UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}",
        'Source: canonical Freqtrade owner ledger + Freqtrade /profit.',
    ])

def _pretty_report():
    cfg=_ft_data('GET','/show_config') or {}; ext=_binana_db_rows()
    utc_day=time.strftime('%Y-%m-%d', time.gmtime())
    settled=[t for t in _owner_settled_trades(5000) if str(t.get('settled_at') or '').startswith(utc_day)]
    closed_today=sum(1 for t in settled if t.get('kind')=='CLOSED')
    dust_today=sum(1 for t in settled if t.get('kind')=='DUST_RETAINED')
    pnl=sum(float(t.get('profit_abs') or 0) for t in settled)
    return '\n'.join([
        '📅 DAILY TESTNET REPORT','',
        f'UTC trading day: {utc_day}',
        f"Freqtrade state: {str(cfg.get('state','unknown')).upper()}",
        f'Economically settled today: {len(settled)}',
        f'Fully closed today: {closed_today}',
        f'Dust-retained settlements today: {dust_today}',
        f'Settled P&L today (USDT): {pnl:.8f}',
        f"Open incidents: {len(ext.get('incidents') or [])}",
        f"Refreshed UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}",
        'Source: canonical Freqtrade owner ledger.',
    ])

def _pretty_universe():
    raw = _decode(base._universe_status())
    pairs = raw.get('pairs', []) if isinstance(raw, dict) else []
    sel = raw.get('selection', {}) if isinstance(raw, dict) else {}
    mode='TOP-N'
    try:
        name=str(raw.get('snapshot_file') or '')
        if re.fullmatch(r'universe_[A-Za-z0-9._-]{1,180}\.json',name) and '..' not in name:
            full=base.read_json(base.Path('/app/shared/universe/snapshots')/name,{}) or {}
            if (full.get('configuration') or {}).get('limit_mode')=='all_eligible':
                mode='ALL eligible after hard quality + halal gates'
    except Exception:
        pass
    symbols,_registry_err=_load_halal_registry_symbols()
    lines = [
        '📦 CURRENT TRADING UNIVERSE','',
        f'Eligible selected: {len(pairs)}',
        f'Mode: {mode}',
        f"State: {sel.get('state','unknown')}",
        f'Owner-approved halal assets: {len(symbols)}',
        'Full Binance Spot market is scanned before eligibility filters are applied.',
    ]
    if sel.get('shortfall_count'):
        lines.append(f"Market shortfall: {sel.get('shortfall_count')} below requested limit")
    if pairs:
        lines += ['', 'Pairs:', ', '.join(pairs)]
    return '\n'.join(lines)[:3900]


def _pretty_health():
    ft=base.ft_call('GET','/ping'); cfg=_ft_data('GET','/show_config') or {}; tg=base.read_json(base.RUNTIME/'telegram_health.json',{}) or {}; ext=_binana_db_rows()
    canonical_ok=ext.get('available') is True
    incidents=ext.get('incidents') or []
    overall='BLOCKED' if (not canonical_ok) or incidents or str(cfg.get('state','stopped')).lower()!='running' else 'READY'
    return '\n'.join(['🩺 SYSTEM HEALTH','',f"Overall trading readiness: {overall}",f"Freqtrade API: {'HEALTHY' if isinstance(ft,dict) and ft.get('ok') else 'UNHEALTHY'}",f"Freqtrade state: {str(cfg.get('state','unknown')).upper()}",f"Canonical DB: {'AVAILABLE' if canonical_ok else 'UNAVAILABLE'}",f"Canonical DB incidents: {len(incidents) if canonical_ok else 'unknown'}",f"Telegram poller: {'HEALTHY' if tg.get('ok') else 'UNHEALTHY'}",'Execution sidecar: MONITOR-ONLY (no order credentials)'])

def _pretty_alerts():
    d = _decode(base._alert_status())
    if not isinstance(d, dict):
        return '📢 ALERTS\n\nStatus unavailable.'
    return '\n'.join(['📢 ALERTS', '', f"Delivery: {'HEALTHY' if d.get('delivery_ok') else 'BLOCKED'}", f"Pending: {d.get('pending_alert_count',0)}", f"Dead letters: {d.get('dead_letter_count',0)}"])


def _pretty_protection():
    ext=_binana_db_rows()
    if ext.get('available') is not True:
        return '🛡 PROTECTION STATUS\n\nStatus: UNKNOWN / BLOCKED\nCanonical BINANA state is unavailable. No absence-of-protection conclusion can be made.'
    rows=ext.get('protection') or []; inc=ext.get('incidents') or []
    status=_ft_data('GET','/status')
    open_pairs=[]
    if isinstance(status,list):
        open_pairs=[str(t.get('pair') or '') for t in status if t.get('pair')]
    current=[]
    for pair in dict.fromkeys(open_pairs):
        candidates=[r for r in rows if str(r.get('pair') or '')==pair]
        active=[r for r in candidates if 'ACTIVE' in str(r.get('status') or '').upper()]
        pool=active or candidates
        if pool:
            current.append(max(pool,key=lambda r:int(r.get('generation') or 0)))
    lines=['🛡 PROTECTION STATUS','', 'Order/protection owner: FREQTRADE',
           f'Open positions: {len(open_pairs)}',f'Current protection records: {len(current)}',
           f'Open incidents: {len(inc)}']
    for r in current:
        lines += [f"• {r.get('pair','?')} | gen {r.get('generation','?')} | {r.get('mode','?')} | {r.get('status','?')}"]
    if open_pairs and len(current) != len(open_pairs):
        lines.append('WARNING: one or more open positions have no current protection record.')
    elif not open_pairs:
        lines.append('No Freqtrade-owned open positions.')
    return '\n'.join(lines)[:3900]

def _pretty_selftest():
    cfg=_ft_data('GET','/show_config') or {}
    ping=base.ft_call('GET','/ping')
    ext=_binana_db_rows()
    telegram=base.read_json(base.RUNTIME/'telegram_health.json',{}) or {}
    spot=str(cfg.get('trading_mode','')).lower()=='spot'
    short_off=cfg.get('short_allowed') is False
    nfi=str(cfg.get('strategy',''))=='BinanaNfiSpot'
    stake=str(cfg.get('stake_amount','')) in {'250','250.0'}
    max4=float(cfg.get('max_open_trades',0) or 0)==4.0
    canonical_ok=ext.get('available') is True
    incidents=ext.get('incidents') or []
    core_ok=bool(isinstance(ping,dict) and ping.get('ok') and spot and short_off and nfi and stake and max4 and telegram.get('ok') and canonical_ok and not incidents)
    return '\n'.join([
        'SELF-TEST', '',
        f"Freqtrade API: {'PASS' if isinstance(ping,dict) and ping.get('ok') else 'FAIL'}",
        f"Strategy: {'PASS' if nfi else 'FAIL'} ({cfg.get('strategy','n/a')})",
        f"Spot only: {'PASS' if spot and short_off else 'FAIL'}",
        f"Stake / slots: {'PASS' if stake and max4 else 'FAIL'} ({cfg.get('stake_amount','n/a')} USDT / {cfg.get('max_open_trades','n/a')})",
        f"Telegram poller: {'PASS' if telegram.get('ok') else 'FAIL'}",
        f"Canonical state: {'AVAILABLE' if canonical_ok else 'UNAVAILABLE'}",
        f"Canonical incidents: {len(incidents) if canonical_ok else 'unknown'}",
        f"Core runtime checks: {'PASS' if core_ok else 'FAIL'}", '',
        'Acceptance certification: INCOMPLETE',
        'Authenticated Testnet OTOCO/OCO lifecycle, restart recovery and soak evidence are separate acceptance tests and are not implied by this screen.',
    ])


def _pretty_logs():
    raw = base._tail_audit(12)
    rows=[]
    for line in str(raw).splitlines():
        try:
            d=json.loads(line); rows.append((d.get('ts','')[-14:-6], d.get('severity','INFO'), d.get('event','event')))
        except Exception:
            continue
    lines=['📄 RECENT AUDIT EVENTS','']
    if not rows:
        lines.append('No recent audit events.')
    else:
        for ts, sev, event in rows[-10:]:
            lines.append(f'• {sev}: {event}')
    return '\n'.join(lines)




def _halal_menu():
    return [
        [{'text': 'View Approved Halal List', 'callback_data': 'do|halal_list'}],
        [{'text': 'Halal Registry Status', 'callback_data': 'do|halal_registry_status'}],
        [{'text': 'Scan One Coin', 'callback_data': 'do|scan_help'},
         {'text': 'Latest Scan Report', 'callback_data': 'do|sharia_report_help'}],
        [{'text': 'Research Queue', 'callback_data': 'do|sharia_review_queue'},
         {'text': 'Latest Scan Failure', 'callback_data': 'do|sharia_failures'}],
        [{'text': 'Manage Approved List', 'callback_data': 'do|manual_registry_help'}],
        [{'text': 'Back to Main Menu', 'callback_data': 'do|home'}],
    ]

def _load_halal_registry_symbols():
    path = base.Path('/app/shared/sharia/halal_coins.json')
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        return [], f'registry read failed: {type(exc).__name__}'
    items = (raw.get('symbols') or raw.get('coins')) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return [], 'registry format invalid'
    out=[]
    for item in items:
        if isinstance(item, str):
            symbol=item
        elif isinstance(item, dict):
            symbol=item.get('symbol') or item.get('base') or item.get('ticker')
        else:
            symbol=None
        if symbol:
            symbol=str(symbol).upper().replace('/USDT','').replace('USDT','').strip()
            if symbol and symbol not in out:
                out.append(symbol)
    return out, ''

def _pretty_halal_list():
    symbols, err = _load_halal_registry_symbols()
    if err:
        return 'HALAL COIN LIST\n\nStatus: UNAVAILABLE\nReason: ' + err
    lines=['HALAL COIN LIST','',f'Approved assets: {len(symbols)}','']
    if not symbols:
        lines.append('No approved assets are recorded.')
    else:
        chunk=12
        for i in range(0, min(len(symbols), 120), chunk):
            lines.append(', '.join(symbols[i:i+chunk]))
        if len(symbols) > 120:
            lines += ['', f'Showing first 120 of {len(symbols)} approved assets.']
    lines += ['', 'Authority: owner-maintained halal_coins.json',
              'Research scanner cannot add, remove, approve, reject or block a trade.']
    return '\n'.join(lines)[:3800]

def _pretty_halal_registry_status():
    symbols, err = _load_halal_registry_symbols()
    health = base.read_json(base.Path('/app/shared/runtime/sharia_screener/health.json'), {}) or {}
    return '\n'.join([
        'HALAL REGISTRY STATUS','',
        f"Registry readable: {'YES' if not err else 'NO'}",
        f"Approved assets in file: {len(symbols)}",
        f"Registry service healthy: {'YES' if health.get('ok') is True else 'NO'}",
        f"Registry version: {health.get('registry_version','n/a')}",
        f"Trading gate ready: {'YES' if health.get('sharia_trade_ready') is True else 'NO'}",
        '',
        'Trading permission comes only from the owner-maintained approved list.',
        'Scanner/research results are advisory and have no trading authority.',
    ])


def _pretty_halal_status():
    symbols, err = _load_halal_registry_symbols()
    if err:
        return 'HALAL LIST\n\nStatus: UNAVAILABLE\nReason: ' + err
    return '\n'.join([
        'HALAL LIST','',
        f'Approved assets: {len(symbols)}',
        'Authority: owner-maintained halal_coins.json',
        'Research scanner: advisory only; it cannot approve, reject or block trades.'
    ])


def _allocation_limit_usdt() -> float:
    # Mirrors the validated BINANA owner runtime contract; Freqtrade remains the
    # authoritative server-side enforcer even if this UI helper is unavailable.
    return 1000.0


def _pretty_sizing_status():
    cfg=_ft_data('GET','/show_config') or {}
    return '\n'.join([
        '💵 AUTO-TRADE SIZE', '',
        f"USDT per automatic trade: {_effective_trade_size()}",
        f"Total BINANA allocation cap: {_allocation_limit_usdt():g} USDT",
        'Maximum open Spot trades: 4',
        f"Automatic entries: {'ON' if str(cfg.get('state','stopped')).lower() == 'running' else 'OFF'}", '',
        'Change size with /setsize AMOUNT (example: /setsize 100).',
        'The 4-position limit remains fixed.',
    ])


def _auto_limits_menu():
    return [[{'text': '📊 Current Fixed Limits', 'callback_data': 'do|sizing_status'},
             {'text': 'Back to Controls', 'callback_data': 'do|menu_controls'}]]


def _signal_control_status_payload():
    return {'ok': True, 'mode': 'auto', 'authority': 'freqtrade',
            'detail': 'Manual sidecar signal approval is retired.'}


def _pending_signals(limit=5):
    rows=[]
    try:
        files=sorted(base.SIGNAL_INBOX.glob('*.json'), key=lambda q:q.stat().st_mtime, reverse=True)
    except Exception:
        return rows
    for path in files[:max(1,min(int(limit),10))]:
        try:
            raw=json.loads(path.read_text(encoding='utf-8'))
            sig=base.envelope.verify_envelope(raw, purpose=base.envelope.BUS_SIGNAL,
                                              expected_producers={'freqtrade'})
            rows.append({
                'signal_id': str(sig.get('signal_id') or ''),
                'pair': str(sig.get('pair') or ''),
                'candle_time': str(sig.get('candle_time') or ''),
                'entry_tag': str(sig.get('entry_tag') or ''),
            })
        except Exception:
            continue
    return rows


def _signal_control_menu_text():
    return ('🎯 SIGNAL CONTROL\n\nMode: AUTO\nAuthority: FREQTRADE\n'
            'Manual sidecar Take/Reject approval is retired. Genuine NFI signals proceed only through '
            'the Freqtrade Spot/Testnet, one-halal-decision, liquidity, allocation and reconciliation gates.')


def _signal_control_menu():
    return [[{'text':'Back to Main Menu','callback_data':'do|home'}]]





def _check_signal_text(pair: str) -> str:
    base_asset, reason = base.normalize_pair_input(pair)
    if not base_asset:
        return 'CHECK COIN SIGNAL\n\nInvalid pair: ' + reason
    if base_asset in {'BTC','ETH','BNB','SOL','XRP'}:
        return f'CHECK COIN SIGNAL\n\n{base_asset}/USDT is excluded from BINANA trading.'
    approved, err = _load_halal_registry_symbols()
    if err:
        return 'CHECK COIN SIGNAL\n\nHalal registry unavailable: ' + err
    if base_asset not in approved:
        return f'CHECK COIN SIGNAL\n\n{base_asset}/USDT is not in the owner-approved halal list.'
    pair_name=f'{base_asset}/USDT'
    data=_ft_data('GET', f'/pair_candles?pair={pair_name}&timeframe=5m&limit=2')
    rows=(data or {}).get('data') if isinstance(data,dict) else None
    if not isinstance(rows,list) or not rows:
        return f'CHECK COIN SIGNAL\n\nPair: {pair_name}\nNFI analysis: unavailable/not currently in analyzed universe.'
    raw_row=rows[-1]
    if isinstance(raw_row,dict):
        row=raw_row
    elif isinstance(raw_row,list) and isinstance(data.get('columns'),list):
        row=dict(zip(data['columns'],raw_row))
    else:
        row={}
    signal=int(row.get('enter_long',0) or 0)==1
    tag=str(row.get('enter_tag') or '')
    try:
        current=float(row.get('close') or 0)
    except Exception:
        current=0.0
    target_fraction=0.015; stop_fraction=0.01; stop_buffer=0.0015
    try:
        cfg=json.loads(base.Path('/freqtrade/binana-config/config.json').read_text(encoding='utf-8'))
        bp=cfg.get('binana',{}) if isinstance(cfg,dict) else {}
        target_fraction=float(bp.get('fixed_target_fraction',target_fraction))
        stop_fraction=float(bp.get('normal_stop_fraction',stop_fraction))
        stop_buffer=float(bp.get('stop_limit_buffer_fraction',stop_buffer))
    except Exception:
        pass
    buy_limit=current if current>0 else None
    take_profit=(current*(1.0+target_fraction)) if current>0 else None
    stop_trigger=(current*(1.0-stop_fraction)) if current>0 else None
    stop_limit=(stop_trigger*(1.0-stop_buffer)) if stop_trigger else None
    lines=['CHECK COIN SIGNAL','',f'Pair: {pair_name}',
        f'Active strategy BUY signal: {"YES" if signal else "NO"}',
        f'Entry tag: {tag or "none"}']
    if current>0:
        lines += [f'Current analyzed price: {current:.10g}',
                  f'Manual reference buy limit: {buy_limit:.10g}',
                  f'Manual reference OCO take-profit: {take_profit:.10g}',
                  f'Manual reference OCO stop trigger: {stop_trigger:.10g}',
                  f'Manual reference stop-limit: {stop_limit:.10g}']
    lines += ['Reference levels use the current unchanged BINANA target/stop configuration and are informational only; they do not place an order.',
        'Auto trade: Freqtrade acts only when the active BinanaNfiSpot signal and all Spot/halal/liquidity/slot/reconciliation gates pass.',
        'No signal is not a permanent rejection; the strategy evaluates future candles again.']
    return '\n'.join(lines)

def _r6_market_menu():
    return [
        [{'text':'Current Spot Universe','callback_data':'do|universe'}, {'text':'Market Movers','callback_data':'do|universe_movers'}],
        [{'text':'Market Context','callback_data':'do|market_context'}, {'text':'Data Readiness','callback_data':'do|data_readiness'}],
        [{'text':'CoinGecko','callback_data':'do|provider_coingecko'}, {'text':'CoinMarketCap','callback_data':'do|provider_coinmarketcap'}],
        [{'text':'Back to Main Menu','callback_data':'do|home'}],
    ]

def _r6_controls_menu():
    return [
        [{'text':'Resume Entries','callback_data':'do|entries_on_confirm'}, {'text':'Pause Entries','callback_data':'do|entries_off_confirm'}],
        [{'text':'Set Trade Amount','callback_data':'do|menu_sizing'}, {'text':'Check Coin Signal','callback_data':'do|check_signal_help'}],
        [{'text':'Status','callback_data':'do|status'}, {'text':'Market Context','callback_data':'do|market_context'}],
        [{'text':'Back to Main Menu','callback_data':'do|home'}],
    ]

def _r6_health_menu():
    return [
        [{'text':'System Health','callback_data':'do|system_health'}, {'text':'Data Readiness','callback_data':'do|data_readiness'}],
        [{'text':'Self-Test','callback_data':'do|selftest'}, {'text':'Deployment','callback_data':'do|deploy'}],
        [{'text':'Logs','callback_data':'do|logs'}, {'text':'Protection','callback_data':'do|protection_status'}],
        [{'text':'Back to Main Menu','callback_data':'do|home'}],
    ]

def _r6_alerts_menu():
    return [[{'text':'Delivery Status','callback_data':'do|alert_status'}],[{'text':'Back to Main Menu','callback_data':'do|home'}]]

def _r6_help_text():
    return ('BINANA Spot Testnet — r6 owner controls\n\n'
            'Trading engine: Freqtrade fork / BinanaNfiSpot\n'
            'Mode: Spot only, long only, Testnet only\n'
            'Order owner: Freqtrade only\n'
            'Automatic stake: owner-adjustable USDT amount; maximum occupied slots: 4\n'
            'Halal authority: owner-maintained halal_coins.json, one decision per new entry intent\n'
            'Sharia scanner: research only; no trading authority\n'
            'Protection: OTOCO entry, fixed OCO, controlled trailing-OCO promotion\n'
            'Legacy sidecar sizing, signal approval and protection mutation controls are retired.')


def _r6_universe_movers():
    raw = base._universe_movers() or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    movers = raw.get('movers', []) if isinstance(raw, dict) else []
    lines = ['MARKET MOVERS', '']
    if not movers:
        lines.append('No validated mover data is available right now.')
    else:
        for row in movers[:10]:
            pair = str(row.get('pair') or '?')
            change = float(row.get('change_pct') or 0)
            volume = float(row.get('quote_volume') or 0)
            spread = float(row.get('spread_ratio') or 0) * 100
            lines.append(f"{int(row.get('rank') or 0)}. {pair}  {change:+.2f}%")
            lines.append(f"   Volume: {volume:,.0f} USDT | Spread: {spread:.3f}%")
    lines += ['', 'Read-only market ranking. It does not authorize trades.']
    return '\n'.join(lines)[:3800]

def _r6_market_context():
    text = str(base._market_context_status() or '')
    status = 'unknown'
    health = 'unknown'
    ready = 'unknown'
    fresh = 'unknown'
    for line in text.splitlines():
        if line.startswith('status='):
            parts = dict(item.split('=',1) for item in line.split('; ') if '=' in item)
            status = parts.get('status', status)
            health = parts.get('health_ok', health)
            ready = parts.get('subscription_ready', ready)
        elif line.startswith('fresh symbols='):
            fresh = line.split('=',1)[1]
    return '\n'.join([
        'MARKET CONTEXT', '',
        f'Status: {status.upper()}',
        f'Scanner healthy: {health}',
        f'Subscriptions ready: {ready}',
        f'Fresh symbols: {fresh.replace("/", " / ")}',
        '',
        'Monitoring: aggressive buy/sell flow, CVD, spread and liquidity.',
        'Read-only evidence only. It cannot approve, reject or place a trade.'
    ])[:3800]

def _r6_provider(name=None):
    raw = base.read_json(PROVIDER_STATUS_FILE, {}) or {}
    key = 'cmc' if str(name).lower() in {'cmc','coinmarketcap'} else 'coingecko'
    row = raw.get(key, {}) if isinstance(raw, dict) else {}
    enabled = row.get('enabled') is True
    missing_key = row.get('requested_but_missing_key') is True
    label = 'CoinMarketCap' if key == 'cmc' else 'CoinGecko'
    state = 'ENABLED' if enabled else ('API KEY REQUIRED' if key == 'cmc' and missing_key else 'DISABLED')
    lines=[f'{label.upper()} — {state}', '']
    if enabled:
        age=row.get('cache_age_seconds')
        lines.append(f'Cache age: {age if age is not None else "unknown"} seconds')
        breaker=row.get('breaker', {}) if isinstance(row.get('breaker'), dict) else {}
        lines.append(f'Circuit breaker: {"OPEN" if breaker.get("open") else "closed"}')
        budget=row.get('budget', {}) if isinstance(row.get('budget'), dict) else {}
        if budget:
            lines.append(f"Quota: {budget.get('minute_used','n/a')}/{budget.get('minute_cap','n/a')} calls this minute; {budget.get('month_used','n/a')}/{budget.get('month_cap','n/a')} this month")
    elif key == 'coingecko':
        lines.append('CoinGecko enrichment is not currently active in the universe service.')
        lines.append('Binance Spot scanning continues independently.')
    elif missing_key:
        lines.append('CoinMarketCap enrichment has been requested but no bot-side API key is available.')
        lines.append('Add COINMARKETCAP_API_KEY or CMC_API_KEY to the AWS bot environment to activate it.')
    else:
        lines.append('CoinMarketCap enrichment is not currently active.')
        lines.append('It requires its own bot-side API key; Binance Spot scanning is independent.')
    lines += ['', 'Advisory metadata only; no trading authority.']
    return '\n'.join(lines)[:3800]

def _r6_provider_status():
    raw = base.read_json(PROVIDER_STATUS_FILE, {}) or {}
    rows = raw if isinstance(raw, dict) else {}
    def state(key, label):
        row=rows.get(key,{}) if isinstance(rows.get(key,{}),dict) else {}
        if row.get('enabled') is True:
            age=row.get('cache_age_seconds')
            return f'{label}: ENABLED' + (f' | cache age {age}s' if age is not None else '')
        if key=='cmc' and row.get('requested_but_missing_key') is True:
            return f'{label}: API KEY REQUIRED'
        return f'{label}: DISABLED'
    return '\n'.join(['MARKET-DATA PROVIDERS','',state('coingecko','CoinGecko'),state('cmc','CoinMarketCap'),'','Advisory metadata only; no trading authority.'])[:3800]

def _r6_data_readiness():
    raw = base._data_readiness() or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    pre = raw.get('api_preflight', {}) if isinstance(raw, dict) else {}
    providers = pre.get('providers', {}) if isinstance(pre, dict) else {}
    market = raw.get('spot_market_context', {}) if isinstance(raw, dict) else {}
    tg = raw.get('telegram', {}) if isinstance(raw, dict) else {}
    symbols, registry_err = _load_halal_registry_symbols()
    provider_state = base.read_json(PROVIDER_STATUS_FILE, {}) or {}
    cg = provider_state.get('coingecko', {}) if isinstance(provider_state, dict) else {}
    cmc = provider_state.get('cmc', {}) if isinstance(provider_state, dict) else {}
    cg_state = 'PASS' if cg.get('enabled') is True else 'DISABLED'
    if cmc.get('enabled') is True:
        cmc_state = 'PASS'
    elif cmc.get('requested_but_missing_key') is True:
        cmc_state = 'API KEY REQUIRED'
    else:
        cmc_state = 'DISABLED'
    return '\n'.join([
        'DATA READINESS', '',
        f"Binance: {providers.get('binance','UNKNOWN')}",
        f"Telegram: {'PASS' if tg.get('ok') is True else providers.get('telegram','UNKNOWN')}",
        f"Market scanner: {'PASS' if market.get('ok') is True else str(market.get('status','UNKNOWN')).upper()}",
        f"Fresh symbols: {market.get('fresh_symbol_count','n/a')}",
        f"Halal registry: {'READY' if not registry_err and symbols else 'NOT READY'}",
        f"Approved assets: {len(symbols)}",
        f"CoinGecko: {cg_state}",
        f"CoinMarketCap: {cmc_state}",
        '',
        'Trading permission comes only from the owner-maintained halal registry.',
        'External market providers are advisory only; no trading authority.',
        'Read-only readiness summary; not a LIVE-trading certification.'
    ])[:3800]

def _r6_deploy():
    return json.dumps(base.read_json(base.RUNTIME / 'deployment_status.json', {}) or {}, indent=2)[:3800]

def _r6_scan_help():
    return ('SCAN ONE COIN\n\nSend /scan TICKER, for example /scan ETH. '
            'This is research only and never changes the approved trading list automatically.')

def _r6_report_help():
    return ('LATEST SCAN REPORT\n\nSend /shariareport TICKER, for example /shariareport ETH. '
            'A research result has no trading authority.')

def _r6_review_queue():
    raw=base._sharia_review_queue()
    try:
        data=json.loads(raw) if isinstance(raw,str) else (raw or {})
    except Exception:
        data={}
    queue=data.get('research_queue',{}) if isinstance(data,dict) else {}
    return '\n'.join([
        'SHARIA RESEARCH QUEUE','',
        f"Queued: {queue.get('queued', queue.get('pending','n/a')) if isinstance(queue,dict) else 'n/a'}",
        f"Running: {queue.get('running','n/a') if isinstance(queue,dict) else 'n/a'}",
        f"Discovery candidates: {data.get('discovery_candidates','n/a') if isinstance(data,dict) else 'n/a'}",
        f"Research-source registered assets: {data.get('registered_assets','n/a') if isinstance(data,dict) else 'n/a'}",
        '',
        'This is the research-source registry, not the owner-approved trading list.',
        'Research results have no trading authority.'
    ])[:3800]

def _r6_scan_failure():
    return str(base._sharia_failure_status())[:3800]

def _r6_registry_help():
    return ('MANAGE APPROVED HALAL LIST\n\nThe approved list is owner-maintained. '
            'Scanner results do not add or remove coins automatically. Use the signed owner registry workflow only.')

def _custom_route(action, chat, message_id=None):
    human = {
        'status': _pretty_status,
        'balance': _pretty_balance,
        'open_trades': _pretty_open_trades,
        'trade_history': _pretty_history,
        'last_signal': _pretty_last_signal,
        'signal_history': _pretty_signals,
        'signal_rejected': _pretty_rejected,
        'profit': _pretty_profit,
        'daily_report': _pretty_report,
        'universe': _pretty_universe,
        'system_health': _pretty_health,
        'alert_status': _pretty_alerts,
        'protection_status': _pretty_protection,
        'selftest': _pretty_selftest,
        'logs': _pretty_logs,
        'sizing_status': _pretty_sizing_status,
        'universe_movers': _r6_universe_movers,
        'market_context': _r6_market_context,
        'data_readiness': _r6_data_readiness,
        'provider_status': _r6_provider_status,
        'deploy': _r6_deploy,
        'sharia_review_queue': _r6_review_queue,
        'sharia_failures': _r6_scan_failure,
    }
    if action in {'home','menu_dashboard'}:
        base.send('BINANA Spot TestNet r6 control panel\nFreqtrade is the sole order owner.', chat)
        return
    if action in {'menu_market_scanner','menu_research'}:
        base.edit_or_send('Market scanner — read-only Spot market evidence. It cannot authorize a trade.', chat, message_id, _r6_market_menu())
        return
    if action in {'menu_controls','menu_trading','menu_autotrade'}:
        base.edit_or_send('Testnet controls — adjustable USDT trade size, fixed max 4 occupied slots. Freqtrade/NFI remains the sole automatic entry and order owner.', chat, message_id, _r6_controls_menu())
        return
    if action in {'menu_health','menu_system'}:
        base.edit_or_send('Runtime and deployment checks for the r6 Freqtrade-owner architecture.', chat, message_id, _r6_health_menu())
        return
    if action == 'menu_alerts':
        base.edit_or_send('Alert delivery status. Alerts do not authorize trading.', chat, message_id, _r6_alerts_menu())
        return
    if action == 'check_signal_help':
        base.edit_or_send('CHECK COIN SIGNAL\n\nSend /checksignal TICKER, for example /checksignal ADA. This reads the current active BinanaNfiSpot strategy signal; it does not create a trade or change the strategy.', chat, message_id, [[{'text':'Back to Main Menu','callback_data':'do|home'}]])
        return
    if action == 'menu_help':
        base.edit_or_send(_r6_help_text(), chat, message_id, [[{'text':'Back to Main Menu','callback_data':'do|home'}]])
        return
    if action == 'menu_emergency':
        base.edit_or_send('Emergency control pauses new Freqtrade entries only. It does not sell positions or change protection.', chat, message_id, [[{'text':'⏸ Pause new entries','callback_data':'do|entries_off_confirm'}],[{'text':'Back to Main Menu','callback_data':'do|home'}]])
        return
    if action == 'menu_sharia':
        base.edit_or_send(
            'HALAL LIST\n\nOnly coins in the owner-approved halal_coins.json file can be traded. '
            'The research scanner is separate, advisory only, and cannot change trading eligibility.',
            chat, message_id, _halal_menu())
        return
    if action in {'sharia','halal_list'}:
        base.edit_or_send(_pretty_halal_list(), chat, message_id, _halal_menu())
        return
    if action in {'sharia_service','halal_registry_status'}:
        base.edit_or_send(_pretty_halal_registry_status(), chat, message_id, _halal_menu())
        return
    if action == 'scan_help':
        base.edit_or_send(_r6_scan_help(), chat, message_id, _halal_menu())
        return
    if action == 'sharia_report_help':
        base.edit_or_send(_r6_report_help(), chat, message_id, _halal_menu())
        return
    if action == 'manual_registry_help':
        base.edit_or_send(_r6_registry_help(), chat, message_id, _halal_menu())
        return
    if action == 'provider_coingecko':
        base.edit_or_send(_r6_provider('coingecko'), chat, message_id, _r6_market_menu())
        return
    if action == 'provider_coinmarketcap':
        base.edit_or_send(_r6_provider('cmc'), chat, message_id, _r6_market_menu())
        return
    if action == 'menu_signal_control' or action == 'signal_control_status':
        base.edit_or_send(_signal_control_menu_text(), chat, message_id, _signal_control_menu())
        return
    if action == 'menu_sizing':
        base.edit_or_send(_pretty_sizing_status(), chat, message_id, _auto_limits_menu())
        return
    if action == 'custom_size_help':
        base.edit_or_send(_pretty_sizing_status(), chat, message_id, _auto_limits_menu())
        return
    if action == 'custom_slots_help':
        base.edit_or_send(_pretty_sizing_status(), chat, message_id, _auto_limits_menu())
        return
    renderer=human.get(action)
    if renderer:
        base.send(renderer(), chat)
        return
    if action == 'entries_on_confirm':
        cfg=_ft_data('GET','/show_config') or {}; ext=_binana_db_rows()
        if ext.get('available') is not True:
            base.edit_or_send('ENTRIES BLOCKED\n\nCanonical BINANA state is unavailable. Resume confirmation is disabled until the canonical database is readable again.', chat, message_id, [[{'text':'Back to Main Menu','callback_data':'do|home'}]])
            return
        incidents=ext.get('incidents') or []
        active_states={'HALAL_DECIDED','RESERVED','ENTRY_PENDING','ENTRY_PARTIAL','OPEN','EXIT_PENDING','UNKNOWN'}
        occupied=sum(1 for row in (ext.get('intents') or []) if str(row.get('state') or '').upper() in active_states)
        if str(cfg.get('state','stopped')).lower() == 'running':
            if incidents:
                text='ENTRIES BLOCKED BY RECONCILIATION\n\nFreqtrade is RUNNING, but new entries are blocked because canonical BINANA incidents remain open. Use Reconcile Status first.'
            elif occupied >= 4:
                text=f'AUTOMATIC ENTRIES ENABLED — CAPACITY FULL\n\nFreqtrade is RUNNING. Occupied slots: {occupied}/4. No resume action is required; the bot will evaluate new entries when a real slot becomes available.'
            else:
                text=f'AUTOMATIC ENTRIES ENABLED\n\nFreqtrade is RUNNING. Occupied slots: {occupied}/4. New entries remain subject to Spot/Testnet, halal, liquidity, allocation and reconciliation gates.'
            base.edit_or_send(text, chat, message_id, [[{'text':'Back to Main Menu','callback_data':'do|home'}]])
            return
        button = base.confirm_button('CONFIRM resume entries', 'resume_entries')
        base.edit_or_send('Resume automatic entries? Freqtrade remains the sole order owner and all Spot/Testnet, halal, liquidity, allocation and reconciliation gates remain active.', chat, message_id, [[button],[{'text':'Cancel','callback_data':'do|home'}]])
        return
    if action == 'entries_off_confirm':
        button = base.confirm_button('CONFIRM pause entries', 'pause_entries')
        base.edit_or_send('Pause all new automatic entries? Existing positions will not be sold by this action.', chat, message_id, [[button],[{'text':'Cancel','callback_data':'do|home'}]])
        return
    if action == 'menu_protection':
        base.edit_or_send(_pretty_protection(), chat, message_id, [[{'text':'Refresh Protection Status','callback_data':'do|protection_status'}],[{'text':'Back to Main Menu','callback_data':'do|home'}]])
        return
    if action == 'reconcile':
        return dispatch_action(action, chat)
    safe_base_actions = {'alert_policy','activity','test_telegram','restart_services_info','backtest'}
    if action in safe_base_actions:
        return ORIGINAL_ROUTE(action, chat, message_id)
    base.send('Unsupported control. Open the current BINANA menu and choose an available action.', chat)
    return


def _resume_blockers_from_state(cfg, ext, market, sidecar, telegram, code_marker):
    blockers=[]
    if isinstance(ext,dict) and 'available' in ext and ext.get('available') is not True:
        blockers.append('canonical_state_unavailable')
    if not isinstance(cfg,dict) or not cfg:
        blockers.append('freqtrade_config_unavailable')
    incidents=(ext or {}).get('incidents') or []
    if incidents: blockers.append(f'open_incidents={len(incidents)}')
    intents=(ext or {}).get('intents') or []
    unresolved={'HALAL_DECIDED','RESERVED','ENTRY_PENDING','ENTRY_PARTIAL','EXIT_PENDING','UNKNOWN'}
    bad=sorted({str(r.get('state') or '').upper() for r in intents if str(r.get('state') or '').upper() in unresolved})
    if bad: blockers.append('unresolved_intents='+','.join(bad))
    open_ids={str(r.get('intent_id')) for r in intents if str(r.get('state') or '').upper()=='OPEN'}
    latest={}
    for row in (ext or {}).get('protection') or []:
        iid=str(row.get('intent_id') or '')
        try: gen=int(row.get('generation') or 0)
        except Exception: gen=0
        if iid and (iid not in latest or gen>latest[iid][0]): latest[iid]=(gen,str(row.get('status') or '').upper())
    unprotected=sorted(iid for iid in open_ids if latest.get(iid,(0,''))[1] not in {'FIXED_ACTIVE','TRAILING_ACTIVE'})
    if unprotected: blockers.append(f'unprotected_open_intents={len(unprotected)}')
    stream=(market or {}).get('stream') or {}
    if (market or {}).get('status')!='fresh' or stream.get('connected') is not True or stream.get('probation_complete') is not True:
        blockers.append('market_context_not_fresh')
    if (sidecar or {}).get('order_authority') is not False or (sidecar or {}).get('exchange_credentials_present') is not False:
        blockers.append('sidecar_authority_not_retired')
    if (telegram or {}).get('ok') is not True: blockers.append('telegram_unhealthy')
    if not isinstance(code_marker,dict) or code_marker.get('verified') is not True:
        blockers.append('freqtrade_owner_patch_not_loaded')
    return blockers


def _resume_blockers():
    cfg=_ft_data('GET','/show_config') or {}
    ext=_binana_db_rows()
    market=base.read_json(base.RUNTIME/'market_context'/'health.json',{}) or {}
    sidecar=base.read_json(base.RUNTIME/'sidecar_health.json',{}) or {}
    telegram=base.read_json(base.RUNTIME/'telegram_health.json',{}) or {}
    marker=base.read_json(base.RUNTIME/'freqtrade_owner_patch_ready.json',{}) or {}
    return _resume_blockers_from_state(cfg,ext,market,sidecar,telegram,marker)


def _command_detail(result, fallback='completed'):
    if isinstance(result, dict):
        value = _decode(result.get('result'))
        if isinstance(value, str) and value.strip():
            return value.strip()[:700]
        if isinstance(value, dict):
            for key in ('message', 'detail', 'reason', 'status', 'result'):
                text = value.get(key)
                if text not in (None, ''):
                    return str(text)[:700]
        for key in ('message', 'detail', 'reason', 'status'):
            text = result.get(key)
            if text not in (None, ''):
                return str(text)[:700]
    text = str(result or '').strip()
    return text[:700] if text and not text.startswith('{') else fallback


def _custom_confirm_action(action, args, chat):
    args = args or {}
    if action in {'signal_mode_manual','signal_mode_auto','signal_take','signal_reject'}:
        base.send('ℹ️ Manual sidecar signal approval is retired. Freqtrade is the sole entry authority and automatically applies all admission gates.', chat)
        return
    if action == 'set_size':
        try:
            value=float(args.get('usdt'))
        except (TypeError, ValueError):
            base.send('Invalid USDT amount.', chat); return
        if not math.isfinite(value) or value <= 0:
            base.send('USDT amount must be a positive finite number.', chat); return
        allocation=_allocation_limit_usdt()
        if value > allocation:
            base.send(f'USDT amount cannot exceed the {allocation:g} USDT BINANA allocation cap.', chat); return
        cfg_path=base.Path('/freqtrade/binana-config/config.json')
        # Telegram container cannot write the Freqtrade read-only config mount.
        # Persist requested size in the shared runtime override consumed by the strategy.
        override=base.Path('/app/shared/runtime/trade_size.json')
        base.atomic_write_json(override, {'usdt': value, 'updated_at': time.time(), 'source':'telegram-owner'})
        base.send(f'✅ Trade size set to {value:g} USDT for new BINANA Spot entries. Maximum open trades remains 4.', chat)
        return
    if action == 'set_max':
        base.send('Maximum concurrent open trades is fixed at 4.', chat)
        return
    if action == 'resume_entries':
        blockers=_resume_blockers()
        if blockers:
            base.send('⚠️ ENTRIES REMAIN BLOCKED\n\nResume gate failed closed:\n• ' + '\n• '.join(blockers) + '\n\nNo /start request was sent.', chat)
            return
        ft=base.ft_call('POST','/start')
        if isinstance(ft,dict) and ft.get('ok') is True:
            base.send('▶️ AUTOMATIC ENTRIES RESUMED\n\n✅ Freqtrade is the sole order owner.\n✅ Legacy execution sidecar has no Binance credentials.\n✅ New entries now use the Freqtrade admission/OTOCO path.\n\nAll Spot/Testnet, halal, liquidity, allocation and reconciliation gates remain enforced.', chat)
        else:
            base.send('⚠️ ENTRIES WERE NOT RESUMED\n\nFreqtrade start request failed safely. No sidecar order path was enabled.', chat)
        return
    if action == 'pause_entries':
        ft=base.ft_call('POST','/stopentry')
        if isinstance(ft,dict) and ft.get('ok') is True:
            base.send('⏸ AUTOMATIC ENTRIES PAUSED\n\n✅ Freqtrade is PAUSED: no new entries will be opened.\n✅ Existing Freqtrade-owned positions continue to be managed and reconciled normally.', chat)
        else:
            base.send('⚠️ Pause request was not confirmed by Freqtrade. Check /status before taking further action.', chat)
        return
    if action == 'restart_stream':
        base.send('ℹ️ Binance connectivity is owned by Freqtrade. Use configuration reload or container health recovery; the legacy sidecar stream control is retired.', chat)
        return
    if action == 'reload_config':
        ft=base.ft_call('POST','/reload_config')
        ok=isinstance(ft,dict) and ft.get('ok') is True
        base.send(('♻️ CONFIGURATION RELOADED\n\n✅ Freqtrade configuration reloaded.\nThe owner halal file will be read exactly once for each genuinely new entry intent.' if ok else '⚠️ Freqtrade configuration reload was not confirmed.'), chat)
        return
    if action == 'set_mode':
        base.send('ℹ️ Manual protection-mode switching is retired. The Freqtrade ProtectionManager owns fixed-to-trailing promotion.', chat)
        return
    if action in {'convert','break_even','lock_profit','emergency_exit','set_mode'}:
        base.send('⚠️ This legacy sidecar mutation command is disabled after the Freqtrade-owner cutover. Protection changes must go through the Freqtrade ProtectionManager so duplicate sells cannot occur.', chat)
        return
    if action == 'scan_all':
        result=base.sharia_scan_request('*', priority='bulk')
        base.send('🔍 SHARIA RESEARCH SCAN\n\nRequest queued for current Spot/USDT research.\nThis research does not change the owner-approved Halal Coin List automatically.\n'+_command_detail(result,'queued'), chat)
        return
    if action == 'scan_bulk':
        result=base.sharia_bounded_scan_requests(int(args.get('limit',0)))
        base.send('🔍 SHARIA RESEARCH BATCH\n\n'+_command_detail(result,'Research requests queued.'), chat)
        return
    if action in {'sharia_approve','sharia_reject'}:
        result=base.sharia_owner_decision('APPROVE' if action=='sharia_approve' else 'REJECT', args)
        base.send(('✅ Research review decision recorded.' if action=='sharia_approve' else '⛔ Research review rejection recorded.')+'\n'+_command_detail(result,'Decision recorded.')+'\nTrading list is unchanged unless the signed owner registry workflow applies it.', chat)
        return
    if action in {'registry_add','registry_remove'}:
        cmd='REGISTRY_ADD' if action=='registry_add' else 'REGISTRY_REMOVE'
        result=base.registry_owner_command(cmd,args)
        base.send(('🕌 Halal Coin List change queued for signed projection.\n' + _command_detail(result,'queued') + '\nThe trading gate remains fail-closed until the projector verifies and publishes it.'), chat)
        return
    return ORIGINAL_CONFIRM_ACTION(action, args, chat)

def dispatch_action(action, chat):
    renderers = {
        'status': _pretty_status,
        'balance': _pretty_balance,
        'open_trades': _pretty_open_trades,
        'trade_history': _pretty_history,
        'last_signal': _pretty_last_signal,
        'signal_history': _pretty_signals,
        'signal_rejected': _pretty_rejected,
        'profit': _pretty_profit,
        'daily_report': _pretty_report,
        'universe': _pretty_universe,
        'system_health': _pretty_health,
        'alert_status': _pretty_alerts,
        'protection_status': _pretty_protection,
        'selftest': _pretty_selftest,
        'logs': _pretty_logs,
    }
    if action == 'reconcile':
        ext=_binana_db_rows()
        if ext.get('available') is not True:
            base.send('🔁 RECONCILIATION\n\n⚠️ Canonical state is unavailable. Reconciliation status is UNKNOWN/BLOCKED; Resume remains fail-closed.',chat)
            return
        inc=ext.get('incidents') or []
        if inc:
            lines=['🔁 RECONCILIATION','',f'⚠️ Open canonical incidents: {len(inc)}']
            for row in inc[:8]: lines.append(f"• {row.get('pair','?')} | {row.get('code','UNKNOWN')} | {row.get('detail','')}")
            base.send('\n'.join(lines)[:3900],chat)
        else:
            base.send('🔁 RECONCILIATION\n\n✅ No open BINANA canonical incidents are recorded. Exchange reconciliation remains owned by Freqtrade.',chat)
        return
    renderer = renderers.get(action)
    if renderer:
        base.send(renderer(), chat)
        return
    _custom_route(action, chat)

def handle_message(message):
    chat = str(message.get('chat', {}).get('id', ''))
    user_id = message.get('from', {}).get('id')
    text = str(message.get('text', '')).strip()
    if base.is_owner(user_id, chat):
        action = resolve_button_action(text)
        if action:
            base.audit(
                'telegram_fixed_keyboard_action',
                actor='telegram-owner',
                details={'action': action},
            )
            try:
                dispatch_action(action, chat)
            except Exception as exc:
                base.audit('telegram_button_backend_failed', severity='ERROR', details={'action': action, 'error_type': type(exc).__name__, 'error': base._redact_secrets(exc)})
                base.send('⚠️ This control is temporarily unavailable. No trading action was performed.', chat)
            return
        command = text.split()[0].lower().split('@', 1)[0] if text else ''
        if command in {'/start', '/menu', '/owner'}:
            show_panel(chat)
            return
        if command == '/autosize':
            base.send(_pretty_sizing_status(), chat); return
        if command == '/setsize':
            parts=text.split()
            if len(parts) != 2:
                base.send('Use /setsize AMOUNT, for example /setsize 100.', chat); return
            try: value=float(parts[1])
            except ValueError:
                base.send('Invalid USDT amount.', chat); return
            if not math.isfinite(value) or value <= 0:
                base.send('USDT amount must be a positive finite number.', chat); return
            allocation=_allocation_limit_usdt()
            if value > allocation:
                base.send(f'USDT amount cannot exceed the {allocation:g} USDT BINANA allocation cap.', chat); return
            button=base.confirm_button(f'CONFIRM {value:g} USDT per trade','set_size',{'usdt':value})
            base.send(f'Confirm new BINANA Spot trade size: {value:g} USDT. Maximum concurrent positions stays 4.', chat, [[button]])
            return
        if command == '/checksignal':
            parts=text.split()
            if len(parts) != 2:
                base.send('Use /checksignal TICKER, for example /checksignal ADA.', chat); return
            base.send(_check_signal_text(parts[1]), chat); return
        if command in {'/maxtrades', '/setmax'}:
            base.send(_pretty_sizing_status(), chat); return
        if command == '/signalmode':
            base.send(_signal_control_menu_text(), chat); return
        if command == '/take':
            base.send('ℹ️ /take is retired; Freqtrade automatically handles genuine strategy signals.', chat); return
        if command == '/reject':
            base.send('ℹ️ /reject is retired; pause new entries if you do not want strategy signals executed.', chat); return
        if command == '/shariareport':
            parts=text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                base.send(_r6_report_help(), chat); return
            return ORIGINAL_HANDLE_MESSAGE(message)
        if command in SHORTCUTS:
            try:
                dispatch_action(SHORTCUTS[command], chat)
            except Exception as exc:
                base.audit('telegram_shortcut_backend_failed', severity='ERROR', details={'command': command, 'error_type': type(exc).__name__, 'error': base._redact_secrets(exc)})
                base.send('⚠️ This command is temporarily unavailable. No trading action was performed.', chat)
            return
    return ORIGINAL_HANDLE_MESSAGE(message)


def _deliver_sidecar_notifications_with_buttons(limit: int = 20) -> int:
    """Preserve durable sidecar inline buttons while retaining owner-only delivery."""
    try:
        state = base._load_alert_delivery_state()
    except RuntimeError as exc:
        base.audit('telegram_alert_dedupe_state_invalid', severity='CRITICAL',
                   details={'error': base._redact_secrets(exc)})
        base._alert_outbox_health(blocked_reason=str(exc))
        return 0
    delivered = state.get('delivered', {})
    processed = 0
    blocked_reason = ''
    scan_max = base.env_int('TELEGRAM_ALERT_SCAN_MAX', 1000, 20, 10000)
    for path in sorted(base.TELEGRAM_ALERT_OUTBOX.glob('*.json'))[:scan_max]:
        if processed >= max(0, int(limit)):
            break
        payload = base.read_json(path, None)
        if not isinstance(payload, dict):
            base._quarantine_alert(path, 'payload is not a JSON object')
            continue
        notification_id = str(payload.get('notification_id') or '')
        if not re.fullmatch(r'[0-9a-f]{32}', notification_id) or path.stem != notification_id:
            base._quarantine_alert(path, 'notification_id or filename is invalid')
            continue
        if notification_id in delivered:
            path.unlink(missing_ok=True)
            continue
        buttons = payload.get('buttons')
        if buttons is not None and not isinstance(buttons, list):
            base._quarantine_alert(path, 'buttons must be a list when present')
            continue
        if isinstance(buttons, list) and len(buttons) > 20:
            base._quarantine_alert(path, 'too many button rows')
            continue
        try:
            base.send(base._redact_secrets(payload.get('text', '')), base.OWNER, buttons)
        except Exception as exc:
            blocked_reason = 'Telegram alert delivery failed: ' + base._redact_secrets(exc)
            base.audit('telegram_alert_delivery_failed', severity='ERROR', details={
                'notification_id': notification_id, 'error': base._redact_secrets(exc),
            })
            break
        delivered[notification_id] = time.time()
        max_ids = max(100, int(os.getenv('TELEGRAM_ALERT_DEDUPE_MAX', '5000')))
        if len(delivered) > max_ids:
            delivered = dict(sorted(delivered.items(), key=lambda item: item[1])[-max_ids:])
        try:
            base.atomic_write_json(base.ALERT_DELIVERY_STATE, {
                'delivered': delivered, 'updated_at': time.time(),
            })
        except Exception as exc:
            blocked_reason = 'Telegram alert dedupe persistence failed: ' + base._redact_secrets(exc)
            base.audit('telegram_alert_dedupe_persist_failed', severity='CRITICAL', details={
                'notification_id': notification_id, 'error': base._redact_secrets(exc),
            })
            break
        path.unlink(missing_ok=True)
        base.audit('telegram_alert_delivered', details={'notification_id': notification_id,
                                                        'buttons_preserved': bool(buttons)})
        processed += 1
    base._alert_outbox_health(blocked_reason=blocked_reason)
    return processed


def main():
    base.send = send_fixed
    base.deliver_sidecar_notifications = _deliver_sidecar_notifications_with_buttons
    base.deliver_signal_notifications = lambda limit=20: 0
    base.handle_message = handle_message
    base.route = _custom_route
    base._confirm_action = _custom_confirm_action
    try:
        register_commands()
    except Exception as exc:
        base.audit(
            'telegram_commands_registration_failed',
            severity='WARNING',
            details={'error': base._redact_secrets(exc)},
        )
    _seed_current_trade_history()
    threading.Thread(target=_rpc_lifecycle_loop, name='freqtrade-rpc-bridge', daemon=True).start()
    threading.Thread(target=_canonical_lifecycle_loop, name='canonical-lifecycle-monitor', daemon=True).start()
    threading.Thread(target=_protection_monitor_loop, name='binana-protection-monitor', daemon=True).start()
    threading.Thread(target=_freqtrade_log_monitor_loop, name='freqtrade-log-monitor', daemon=True).start()
    threading.Thread(target=_strategy_journal_notice_loop, name='strategy-notice-monitor', daemon=True).start()
    show_panel(base.OWNER)
    base.main()


if __name__ == '__main__':
    main()
