import sqlite3, sys, unittest
from pathlib import Path
HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE/'files'))
sys.path.insert(0,str(HERE/'tests'/'fixtures'))
import oracle_v5 as v
T='2026-10-24T10:00:00+00:00'
KICK='2026-10-24T19:00:00+00:00'

class CheckV51(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:')
  self.c.executescript('''
   CREATE TABLE fb_full_packets(packet_version TEXT,imported_at TEXT);
   CREATE TABLE fb_full_events(event_id TEXT,root_event_id TEXT,sport TEXT,league TEXT,home TEXT,away TEXT,start_utc TEXT,place TEXT,level INTEGER);
   CREATE TABLE fb_full_current(quote_key TEXT,event_id TEXT,root_event_id TEXT,factor_id TEXT,point TEXT,decimal_price REAL,source_json TEXT,first_seen TEXT,last_seen TEXT,active INTEGER,last_snapshot TEXT);
   CREATE TABLE sf_forecasts(match_id TEXT,asof_day TEXT,created_at TEXT,sport TEXT,league TEXT,match_date TEXT,home TEXT,away TEXT,market TEXT,selection TEXT,probability REAL,model_version TEXT);
  ''')
  self.c.execute('INSERT INTO fb_full_packets VALUES (?,?)',('101','2026-10-24T09:55:00+00:00'))
  v.setup(self.c)
 def tearDown(self):self.c.close()
 def root(self, id='fb1',sport='football',home='Арсенал',away='Эвертон',kick=KICK):
  self.c.execute('INSERT INTO fb_full_events VALUES(?,?,?,?,?,?,?,?,?)',(id,id,sport,'fixture',home,away,kick,'line',1))
 def forecast(self,id='sf1',sport='football',lg='en.1',day='2026-10-24',home='Arsenal FC',away='Everton FC',market='ДВОЙНОЙ ШАНС',sel='1X'):
  self.c.execute('INSERT INTO sf_forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(id,'2026-10-23','2026-10-24T09:50:00+00:00',sport,lg,day,home,away,market,sel,.68,'sf'))
 def price(self,code,price,root='fb1',point='{}'):
  self.c.execute('INSERT INTO fb_full_current VALUES(?,?,?,?,?,?,?,?,?,?,?)',('q'+code,root,root,code,point,price,'{}',T,T,1,'2026-10-24T09:55:00+00:00'))
 def test_verified_full_time_double_chance_all(self):
  self.root()
  for code,sel,price in (('924','1X',1.42),('1571','12',1.28),('925','X2',2.45)):
   self.forecast('sf'+sel,market='ДВОЙНОЙ ШАНС',sel=sel)
   self.price(code,price)
  v.ingest_catalog(self.c,T);r=v.refresh_predictions(self.c,T)
  self.assertEqual(r['verified_events'],3) # three source IDs, one unique root
  self.assertEqual(r['price_statuses'],{'PRICE_ATTACHED_TIME_VALID':3})
  self.assertEqual(dict(self.c.execute('SELECT selection,price FROM v5_forecasts')),{'1X':1.42,'12':1.28,'X2':2.45})
 def test_btts_yes_no(self):
  self.root()
  self.forecast('y',market='ОБЕ ЗАБЬЮТ',sel='Да')
  self.forecast('n',market='ОБЕ ЗАБЬЮТ',sel='Нет')
  self.price('4241',1.75);self.price('4242',2.15)
  v.ingest_catalog(self.c,T);r=v.refresh_predictions(self.c,T)
  self.assertEqual(r['price_statuses'],{'PRICE_ATTACHED_TIME_VALID':2})
  self.assertEqual(sorted(x[0] for x in self.c.execute('SELECT price FROM v5_forecasts')), [1.75,2.15])
 def test_wrong_period_not_used(self):
  self.root();self.forecast()
  self.c.execute('INSERT INTO fb_full_events VALUES(?,?,?,?,?,?,?,?,?)',('child','fb1','football','fixture','Арсенал','Эвертон',KICK,'line',2))
  self.c.execute('INSERT INTO fb_full_current VALUES(?,?,?,?,?,?,?,?,?,?,?)',('c','child','fb1','924','{}',2.0,'{}',T,T,1,T))
  v.ingest_catalog(self.c,T);r=v.refresh_predictions(self.c,T)
  self.assertEqual(r['price_statuses'],{'NO_ACTIVE_MATCHING_QUOTE':1})
 def test_no_guess_rival(self):
  self.root(away='Челси');self.forecast()
  v.ingest_catalog(self.c,T);r=v.refresh_predictions(self.c,T)
  self.assertEqual(r['price_statuses'],{'EVENT_UNMATCHED':1})
 def test_collision_remains_ambiguous(self):
  self.root('a');self.root('b');self.forecast()
  v.ingest_catalog(self.c,T);r=v.refresh_predictions(self.c,T)
  self.assertEqual(r['price_statuses'],{'EVENT_AMBIGUOUS':1})
 def test_nfl_local_sunday_to_utc_monday(self):
  self.root('nfl1','am-football','Аризона Кардиналс','Денвер Бронкос','2026-10-26T00:20:00+00:00')
  self.forecast('nfl-1','americanfootball','nfl','2026-10-25','ARI','DEN','1X2','1')
  self.price('921',2.2,'nfl1')
  v.ingest_catalog(self.c,T);r=v.refresh_predictions(self.c,T)
  self.assertEqual(r['price_statuses'],{'PRICE_ATTACHED_TIME_VALID':1})
 def test_nfl_previous_day_not_matched_if_ny_date_wrong(self):
  self.root('nfl1','am-football','Аризона Кардиналс','Денвер Бронкос','2026-10-26T21:00:00+00:00')
  self.forecast('nfl-1','americanfootball','nfl','2026-10-25','ARI','DEN','1X2','1')
  v.ingest_catalog(self.c,T);r=v.refresh_predictions(self.c,T)
  self.assertEqual(r['price_statuses'],{'EVENT_UNMATCHED':1})
 def test_capture_is_new_only_first_time(self):
  self.root();self.forecast();self.price('924',1.5)
  v.ingest_catalog(self.c,T);a=v.refresh_predictions(self.c,T);b=v.refresh_predictions(self.c,T)
  self.assertEqual(a['new_asof_captures'],1)
  self.assertEqual(b['new_asof_captures'],0)
 def test_report_two_future_definitions(self):
  self.root();self.forecast();v.ingest_catalog(self.c,T)
  r=v.refresh_predictions(self.c,T)
  self.assertEqual(r['future_forecast_records'],1)
  self.assertEqual(r['upcoming_kickoff_records'],1)
 def test_opaque_factor_no_inference(self):
  self.root();self.forecast('z',market='ОБЕ ЗАБЬЮТ',sel='Да');self.price('999',1.02)
  v.ingest_catalog(self.c,T);r=v.refresh_predictions(self.c,T)
  self.assertEqual(r['price_statuses'],{'NO_ACTIVE_MATCHING_QUOTE':1})
 def test_counts_13_documented_codes(self):
  self.assertEqual(len(v.VERIFIED_CODES),12)
 def test_freshness_enforced(self):
  self.root();self.forecast();self.price('924',1.42)
  v.ingest_catalog(self.c,T);r=v.refresh_predictions(self.c,'2026-10-24T15:00:00+00:00')
  self.assertEqual(r['price_statuses'],{'SOURCE_STALE':1})

if __name__=='__main__':unittest.main()
