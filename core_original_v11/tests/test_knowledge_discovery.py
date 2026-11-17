import datetime as dt
import hashlib
import json
import math
import sqlite3
import tempfile
import unittest
from unittest import mock

import collect
import knowledge
import discovery
import brain_daemon

class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.c=collect.db_open(':memory:')
    def tearDown(self):self.c.close()
    def test_point_in_time_no_future(self):
        old='2026-10-01T10:00:00+00:00';new='2026-10-01T10:30:00+00:00'
        doc1={'current_price':100,'last_updated':'2026-10-01T09:50:00Z'}
        doc2={'current_price':110,'last_updated':'2026-10-01T10:30:00Z'}
        for t,obj in ((old,doc1),(new,doc2)):
            payload=json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(',',':'))
            sha=hashlib.sha256(payload.encode()).hexdigest()
            self.c.execute('INSERT INTO raw_snapshots(provider,subject,observed_at,source_time,sha256,payload) VALUES(?,?,?,?,?,?)',
                ('coingecko_market','coin',t,'2026-09-01T00:00:00Z',sha,payload))
        knowledge.ingest(self.c)
        before=knowledge.as_known(self.c,'2026-10-01T10:15:00+00:00','coingecko_market','coin')
        price=[x for x in before if x['field']=='current_price'][0]
        self.assertEqual(price['value'],100)
        self.assertEqual(price['known_since'],old)
        after=knowledge.as_known(self.c,'2026-10-01T10:45:00+00:00','coingecko_market','coin')
        self.assertEqual([x for x in after if x['field']=='current_price'][0]['value'],110)
    def test_offset_normalization(self):
        collect.snapshot(self.c,'bitcoin_mempool','x',{'count':22})
        knowledge.ingest(self.c)
        self.assertTrue(knowledge.as_known(self.c,'2030-01-01T09:00:00+09:00'))
        self.assertFalse(knowledge.as_known(self.c,'2000-01-01T00:00:00+00:00'))
    def test_no_naive_cutoff(self):
        with self.assertRaises(ValueError):knowledge.as_known(self.c,'2026-01-01T12:00:00')
    def test_unsupported_fact_never_auto_promoted(self):
        collect.snapshot(self.c,'unknown_blog','abc',{'prediction':'guaranteed'})
        result=knowledge.ingest(self.c)
        self.assertEqual(result['facts_added'],0)
        self.assertEqual(knowledge.status(self.c)['facts'],0)
    def test_hash_tamper_cannot_be_promoted(self):
        collect.snapshot(self.c,'bitcoin_mempool','x',{'count':22})
        self.c.execute("UPDATE raw_snapshots SET payload='{}'")
        result=knowledge.ingest(self.c)
        self.assertEqual(result['facts_added'],0)
        self.assertEqual(result['rejected'],1)
    def test_checkpoint_prevents_reprocessing(self):
        collect.snapshot(self.c,'bitcoin_mempool','x',{'count':10})
        a=knowledge.ingest(self.c)
        b=knowledge.ingest(self.c)
        self.assertEqual(a['facts_added'],1)
        self.assertEqual(b['snapshots_scanned'],0)
    def test_sources_are_observations_not_verified_facts(self):
        collect.snapshot(self.c,'official_news_bitcoin_core_releases','story',
            {'title':'Proposal (unverified)','link':'https://example.com/x'})
        knowledge.ingest(self.c)
        self.assertTrue(knowledge.status(self.c)['facts']>0)
        self.assertFalse('confidence' in knowledge.status(self.c))
    def test_ingest_pagination(self):
        for i in range(3):collect.snapshot(self.c,'bitcoin_mempool',str(i),{'count':i})
        self.assertEqual(knowledge.ingest(self.c,limit=2)['facts_added'],2)
        self.assertEqual(knowledge.ingest(self.c,limit=2)['facts_added'],1)

class DiscoveryTests(unittest.TestCase):
    def setUp(self):self.c=collect.db_open(':memory:')
    def tearDown(self):self.c.close()
    def test_preregister_idempotent(self):
        x=discovery.register(self.c,'BTCUSDT',6,.01,'momentum6')
        self.assertEqual(x,discovery.register(self.c,'BTCUSDT',6,.01,'momentum6'))
        self.assertEqual(discovery.status(self.c)['registered_hypotheses'],1)
    def test_never_equates_insufficient_data_to_a_signal(self):
        out=discovery.assess(self.c,'BTCUSDT',6,.01,'momentum6',[])
        self.assertEqual(out['status'],'WAIT_DATA')
    def test_identical_results_do_not_count_as_independent_tests(self):
        records=[]
        for i in range(260):
            records.append((1_700_000_000_000+i*6*3_600_000,i%4==0,{'momentum6':i%3==0}))
        a=discovery.assess(self.c,'BTCUSDT',6,.01,'momentum6',records)
        b=discovery.assess(self.c,'BTCUSDT',6,.01,'momentum6',records)
        self.assertEqual(a['status'],'REJECTED')
        self.assertTrue(b['retested'])
        self.assertEqual(discovery.status(self.c)['experiments'],1)
    def test_wilson_prevents_perfect_score_overconfidence(self):
        a=discovery._wilson(20,20)
        self.assertLess(a[0],1.)
        b=discovery._wilson(0,20)
        self.assertGreater(b[1],0.)
    def test_no_overconfident_tiny_sample(self):
        self.assertFalse(discovery.score([1]*7,[0]*7)['pass'])
    def test_autonomous_never_places_trades(self):
        self.assertFalse(discovery.status(self.c)['autonomous_trading'])
    def test_scheduler_has_knowledge_and_discovery(self):
        self.assertIn('knowledge',brain_daemon.JOBS)
        self.assertIn('discover',brain_daemon.JOBS)

if __name__=='__main__':unittest.main()

class DashboardCompatibility(unittest.TestCase):
    def test_new_status_on_read_only_connection_before_ingestion(self):
        import os,tempfile
        import dashboard
        with tempfile.TemporaryDirectory() as d:
            path=os.path.join(d,'data.sqlite3')
            c=sqlite3.connect(path)
            c.execute('CREATE TABLE dummy(x)');c.commit();c.close()
            with sqlite3.connect('file:'+path+'?mode=ro',uri=True) as r:
                self.assertFalse(knowledge.status(r)['initialized'])
                self.assertFalse(discovery.status(r)['initialized'])
    def test_status_on_read_only_connection_after_ingestion(self):
        import os,tempfile
        with tempfile.TemporaryDirectory() as d:
            path=os.path.join(d,'data.sqlite3')
            c=collect.db_open(path);knowledge.setup(c);discovery.setup(c);c.close()
            with sqlite3.connect('file:'+path+'?mode=ro',uri=True) as r:
                self.assertTrue(knowledge.status(r)['facts']==0)
                self.assertTrue(discovery.status(r)['registered_hypotheses']==0)

class ResearchIdentityTests(unittest.TestCase):
    def test_frozen_shadow_model_cannot_be_overwritten_by_new_training(self):
        import tempfile,pathlib,research
        from unittest.mock import patch
        c=collect.db_open(':memory:');research.initialize(c)
        try:
            with tempfile.TemporaryDirectory() as d:
                art=pathlib.Path(d)/'frozen.pkl';art.write_bytes(b'trusted-existing-artifact')
                name=research.model_id('BTCUSDT',6,.02)
                research.store(c,name,'BTCUSDT',6,.02,'HISTORICAL_PASS_SHADOW',{'passed_historical_gate':True},art.name,'initialsha')
                with patch.object(research,'MODEL_DIR',pathlib.Path(d)), patch.object(research,'get_candles',side_effect=AssertionError('should NOT retrain')):
                    a=research.train_one(c,'BTCUSDT',6,.02)
                    self.assertEqual(a['state'],'FROZEN_SHADOW')
                self.assertEqual(c.execute('SELECT data_sha FROM research_models WHERE id=?',(name,)).fetchone()[0],'initialsha')
        finally:c.close()
