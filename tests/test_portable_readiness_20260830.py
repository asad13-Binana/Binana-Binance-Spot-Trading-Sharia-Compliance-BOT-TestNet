import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests import _harness
from scripts import s3_backup_transport as s3
from scripts.seed_source_registry import CLAIM_CUES, candidates, infer_verdict
from services.sharia_screener.readiness import screening_readiness

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('status,expected', [('GREEN', True), ('NO_TRADE_INFO', False)])
def test_readiness_uses_signed_eligibility_not_liveness(tmp_path, status, expected):
    path = _harness.write_attested_status(tmp_path / 'status.json', [('ETH', status)])
    before = path.read_bytes()
    assert screening_readiness(path)['sharia_trade_ready'] is expected
    assert path.read_bytes() == before


def test_missing_sharia_and_bad_controller_never_become_ready(tmp_path):
    path = tmp_path / 'missing.json'
    assert screening_readiness(path)['sharia_trade_ready'] is False
    path.write_text('{}')
    assert screening_readiness(path)['sharia_trade_ready'] is False


def test_revenue_proposal_prefers_network_fees_not_fundraising():
    corporate = 'Our company revenue story and venture fundraising with many shareholders and partners expanded rapidly over several funding rounds.'
    fee = 'Users pay storage fees to operators for storing data on this network.'
    found = candidates(corporate + '\n' + fee, CLAIM_CUES['revenue'])
    assert found[0] == fee
    assert corporate in found
    assert infer_verdict(fee) == ''  # a useful quote is not religious approval


@pytest.mark.parametrize('used,total,expected', [(89,100,''), (90,100,'critical-disk-pressure'),
                                               (0,0,'disk-capacity-unknown')])
def test_disk_guard_works_without_writable_health_file(monkeypatch, used, total, expected):
    from services.execution_sidecar import main
    monkeypatch.setenv('DISK_CRITICAL_PERCENT', '90')
    monkeypatch.setattr(main.shutil, 'disk_usage', lambda path: SimpleNamespace(used=used,total=total))
    assert main.disk_pressure_reason() == expected


def test_disk_probe_error_is_unknown(monkeypatch):
    from services.execution_sidecar import main
    monkeypatch.setattr(main.shutil, 'disk_usage', Mock(side_effect=OSError('full')))
    assert main.disk_pressure_reason() == 'disk-capacity-unknown'


@pytest.mark.parametrize('profile,mode,occupants,code', [
    ('four-bot-oracle','live','',0),
    ('single-bot-testnet-experiment','testnet','',0),
    ('single-bot-testnet-experiment','live','',1),
    ('single-bot-testnet-experiment','testnet','abc bitcoin-testnet',1),
    ('single-bot-testnet-experiment','testnet','abc',1),
    ('single-bot-testnet-experiment','testnet','abc binana-testnet',0),
    ('unrecognised','testnet','',1),
])
def test_capacity_profile_is_explicit_and_never_allows_mixed_small_host(profile, mode, occupants, code):
    bash = _harness.posix_bash()
    if not bash:
        pytest.skip('POSIX bash unavailable')
    command = 'docker(){ printf "%s\\n" "$OCCUPANTS"; }; source "$1"; apply_capacity_profile'
    env = dict(os.environ, DEPLOYMENT_PROFILE=profile, INSTANCE_MODE=mode,
               COMPOSE_PROJECT_NAME='binana-testnet', OCCUPANTS=occupants)
    proc = subprocess.run([bash,'-c',command,'test',str(ROOT/'deploy/lib/capacity_profile.sh')],
                          env=env,capture_output=True,text=True,timeout=10)
    assert proc.returncode == code, proc.stderr


def test_role_environment_removes_all_other_credential_sources(monkeypatch):
    for key in ('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_PROFILE',
                'AWS_ENDPOINT_URL','AWS_WEB_IDENTITY_TOKEN_FILE','AWS_CONTAINER_CREDENTIALS_FULL_URI'):
        monkeypatch.setenv(key, 'must-not-be-used')
        assert key not in s3.role_environment()
    assert s3.role_environment()['AWS_EC2_METADATA_V1_DISABLED'] == 'true'


def test_s3_upload_is_conditional_and_readback_verified(tmp_path, monkeypatch):
    source = tmp_path / 'encrypted.tar.age'
    source.write_bytes(b'encrypted test fixture')
    calls = []
    def fake(region, *args):
        calls.append(args)
        if args[0] == 'get-object':
            Path(args[-1]).write_bytes(source.read_bytes())
    monkeypatch.setattr(s3, 'aws', fake)
    s3.transfer('ap-northeast-1','private-backups','123456789012','binana-testnet/test.age',source)
    assert calls[0][calls[0].index('--if-none-match')+1] == '*'
    assert '--checksum-sha256' in calls[0]
    assert calls[1][0] == 'get-object'


def test_s3_failed_readback_does_not_claim_durability(tmp_path, monkeypatch):
    source = tmp_path / 'test.age'; source.write_bytes(b'expected')
    def fake(region, *args):
        if args[0] == 'get-object':
            Path(args[-1]).write_bytes(b'wrong')
    monkeypatch.setattr(s3, 'aws', fake)
    with pytest.raises(RuntimeError, match='checksum'):
        s3.transfer('ap-northeast-1','private-backups','123456789012','test.age',source)


def test_s3_preflight_only_reads_and_errors_are_sanitised(monkeypatch):
    fake = Mock()
    monkeypatch.setattr(s3, 'aws', fake)
    s3.transfer('ap-northeast-1','private-backups','123456789012','preflight')
    assert fake.call_args.args[1] == 'head-bucket'
    with pytest.raises(ValueError):
        s3.transfer('bad region','private-backups','123456789012','preflight')


def test_telegram_readiness_shows_the_eligibility_blocker(monkeypatch):
    from services.telegram_broker import bot
    monkeypatch.setattr(bot,'read_json',lambda *a: {'ok':True,'sharia_trade_ready':False,
        'eligible_assets':0,'eligibility_blocker':'OWNER_REVIEWED_ELIGIBLE_EVIDENCE_REQUIRED'})
    report=json.loads(bot._data_readiness())
    assert report['sharia_screener']['sharia_trade_ready'] is False
    assert report['sharia_screener']['eligible_assets'] == 0


def test_s3_restore_download_is_get_only_and_refuses_overwrite(tmp_path, monkeypatch):
    target=tmp_path/'download.age'
    def fake(region,*args):
        assert args[0] == 'get-object'
        Path(args[-1]).write_bytes(b'age ciphertext')
    monkeypatch.setattr(s3,'aws',fake)
    s3.transfer('ap-northeast-1','private-backups','123456789012','restore.age',target,download_only=True)
    with pytest.raises(ValueError,match='overwrite'):
        s3.transfer('ap-northeast-1','private-backups','123456789012','restore.age',target,download_only=True)


def test_s3_cli_error_does_not_include_raw_service_output(monkeypatch):
    monkeypatch.setattr(s3.subprocess,'run',lambda *a,**k: SimpleNamespace(
        returncode=1,stderr=b'private secret value',stdout=b'account data'))
    with pytest.raises(RuntimeError) as error:
        s3.aws('ap-northeast-1','head-bucket','--bucket','private-backups')
    assert 'private secret' not in str(error.value)


@pytest.mark.skipif(os.name != 'posix', reason='Linux disk-guard subprocess integration')
@pytest.mark.parametrize('status_failure',[False,True])
def test_real_disk_guard_queues_pause_even_when_logger_or_status_write_fails(tmp_path,status_failure):
    import sys
    deploy=tmp_path/'deploy'; (deploy/'lib').mkdir(parents=True)
    persist=tmp_path/'persist'; (persist/'runtime').mkdir(parents=True)
    (persist/'commands/inbox').mkdir(parents=True)
    app=tmp_path/'app'; app.mkdir(); (app/'current').symlink_to(ROOT,target_is_directory=True)
    (deploy/'instance_identity.sh').write_text(
        f'PERSIST="{persist}"\nPRIVATE_ROOT="{tmp_path}"\nAPP_ROOT="{app}"\n')
    key=_harness.TEST_BUS_KEYS['COMMAND_HMAC_KEY']
    (deploy/'lib/secure_env.sh').write_text(
        f'secure_env_read(){{ DISK_ENV[COMMAND_HMAC_KEY]="{key}"; }}\n')
    script=deploy/'disk_guard.sh'
    script.write_text((ROOT/'deploy/disk_guard.sh').read_text())
    command = '''
df(){ printf 'Filesystem blocks Used Available Capacity Mounted\nfixture 100 95 5 95%% /\n'; }
logger(){ return 1; }
python3(){
  if [[ "$FAIL_STATUS" == true && "${2:-}" == *disk_status.json ]]; then return 1; fi
  "$TEST_PYTHON" "$@"
}
source "$1"
'''
    env=dict(os.environ,TEST_PYTHON=sys.executable,FAIL_STATUS=str(status_failure).lower())
    result=subprocess.run([_harness.posix_bash(),'-c',command,'guard',str(script)],
                          env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode == (3 if status_failure else 2), result.stderr
    queued=list((persist/'commands/inbox').glob('*.json'))
    assert len(queued) == 1
    assert json.loads(queued[0].read_text())['payload']['args'] == {'enabled':False}
