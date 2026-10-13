"""Rollout compares economics strictly while allowing verified metadata refresh."""
import importlib.util
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/recovery/deploy_candidate_owner.py'


class RolloutAccounting(unittest.TestCase):
    def setup_pair(self, tmp):
        spec=importlib.util.spec_from_file_location('rollout_accounting_fixture',SCRIPT)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        backup=Path(tmp)/'backup';live=Path(tmp)/'live';backup.mkdir();live.mkdir()
        module.DB=live
        for root in (backup,live):
            with closing(sqlite3.connect(root/'binana-owner.sqlite')) as c,c:
                c.execute('create table trades(id integer, amount real)');c.execute('insert into trades values(1,10)')
            with closing(sqlite3.connect(root/'binana-extension.sqlite')) as c,c:
                c.execute('create table incidents(incident_id text, code text, detail text, status text, created_ts real, updated_ts real)')
                c.execute("insert into incidents values('retained-inventory-verification','READ','ok','CLOSED',1,2)")
        return module,backup,live

    def test_rechecking_already_closed_incident_does_not_change_accounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            module,backup,live=self.setup_pair(tmp)
            with closing(sqlite3.connect(live/'binana-extension.sqlite')) as c,c:c.execute('update incidents set updated_ts=3')
            changes=module.preserve_accounting(backup)
            self.assertIn('binana-extension.sqlite:incidents',changes)

    def test_economic_or_incident_content_change_is_still_blocked(self):
        for name,sql in [('binana-owner.sqlite','update trades set amount=9'),
                ('binana-extension.sqlite',"update incidents set status='OPEN'"),
                ('binana-extension.sqlite',"update incidents set detail='different'"),
                ('binana-extension.sqlite','update incidents set updated_ts=1')]:
            with self.subTest(sql=sql),tempfile.TemporaryDirectory() as tmp:
                module,backup,live=self.setup_pair(tmp)
                with closing(sqlite3.connect(live/name)) as c,c:c.execute(sql)
                with self.assertRaises(RuntimeError):module.preserve_accounting(backup)
