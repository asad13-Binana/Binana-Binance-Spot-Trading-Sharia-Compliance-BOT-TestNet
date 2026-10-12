"""Isolated owner regressions; full exact-image validation is separate."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest

ROOT=Path(__file__).resolve().parents[1]
OWNER=ROOT/'runtime_owner'
MODULES='''test_protection_plan_durability test_audit_20261012 test_panel_audit_20261012
test_partial_promotion_recovery test_restart_partial_recovery test_release_evidence
test_engine_core test_final_contracts test_execution_venue test_r18_regressions
test_partial_exit_reconciliation test_fill_identity test_commission_reserve
test_identity_integrity test_private_event_ordering test_registry_schema
test_pair_exclusions test_user_stream_health test_telegram_settlements
test_telegram_release test_zero_fill_entry_cleanup test_release_signature'''.split()


def main():
    for key in list(os.environ):
        if any(part in key.upper() for part in ('TOKEN','SECRET','API_KEY','AWS_','BINANCE_','TELEGRAM_')):
            del os.environ[key]
    def offline(event,args):
        if event in {'socket.connect','socket.connect_ex','socket.bind','socket.getaddrinfo',
                     'subprocess.Popen','os.system','os.posix_spawn','os.spawn'}:
            raise RuntimeError('OFFLINE_OWNER_TEST_DENIED:'+event)
    sys.addaudithook(offline)
    sys.dont_write_bytecode=True
    sys.path[:0]=[str(OWNER/'owner'),str(OWNER/'tests')]
    alias=types.ModuleType('binana_tests'); alias.__path__=[str(OWNER/'tests')]
    sys.modules['binana_tests']=alias
    spec=importlib.util.spec_from_file_location('image_validator',OWNER/'deployment/validate_image.py')
    validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
    manifest=json.loads((OWNER/'CANDIDATE_MANIFEST.json').read_text())
    required=[name for name in manifest['validation']['required_test_ids'] if name.split('.')[0] in MODULES]
    suite=unittest.defaultTestLoader.loadTestsFromNames(MODULES)
    validator.require_tests(suite,required)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__=='__main__':
    raise SystemExit(main())
