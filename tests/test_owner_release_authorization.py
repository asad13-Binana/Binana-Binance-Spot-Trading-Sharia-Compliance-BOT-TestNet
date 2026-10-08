"""Authorization binds completed acceptance to the same current owner boot."""
import importlib.util
from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]/'scripts/recovery'

def module():
    spec=importlib.util.spec_from_file_location('authorization',ROOT/'authorize_verified_owner_20261008.py')
    m=importlib.util.module_from_spec(spec)
    sys.path.insert(0,str(ROOT))
    try:spec.loader.exec_module(m)
    finally:sys.path.pop(0)
    return m


def status():
    return {'code_sha256':'a'*64,'config_sha256':'b'*64,'account_key_sha256':'c'*64,
            'exchange_epoch_id':'same-testnet-epoch','blockers':['release_evidence_unavailable','RECONCILIATION_BLOCKER_OPEN']}


@pytest.mark.parametrize('field',['code_sha256','config_sha256','account_key_sha256','exchange_epoch_id'])
def test_acceptance_cannot_move_to_different_runtime(monkeypatch,field):
    m=module();accepted=status();current=status();current[field]='different'
    container={'Id':'same','Image':m.deploy.IMAGE,'State':{'StartedAt':'one-boot'}}
    monkeypatch.setattr(m.deploy,'inspect',lambda:container)
    monkeypatch.setattr(m.deploy,'wait_owner',lambda *a,**kw:{'readiness':current})
    with pytest.raises(RuntimeError,match='ACCEPTED_RUNTIME_BINDINGS_CHANGED'):
        m.verified_boot({'stages':[{'readiness':accepted}]})


def test_same_container_restart_invalidates_prior_readiness(monkeypatch):
    m=module();containers=iter([{'Id':'same','Image':m.deploy.IMAGE,'State':{'StartedAt':'old'}},
                              {'Id':'same','Image':m.deploy.IMAGE,'State':{'StartedAt':'new'}}])
    monkeypatch.setattr(m.deploy,'inspect',lambda:next(containers))
    monkeypatch.setattr(m.deploy,'wait_owner',lambda *a,**kw:{'readiness':status()})
    with pytest.raises(RuntimeError,match='OWNER_BOOT_CHANGED'):
        m.verified_boot({'stages':[{'readiness':status()}]})


def test_unresolved_runtime_failure_cannot_be_certified():
    m=module();s=status();s['blockers'].append('PRIVATE_USER_STREAM_UNAVAILABLE')
    with pytest.raises(RuntimeError,match='UNEXPECTED_RELEASE_BLOCKERS'):
        m.certificate_from_status(s,{},Path('receipt.json'))
