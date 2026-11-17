import datetime as dt
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import collect,sources,dashboard,quality

UTC=dt.timezone.utc

class SourceUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.cn=collect.db_open(Path(self.tmp.name)/'db.sqlite3')
        sources.create_tables(self.cn)
    def tearDown(self):
        self.cn.close();self.tmp.cleanup()
    def test_snapshot_dedup_keeps_original(self):
        self.assertEqual(sources.smart_snapshot(self.cn,'report','id',{'a':2}),1)
        self.assertEqual(sources.smart_snapshot(self.cn,'report','id',{'a':2}),0)
        self.assertEqual(sources.smart_snapshot(self.cn,'report','id',{'a':3}),1)
        rows=self.cn.execute('select payload from raw_snapshots order by id').fetchall()
        self.assertEqual(len(rows),2)
    def test_realtime_sources_never_emit_signals(self):
        with patch.object(collect,'request_json',side_effect=[{'trades':[{'price':'2'}]}]*3):
            self.assertEqual(sources.coinbase(self.cn),3)
        self.assertEqual(self.cn.execute("select count(*) from sqlite_master where name='signals'").fetchone()[0],0)
    def test_defillama_dual_feed(self):
        with patch.object(collect,'request_json',side_effect=[[{'name':'Ethereum','tvl':200}],{'peggedAssets':[]}]):
            self.assertEqual(sources.defillama(self.cn),2)
    def test_fear_greed_dedup(self):
        with patch.object(collect,'request_json',return_value={'data':[{'timestamp':'1792000000','value':'25'}]}):
            self.assertEqual(sources.fear_greed(self.cn),1)
            self.assertEqual(sources.fear_greed(self.cn),0)
    def test_kraken_skip_unclosed(self):
        now=dt.datetime.now(UTC).timestamp()
        yesterday=int(now-3*3600)
        recent=int(now-100)
        payload={'error':[],'result':{'XXBTZUSD':[[yesterday,'10','11','9','10.5','10','100','3'],[recent,'9','10','8','9','9','20','4']],'last':1}}
        with patch.object(collect,'request_json',return_value=payload):
            self.assertEqual(sources.kraken(self.cn),3)
        row=self.cn.execute("select count(*) from candles where provider='kraken_public'").fetchone()[0]
        self.assertEqual(row,3)
    def test_skip_without_key(self):
        with patch.object(collect,'request_json') as fetch:
            s,n=sources.run_one(self.cn,{},'nba_odds')
            self.assertEqual((s,n),('skip',0));fetch.assert_not_called()
        latest=self.cn.execute('select state from source_runs order by id desc limit 1').fetchone()
        self.assertEqual(latest[0],'skip')
    def test_schedule_remembers_next_due(self):
        old=(dt.datetime(2026,10,20,tzinfo=UTC))
        with patch.dict(sources.SCHEDULE,{'test_good':(3600,lambda c,k:2)},clear=True):
            self.assertEqual(sources.run_due(self.cn,{},old)['test_good']['new_records'],2)
            self.assertEqual(sources.run_due(self.cn,{},old+dt.timedelta(minutes=1)),{})
            self.assertEqual(sources.run_due(self.cn,{},old+dt.timedelta(hours=1))['test_good']['new_records'],2)
    def test_error_backoff_and_health(self):
        old=dt.datetime(2026,10,20,tzinfo=UTC)
        def boom(c,k):raise RuntimeError('provider unavailable')
        with patch.dict(sources.SCHEDULE,{'broken':(3600,boom)},clear=True):
            s=sources.run_due(self.cn,{},old)
            self.assertEqual(s['broken']['status'],'error')
            self.assertEqual(sources.run_due(self.cn,{},old+dt.timedelta(hours=1)),{})
            s2=sources.run_due(self.cn,{},old+dt.timedelta(hours=2))
            self.assertEqual(s2['broken']['status'],'error')
            row=self.cn.execute("select consecutive_errors from source_schedule where job='broken'").fetchone()
            self.assertEqual(row[0],2)
    def test_dashboard_reads_health(self):
        sources.log_run(self.cn,'defillama','error',0,'HTTP 503')
        x=dashboard.snapshot(Path(self.tmp.name)/'db.sqlite3',Path(self.tmp.name)/'models.json')
        self.assertEqual(x['source_jobs'][0]['state'],'error')
    def test_quality_says_no_prediction(self):
        x=quality.audit(self.cn)
        self.assertFalse(x['prediction_authorized'])
        self.assertTrue(any(r['state']=='NEVER_RUN' for r in x['sources']))
    def test_multisport_odds_requires_key(self):
        with patch.object(collect,'request_json') as fetch:
            self.assertEqual(sources.multisport_odds(self.cn,{}),0)
            fetch.assert_not_called()
    def test_multisport_odds_three_categories(self):
        with patch.object(collect,'request_json',return_value=[{'id':'m1','commence_time':'2026-10-22T00:00:00Z'}]) as fetch:
            self.assertEqual(sources.multisport_odds(self.cn,{'ORACLE_ODDS_KEY':'test'}),3)
            self.assertEqual(fetch.call_count,3)
    def test_rss_parse_atom(self):
        atom='''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>a1</id><title>Release 3</title><updated>2026-10-20T00:00:00Z</updated><link href="https://github.com/bitcoin/bitcoin/releases/tag/v3" /></entry></feed>'''
        # All 3 sources use same feed sample; separate provider names.
        import xml.etree.ElementTree as ET
        with patch.object(sources,'retrieve_xml',return_value=ET.fromstring(atom)):
            self.assertEqual(sources.rss_news(self.cn),3)
            self.assertEqual(sources.rss_news(self.cn),0)

if __name__=='__main__':unittest.main()
