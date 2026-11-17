import datetime as dt
import io
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import numpy as np
import collect
import research
import backfill
import market
import brain_daemon

class BrainTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.c=collect.db_open(pathlib.Path(self.temp.name)/'test.sqlite3')
        research.initialize(self.c)
    def tearDown(self):
        self.c.close();self.temp.cleanup()
    def test_sports_vig(self):
        a=market.normalize({'A':1.91,'B':1.91})
        self.assertAlmostEqual(a['A'],.5)
        self.assertAlmostEqual(sum(a.values()),1.)
    def test_invalid_market_rejected(self):
        self.assertIsNone(market.normalize({'A':1.,'B':2.}))
        self.assertIsNone(market.normalize({'A':float('nan'),'B':2.}))
    def test_consensus_two_bookmakers(self):
        sample={'id':'m1','sport_key':'basketball_nba','bookmakers':[
          {'key':'a','markets':[{'key':'h2h','outcomes':[{'name':'A','price':1.9},{'name':'B','price':2.05}]}]},
          {'key':'b','markets':[{'key':'h2h','outcomes':[{'name':'A','price':1.85},{'name':'B','price':2.15}]}]}]}
        result=market.extract(sample)
        self.assertEqual(result['bookmaker_count'],2)
        self.assertAlmostEqual(sum(result['outcomes'].values()),1,places=3)
        self.assertIn('not independent',result['meaning'])
    def test_research_starts_without_data_and_does_not_fake_signals(self):
        result=research.train_one(self.c,'BTCUSDT',6,.02)
        self.assertEqual(result['why'],'history_incomplete')
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM paper_forecasts').fetchone()[0],0)
    def test_only_contiguous_completed_candles(self):
        now=int(dt.datetime.now(dt.timezone.utc).timestamp()//3600)*3600000-1000*3600
        for i in range(400):
            t=now-(399-i)*3600000
            if i==220:continue
            p=100+i*.01
            self.c.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('binance_public','BTCUSDT','1h',t,p,p+1,p-1,p,100,collect.stamp()))
        rows=research.get_candles(self.c,'BTCUSDT');x,y,t,prices=research.features(rows,6)
        self.assertTrue(len(x)>0)
        # All windows have contiguous history, including no cross-gap samples.
        missing=now-(399-220)*3600000
        self.assertFalse(any(missing < z < missing+168*3600000 for z in t))
    def test_live_guard_rejects_bad_live_results(self):
        for i in range(65):
            self.c.execute('''INSERT INTO paper_forecasts(id,model_id,symbol,recorded_at,open_ms,horizon_hours,
                threshold,expires_at,entry,target,probability,model_state,status,actual_hit)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (f'f{i}','test','BTCUSDT','2025-01-01',i*100000,6,.02,'2025-01-02',1,1.02,.95,'SHADOW','settled',0))
        self.c.commit()
        self.assertFalse(research.live_guard(self.c,'test',.5))
    def test_bootstrap_default_gate_random_labels(self):
        rng=np.random.default_rng(200)
        x=rng.normal(size=(650,12)); y=1+.03*(rng.normal(size=650))
        fit,metrics=research.select_and_test(x,y,list(range(650)),6,.02)
        self.assertIsNotNone(fit)
        self.assertIn('brier',metrics)
        self.assertIn('baseline_brier',metrics)
        self.assertGreaterEqual(metrics['n_holdout'],110)
    def test_backfill_zip_validation_and_dedup(self):
        month=(2024,1);dates=[int(dt.datetime(2024,1,1,h,tzinfo=dt.timezone.utc).timestamp()*1000) for h in range(3)]
        lines=['{},{},{},{},{},{},0'.format(ts,100,105,95,102,10) for ts in dates]
        payload=io.BytesIO()
        with zipfile.ZipFile(payload,'w') as z:z.writestr('BTCUSDT-1h-2024-01.csv','\n'.join(lines))
        with patch('urllib.request.urlopen',return_value=io.BytesIO(payload.getvalue())):
            num,status=backfill.one(self.c,'BTCUSDT',*month)
        self.assertEqual(status,'ok');self.assertEqual(num,3)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM candles WHERE provider='binance_archive'").fetchone()[0],3)
    def test_schedule_restarts(self):
        with patch.object(backfill,'run',return_value=[]),patch.object(research,'train_all',return_value=[]),\
             patch.object(research,'settle',return_value=[]),patch.object(research,'generate_shadow',return_value=[]),\
             patch.object(market,'refresh',return_value=0),patch.object(brain_daemon.backup,'perform',return_value=None):
            first=brain_daemon.jobs(self.c,force=True)
            second=brain_daemon.jobs(self.c)
        self.assertIn('train',first);self.assertEqual(second,{})

if __name__=='__main__':unittest.main()

class DerivativesTest(unittest.TestCase):
    def test_derivatives_data_has_no_signal_side_effect(self):
        import sources
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cn=collect.db_open(pathlib.Path(d)/'d.sqlite3')
            def mock(url):
                return {'symbol':url.split('symbol=')[-1], 'markPrice':'123', 'openInterest':'456'}
            with patch.object(collect,'request_json',side_effect=mock):
                count=sources.binance_derivatives(cn)
            self.assertEqual(count,6)
            self.assertFalse(any(x[0]=='signals' for x in cn.execute("SELECT name FROM sqlite_master WHERE name='signals'")))
            cn.close()
