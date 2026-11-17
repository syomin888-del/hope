import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import backup
import collect
import knowledge
import research
import storage
import brain_daemon

UTC=dt.timezone.utc

class StorageUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.dir=Path(self.tmp.name)
        self.db=self.dir/'oracle.sqlite3'
        self.arch=self.dir/'archive'
        self.bk=self.dir/'backups'
        self.patches=[
            mock.patch.object(collect,'DB',self.db),
            mock.patch.object(storage,'ARCHIVE',self.arch),
            mock.patch.object(backup,'DIR',self.bk)
        ]
        for p in self.patches:p.start()
        self.c=collect.db_open(self.db)
        knowledge.setup(self.c)
        storage.setup(self.c)

    def tearDown(self):
        self.c.close()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def insert_old(self,payload,days,subject='token'):
        value=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False)
        sha=hashlib.sha256(value.encode()).hexdigest()
        t=(dt.datetime.now(UTC)-dt.timedelta(days=days)).isoformat(timespec='seconds')
        with self.c:
            cur=self.c.execute('''INSERT INTO raw_snapshots
             (provider,subject,observed_at,source_time,sha256,payload)
             VALUES (?,?,?,?,?,?)''',('coingecko_market',subject,t,t,sha,value))
        return cur.lastrowid,sha,value

    def test_knowledge_not_processed_is_untouched(self):
        old,sha,_=self.insert_old({'current_price':1,'blob':'x'*1000},70)
        self.insert_old({'current_price':2},1)
        result=storage.compress_sources(self.c,age_days=30)
        self.assertEqual(result['status'],'not_ready')
        self.assertEqual(self.c.execute('SELECT typeof(payload) FROM raw_snapshots WHERE id=?',(old,)).fetchone()[0],'text')

    def test_lossless_compression_and_latest_retained(self):
        old,sha,text=self.insert_old({'current_price':1,'extra':'x'*2500},70)
        recent,_,_=self.insert_old({'current_price':2},1)
        with self.c:
            self.c.execute("INSERT INTO knowledge_cursor(name,last_id) VALUES('raw_snapshots',?)",(recent,))
        result=storage.compress_sources(self.c,age_days=30)
        self.assertEqual(result['rows'],1)
        packed=self.c.execute('SELECT payload FROM raw_snapshots WHERE id=?',(old,)).fetchone()[0]
        self.assertIsInstance(packed,bytes)
        self.assertEqual(storage.unpack(packed,sha),text)
        self.assertEqual(self.c.execute('SELECT typeof(payload) FROM raw_snapshots WHERE id=?',(recent,)).fetchone()[0],'text')

    def test_invalid_provenance_not_compressed(self):
        old,_,_=self.insert_old({'current_price':1,'extra':'x'*2500},70)
        recent,_,_=self.insert_old({'current_price':2},1)
        with self.c:
            self.c.execute('UPDATE raw_snapshots SET sha256=? WHERE id=?',('bad',old))
            self.c.execute("INSERT INTO knowledge_cursor(name,last_id) VALUES('raw_snapshots',?)",(recent,))
        self.assertEqual(storage.compress_sources(self.c)['rows'],0)

    def test_backup_verified_and_restorable(self):
        self.insert_old({'current_price':1},1)
        self.c.commit()
        f=backup.perform(keep=2)
        self.assertTrue(f.exists())
        raw=gzip.open(f,'rb').read()
        restored=self.dir/'restored.sqlite3'
        restored.write_bytes(raw)
        with sqlite3.connect(str(restored)) as cn:
            self.assertEqual(cn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(cn.execute('SELECT COUNT(*) FROM raw_snapshots').fetchone()[0],1)
        self.assertEqual(len(list(self.bk.glob('oracle-*.sqlite3.gz'))),1)

    def test_archive_failure_is_visible_to_models(self):
        storage.setup(self.c)
        with self.c:
            self.c.execute('''INSERT INTO candle_archives VALUES
            ('binance_archive','BTCUSDT','1h','2024-01',
             'candles/binance_archive/BTCUSDT/2024-01.parquet',720,'sha','sha','2024-03-01T00:00:00+00:00')''')
        with self.assertRaises(RuntimeError):
            research.get_candles(self.c,'BTCUSDT')

    def test_no_unsafe_deletion_when_missing_archive_library(self):
        with self.c:
            self.c.execute('''CREATE TABLE IF NOT EXISTS history_downloads(
              symbol TEXT,year INT,month INT,status TEXT,rows INT,updated_at TEXT,
              error TEXT, PRIMARY KEY(symbol,year,month))''')
            self.c.execute('''INSERT INTO history_downloads
                 VALUES('BTCUSDT',2020,1,'ok',24,'2020-01-01T00:00:00+00:00','')''')
            for hour in range(24):
                self.c.execute('''INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?,?)''',
                     ('binance_archive','BTCUSDT','1h',1577836800000+hour*3600000,
                      1.,2.,.5,1.5,100.,'2020-01-01T00:00:00+00:00'))
        result=storage.archive_month(self.c)
        self.assertIn(result['status'],('duckdb_unavailable','ok'))
        # Without DuckDB no candle is ever removed; with DuckDB exact roundtrip is required.
        if result['status']=='duckdb_unavailable':
            self.assertEqual(self.c.execute('SELECT COUNT(*) FROM candles').fetchone()[0],24)
        else:
            self.assertEqual(len(storage.archived_candles(self.c,'BTCUSDT')),24)

    def test_backup_archive_mirror_is_incremental(self):
        content=b'confirmed historical Parquet partition'
        sha=hashlib.sha256(content).hexdigest()
        rel='candles/binance_archive/BTCUSDT/2020-01.parquet'
        source=self.arch/rel
        source.parent.mkdir(parents=True,exist_ok=True)
        source.write_bytes(content)
        with self.c:
            self.c.execute('''INSERT INTO candle_archives VALUES
              (?,?,?,?,?,?,?,?,?)''',('binance_archive','BTCUSDT','1h','2020-01',
               rel,1,'rowsha',sha,'2020-03-01T00:00:00+00:00'))
        self.assertEqual(backup.mirror_archives(),1)
        self.assertEqual(backup.mirror_archives(),0)
        self.assertEqual((self.bk/'archive_mirror'/rel).read_bytes(),content)

    def test_vacuum_rejects_active_daemon(self):
        now=dt.datetime.now(UTC).isoformat(timespec='seconds')
        self.c.execute('''CREATE TABLE IF NOT EXISTS telemetry_heartbeats
              (service TEXT PRIMARY KEY,last_seen_utc TEXT,state TEXT,details TEXT)''')
        self.c.execute('INSERT INTO telemetry_heartbeats VALUES(?,?,?,?)',
                       ('research',now,'running',''))
        self.c.commit()
        with self.assertRaisesRegex(RuntimeError,'Stop the active'):
            storage.vacuum_when_stopped(self.c)

    def test_periodic_schedule_and_report(self):
        self.assertEqual(brain_daemon.JOBS['storage'],21600)
        self.assertEqual(brain_daemon.JOBS['backup'],86400)
        report=storage.report(self.c)
        self.assertEqual(report['parquet_partitions'],0)
        self.assertGreater(report['free_bytes'],0)

if __name__=='__main__':unittest.main()
