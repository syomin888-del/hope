import importlib.util,os,sqlite3,tempfile,unittest,json,hashlib,shutil
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('v7_installer',ROOT/'install.py');installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
class TestInstallerV7(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name)/'root';self.root.mkdir()
  (self.root/'compose.yaml').write_text('services:\n  sportslab: {}\n  dashboard: {}\n')
  self.db=Path(self.tmp.name)/'db.sqlite3'
  with sqlite3.connect(self.db) as c:
   for t in ('v5_catalog','v5_forecasts','v55_paper','v56_portfolio','fb_full_packets','mw_quotes'):c.execute('CREATE TABLE '+t+'(id TEXT)')
  self.manifest=json.loads((ROOT/'manifest.json').read_text())
  for name,m in self.manifest['files'].items():
   if m['base_sha256'] is None:continue
   if name=='oracle_v5.py':src=Path('/mnt/data/ORACLE_V5_7_KICKOFF_REPAIR/files/oracle_v5.py')
   elif name=='oracle_v5_settlement.py':src=Path('/mnt/data/ORACLE_V5_6_1_INSTALLER_FIX/files/oracle_v5_settlement.py')
   elif name=='oracle_v56_portfolio.py':src=Path('/mnt/data/ORACLE_V5_6_1_INSTALLER_FIX/files/oracle_v56_portfolio.py')
   elif name=='oracle_console.py':src=Path('/mnt/data/ORACLE_V5_6_1_INSTALLER_FIX/files/oracle_console.py')
   elif name=='oracle_scorecard.py':src=Path('/mnt/data/_oracle_v7_analysis/consolidated/files/oracle_scorecard.py')
   else:raise RuntimeError(name)
   shutil.copy2(src,self.root/name)
 def test_preflight_good(self):self.assertIn('PREFLIGHT OK',installer.check(self.root,self.db,False))
 def test_installs_and_preserves_db(self):
  installer.apply(self.root,self.db,False)
  self.assertTrue((self.root/'oracle_v7.py').exists())
  with sqlite3.connect(self.db) as c:self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')
  self.assertTrue(list((self.root/'backups').rglob('oracle-before.sqlite3')))
 def test_invalid_fingerprint_fails_closed(self):
  (self.root/'oracle_v5.py').write_text('bad')
  with self.assertRaisesRegex(RuntimeError,'SOURCE_HASH_MISMATCH'):installer.check(self.root,self.db,False)
 def test_existing_v58_cannot_overwrite_silently(self):
  (self.root/'oracle_v58_brain.py').write_text('existing')
  with self.assertRaisesRegex(RuntimeError,'NEW_FILE_ALREADY_EXISTS'):installer.check(self.root,self.db,False)
 def test_rollback_after_injected_failure(self):
  old=(self.root/'oracle_v5.py').read_bytes()
  orig=installer.atomic;step={'n':0}
  def bad_copy(a,b):
   step['n']+=1
   if step['n']==3:raise IOError('Injected disk failure')
   return orig(a,b)
  with patch.object(installer,'atomic',bad_copy):
   with self.assertRaisesRegex(IOError,'Injected'):installer.apply(self.root,self.db,False)
  self.assertEqual((self.root/'oracle_v5.py').read_bytes(),old)
  self.assertFalse((self.root/'oracle_v7.py').exists())
 def test_corrupted_archive_rejected(self):
  with patch.object(installer,'digest',lambda p:'not-correct'):
   with self.assertRaisesRegex(RuntimeError,'RELEASE_HASH_MISMATCH'):installer.check(self.root,self.db,False)
if __name__=='__main__':unittest.main()
