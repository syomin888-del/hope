import importlib.util,json,os,sqlite3,sys,tempfile,unittest,datetime as dt
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'files'))
import oracle_v7_core as co
import oracle_v7_crypto as cr

class V7Tests(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:')
  self.c.executescript('''
   CREATE TABLE v5_catalog(root_id TEXT PRIMARY KEY,root_exists INTEGER,active_quotes INTEGER,kickoff_utc TEXT);
   CREATE TABLE fb_full_current(active INTEGER);
   CREATE TABLE fb_full_packets(imported_at TEXT);
   CREATE TABLE v5_forecasts(id TEXT);
   CREATE TABLE v55_paper(forecast_id TEXT PRIMARY KEY,market TEXT,selection TEXT,status TEXT,profit REAL,stake REAL);
   CREATE TABLE v56_portfolio(root_id TEXT PRIMARY KEY,forecast_id TEXT);
   CREATE TABLE pf_backtests(run_at TEXT,method TEXT,training_json TEXT,test_json TEXT,warning TEXT);
   CREATE TABLE mw_quotes(observed_at TEXT);
   CREATE TABLE candles(symbol TEXT,timeframe TEXT,open_ms INTEGER,open REAL,close REAL,volume REAL,provider TEXT);
   CREATE TABLE mw_symbols(symbol TEXT,eligible INTEGER);
  ''')
  self.c.execute('INSERT INTO fb_full_packets VALUES(?)',(dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),))
  self.c.execute('INSERT INTO v5_catalog VALUES(?,?,?,?)',('root-a',1,20,'2026-11-01T00:00:00+00:00'))
  self.c.execute('INSERT INTO fb_full_current VALUES(1)')
  self.c.execute('INSERT INTO v55_paper VALUES(?,?,?,?,?,?)',('first','1X2','1','PENDING',None,10.0))
  self.c.execute('INSERT INTO v56_portfolio VALUES(?,?)',('root-a','first'))
 def tearDown(self):self.c.close()
 def test_crypto_negative_backtest_blocks_claims(self):
  self.c.execute('INSERT INTO pf_backtests VALUES(?,?,?,?,?)',('2026-10-24','fixed',json.dumps({'n':300}),json.dumps({'n':250,'mean_net_pct':-.38,'positive_frac':.384}),''))
  a=cr.last_failure(self.c)
  self.assertEqual(a['state'],'BLOCKED_NO_PROVEN_EDGE');self.assertIn('NEGATIVE_OUT_OF_SAMPLE_AFTER_COSTS',a['blockers'])
 def test_crypto_no_history_yet_not_success(self):self.assertEqual(cr.last_failure(self.c)['state'],'NOT_YET_TESTED')
 def test_source_fresh_verified(self):self.assertEqual(co.sports(self.c)['state'],'WORKING')
 def test_source_stale_must_not_mark_connected(self):
  self.c.execute('DELETE FROM fb_full_packets');self.c.execute('INSERT INTO fb_full_packets VALUES(?)',('2020-01-01T00:00:00+00:00',))
  self.assertNotEqual(co.sports(self.c)['state'],'WORKING')
 def test_no_secrets_or_fake_ai(self):
  for pid,name,env,url in co.AI:
   self.assertEqual(co._conn(self.c,pid,{})['state'],'NOT_CONFIGURED')
 def test_read_only_probe_and_lifecycle(self):
  with patch.dict(os.environ,{'OPENAI_API_KEY':'SECRET_SHOULD_NOT_LEAK'},clear=True):
   x=co.probe(self.c,'openai',lambda request:200)
   self.assertEqual(x['openai']['state'],'WORKING')
   data=json.dumps(co.dashboard(self.c));self.assertNotIn('SECRET_SHOULD_NOT_LEAK',data)
 def test_no_external_request_when_missing_key(self):
  with patch.dict(os.environ,{},clear=True):
   x=co.probe(self.c,'openai',lambda req:self.fail('Should not call'))
   self.assertEqual(x['openai']['state'],'NOT_CONFIGURED')
 def test_one_portfolio_pick_not_double_count(self):
  p=co.portfolio(self.c);self.assertEqual(p['signals'],1);self.assertTrue(p['unique_event_constraint_ok'])
  self.assertIsNone(p['roi_pct'])
 def test_resolved_roi_only_after_final_outcome(self):
  self.c.execute("UPDATE v55_paper SET status='WIN',profit=15 WHERE forecast_id='first'")
  p=co.portfolio(self.c);self.assertEqual(p['net_usd'],15);self.assertEqual(p['roi_pct'],150.0)
 def test_portfolio_no_conflicting_opposite_on_same_event(self):
  self.c.execute('INSERT INTO v55_paper VALUES(?,?,?,?,?,?)',('second','1X2','2','PENDING',None,10.0))
  with self.assertRaises(sqlite3.IntegrityError):
   self.c.execute('INSERT INTO v56_portfolio VALUES(?,?)',('root-a','second'))
 def test_model_backtest_no_data_not_fabricated(self):
  r=cr.run_hypothesis_test(self.c,5,300);self.assertNotEqual(r.get('target_3pct_daily_proven'),True)
 def test_dashboard_and_scorecard(self):
  d=co.dashboard(self.c);self.assertEqual(d['version'],'7.0');self.assertEqual(d['portfolio']['pending'],1)
  self.assertFalse(d['safety']['target_sport_win_rate_80pct_proven'])
  self.assertEqual(co.scorecard(self.c)['official_sports']['state'],'AWAITING_VERIFIED_SETTLEMENT')

if __name__=='__main__':unittest.main()
