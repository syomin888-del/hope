"""V5.5 regression tests: forward-only paper, scoring, archive, and Fonbet-only UI."""
import sqlite3, sys, unittest
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'files'))
sys.path.insert(0,str(ROOT/'tests'/'fixtures'))
import oracle_v5 as v
import oracle_v5_settlement as paper

NOW='2026-10-24T10:00:00+00:00'
KICK='2026-10-25T20:00:00+00:00'

class NewSettlementTests(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:')
  self.c.executescript('''
   CREATE TABLE fb_full_packets(packet_version TEXT, imported_at TEXT);
   CREATE TABLE fb_full_events(event_id TEXT,root_event_id TEXT,sport TEXT,league TEXT,home TEXT,away TEXT,start_utc TEXT,place TEXT,level INTEGER);
   CREATE TABLE fb_full_current(quote_key TEXT,event_id TEXT,root_event_id TEXT,factor_id TEXT,point TEXT,decimal_price REAL,source_json TEXT,first_seen TEXT,last_seen TEXT,active INTEGER,last_snapshot TEXT);
   CREATE TABLE fb_full_history(id INTEGER PRIMARY KEY,quote_key TEXT,event_id TEXT,decimal_price REAL,active INTEGER,changed_at TEXT,raw_sha TEXT);
   CREATE TABLE sf_forecasts(match_id TEXT,asof_day TEXT,created_at TEXT,sport TEXT,league TEXT,match_date TEXT,home TEXT,away TEXT,market TEXT,selection TEXT,probability REAL,model_version TEXT);
   CREATE TABLE sf_matches(id TEXT PRIMARY KEY,home_score INTEGER,away_score INTEGER,last_seen TEXT,source TEXT);
  ''')
  self.c.execute('INSERT INTO fb_full_packets VALUES (?,?)',('snap','2026-10-24T09:57:00+00:00'))
  self.c.execute('INSERT INTO fb_full_events VALUES (?,?,?,?,?,?,?,?,?)',
    ('event','event','football','Spain Primera','Барселона','Реал Мадрид',KICK,'line',1))
  self.c.execute('INSERT INTO fb_full_current VALUES (?,?,?,?,?,?,?,?,?,?,?)',
    ('quote','event','event','930','{"p":250,"pt":"2.5"}',1.9,'{}',NOW,NOW,1,NOW))
  self.c.execute('INSERT INTO fb_full_history VALUES(?,?,?,?,?,?,?)',
    (1,'quote','event',1.9,1,NOW,'sha'))
  self.c.execute('INSERT INTO sf_forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
    ('match','2026-10-24','2026-10-24T09:30:00+00:00','football','es.1',
     '2026-10-25','FC Barcelona','Real Madrid CF','ТОТАЛ 2.5','Больше 2.5',0.62,'research'))
  v.setup(self.c);paper.setup(self.c)
  v.ingest_catalog(self.c,NOW);v.refresh_predictions(self.c,NOW)
 def tearDown(self):self.c.close()
 def test_paper_forward_locked_and_repeat_no_double_stakes(self):
  first=paper.prepare(self.c,NOW)
  self.assertEqual(first['new_forward_paper_experiments'],1)
  self.assertEqual(paper.prepare(self.c,NOW)['new_forward_paper_experiments'],0)
  row=self.c.execute('SELECT stake,odds,registered_at,expected_ev_pct FROM v55_paper').fetchone()
  self.assertEqual(row[:3],(10.0,1.9,NOW))
  self.assertAlmostEqual(row[3],17.8)
 def test_no_hindsight_new_paper_after_kickoff(self):
  self.assertEqual(paper.prepare(self.c,'2026-10-26T00:00:00+00:00')['new_forward_paper_experiments'],0)
 def test_settle_win_profit(self):
  paper.prepare(self.c,NOW)
  self.c.execute('INSERT INTO sf_matches VALUES(?,?,?,?,?)',('match',2,1,'2026-10-26T00:00:00+00:00','openfootball_CC0'))
  res=paper.settle(self.c,'2026-10-26T03:00:00+00:00')
  self.assertEqual(res['settled_now'],1)
  row=self.c.execute('SELECT status,profit,source FROM v55_paper').fetchone()
  self.assertEqual((row[0],round(row[1],2)),('WIN',9.))
  self.assertEqual(paper.summary(self.c)['net_profit_usd'],9.0)
  self.assertEqual(paper.settle(self.c,'2026-10-26T05:00:00+00:00')['settled_now'],0)
 def test_stale_or_unavailable_result_not_settled(self):
  paper.prepare(self.c,NOW)
  self.c.execute('INSERT INTO sf_matches VALUES(?,?,?,?,?)',('match',4,2,'2026-10-25T20:20:00+00:00','openfootball_CC0'))
  out=paper.settle(self.c,'2026-10-26T03:00:00+00:00')
  self.assertEqual(out['settled_now'],0)
  self.assertEqual(out['remaining_without_confirmed_final'],1)
 def test_result_loss_and_push_math(self):
  self.assertEqual(paper._market_outcome('ТОТАЛ 2.5','Больше 2.5',1,1),'LOSS')
  self.assertEqual(paper._market_outcome('ТОТАЛ 2','Больше 2',1,1),'PUSH')
  self.assertEqual(paper._market_outcome('1X2','Ничья',0,0),'WIN')
  self.assertEqual(paper._market_outcome('ДВОЙНОЙ ШАНС','12',0,0),'LOSS')
  self.assertEqual(paper._market_outcome('ДВОЙНОЙ ШАНС','1X',0,0),'WIN')
  self.assertEqual(paper._market_outcome('ОБЕ ЗАБЬЮТ','Да',1,0),'LOSS')
  self.assertEqual(paper._market_outcome('PLAYER SHOTS','Over 2',2,1),'UNKNOWN')
 def test_archival_prices_require_evidence_at_time(self):
  r=paper.backfill(self.c,NOW)
  self.assertEqual(r['archive_quotes_added'],1)
  self.assertEqual(paper.backfill(self.c,NOW)['archive_quotes_added'],0)
  row=self.c.execute('SELECT origin,observed_at FROM v55_archive_prices').fetchone()
  self.assertEqual(row,('fb_full_history_OBSERVED',NOW))
  shown=v.forecast_page(self.c,limit=10,offset=0)
  self.assertEqual(shown['total'],1)
  self.assertAlmostEqual(shown['records'][0]['price'],1.9)
 def test_no_created_after_price_retrofill(self):
  self.c.execute('DELETE FROM v55_archive_prices')
  self.c.execute("UPDATE v5_forecasts SET created_at='2026-10-24T11:00:00+00:00'")
  self.assertEqual(paper.backfill(self.c,NOW)['archive_quotes_added'],0)
 def test_catalog_and_forecasts_only_fonbet(self):
  self.c.execute("INSERT INTO v5_catalog VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
     'other','football','fake','FAKE','UNKNOWN','2026-10-26T12:00:00+00:00','',1,0,0,0,'NO_ACTIVE_QUOTE','snap',NOW,NOW))
  self.c.execute("INSERT INTO v5_forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
    'bad','sf_forecasts','orphan',NOW,'football','fake','FAKE','UNKNOWN','2026-10-26',
    '2026-10-26T12:00:00+00:00','1X2','1',0.5,'test',None,'NO_MATCH',None,None,None,'EVENT_UNMATCHED',NOW,'{}'))
  self.assertEqual(v.catalog_page(self.c,limit=50,offset=0)['total'],1)
  self.assertEqual(v.forecast_page(self.c,limit=50,offset=0)['total'],1)
 def test_no_unsupported_market_bet(self):
  self.c.execute("UPDATE v5_forecasts SET market='PLAYER_PROP' WHERE origin='sf_forecasts'")
  self.assertEqual(paper.prepare(self.c,NOW)['new_forward_paper_experiments'],0)
 def test_source_outcome_final_after_kickoff(self):
  paper.prepare(self.c,NOW)
  self.c.execute('INSERT INTO sf_matches VALUES(?,?,?,?,?)',('match',1,0,'2026-10-26T05:00:00+00:00','source'))
  self.assertEqual(paper.settle(self.c,'2026-10-26T06:00:00+00:00')['outcomes']['LOSS'],1)
 def test_evidence_day_capacity_config(self):
  code=(ROOT/'files'/'oracle_v4.py').read_text()
  self.assertIn("'48'",code)
  self.assertIn('v5_paper_settlement_6h',code)
  self.assertIn('6*3600',code)
  self.assertIn('1800',code)
 def test_no_long_term_market_display_as_fixture(self):
  self.assertFalse(v.is_fixture_market({'root_exists':1,'league':'Испания. Кто выше по итогам чемпионата?'}))
 def test_news_not_inferred_as_facts(self):
  self.assertIn('UNVERIFIED_SEARCH_HIT',(ROOT/'files'/'oracle_v5.py').read_text())
 def test_past_event_quotes_show_genuine_prematch_archive(self):
  self.c.execute("UPDATE fb_full_current SET active=0,last_snapshot='2026-10-26T05:00:00+00:00',decimal_price=9.0")
  with patch.object(v,'stamp',lambda t=None:'2026-10-26T12:00:00+00:00'):
   page=v.price_page(self.c,'event')
  self.assertTrue(page['historical_archive'])
  self.assertEqual(page['total_active_factors'],1)
  self.assertAlmostEqual(page['items'][0]['price'],1.9)
  self.assertEqual(page['items'][0]['quote_mode'],'PRE_KICKOFF_ARCHIVE')
 def test_postgame_only_quote_never_in_archive(self):
  self.c.execute("UPDATE fb_full_current SET first_seen='2026-10-26T02:00:00+00:00',last_snapshot='2026-10-26T02:00:00+00:00'")
  self.c.execute("UPDATE fb_full_history SET changed_at='2026-10-26T02:00:00+00:00'")
  with patch.object(v,'stamp',lambda t=None:'2026-10-26T12:00:00+00:00'):
   page=v.price_page(self.c,'event')
  self.assertEqual(page['total_active_factors'],0)
 def test_wrong_event_legacy_capture_cannot_price_past_fixture(self):
  fid=self.c.execute('SELECT id FROM v5_forecasts').fetchone()[0]
  self.c.execute('DELETE FROM v5_price_captures')
  self.c.execute('INSERT INTO v5_price_captures VALUES(?,?,?,?,?,?,?)',
     (fid,'bad-quote',NOW,88.0,'Больше 2.5',"('TOTAL','over',2.5)",NOW))
  self.c.execute('INSERT INTO fb_full_current VALUES (?,?,?,?,?,?,?,?,?,?,?)',
    ('bad-quote','wrong','wrong','930','{"p":250,"pt":"2.5"}',88.0,'{}',NOW,NOW,0,NOW))
  result=v.refresh_predictions(self.c,'2026-10-26T12:00:00+00:00')
  self.assertEqual(result['price_statuses'],{'HISTORICAL_OR_STARTED':1})
  self.assertEqual(self.c.execute('SELECT price FROM v5_forecasts').fetchone()[0],None)
 def test_paper_page(self):
  paper.prepare(self.c,NOW)
  items=paper.paper_page(self.c,10,0)
  self.assertEqual(items['total'],1)
  self.assertEqual(items['items'][0]['stake'],10.)

if __name__=='__main__': unittest.main()
