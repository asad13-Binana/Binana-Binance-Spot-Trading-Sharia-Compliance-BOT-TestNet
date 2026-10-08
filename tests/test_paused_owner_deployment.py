"""Failure-edge regressions for the paused deployment runner; no Docker calls."""
import importlib.util
import json
from pathlib import Path
import pytest

PATH=Path(__file__).resolve().parents[1]/'scripts/recovery/deploy_paused_owner_20261008.py'

def module():
    spec=importlib.util.spec_from_file_location('paused_deployment',PATH)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod


def setup_wait(monkeypatch,tmp_path,status):
    m=module();monkeypatch.setattr(m,'DBROOT',tmp_path)
    (tmp_path/'runtime').mkdir();(tmp_path/'runtime/owner_readiness.json').write_text(json.dumps(status))
    monkeypatch.setattr(m.time,'time',lambda:1000)
    ticks=iter([0,0,181]);monkeypatch.setattr(m.time,'monotonic',lambda:next(ticks))
    monkeypatch.setattr(m.time,'sleep',lambda _:None)
    container={'Id':'candidate','Image':m.IMAGE,'State':{'StartedAt':'1970-01-01T00:16:00Z','Health':{'Status':'healthy'}}}
    monkeypatch.setattr(m,'inspect',lambda:container)
    monkeypatch.setattr(m,'api_state',lambda:{'state':'stopped','dry_run':False,'trading_mode':'spot'})
    return m


def ready():
    return {'generated_at':999,'execution_environment':'TESTNET','release_approved':False,
            'blockers':['RECONCILIATION_BLOCKER_OPEN','verified_missing'],
            'authenticated_reconciliation':{'ok':True,'checked_at':999,'open_orders':0,'open_lists':0},
            'retained_inventory':{'ok':True,'checked_at':999,'executable_pairs':[]}}


@pytest.mark.parametrize('field',['generated_at','authenticated_reconciliation','retained_inventory'])
def test_previous_boot_evidence_is_rejected(monkeypatch,tmp_path,field):
    status=ready()
    if field=='generated_at': status[field]=959
    else: status[field]['checked_at']=959
    m=setup_wait(monkeypatch,tmp_path,status)
    with pytest.raises(RuntimeError,match='STARTUP_NOT_VERIFIED'):m.wait_owner(m.IMAGE,candidate=True)


def test_fresh_file_cannot_hide_stale_component(monkeypatch,tmp_path):
    status=ready();status['authenticated_reconciliation']['checked_at']=964
    m=setup_wait(monkeypatch,tmp_path,status)
    with pytest.raises(RuntimeError,match='STARTUP_NOT_VERIFIED'):m.wait_owner(m.IMAGE,candidate=True)


def test_transient_api_warmup_retries(monkeypatch,tmp_path):
    m=setup_wait(monkeypatch,tmp_path,ready())
    ticks=iter([0,0,1]);monkeypatch.setattr(m.time,'monotonic',lambda:next(ticks))
    calls=[]
    def api():
        calls.append(1)
        if len(calls)==1:raise RuntimeError('COMMAND_FAILED:docker:1')
        return {'state':'stopped','dry_run':False,'trading_mode':'spot'}
    monkeypatch.setattr(m,'api_state',api)
    assert m.wait_owner(m.IMAGE,candidate=True)['image']==m.IMAGE
    assert len(calls)==2


def test_absent_owner_is_already_stopped(monkeypatch):
    m=module();calls=[]
    def run(args,**kw):calls.append(args);return b''
    monkeypatch.setattr(m,'run',run)
    m.stop_owner()
    assert len(calls)==1 and calls[0][1:3]==['container','ls']


def test_running_owner_cannot_be_restored(monkeypatch):
    m=module();monkeypatch.setattr(m,'run',lambda args,**kw:(m.OWNER+'\n').encode())
    monkeypatch.setattr(m,'inspect',lambda:{'Image':m.IMAGE,'State':{'Running':True}})
    with pytest.raises(RuntimeError,match='STOP_NOT_CONFIRMED'):m.stop_owner()


@pytest.mark.parametrize('changed',['startup_only','accounting','another_key'])
def test_only_observed_startup_timestamp_may_change(monkeypatch,tmp_path,changed):
    import sqlite3
    import shutil
    m=module();backup=tmp_path/'backup';backup.mkdir();current=tmp_path/'current';current.mkdir()
    name='binana-owner.sqlite'
    with sqlite3.connect(backup/name) as c:
        c.executescript("CREATE TABLE KeyValueStore(id integer,key text,datetime_value text); CREATE TABLE trades(id integer,amount real);")
        c.execute('INSERT INTO KeyValueStore VALUES(?,?,?)',(4,'startup_time','1970-01-01 00:10:00'))
        c.execute('INSERT INTO KeyValueStore VALUES(?,?,?)',(3,'bot_start_time','1970-01-01 00:09:00'))
        c.execute('INSERT INTO trades VALUES(1,2.5)')
    digest=m.logical(backup/name);shutil.copyfile(backup/name,current/name)
    with sqlite3.connect(current/name) as c:
        c.execute("UPDATE KeyValueStore SET datetime_value='1970-01-01 00:16:00' WHERE key='startup_time'")
        if changed=='accounting':c.execute('UPDATE trades SET amount=3')
        if changed=='another_key':c.execute("UPDATE KeyValueStore SET datetime_value='1970-01-01 00:16:00' WHERE key='bot_start_time'")
    monkeypatch.setattr(m,'DBROOT',current);monkeypatch.setattr(m.time,'time',lambda:1000)
    if changed=='startup_only':
        assert m.preserved_databases(backup,{name:digest},950)[name]['key']=='startup_time'
        with sqlite3.connect(current/name) as c:
            assert c.execute("SELECT datetime_value FROM KeyValueStore WHERE key='startup_time'").fetchone()[0]=='1970-01-01 00:16:00'
    else:
        with pytest.raises(RuntimeError,match='CONTENT_CHANGED_BEYOND_STARTUP_TIME'):
            m.preserved_databases(backup,{name:digest},950)


@pytest.mark.parametrize('change',['expected_schema','altered_trigger','row_change','extra_schema'])
def test_only_exact_identity_constraints_may_be_added(monkeypatch,tmp_path,change):
    import ast
    import sqlite3
    import shutil
    m=module();backup=tmp_path/'backup';backup.mkdir();current=tmp_path/'current';current.mkdir()
    source=PATH.parents[2]/'runtime_owner/owner/freqtrade/binana/state_store.py'
    tree=ast.parse(source.read_text());schema=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SCHEMA' for t in n.targets))
    name='binana-extension.sqlite'
    with sqlite3.connect(current/name) as c:
        c.executescript(schema)
        c.execute("INSERT INTO incidents(incident_id,code,detail,status,created_ts,updated_ts) VALUES('original','recovery','keep','OPEN',1,1)")
        for object_name,spec in m.EXPECTED_SCHEMA.items():
            row=c.execute('SELECT type,sql FROM sqlite_master WHERE name=?',(object_name,)).fetchone()
            assert row[0]==spec['type']
            assert m.hashlib.sha256(row[1].encode()).hexdigest()==spec['sql_sha256']
    with sqlite3.connect(current/name) as c,sqlite3.connect(backup/name) as old:c.backup(old)
    with sqlite3.connect(backup/name) as old:
        for object_name,spec in m.EXPECTED_SCHEMA.items():old.execute('DROP '+spec['type']+' '+object_name)
    digest=m.logical(backup/name)
    with sqlite3.connect(current/name) as c:
        if change=='altered_trigger':
            c.executescript("DROP TRIGGER immutable_intent_trade; CREATE TRIGGER immutable_intent_trade BEFORE UPDATE ON intents BEGIN SELECT 1; END;")
        if change=='row_change':c.execute("UPDATE incidents SET detail='changed'")
        if change=='extra_schema':c.execute('CREATE INDEX extra_index ON incidents(code)')
    monkeypatch.setattr(m,'DBROOT',current)
    if change=='expected_schema':
        assert m.preserved_databases(backup,{name:digest},0)[name]['added_identity_constraints']==m.EXPECTED_SCHEMA
        with sqlite3.connect(current/name) as c:
            assert c.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0]==3
    else:
        with pytest.raises(RuntimeError):m.preserved_databases(backup,{name:digest},0)
