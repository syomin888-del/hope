"""Tests for truthful daily reporting, WITA boundaries and process health."""
import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import telemetry

UTC=dt.timezone.utc

class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Path(self.temp.name)/'oracle.sqlite3'
        self.c=sqlite3.connect(self.db)
        self.c.executescript('''
        CREATE TABLE raw_snapshots(id INTEGER PRIMARY KEY,provider TEXT,subject TEXT,observed_at TEXT);
        CREATE TABLE candles(provider TEXT,fetched_at TEXT);
        CREATE TABLE source_runs(job TEXT,run_at TEXT,state TEXT,new_rows INTEGER,id INTEGER PRIMARY KEY);
        CREATE TABLE signals(id TEXT,issued_at TEXT,resolved_at TEXT,status TEXT);
        CREATE TABLE paper_forecasts(recorded_at TEXT,settled_at TEXT);
        CREATE TABLE research_models(state TEXT);
        ''')
        telemetry.setup(self.c)
        self.now=dt.datetime(2026,10,21,15,35,tzinfo=UTC)
    def tearDown(self):
        self.c.close();self.temp.cleanup()
    def test_date_boundary_bali(self):
        # 21 Oct Bali runs from 20 Oct 16:00Z through 21 Oct 15:59:59Z
        self.c.execute('INSERT INTO raw_snapshots VALUES (1,?,?,?)',('p','A','2026-10-20T15:59:59+00:00'))
        self.c.execute('INSERT INTO raw_snapshots VALUES (2,?,?,?)',('p','A','2026-10-20T16:00:00+00:00'))
        self.c.execute('INSERT INTO raw_snapshots VALUES (3,?,?,?)',('p','A','2026-10-21T15:59:59+00:00'))
        self.c.execute('INSERT INTO raw_snapshots VALUES (4,?,?,?)',('p','B','2026-10-21T16:00:00+00:00'))
        d=telemetry.snapshot(self.c,'2026-10-21',now=self.now)
        self.assertEqual(d['counts']['raw_source_versions'],2)
        self.assertEqual(d['counts']['unique_provider_subject_pairs'],1)
    def test_heartbeats_are_truthful(self):
        telemetry.heartbeat(self.c,'sources',now=self.now-dt.timedelta(seconds=10))
        telemetry.heartbeat(self.c,'research',now=self.now-dt.timedelta(seconds=400))
        d=telemetry.snapshot(self.c,now=self.now)
        self.assertEqual([(x['service'],x['heartbeat_fresh']) for x in d['services']], [('research',False),('sources',True)])
    def test_signal_count_is_db_only(self):
        self.c.execute('INSERT INTO signals VALUES (?,?,?,?)',('s1','2026-10-21T12:00:00+00:00',None,'open'))
        self.c.execute('INSERT INTO paper_forecasts VALUES (?,?)',('2026-10-21T12:00:00+00:00',None))
        self.c.commit()
        d=telemetry.snapshot(self.c,now=self.now)
        self.assertEqual(d['counts']['signals_issued'],1)
        self.assertEqual(d['counts']['paper_forecasts_created'],1)
        self.assertEqual(d['live_certified_models'],0)
    def test_missing_file_does_not_create_database_or_invent_data(self):
        target=Path(self.temp.name)/'does-not-exist.sqlite3'
        d=telemetry.get(target,'2026-10-21')
        self.assertFalse(target.exists())
        self.assertFalse(d['database_connected'])
        self.assertEqual(d['counts'],{})
        self.assertIn('неизвестны',telemetry.report_text(d).lower())
    def test_source_states_report_exactly(self):
        self.c.executemany('INSERT INTO source_runs(job,run_at,state,new_rows) VALUES (?,?,?,?)',[
         ('a','2026-10-21T14:01:00+00:00','ok',5),('b','2026-10-21T14:01:00+00:00','error',0),('c','2026-10-21T14:01:00+00:00','skip',0)])
        self.c.commit()
        d=telemetry.snapshot(self.c,now=self.now)
        self.assertEqual(d['source_job_runs_today'],{'ok':1,'error':1,'skip':1})
        self.assertTrue(any('ошибки' in x.lower() for x in d['warnings']))

if __name__=='__main__':unittest.main()
