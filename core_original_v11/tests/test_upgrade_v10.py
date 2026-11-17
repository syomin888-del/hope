"""Migration never mutates the original database and rejects overwrite."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import upgrade_from_v09

class UpgradeTest(unittest.TestCase):
    def test_copy_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            old=Path(td)/'old';new=Path(td)/'new';old.mkdir();new.mkdir()
            src=old/'oracle.sqlite3'
            with sqlite3.connect(src) as c:
                c.execute('CREATE TABLE raw_snapshots(id INTEGER PRIMARY KEY)')
                c.execute('CREATE TABLE candles(id INTEGER PRIMARY KEY)')
                c.execute('INSERT INTO raw_snapshots VALUES (123)')
            with patch.object(upgrade_from_v09,'ROOT',new):
                msg=upgrade_from_v09.migrate(old)
                self.assertIn('original unchanged',msg)
                with sqlite3.connect(new/'oracle.sqlite3') as c:
                    self.assertEqual(c.execute('SELECT id FROM raw_snapshots').fetchone()[0],123)
                with self.assertRaises(FileExistsError):upgrade_from_v09.migrate(old)
            with sqlite3.connect(src) as c:
                self.assertEqual(c.execute('SELECT id FROM raw_snapshots').fetchone()[0],123)

if __name__=='__main__':unittest.main()
