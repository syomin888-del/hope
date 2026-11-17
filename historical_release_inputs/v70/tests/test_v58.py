import sqlite3, unittest, sys, pathlib, datetime as dt, json
from unittest.mock import patch
R=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(R/'files'))
sys.path.insert(0,str(R/'tests/fixtures'))
import oracle_v5 as v5
import oracle_v58_brain as b

AT='2026-10-24T10:00:00+00:00'
KICK='2026-10-25T13:30:00+00:00'

class BrainTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:')
        v5.setup(self.c);b.init(self.c)
        self.c.execute('''CREATE TABLE sf_matches(id TEXT PRIMARY KEY,sport TEXT,league TEXT,
         season TEXT,match_date TEXT,home TEXT,away TEXT,home_score INTEGER,away_score INTEGER,
         source TEXT,source_url TEXT,last_seen TEXT)''')
        self.c.execute('''CREATE TABLE fb_full_packets(packet_version TEXT,imported_at TEXT)''')
        self.c.execute('''CREATE TABLE fb_full_current(quote_key TEXT PRIMARY KEY,event_id TEXT,root_event_id TEXT,
         factor_id TEXT,point TEXT,decimal_price REAL,source_json TEXT,first_seen TEXT,
         last_seen TEXT,active INTEGER,last_snapshot TEXT)''')
        self.c.execute('''INSERT INTO fb_full_packets VALUES (?,?)''',('p','2026-10-24T09:58:00+00:00'))
        self.c.execute('''INSERT INTO v5_catalog VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
         ('root','football','Germany','Майнц','Вердер',KICK,'online',1,5,3,0,'ACTIVE_QUOTED','p',AT,AT))
    def tearDown(self):self.c.close()
    def games(self):
        team=['1. FSV Mainz 05','SV Werder Bremen','FC Bayern München','SC Freiburg','FC Augsburg','FC Schalke 04']
        start=dt.date(2023,1,1)
        records=[]
        for i in range(340):
            day=(start+dt.timedelta(days=i*3//2)).isoformat()
            h,a=team[i%6],team[(i+1+(i//6)%4)%6]
            if h==a:a=team[(i+2)%6]
            sh,sa=((i*7)%5,(i*3)%4)
            records.append(('football','de.1',day,h,a,sh,sa,'openfootball_CC0','url'))
        return records
    def test_scoreboard_rejects_started_and_unverified(self):
        raw={'events':[{'id':'one','date':'2026-10-20T10:00:00Z','status':{'type':{'completed':False,'name':'STATUS_IN_PROGRESS'}},'competitions':[{'competitors':[{'homeAway':'home','team':{'displayName':'A'},'score':'2'},{'homeAway':'away','team':{'displayName':'B'},'score':'1'}]}]}]}
        self.assertEqual(b.parse_espn(raw,'basketball','nba',AT),[])
        raw['events'][0]['status']['type'].update(completed=True,name='STATUS_FINAL')
        vals=b.parse_espn(raw,'basketball','nba',AT)
        self.assertEqual(len(vals),1)
        self.assertEqual(vals[0][-1],'SOURCE_MARKED_FINAL')
        raw['events'][0]['date']='2026-10-25T10:00:00Z'
        self.assertEqual(b.parse_espn(raw,'basketball','nba',AT),[])
    def test_identity_requires_verified_alias(self):
        self.assertFalse(b.same_team('1. FSV Mainz 05','Бавария'))
        self.assertTrue(b.same_team('1. FSV Mainz 05','Майнц'))
        self.assertFalse(b.same_team('SC Freiburg','Майнц'))
    def test_historical_future_scores_excluded(self):
        self.c.execute('INSERT INTO sf_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
          ('a','football','de.1','2026-27','2026-10-23','Home','Away',2,1,'openfootball_CC0','url',AT))
        self.c.execute('INSERT INTO sf_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
          ('b','football','de.1','2026-27','2026-10-25','Home','Away',30,0,'openfootball_CC0','url',AT))
        r=b.collect_history(self.c,AT)
        self.assertEqual(len(r),1)
        self.assertEqual(r[0][5],2)
    def test_multisport_lacks_history_no_fabricated_probability(self):
        self.c.execute('''INSERT INTO v5_catalog VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        ('unseen','volleyball','league','Север','Юг',KICK,'online',1,2,1,0,'ACTIVE_QUOTED','p',AT,AT))
        out=b.forecast(self.c,AT,[])
        self.assertEqual(out['fonbet_quoted_upcoming_scanned'],2)
        self.assertEqual(out['coverage_states'].get('UNSUPPORTED_SPORT_HISTORY'),1)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM v58_forecasts').fetchone()[0],0)
    def test_model_generates_3_distributions_only_with_history(self):
        g=self.games()
        for model in b.MODEL_NAMES:
            result=b.predict(model,g,'1. FSV Mainz 05','SV Werder Bremen','football','de.1')
            self.assertIsNotNone(result)
            self.assertAlmostEqual(sum(result[0]),1.,places=8)
        self.assertIsNone(b.predict('FORM',g[:50],'1. FSV Mainz 05','SV Werder Bremen','football','de.1'))
    def test_forecast_and_price_do_not_retroactively_assume_old_quote(self):
        hist=self.games()
        with self.c:
            for model in b.MODEL_NAMES:
                self.c.execute('''INSERT INTO v58_validation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                 ('football','de.1',model,AT,60,.12,.18,.03,.7,0,None,1.,1,'PASSED'))
        out=b.forecast(self.c,AT,hist)
        self.assertEqual(out['coverage_states']['VALIDATED_RESEARCH_READY'],1)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM v5_forecasts WHERE origin='v58_brain'").fetchone()[0],9)
        # Quote existed before forecast: must not attach until a quote is observed after asof.
        self.c.execute('INSERT INTO fb_full_current VALUES (?,?,?,?,?,?,?,?,?,?,?)',
         ('q1','root','root','921','{}',2.5,'{}','2026-10-24T09:50:00+00:00','2026-10-24T09:50:00+00:00',1,'2026-10-24T09:58:00+00:00'))
        out=b.attach(self.c,AT)
        self.assertEqual(out['attached'],0)
        self.c.execute("UPDATE fb_full_current SET last_snapshot='2026-10-24T10:02:00+00:00'")
        self.c.execute("UPDATE fb_full_packets SET imported_at='2026-10-24T10:02:00+00:00'")
        out=b.attach(self.c,'2026-10-24T10:03:00+00:00')
        self.assertEqual(out['attached'],3) # three distinct calibrated model candidates for 1X2 home
        self.assertEqual(b.attach(self.c,'2026-10-24T10:03:00+00:00')['attached'],0)
    def test_quote_after_kickoff_cannot_attach(self):
        hist=self.games()
        with self.c:
            self.c.execute('INSERT INTO v58_validation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              ('football','de.1','ELO',AT,60,.12,.2,.03,.7,0,None,1.,1,'PASSED'))
        b.forecast(self.c,AT,hist)
        self.c.execute('INSERT INTO fb_full_current VALUES (?,?,?,?,?,?,?,?,?,?,?)',
         ('q1','root','root','921','{}',2.5,'{}',AT,AT,1,'2026-10-25T13:31:00+00:00'))
        self.c.execute("UPDATE fb_full_packets SET imported_at='2026-10-25T13:31:00+00:00'")
        self.assertEqual(b.attach(self.c,'2026-10-25T13:32:00+00:00')['attached'],0)
    def test_settlement_only_one_verified_full_time_result(self):
        self.c.execute('INSERT INTO sf_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
          ('a','football','de.1','2026-27','2026-10-25','1. FSV Mainz 05','SV Werder Bremen',2,1,'openfootball_CC0','url','2026-10-25T18:00:00+00:00'))
        self.assertIsNone(b.resolve_final_for_portfolio(self.c,'v58|root',KICK,'2026-10-25T14:00:00+00:00'))
        self.assertEqual(b.resolve_final_for_portfolio(self.c,'v58|root',KICK,'2026-10-25T19:00:00+00:00')[:2],(2,1))
        self.c.execute('INSERT INTO sf_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
          ('a2','football','de.1','2026-27','2026-10-25','1. FSV Mainz 05','SV Werder Bremen',3,1,'openfootball_CC0','url','2026-10-25T18:00:00+00:00'))
        self.assertIsNone(b.resolve_final_for_portfolio(self.c,'v58|root',KICK,'2026-10-25T19:00:00+00:00'))
    def test_shared_history_models_do_not_double_vote_in_portfolio(self):
        import oracle_v5_settlement as paper
        import oracle_v56_portfolio as portfolio
        paper.setup(self.c)
        for i,model in enumerate(('v58_ELO_WALKFORWARD','v58_POISSON_WALKFORWARD')):
            fid=f'signal{i}'
            self.c.execute("""INSERT INTO v5_forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (fid,'v58_brain','v58|root',AT,'football','de.1','Майнц','Вердер','2026-10-25',KICK,
              '1X2','1',.6,model,'root','verified',2.2,'q',AT,'PRICE_ATTACHED_TIME_VALID',AT,'{}'))
            self.c.execute("""INSERT INTO v55_paper(forecast_id,root_id,original_event_id,
               origin,market,selection,probability,odds,stake,quote_key,quote_at,
               kickoff_utc,registered_at,expected_ev_pct,status,reason)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (fid,'root','v58|root','v58_brain','1X2','1',.6,2.2,10,'q',AT,KICK,AT,32,'PENDING','research'))
        self.assertEqual(portfolio.sync(self.c,AT)['new_portfolio_selections'],0)

    def test_80_percent_goal_needs_long_record_and_confidence(self):
        self.assertLess(b._wilson_lower(8,10),.8)
        self.assertGreater(b._wilson_lower(100,100),.8)

    def test_no_license_restricted_feeds(self):
        self.assertNotIn('football-data.co.uk',str(b.ESPN_LEAGUES))
    def test_validation_nontrivial_and_no_future_scoring(self):
        r=b.validate_sport('ELO',self.games(),'football','de.1')
        self.assertIn('passed',r)
        self.assertNotEqual(r['reason'],'UNDEFINED')
        if r['oos_n']:
            self.assertIsNotNone(r['brier'])
    def test_espn_ingestion_is_idempotent(self):
        data={'events':[{'id':'x1','date':'2026-10-23T12:00:00Z','status':{'type':{'completed':True,'name':'STATUS_FINAL'}},'competitions':[{'competitors':[{'homeAway':'home','team':{'displayName':'Boston Celtics'},'score':'110'},{'homeAway':'away','team':{'displayName':'Chicago Bulls'},'score':'90'}]}]}]}
        def fetch(url):return data
        with patch.dict('os.environ',{'ORACLE_V58_ESPN_ENABLED':'1'}):
            a=b.ingest_espn(self.c,AT,2,fetch)
        self.assertEqual(a['state'],'OK')
        self.assertEqual(a['attempted'],2)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM v58_results').fetchone()[0],2) # different league IDs
        with patch.dict('os.environ',{'ORACLE_V58_ESPN_ENABLED':'1'}):
            self.assertEqual(b.ingest_espn(self.c,AT,2,fetch)['attempted'],2) # moves to next source days
    def test_validation_accuracy_not_claimed_in_advance(self):
        self.assertFalse(b.report(self.c)['goal_verified'])

if __name__=='__main__':unittest.main()
