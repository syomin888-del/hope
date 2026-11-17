import datetime as dt
import json
import pathlib
import tempfile
import unittest
import sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from oracle import db_open,add_signal,resolve,validate,dispatch,UTC

class TestOracle(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.con=db_open(pathlib.Path(self.tmp.name)/'t.db')
        self.clock=dt.datetime(2026,10,20,10,2,tzinfo=UTC)
        self.base=json.loads((pathlib.Path(__file__).resolve().parents[1]/'examples/forecast_structure.json').read_text())
        self.ok={'model-sport':{'approved':True,'kind':'sport'}}
        self.base['model_id']='model-sport'
    def tearDown(self):
        self.con.close();self.tmp.cleanup()
    def test_default_registry_prevents_send(self):
        with self.assertRaisesRegex(ValueError,'not independently approved'):
            add_signal(self.con,self.base,{},self.clock)
        self.assertEqual(self.con.execute('select count(*) from outbox').fetchone()[0],0)
    def test_publish_two_notifications(self):
        add_signal(self.con,self.base,self.ok,self.clock)
        self.assertEqual(self.con.execute('select count(*) from outbox').fetchone()[0],1)
        self.assertEqual(dispatch(self.con,False),0)
        label=resolve(self.con,{'id':self.base['id'],'outcome':'loss','source_url':'https://stats.example.com/result'},dt.datetime(2026,10,23,tzinfo=UTC))
        self.assertEqual(label,'loss')
        self.assertEqual(self.con.execute('select count(*) from outbox').fetchone()[0],2)
    def test_no_duplicates(self):
        add_signal(self.con,self.base,self.ok,self.clock)
        with self.assertRaises(Exception):add_signal(self.con,self.base,self.ok,self.clock)
    def test_future_evidence_rejected(self):
        self.base['sources'][0]['published_at']='2026-10-21T00:00:00Z'
        with self.assertRaisesRegex(ValueError,'Future evidence'):validate(self.base,self.ok,self.clock)
    def test_probabilities_must_clear_edge(self):
        self.base['probability_lower']=0.54
        with self.assertRaisesRegex(ValueError,'Conservative edge'):validate(self.base,self.ok,self.clock)
    def test_quote_staleness(self):
        self.base['odds_checked_at']='2026-10-20T09:00:00Z'
        with self.assertRaisesRegex(ValueError,'too old'):validate(self.base,self.ok,self.clock)
    def test_early_settlement_rejected(self):
        add_signal(self.con,self.base,self.ok,self.clock)
        with self.assertRaisesRegex(ValueError,'Not yet'):resolve(self.con,{'id':self.base['id'],'outcome':'win','source_url':'https://stats.example.com/result'},self.clock)
    def test_crypto_horizon_validation(self):
        c=dict(self.base,kind='crypto',model_id='model-crypto',coin_id='test-coin',entry_usd=1.0,target_usd=1.3,entry_checked_at='2026-10-20T10:00:00Z')
        del c['odds'];del c['bookmaker'];del c['selection'];del c['odds_checked_at']
        add_signal(self.con,c,{'model-crypto':{'approved':True,'kind':'crypto'}},self.clock)
        label=resolve(self.con,{'id':c['id'],'price_usd':1.15,'source_url':'https://prices.example.com'},dt.datetime(2026,10,23,tzinfo=UTC))
        self.assertEqual(label,'loss')
if __name__=='__main__':unittest.main()
