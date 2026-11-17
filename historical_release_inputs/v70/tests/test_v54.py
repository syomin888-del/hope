"""ORACLE V5.4 evidence retrieval tests: discovery is not fact verification."""
import sys, unittest, sqlite3, urllib.parse
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'files'))
sys.path.insert(0,str(ROOT/'tests'/'fixtures'))
import oracle_v5 as v
AT='2026-10-24T10:00:00+00:00'

class EvidenceDiscoveryTests(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:')
  v.setup(self.c)
 def tearDown(self):self.c.close()
 def add(self,root,home='Боруссия Дортмунд',away='Айнтрахт Франкфурт',priority=100,checked=None):
  self.c.execute('''INSERT INTO v5_catalog (root_id,sport,league,home,away,kickoff_utc,place,root_exists,active_quotes,market_codes,nested_events,quote_status,packet_version,packet_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(root,'football','Germany',home,away,'2026-10-25T18:30:00+00:00','line',1,10,8,0,'ACTIVE_QUOTED','p',AT,AT))
  self.c.execute('''INSERT INTO v5_research_queue (root_id,priority,reasons,updated_at,last_checked) VALUES(?,?,?,?,?)''',(root,priority,'FORECAST_EVENT',AT,checked))
 def test_query_uses_or_and_not_implicit_and(self):
  url=v.evidence_feed_url({'home':'Боруссия Дортмунд','away':'Айнтрахт Франкфурт','sport':'football'})
  params=urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
  q=params['q'][0]
  self.assertIn(' OR ',q)
  self.assertIn('when:3d',q)
  self.assertNotIn(' football ',q)
  self.assertEqual(params['ceid'],['RU:ru'])
 def test_known_aliases_appear_and_switch_locale(self):
  url=v.evidence_feed_url({'home':'Боруссия Дортмунд','away':'Айнтрахт Франкфурт','home_alias':'Borussia Dortmund','away_alias':'Eintracht Frankfurt'})
  params=urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
  self.assertIn('Borussia Dortmund',params['q'][0]);self.assertIn('Боруссия',params['q'][0]);self.assertEqual(params['ceid'],['US:en'])
 def test_missing_participants_not_queried(self):
  self.assertIsNone(v.evidence_feed_url({'home':'','away':''}))
 def test_news_items_are_unverified_and_counted(self):
  self.add('r1')
  def fake(url):return [{'url':'https://news.google.com/rss/articles/abc','title':'A headline, not a proven fact','published':None}]
  out=v.check_public_evidence(self.c,AT,loader=fake,pause=lambda _:None)
  self.assertEqual(out['articles_added'],1)
  self.assertEqual(out['events_with_hits'],1)
  self.assertEqual(out['http_or_parse_errors'],0)
  row=self.c.execute('SELECT verification FROM v5_research_sources').fetchone()
  self.assertEqual(row[0],'UNVERIFIED_SEARCH_HIT')
  second=v.check_public_evidence(self.c,AT,loader=fake,pause=lambda _:None)
  self.assertEqual(second['events_attempted'],0)
 def test_empty_feed_not_reported_as_error(self):
  self.add('r1')
  out=v.check_public_evidence(self.c,AT,loader=lambda url:[],pause=lambda _:None)
  self.assertEqual((out['events_attempted'],out['empty_searches'],out['http_or_parse_errors'],out['articles_added']), (1,1,0,0))
 def test_error_visible_separately(self):
  self.add('r1')
  def bad(url):raise TimeoutError('source timeout')
  out=v.check_public_evidence(self.c,AT,loader=bad,pause=lambda _:None)
  self.assertEqual(out['state'],'PARTIAL')
  self.assertEqual(out['http_or_parse_errors'],1)
  self.assertEqual(out['error_kinds'],{'TimeoutError':1})
 def test_invalid_url_not_saved(self):
  self.add('r1')
  result=v.check_public_evidence(self.c,AT,loader=lambda url:[{'url':'https://not-trusted.example','title':'Fake'}],pause=lambda _:None)
  self.assertEqual(result['articles_returned'],1)
  self.assertEqual(result['articles_added'],0)
 def test_priority_and_batch(self):
  self.add('low',priority=30)
  self.add('high',priority=100)
  out=v.check_public_evidence(self.c,AT,max_events=1,loader=lambda url:[],pause=lambda _:None)
  self.assertEqual(out['events_attempted'],1)
  self.assertIsNotNone(self.c.execute('SELECT last_checked FROM v5_research_queue WHERE root_id=?',('high',)).fetchone()[0])
  self.assertIsNone(self.c.execute('SELECT last_checked FROM v5_research_queue WHERE root_id=?',('low',)).fetchone()[0])
 def test_existing_forecast_alias_is_used(self):
  self.add('r1')
  self.c.execute('''INSERT INTO v5_forecasts (id,origin,original_event_id,created_at,sport,league,home,away,match_date,market,selection,probability,model,root_id,match_rule,price_status,evaluated_at,details_json)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',('f','sf_forecasts','e',AT,'football','de.1','Borussia Dortmund','Eintracht Frankfurt','2026-10-25','1X2','1',0.4,'test','r1','verified','NO_ACTIVE_MATCHING_QUOTE',AT,'{}'))
  urls=[]
  v.check_public_evidence(self.c,AT,loader=lambda url:(urls.append(url) or []),pause=lambda _:None)
  params=urllib.parse.parse_qs(urllib.parse.urlparse(urls[0]).query)
  self.assertIn('Borussia Dortmund',params['q'][0])
 def test_scheduler_batch_and_cadence(self):
  code=(ROOT/'tests/support/oracle_v4.py').read_text()
  self.assertIn('ORACLE_V5_EVIDENCE_BATCH',code)
  self.assertIn("'48'",code)
  self.assertIn("'1800'",code)

if __name__=='__main__':unittest.main()
