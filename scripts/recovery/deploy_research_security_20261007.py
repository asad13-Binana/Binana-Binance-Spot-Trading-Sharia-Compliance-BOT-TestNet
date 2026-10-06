import json,subprocess,pathlib,hashlib,os,shutil,datetime,sys,time
if sys.flags.optimize: raise RuntimeError('Optimized Python is forbidden')
root=pathlib.Path('/var/backups/binana-testnet/research-security-20261007');root.mkdir(mode=0o700,exist_ok=False)
def run(args):
 r=subprocess.run(args,capture_output=True,text=True)
 if r.returncode: raise RuntimeError('Command failed: '+args[0]+' '+args[1]+' (private output suppressed)')
 return r.stdout
base=['docker','compose','--env-file','/etc/binana-testnet/.env','-p','binana-testnet','-f','/opt/binana-testnet/current/docker-compose.yml','-f','/home/ubuntu/binana-runtime-fixes/compose.runtime-fixes.yml','-f','/home/ubuntu/binana-runtime-fixes/compose.freqtrade-owner.yml']
names=['freqtrade','telegram-broker','universe','execution-sidecar','sharia-screener','sharia-research','sharia-egress-proxy'];before={n:json.loads(run(['docker','inspect','binana-testnet-'+n+'-1']))[0] for n in names}
(root/'container-private-backup.json').write_text(json.dumps(before));os.chmod(root/'container-private-backup.json',0o600)
old=before['sharia-research'];env=dict(e.split('=',1) for e in old['Config']['Env']);cfg=json.loads(run(base+['-f','/home/ubuntu/binana-runtime-fixes/compose.sharia-v193.yml','config','--format','json']))['services']['sharia-research']
assert all(str(v)==env.get(k) for k,v in cfg['environment'].items());assert env['ENVELOPE_RELEASE_HASH'] and env['SHARIA_IDLE_SCAN_ENABLED']=='false'
assert env['SHARIA_FILE']=='/app/shared/sharia_research/status.json' and env['LEGACY_HALAL_FILE']=='/app/shared/sharia_research/research_green.json'
assert not any(env.get(k) for k in ['BINANCE_API_KEY','BINANCE_API_SECRET','FREQTRADE__EXCHANGE__KEY','FREQTRADE__EXCHANGE__SECRET'])
for n in ['telegram-broker','sharia-screener']:assert dict(e.split('=',1) for e in before[n]['Config']['Env'])['ENVELOPE_RELEASE_HASH']==env['ENVELOPE_RELEASE_HASH']
source=pathlib.Path('/home/ubuntu/binana-recovery-20261007-source/shared/sharia/HALAL_CRYPTO_SPOT_SCREENING_V19_3_PRODUCTION.json');raw=source.read_bytes();assert hashlib.sha256(raw).hexdigest()=='418e7280f0b6a5f4cd9ba3887b8be3099f5fcc4b18bfca66808749720a4dd355'
target=pathlib.Path('/var/lib/binana-testnet/shared/sharia')/source.name
assert target.is_file() and target.read_bytes()==raw
override=pathlib.Path('/home/ubuntu/binana-runtime-fixes/compose.sharia-v193.yml')
new={'services':{'sharia-research':{'image':'binana-testnet-services@sha256:b74ef9a872624554a9377c50af7eec878ed2cd828730151a24970fbd1d8d58e4','environment':{'SHARIA_CONTROLLER_FILE':'/app/shared/sharia/'+source.name}}}}
rollback={'services':{'sharia-research':{'image':old['Image'],'environment':{'SHARIA_CONTROLLER_FILE':env['SHARIA_CONTROLLER_FILE']}}}}
(root/'rollback.compose.json').write_text(json.dumps(rollback))
old_override=override.read_bytes();(root/'override.before').write_bytes(old_override)
image_pins=pathlib.Path('/home/ubuntu/binana-runtime-fixes/compose.running-images-20261007.json')
old_pins=image_pins.read_bytes();(root/'image-pins.before').write_bytes(old_pins)
old_registry=hashlib.sha256(pathlib.Path('/var/lib/binana-testnet/shared/sharia/halal_coins.json').read_bytes()).hexdigest()
run(['docker','image','inspect',new['services']['sharia-research']['image']])
try:
 staged=override.with_suffix('.security-stage');staged.write_text(json.dumps(new));os.replace(staged,override)
 run(['docker','stop','--time','30',old['Id']])
 for name in ['sharia_research','runtime/sharia_research']:shutil.copytree(pathlib.Path('/var/lib/binana-testnet/shared')/name,root/'state'/name)
 run(base+['-f',str(override),'up','-d','--no-deps','--no-build','--pull','never','sharia-research'])
 current=json.loads(run(['docker','inspect','binana-testnet-sharia-research-1']))[0];new_env=dict(e.split('=',1) for e in current['Config']['Env'])
 assert current['Image']==json.loads(run(['docker','image','inspect','binana-testnet-services@sha256:b74ef9a872624554a9377c50af7eec878ed2cd828730151a24970fbd1d8d58e4']))[0]['Id']
 assert all(new_env.get(k)==v for k,v in env.items() if k not in ['SHARIA_CONTROLLER_FILE','PYTHON_SHA256','PYTHON_VERSION','GPG_KEY','LANG','PATH'])
 assert sorted(current['Mounts'],key=lambda m:m['Destination'])==sorted(old['Mounts'],key=lambda m:m['Destination'])
 for n in names:
  if n!='sharia-research':assert json.loads(run(['docker','inspect','binana-testnet-'+n+'-1']))[0]['Id']==before[n]['Id']
 assert hashlib.sha256(pathlib.Path('/var/lib/binana-testnet/shared/sharia/halal_coins.json').read_bytes()).hexdigest()==old_registry
 probe="import json,os,hashlib;from pathlib import Path;from services.common.sharia_v19 import load_controller,V19_CONTROLLER_SHA256; p=Path(os.environ['SHARIA_CONTROLLER_FILE']);load_controller(p); h=json.loads(Path(os.environ['SHARIA_RUNTIME_DIR'],'health.json').read_text());print(json.dumps({'controller_sha256':V19_CONTROLLER_SHA256,'health_ok':h.get('ok'),'health_ts':h.get('ts'),'ready_for_screening':h.get('ready_for_screening'),'controller_integrity':h.get('controller_integrity'),'keys_loaded':bool(os.getenv('COINGECKO_API_KEY') or os.getenv('COINMARKETCAP_API_KEY') or os.getenv('CMC_API_KEY'))}))"
 deadline=time.time()+75;verified=None
 while time.time()<deadline:
  result=subprocess.run(['docker','exec','binana-testnet-sharia-research-1','python','-c',probe],capture_output=True,text=True)
  if result.returncode==0:
   report=json.loads(result.stdout)
   if report.get('health_ok') is True and float(report.get('health_ts',0))>datetime.datetime.fromisoformat(current['State']['StartedAt'].replace('Z','+00:00')).timestamp():verified=report;break
  time.sleep(5)
 assert verified is not None,'Research health did not become current'
 receipt={'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'old_image':old['Image'],'new_image':current['Image'],'controller':verified,'registry_unchanged':True,'other_container_ids_unchanged':True,'idle_scan_enabled':False,'candidate_limitations':['typed adverse verdicts and retry evidence incomplete','autonomous official-domain traversal incomplete','old Telegram labels may show V19.1']}
 image_details=json.loads(run(['docker','image','inspect','binana-testnet-services@sha256:b74ef9a872624554a9377c50af7eec878ed2cd828730151a24970fbd1d8d58e4']))[0]
 digest_reference=next(value for value in image_details['RepoDigests'] if value.startswith('binana-testnet-services@sha256:'))
 assert json.loads(run(['docker','image','inspect',digest_reference]))[0]['Id']==current['Image']
 pins=json.loads(old_pins);pins['services']['sharia-research']['image']=digest_reference
 staged_pins=image_pins.with_suffix('.security-stage');staged_pins.write_text(json.dumps(pins,indent=2)+'\n');os.replace(staged_pins,image_pins)
 receipt['source_commit']='0c4a9d82dcf117a516eb5c77a0052daa74441266'
 versions=json.loads(run(['docker','exec','binana-testnet-sharia-research-1','python','-c',"import importlib.metadata as m,json;print(json.dumps({n:m.version(n) for n in ['multidict','pypdf','urllib3']}))"]))
 assert versions=={'multidict':'6.9.1','pypdf':'6.19.0','urllib3':'2.8.0'}
 receipt['security_dependency_versions']=versions
 (root/'receipt.json').write_text(json.dumps(receipt,indent=2));print(json.dumps(receipt))
except BaseException:
 override.write_bytes(old_override);image_pins.write_bytes(old_pins)
 run(base+['-f',str(root/'rollback.compose.json'),'up','-d','--no-deps','--no-build','--pull','never','sharia-research']);raise
