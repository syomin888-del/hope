"""Offline install checks; never touch live ORACLE volume or Docker."""
from pathlib import Path
import importlib.util, sqlite3, tempfile, unittest, shutil
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('v55_install',ROOT/'install.py')
install=importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)
SOURCES=ROOT/'tests'/'fixtures'/'base_v53'
class InstallV55Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory()
  self.project=Path(self.temp.name)/'ORACLE'
  self.project.mkdir();(self.project/'compose.yaml').write_text('services: {}\n')
  old={'oracle_v5.py':SOURCES/'oracle_v5.py',
       'oracle_v4.py':SOURCES/'oracle_v4.py',
       'oracle_console.py':SOURCES/'oracle_console.py',
       'oracle_v5.html':SOURCES/'oracle_v5.html'}
  for name,source in old.items():shutil.copy2(source,self.project/name)
  for name in ('oracle_fonbet_full.py','oracle_fonbet_bridge.py','oracle_v4_sports.py'):
   (self.project/name).write_text('pass\n')
  self.db=self.project/'oracle.sqlite3'
  with sqlite3.connect(self.db) as c:
   for table in ('sf_forecasts','sf_matches','v4_games','v4_sport_preds','fb_full_current',
                 'fb_full_events','fb_full_history','fb_full_packets','fb_bridge_links','v5_forecasts'):
    c.execute('CREATE TABLE '+table+'(id TEXT)')
 def tearDown(self):self.temp.cleanup()
 def test_correct_installed_v53_is_accepted(self):
  self.assertIn('PREFLIGHT OK',install.check(self.project,self.db,docker=False))
 def test_unknown_source_blocks_any_overwrite(self):
  (self.project/'oracle_v5.py').write_text('unrecognized modifications\n')
  with self.assertRaisesRegex(RuntimeError,'UNRECOGNIZED_INSTALLED_SOURCE'):
   install.check(self.project,self.db,docker=False)
 def test_corrupt_release_fails_hash_validation(self):
  import json
  m=json.loads((ROOT/'manifest.json').read_text())
  for name in install.FILES:
   self.assertEqual(install.sha(ROOT/'files'/name),m['files'][name]['sha256'])
 def test_new_module_is_additive_only(self):
  self.assertIn('oracle_v5_settlement.py',install.NEW)
  self.assertNotIn('oracle.sqlite3',install.FILES)
if __name__=='__main__':unittest.main()
