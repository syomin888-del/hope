import sqlite3,sys,json,unittest
from pathlib import Path
HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE/'files'))
sys.path.insert(0,str(HERE/'tests'/'fixtures'))
import oracle_v5 as v
AT='2026-10-24T10:00:00+00:00'
K='2026-10-25T19:00:00+00:00'
class V52(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:')
        self.c.executescript('''
        CREATE TABLE fb_full_packets(packet_version TEXT,imported_at TEXT);
        CREATE TABLE fb_full_events(event_id TEXT,root_event_id TEXT,sport TEXT,league TEXT,home TEXT,away TEXT,start_utc TEXT,place TEXT,level INTEGER);
        CREATE TABLE fb_full_current(quote_key TEXT,event_id TEXT,root_event_id TEXT,factor_id TEXT,point TEXT,decimal_price REAL,source_json TEXT,first_seen TEXT,last_seen TEXT,active INTEGER,last_snapshot TEXT);
        CREATE TABLE sf_forecasts(match_id TEXT,asof_day TEXT,created_at TEXT,sport TEXT,league TEXT,match_date TEXT,home TEXT,away TEXT,market TEXT,selection TEXT,probability REAL,model_version TEXT);
        ''')
        self.c.execute('INSERT INTO fb_full_packets VALUES (?,?)',('v52snapshot','2026-10-24T09:58:00+00:00'))
        v.setup(self.c)
    def tearDown(self):self.c.close()
    def root(self,rid='r1',home='Хоффенхайм',away='Байер Леверкузен',sport='football',kick=K):
        self.c.execute('INSERT INTO fb_full_events VALUES(?,?,?,?,?,?,?,?,?)',(rid,rid,sport,'Bundesliga',home,away,kick,'line',1))
    def sf(self,mid='sf',home='TSG 1899 Hoffenheim',away='Bayer 04 Leverkusen',sport='football',league='de.1',date='2026-10-25',market='ТОТАЛ 2.5',selection='Больше 2.5'):
        self.c.execute('INSERT INTO sf_forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(mid,'2026-10-24','2026-10-24T09:50:00+00:00',sport,league,date,home,away,market,selection,.5,'sf'))
    def q(self,code,p=None,pt=None,rid='r1',price=1.93):
        self.c.execute('INSERT INTO fb_full_current VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (v.chash(code,p,pt,rid),rid,rid,code,json.dumps({'p':p,'pt':pt}),price,'{}',AT,AT,1,'2026-10-24T09:58:00+00:00'))
    def run_once(self):
        v.ingest_catalog(self.c,AT)
        return v.refresh_predictions(self.c,AT)
    def test_numeric_p_250_yields_2_5(self):
        self.root();self.sf();self.q('930',p=250)
        out=self.run_once()
        self.assertEqual(out['price_statuses'],{'PRICE_ATTACHED_TIME_VALID':1})
        self.assertAlmostEqual(self.c.execute('SELECT price FROM v5_forecasts').fetchone()[0],1.93)
    def test_numeric_p_4450_yields_44_5_nfl(self):
        self.root('nfl','Теннесси Тайтенс','Кливленд Браунз','am-football',kick='2026-10-25T19:00:00+00:00')
        self.sf('nfl','TEN','CLE','americanfootball','nfl','2026-10-25','ТОТАЛ 44.5','Меньше 44.5')
        self.q('931',p=4450,rid='nfl',price=1.76)
        out=self.run_once()
        self.assertEqual(out['price_statuses'],{'PRICE_ATTACHED_TIME_VALID':1})
    def test_pt_text_only_and_both_consistent(self):
        self.assertEqual(v.total_line('{"pt":"2.5","p":null}'),2.5)
        self.assertEqual(v.total_line('{"pt":"2,5","p":250}'),2.5)
    def test_mismatched_pt_p_rejected(self):
        self.assertIsNone(v.total_line('{"pt":"2.5","p":350}'))
        self.root();self.sf();self.q('930',p=350,pt='2.5')
        self.assertEqual(self.run_once()['price_statuses'],{'NO_ACTIVE_MATCHING_QUOTE':1})
    def test_unparameterized_totals_not_assumed(self):
        self.assertIsNone(v.total_line('{"pt":null,"p":null}'))
        self.root();self.sf();self.q('930')
        self.assertEqual(self.run_once()['price_statuses'],{'NO_ACTIVE_MATCHING_QUOTE':1})
    def test_bad_totals_rejected(self):
        for p in [False,-250,250.4,1e20,float('nan')]:
            self.assertIsNone(v.total_line(json.dumps({'p':p})))
    def test_barcelona_duplicate_kept_ambiguous(self):
        self.root('r1','Барселона','Реал Мадрид')
        self.root('r2','Барселона','Реал Мадрид')
        self.sf('x','FC Barcelona','Real Madrid CF','football','es.1','2026-10-25','1X2','1')
        self.assertEqual(self.run_once()['price_statuses'],{'EVENT_AMBIGUOUS':1})
    def test_la_not_conflated_with_rams_chargers(self):
        self.root('nfl','Лас-Вегас Рэйдерс','Лос-Анджелес Рэмс','am-football')
        self.sf('n','LV','LA','americanfootball','nfl','2026-10-25','1X2','1')
        self.assertEqual(self.run_once()['price_statuses'],{'EVENT_UNMATCHED':1})
    def test_more_clubs_and_cyrillic_spelling(self):
        self.root('r1','Реал Сосьедад','Леванте')
        self.sf('x','Real Sociedad de Fútbol','Levante UD','football','es.1','2026-10-25','1X2','1')
        self.q('921',price=1.80)
        self.assertEqual(self.run_once()['price_statuses'],{'PRICE_ATTACHED_TIME_VALID':1})
    def test_btts_existing_unchanged(self):
        self.root();self.sf(market='ОБЕ ЗАБЬЮТ',selection='Да');self.q('4241',price=1.71)
        self.assertEqual(self.run_once()['price_statuses'],{'PRICE_ATTACHED_TIME_VALID':1})
    def test_triage_read_only_and_identifies_event(self):
        self.root();self.sf(home='TSG 1899 Hoffenheim',away='Wrong Opponent')
        self.run_once()
        before=self.c.total_changes
        out=v.triage(self.c,limit=10,now=AT)
        self.assertEqual(self.c.total_changes,before)
        self.assertEqual(out['state'],'READ_ONLY_DIAGNOSTIC')
        self.assertEqual(out['entries'][0]['status'],'EVENT_UNMATCHED')
        self.assertTrue(any(x['home']=='Хоффенхайм' for x in out['entries'][0]['candidates_not_approved']))
    def test_no_post_kickoff_capture(self):
        self.root(kick='2026-10-24T09:00:00+00:00');self.sf(date='2026-10-24');self.q('930',p=250)
        out=self.run_once()
        self.assertNotIn('PRICE_ATTACHED_TIME_VALID',out['price_statuses'])
    def test_aliases_are_merge_not_override(self):
        self.assertIn('Хоффенхайм',v.ALIASES['TSG 1899 Hoffenheim'])
        self.assertIn('Теннесси Тайтанс',v.ALIASES['TEN'])
    def test_market_codes_still_minimal(self):
        self.assertEqual(len(v.VERIFIED_CODES),12)

if __name__=='__main__':unittest.main()
