import json, sqlite3, sys, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'files'))
sys.path.insert(0,str(ROOT/'tests'/'fixtures'/'bridge'))
import oracle_v5 as v
AT='2026-10-23T22:10:00+00:00'
KICK='2026-10-24T13:30:00+00:00'

class TestV5(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:')
  c=self.c
  c.executescript('''
   CREATE TABLE fb_full_packets(packet_version TEXT,imported_at TEXT);
   CREATE TABLE fb_full_events(event_id TEXT PRIMARY KEY,root_event_id TEXT,sport TEXT,league TEXT,home TEXT,away TEXT,start_utc TEXT,place TEXT,level INTEGER);
   CREATE TABLE fb_full_current(quote_key TEXT PRIMARY KEY,event_id TEXT,root_event_id TEXT,factor_id TEXT,point TEXT,decimal_price REAL,source_json TEXT,first_seen TEXT,last_seen TEXT,active INTEGER,last_snapshot TEXT);
   CREATE TABLE v4_games(event_id TEXT PRIMARY KEY,sport TEXT,league TEXT,home TEXT,away TEXT,kickoff_utc TEXT,final INTEGER,home_score INTEGER,away_score INTEGER);
   CREATE TABLE v4_sport_preds(event_id TEXT,market TEXT,selection TEXT,prob REAL,issued_at TEXT);
   CREATE TABLE sf_forecasts(match_id TEXT,asof_day TEXT,created_at TEXT,sport TEXT,league TEXT,match_date TEXT,home TEXT,away TEXT,market TEXT,selection TEXT,probability REAL,model_version TEXT);
   CREATE TABLE fb_bridge_links(oracle_event_id TEXT,fonbet_event_id TEXT);
   CREATE TABLE sb_independent_forecasts(selection_key TEXT,model TEXT,asof_utc TEXT,p_home REAL,p_draw REAL,p_away REAL);
   CREATE TABLE sb_event_coverage(selection_key TEXT,sport TEXT,home TEXT,away TEXT,kickoff_utc TEXT,fonbet_root_event_id TEXT);
  ''')
  c.execute('INSERT INTO fb_full_packets VALUES (?,?)',('100','2026-10-23T22:05:00+00:00'))
  c.execute('INSERT INTO fb_full_events VALUES (?,?,?,?,?,?,?,?,?)',('1001','1001','football','Germany','Майнц','Вердер',KICK,'line',1))
  c.execute('INSERT INTO fb_full_events VALUES (?,?,?,?,?,?,?,?,?)',('1002','1001','football','Germany','Майнц','Вердер',KICK,'line',2))
  c.execute('INSERT INTO v4_games VALUES (?,?,?,?,?,?,?,?,?)',('g1','football','de.1','1. FSV Mainz 05','SV Werder Bremen',KICK,0,None,None))
  for code,price in [('921',1.73),('922',3.8),('923',4.5),('930',1.9),('931',1.9),('999',77.)]:
   c.execute('INSERT INTO fb_full_current VALUES (?,?,?,?,?,?,?,?,?,?,?)',
     ('q'+code,'1001','1001',code,json.dumps({'pt':'2.5'}) if code in ('930','931') else '{}',price,'{}',AT,AT,1,'2026-10-23T22:05:00+00:00'))
  c.execute('INSERT INTO fb_full_current VALUES (?,?,?,?,?,?,?,?,?,?,?)',
     ('nested','1002','1001','777','{}',2.3,'{}',AT,AT,1,'2026-10-23T22:05:00+00:00'))
  for sel,prob in [('1',.4),('X',.3),('2',.3)]:
   c.execute('INSERT INTO v4_sport_preds VALUES (?,?,?,?,?)',('g1','1X2',sel,prob,'2026-10-23T20:00:00+00:00'))
  c.execute('INSERT INTO sf_forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
    ('old1','2026-10-22','2026-10-23T20:00:00+00:00','football','de.1','2026-10-24',
     '1. FSV Mainz 05','SV Werder Bremen','ТОТАЛ 2.5','Больше 2.5',.6,'sf_model'))
  c.execute('INSERT INTO sf_forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
    ('old1','2026-10-22','2026-10-23T20:00:00+00:00','football','de.1','2026-10-24',
     '1. FSV Mainz 05','SV Werder Bremen','ОБЕ ЗАБЬЮТ','Да',.6,'sf_model'))
  c.commit()
  v.setup(c)
 def tearDown(self):self.c.close()
 def test_all_raw_factor_count(self):
  x=v.ingest_catalog(self.c,AT)
  self.assertEqual(x['active_quotes'],7)
  self.assertEqual(x['catalog_events'],1)
  self.assertEqual(self.c.execute('SELECT nested_events FROM v5_catalog').fetchone()[0],1)
 def test_name_both_sides_verified(self):
  self.assertEqual(v.name_match('1. FSV Mainz 05','Майнц')[0],1)
  self.assertEqual(v.name_match('SV Werder Bremen','Вердер')[0],1)
  self.assertLess(v.name_match('SV Werder Bremen','Бавария')[0],1)
 def test_exact_sports_not_date_only_match(self):
  self.assertEqual(v.sport_norm('football'),'football')
  self.assertNotEqual(v.sport_norm('football'),v.sport_norm('basketball'))
 def test_forecast_inventory_multi_source(self):
  v.ingest_catalog(self.c,AT)
  x=v.refresh_predictions(self.c,AT)
  self.assertEqual(x['all_forecast_records'],5)
  self.assertEqual(x['unique_events'],2)
  self.assertEqual(x['verified_events'],2)
 def test_price_1x2_and_total(self):
  v.ingest_catalog(self.c,AT);v.refresh_predictions(self.c,AT)
  rows=self.c.execute('SELECT market,price_status,price FROM v5_forecasts ORDER BY market,selection').fetchall()
  self.assertEqual(sum(x[1]=='PRICE_ATTACHED_TIME_VALID' for x in rows),4)
  self.assertEqual(sum(x[1]=='NO_ACTIVE_MATCHING_QUOTE' for x in rows),1)  # V5.1 recognizes BTTS but source quote absent
  self.assertIn(('ТОТАЛ 2.5','PRICE_ATTACHED_TIME_VALID',1.9),rows)
 def test_wrong_parameter_does_not_attach(self):
  self.c.execute('UPDATE fb_full_current SET point=? WHERE factor_id IN (\'930\',\'931\')',(json.dumps({'pt':'3.5'}),))
  v.ingest_catalog(self.c,AT);v.refresh_predictions(self.c,AT)
  self.assertEqual(self.c.execute("SELECT price_status FROM v5_forecasts WHERE market='ТОТАЛ 2.5'").fetchone()[0],'NO_ACTIVE_MATCHING_QUOTE')
 def test_nested_does_not_alias_root_1x2(self):
  self.c.execute("DELETE FROM fb_full_current WHERE factor_id='923'")
  self.c.execute("UPDATE fb_full_current SET factor_id='923' WHERE quote_key='nested'")
  v.ingest_catalog(self.c,AT);v.refresh_predictions(self.c,AT)
  s=self.c.execute("SELECT price_status FROM v5_forecasts WHERE selection='2' AND origin='v4_sport_preds'").fetchone()[0]
  self.assertEqual(s,'NO_ACTIVE_MATCHING_QUOTE')
 def test_no_forecast_market_imputation(self):
  v.ingest_catalog(self.c,AT);v.refresh_predictions(self.c,AT)
  self.assertEqual(self.c.execute("SELECT price FROM v5_forecasts WHERE market='ОБЕ ЗАБЬЮТ'").fetchone()[0],None)
 def test_repeated_run_idempotence(self):
  v.run(self.c,AT);v.run(self.c,AT)
  self.assertEqual(self.c.execute('SELECT COUNT(*) FROM v5_forecasts').fetchone()[0],5)
 def test_new_packet_detected(self):
  self.assertEqual(v.ingest_catalog(self.c,AT)['state'],'OK')
  self.assertEqual(v.ingest_catalog(self.c,AT)['state'],'UNCHANGED_PACKET')
 def test_new_quote_price_updates(self):
  v.run(self.c,AT)
  self.c.execute("UPDATE fb_full_current SET decimal_price=7.7 WHERE factor_id='923'")
  self.c.execute("UPDATE fb_full_packets SET packet_version='101'")
  v.run(self.c,AT)
  self.assertEqual(self.c.execute("SELECT price FROM v5_forecasts WHERE origin='v4_sport_preds' AND selection='2'").fetchone()[0],7.7)
 def test_stale_quotes_fail_closed(self):
  v.ingest_catalog(self.c,AT)
  v.refresh_predictions(self.c,'2026-10-24T00:00:00+00:00')
  self.assertEqual(self.c.execute("SELECT price_status FROM v5_forecasts WHERE origin='v4_sport_preds' LIMIT 1").fetchone()[0],'SOURCE_STALE')
 def test_unchanged_packet_still_marks_catalog_stale(self):
  v.ingest_catalog(self.c,AT)
  self.assertEqual(self.c.execute('SELECT quote_status FROM v5_catalog').fetchone()[0],'ACTIVE_QUOTED')
  out=v.ingest_catalog(self.c,'2026-10-24T00:00:00+00:00')
  self.assertEqual(out['state'],'UNCHANGED_PACKET')
  self.assertFalse(out['snapshot_fresh'])
  self.assertEqual(self.c.execute('SELECT quote_status FROM v5_catalog').fetchone()[0],'SOURCE_STALE')
 def test_no_future_results_used(self):
  self.c.execute('INSERT INTO v4_games VALUES (?,?,?,?,?,?,?,?,?)',
      ('hist','football','de.1','Майнц','Вердер','2026-10-24T13:30:00+00:00',1,4,0))
  self.assertEqual(v.form_team(self.c,'Майнц',AT)['sample'],0)
 def test_partial_missed_events(self):
  self.c.execute('INSERT INTO sf_forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
    ('x','2026-10-23','2026-10-23T20:00:00+00:00','tennis','XX','2026-10-24','Foo','Bar','Winner','Foo',.5,'x'))
  v.ingest_catalog(self.c,AT);r=v.refresh_predictions(self.c,AT)
  self.assertGreaterEqual(r['price_statuses'].get('EVENT_UNMATCHED',0),1)
 def test_all_markets_page_preserved(self):
  v.ingest_catalog(self.c,AT)
  x=v.price_page(self.c,'1001',100,0)
  self.assertEqual(x['total_active_factors'],7)
  self.assertIn('UNDECODED_RAW_MARKET', [r['market_label'] for r in x['items']])
 def test_match_ambiguity_no_auto_link(self):
  self.c.execute('INSERT INTO fb_full_events VALUES (?,?,?,?,?,?,?,?,?)',('1003','1003','football','Germany','Майнц','Вердер',KICK,'line',1))
  v.ingest_catalog(self.c,AT)
  p=next(iter(v.iter_predictions(self.c)))
  by_date={'2026-10-24':list(v.catalog_events(self.c))}
  r=v.detect_match(p,by_date,{x['root_id']:x for x in v.catalog_events(self.c)}, {})
  self.assertEqual(r[1],'AMBIGUOUS_EVENT_PAIR')
 def test_stale_catalog_from_zero_packet(self):
  self.c.execute('DELETE FROM fb_full_packets')
  self.assertEqual(v.ingest_catalog(self.c,AT)['state'],'NO_SOURCE_PACKET')
 def test_news_stays_unverified(self):
  v.run(self.c,AT)
  with patch('oracle_v5.time.sleep',lambda _:None):
   x=v.check_public_evidence(self.c,AT,loader=lambda url:[{'url':'https://news.google.com/a','title':'Example','published':None}])
  self.assertEqual(x['articles_added'],1)
  self.assertEqual(self.c.execute('SELECT verification FROM v5_research_sources').fetchone()[0],'UNVERIFIED_SEARCH_HIT')
 def test_dossier_no_player_fabrication(self):
  v.run(self.c,AT)
  data=json.loads(self.c.execute('SELECT payload FROM v5_dossiers').fetchone()[0])
  self.assertEqual(data['player_level']['injuries'],'NOT_VERIFIED')
  self.assertTrue(data['independent_of_odds'])
 def test_read_only_queries(self):
  v.run(self.c,AT)
  self.assertEqual(v.catalog_page(self.c,limit=5)['total'],1)
  self.assertEqual(v.forecast_page(self.c,limit=5)['total'],5)
  self.assertEqual(v.status(self.c)['predictions']['all_records'],5)
 def test_invalid_market_line_strict(self):
  self.assertIsNone(v.canonical_selection('ТОТАЛ 2.5','Больше 3.5'))
  self.assertEqual(v.total_line(json.dumps({'p':250})),2.5)  # V5.2 verified centi-parameter encoding
 def test_locked_price_survives_kickoff(self):
  v.run(self.c,AT)
  earlier=self.c.execute("SELECT price FROM v5_forecasts WHERE origin='v4_sport_preds' AND selection='2'").fetchone()[0]
  self.assertEqual(earlier,4.5)
  self.c.execute("UPDATE fb_full_current SET decimal_price=1.01,last_snapshot='2026-10-24T14:00:00+00:00' WHERE factor_id='923'")
  self.c.execute("UPDATE fb_full_packets SET packet_version='101',imported_at='2026-10-24T14:00:00+00:00'")
  v.run(self.c,'2026-10-24T14:05:00+00:00')
  after=self.c.execute("SELECT price,price_status FROM v5_forecasts WHERE origin='v4_sport_preds' AND selection='2'").fetchone()
  self.assertEqual(after,(4.5,'HISTORICAL_OR_STARTED'))
 def test_not_in_latest_snapshot_is_not_current(self):
  v.ingest_catalog(self.c,AT)
  self.c.execute("UPDATE fb_full_current SET active=0")
  self.c.execute("UPDATE fb_full_packets SET packet_version='101'")
  v.ingest_catalog(self.c,AT)
  self.assertEqual(self.c.execute("SELECT active_quotes FROM v5_catalog WHERE root_id='1001'").fetchone()[0],0)
 def test_doctor_unverified_data_not_green(self):
  v.run(self.c,AT)
  result=v.doctor(self.c)
  self.assertIn('PLAYER_INFORMATION',result['problems'])
  self.assertIn('FACTOR_DICTIONARY',result['problems'])
 def test_day_only_not_guess_clock(self):
  self.assertIsNone(v.selected_predictions(self.c)[-1]['kick'])
 def test_no_orders(self):
  self.assertEqual(v.run(self.c,AT)['orders_placed'],0)

if __name__=='__main__':unittest.main()
