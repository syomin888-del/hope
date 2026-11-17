import sqlite3,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'files'))
import oracle_v5 as v5
import oracle_v5_settlement as paper
import oracle_v56_portfolio as portfolio

NOW='2026-10-24T09:36:00+00:00'
KICK='2026-10-25T12:00:00+00:00'

class PortfolioTests(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:');v5.setup(self.c);paper.setup(self.c)
  self.c.execute('''INSERT INTO v5_catalog VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
      ('event','football','league','Home','Away',KICK,'line',1,15,5,0,'ACTIVE_QUOTED','packet',NOW,NOW))
 def tearDown(self):self.c.close()
 def add(self,i,model,market='1X2',sel='1',ev=10,odds=2.5,root='event',kick=KICK,registered=NOW):
  fid=f'f{i}';self.c.execute('''INSERT INTO v5_forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
   (fid,'sf_forecasts','game',registered,'football','league','Home','Away','2026-10-25',kick,market,sel,.5,model,root,'EXPLICIT',odds,'quote',registered,'PRICE_ATTACHED_TIME_VALID',registered,'{}'))
  self.c.execute('''INSERT INTO v55_paper(
    forecast_id,root_id,original_event_id,origin,market,selection,probability,
    odds,stake,quote_key,quote_at,kickoff_utc,registered_at,expected_ev_pct,status,reason)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
    (fid,root,'game','sf_forecasts',market,sel,.5,odds,10,'q'+str(i),registered,kick,registered,ev,'PENDING','RESEARCH'))
 def test_repeated_model_one_vote_and_one_event_signal(self):
  self.add(1,'poisson');self.add(2,'poisson');self.add(3,'elo')
  self.add(4,'poisson','1X2','2',23);self.add(5,'elo','1X2','2',23)
  r=portfolio.sync(self.c,NOW)
  self.assertEqual(r['new_portfolio_selections'],1)
  self.assertEqual(self.c.execute('SELECT COUNT(*) FROM v55_paper').fetchone()[0],5)
  self.assertEqual(self.c.execute('SELECT COUNT(*) FROM v56_portfolio').fetchone()[0],1)
  self.assertEqual(portfolio.sync(self.c,NOW)['new_portfolio_selections'],0)
  self.assertEqual(portfolio.summary(self.c)['model_experiments_retained'],5)
 def test_not_enough_independent_models_abstains(self):
  self.add(1,'poisson');self.add(2,'poisson')
  self.assertEqual(portfolio.sync(self.c,NOW)['new_portfolio_selections'],0)
 def test_no_retroactive_selection_after_kickoff(self):
  self.add(1,'poisson');self.add(2,'elo')
  self.assertEqual(portfolio.sync(self.c,'2026-10-25T13:00:00+00:00')['new_portfolio_selections'],0)
 def test_separate_model_and_portfolio_profit(self):
  self.add(1,'poisson',ev=22);self.add(2,'elo',ev=12)
  portfolio.sync(self.c,NOW)
  sel=self.c.execute('SELECT forecast_id FROM v56_portfolio').fetchone()[0]
  self.c.execute("UPDATE v55_paper SET status='WIN',profit=15 WHERE forecast_id=?",(sel,))
  self.c.execute("UPDATE v55_paper SET status='LOSS',profit=-10 WHERE forecast_id<>?",(sel,))
  self.assertEqual(portfolio.summary(self.c)['net_profit_usd'],15)
  self.assertEqual(paper.summary(self.c)['net_profit_usd'],5)
 def test_model_outcome_requires_confirmed_source(self):
  self.c.execute('CREATE TABLE sf_matches(id TEXT PRIMARY KEY,home_score INTEGER,away_score INTEGER,last_seen TEXT,source TEXT)')
  self.assertIsNone(paper._resolve_score(self.c,'sf_forecasts','game',KICK))
  self.c.execute('INSERT INTO sf_matches VALUES (?,?,?,?,?)',('game',2,0,'2026-10-25T18:00:00+00:00','nflverse_in_progress'))
  self.assertIsNone(paper._resolve_score(self.c,'sf_forecasts','game',KICK))
  self.c.execute("UPDATE sf_matches SET source='openfootball_CC0' WHERE id='game'")
  self.assertIsNotNone(paper._resolve_score(self.c,'sf_forecasts','game',KICK))
 def test_canonical_key_aliases(self):
  self.assertEqual(portfolio.key_for('1X2','П1'),portfolio.key_for('1X2','1'))
  self.assertNotEqual(portfolio.key_for('1X2','1'),portfolio.key_for('1X2','X'))
 def test_read_only_portfolio_page(self):
  self.add(1,'poisson');self.add(2,'elo');portfolio.sync(self.c,NOW)
  d=portfolio.page(self.c)
  self.assertEqual(d['total'],1)
  self.assertEqual(d['items'][0]['home'],'Home')

if __name__=='__main__': unittest.main()
