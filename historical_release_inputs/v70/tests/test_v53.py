"""V5.3: reject seasonal comparisons and stale links; accept only verified fixtures."""
import sys, sqlite3, unittest, json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'files'))
sys.path.insert(0,str(ROOT/'tests'/'fixtures'))
import oracle_v5 as v
T='2026-10-24T09:59:00+00:00'
K='2026-10-25T20:00:00+00:00'

class IdentityV53(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:')
        self.c.executescript('''
        CREATE TABLE fb_full_packets(packet_version TEXT,imported_at TEXT);
        CREATE TABLE fb_full_events(event_id TEXT,root_event_id TEXT,sport TEXT,league TEXT,home TEXT,away TEXT,start_utc TEXT,place TEXT,level INTEGER);
        CREATE TABLE fb_full_current(quote_key TEXT,event_id TEXT,root_event_id TEXT,factor_id TEXT,point TEXT,decimal_price REAL,source_json TEXT,first_seen TEXT,last_seen TEXT,active INTEGER,last_snapshot TEXT);
        CREATE TABLE sf_forecasts(match_id TEXT,asof_day TEXT,created_at TEXT,sport TEXT,league TEXT,match_date TEXT,home TEXT,away TEXT,market TEXT,selection TEXT,probability REAL,model_version TEXT);
        ''')
        self.c.execute('INSERT INTO fb_full_packets VALUES (?,?)',('snap','2026-10-24T09:55:00+00:00'))
        v.setup(self.c)
    def tearDown(self):self.c.close()
    def event(self,rid,home='Барселона',away='Реал Мадрид',league='Испания. Примера дивизион. Сезон 26/27',kick=K,sport='football'):
        self.c.execute('INSERT INTO fb_full_events VALUES(?,?,?,?,?,?,?,?,?)',(rid,rid,sport,league,home,away,kick,'line',1))
    def forecast(self,mid='sf',home='FC Barcelona',away='Real Madrid CF',league='es.1',day='2026-10-25',sel='1',market='1X2',sport='football'):
        self.c.execute('INSERT INTO sf_forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(mid,'2026-10-24','2026-10-24T09:50:00+00:00',sport,league,day,home,away,market,sel,.41,'model'))
    def q(self,rid,code='921',line=None):
        self.c.execute('INSERT INTO fb_full_current VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('q'+rid+code,rid,rid,code,json.dumps({'p':int(line*100),'pt':str(line)}) if line else '{}',2.1,'{}',T,T,1,'2026-10-24T09:55:00+00:00'))
    def run_once(self):
        v.ingest_catalog(self.c,T)
        return v.refresh_predictions(self.c,T)
    def test_season_comparison_is_not_head_to_head(self):
        self.event('season',league='Испания. Примера дивизион. Кто выше по итогам чемпионата?')
        self.forecast();self.q('season')
        result=self.run_once()
        self.assertEqual(result['price_statuses'],{'EVENT_UNMATCHED':1})
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM v5_event_links').fetchone()[0],0)
    def test_fixture_beats_identical_season_comp(self):
        self.event('season',league='Испания. Примера дивизион. Кто выше по итогам чемпионата?')
        self.event('match')
        self.forecast();self.q('match')
        result=self.run_once()
        self.assertEqual(result['price_statuses'],{'PRICE_ATTACHED_TIME_VALID':1})
        self.assertEqual(self.c.execute('SELECT root_id FROM v5_event_links').fetchone()[0],'match')
    def test_legacy_season_link_repaired_and_audited(self):
        self.event('season',league='Испания. Примера дивизион. Кто выше по итогам чемпионата?')
        self.event('match')
        self.forecast();self.q('match')
        self.c.execute('INSERT INTO v5_event_links VALUES (?,?,?,?,?,?)',('sf_forecasts','sf','season','LEGACY','2026-10-23T00:00:00+00:00',K))
        result=self.run_once()
        self.assertEqual(result['repaired_event_links'],1)
        self.assertEqual(result['price_statuses'],{'PRICE_ATTACHED_TIME_VALID':1})
        self.assertEqual(self.c.execute('SELECT root_id FROM v5_event_links').fetchone()[0],'match')
        self.assertEqual(self.c.execute('SELECT previous_root_id,replacement_root_id FROM v5_link_audit').fetchone(),('season','match'))
        result=self.run_once()
        self.assertEqual(result['repaired_event_links'],0)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM v5_link_audit').fetchone()[0],1)
    def test_legacy_season_link_quarantined_if_no_fixture(self):
        self.event('season',league='Испания. Примера дивизион. Кто выше по итогам чемпионата?')
        self.forecast();self.q('season')
        self.c.execute('INSERT INTO v5_event_links VALUES (?,?,?,?,?,?)',('sf_forecasts','sf','season','OLD','2026-10-23T00:00:00+00:00',K))
        result=self.run_once()
        self.assertEqual(result['quarantined_legacy_links'],1)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM v5_event_links').fetchone()[0],0)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM v5_link_audit').fetchone()[0],1)
        self.assertEqual(self.c.execute('SELECT price,price_status FROM v5_forecasts').fetchone(),(None,'EVENT_UNMATCHED'))
    def test_legacy_link_with_wrong_away_team_quarantined(self):
        self.event('wrong',away='Атлетико Мадрид')
        self.forecast();self.q('wrong')
        self.c.execute('INSERT INTO v5_event_links VALUES (?,?,?,?,?,?)',('sf_forecasts','sf','wrong','OLD',T,K))
        result=self.run_once()
        self.assertEqual(result['quarantined_legacy_links'],1)
    def test_duplicate_genuine_fixtures_still_ambiguous(self):
        self.event('a');self.event('b');self.forecast();self.q('a');self.q('b')
        self.assertEqual(self.run_once()['price_statuses'],{'EVENT_AMBIGUOUS':1})
    def test_nflverse_la_means_rams_only_in_nflverse(self):
        self.event('nfl','Лас-Вегас Рэйдерс','Лос-Анджелес Рэмс','NFL. Регулярный сезон',
                   '2026-10-25T20:25:00+00:00','am-football')
        self.forecast('nflverse|2026_07_LA_LV','LV','LA','nfl','2026-10-25','1','1X2','americanfootball')
        self.q('nfl')
        self.assertEqual(self.run_once()['price_statuses'],{'PRICE_ATTACHED_TIME_VALID':1})
    def test_plain_la_unknown_does_not_map_to_rams(self):
        self.event('nfl','Лас-Вегас Рэйдерс','Лос-Анджелес Рэмс','NFL. Регулярный сезон',
                   '2026-10-25T20:25:00+00:00','am-football')
        self.forecast('non-nflverse','LV','LA','nfl','2026-10-25','1','1X2','americanfootball')
        self.q('nfl')
        self.assertEqual(self.run_once()['price_statuses'],{'EVENT_UNMATCHED':1})
    def test_moreirense_verified_alias(self):
        self.event('pt','Эшторил','Морейренше','Португалия. Премьер-Лига',
                   '2026-10-25T15:30:00+00:00')
        self.forecast('pt','GD Estoril Praia','Moreirense FC','pt.1','2026-10-25')
        self.q('pt')
        self.assertEqual(self.run_once()['price_statuses'],{'PRICE_ATTACHED_TIME_VALID':1})
    def test_totals_never_substitute_different_line(self):
        self.event('match');self.forecast(market='ТОТАЛ 2.5',sel='Больше 2.5')
        self.q('match','930',3.5)
        self.assertEqual(self.run_once()['price_statuses'],{'NO_ACTIVE_MATCHING_QUOTE':1})

if __name__=='__main__':unittest.main()
