import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import collect
import dashboard

class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'sample.sqlite3'
        self.cn=collect.db_open(self.path)
    def tearDown(self):
        self.cn.close();self.temp.cleanup()
    def test_create_tables(self):
        names={r[0] for r in self.cn.execute('select name from sqlite_master')}
        self.assertTrue({'raw_snapshots','candles'}.issubset(names))
    def test_provenance_and_sha(self):
        val={'id':'m42','score':0}
        self.assertEqual(collect.snapshot(self.cn,'test','42',val),1)
        row=self.cn.execute('select sha256,payload,observed_at from raw_snapshots').fetchone()
        self.assertEqual(len(row[0]),64)
        self.assertEqual(json.loads(row[1]),val)
        self.assertRegex(row[2],r'^\d{4}-\d{2}-\d{2}T')
    def test_timestamp_milliseconds_and_microseconds(self):
        self.assertEqual(collect.normalize_ms(1735689600000),1735689600000)
        self.assertEqual(collect.normalize_ms(1735689600000000),1735689600000)
    def test_closed_candle_storage(self):
        row=[1735689600000,'10','11','9','10.5','250',1735693199999]
        self.assertEqual(collect.insert_candle(self.cn,'BTCUSDT','1h',row,'test'),1)
        self.assertEqual(collect.insert_candle(self.cn,'BTCUSDT','1h',row,'test'),0)
        self.assertEqual(self.cn.execute('SELECT COUNT(*) FROM candles').fetchone()[0],1)
    def test_nba_ingestion(self):
        obj={'data':[{'id':11,'datetime':'2026-10-20T05:00:00Z','status':'final'}]}
        with patch.object(collect,'request_json',return_value=obj) as f, patch.object(collect,'stamp',return_value='2026-10-20T19:00:00+00:00'):
            added=collect.nba(self.cn,{'ORACLE_NBA_KEY':'dummy'},dt.date(2026,10,20))
        self.assertEqual(f.call_count,2)
        self.assertEqual(added,1) # duplicate response identity at same second
    def test_odds_without_key_makes_no_requests(self):
        with patch.object(collect,'request_json') as f:
            self.assertEqual(collect.odds(self.cn,{}),0)
            f.assert_not_called()
    def test_empty_feed_is_not_a_prediction(self):
        s=dashboard.snapshot(self.path,Path(self.temp.name)/'models.json')
        self.assertEqual(s['signals'],[])
        self.assertEqual(s['sources_connected'],0)
    def test_feed_shows_in_dashboard(self):
        collect.snapshot(self.cn,'nba_odds','match-1',{'yes':True})
        s=dashboard.snapshot(self.path,Path(self.temp.name)/'models.json')
        self.assertEqual(s['sources_connected'],1)
        self.assertEqual(s['source_status'][0]['provider'],'nba_odds')

if __name__=='__main__':unittest.main()
