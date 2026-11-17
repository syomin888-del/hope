"""Fail-closed self-learning promotion: immutable proposals, forward data and evidence.

Automated idea collection never upgrades production trading logic. Evaluation is
unit-tested; no optimistic assumptions for missing values or tiny samples.
"""
from __future__ import annotations
import datetime as dt,hashlib,json,math,sqlite3,statistics

def setup(c):
 c.executescript('''CREATE TABLE IF NOT EXISTS v4_experiments(
  id TEXT PRIMARY KEY,branch TEXT NOT NULL,source_id TEXT NOT NULL,
  statement TEXT NOT NULL,created_at TEXT NOT NULL,method TEXT NOT NULL,
  feature_cutoff TEXT NOT NULL,locked_params_sha256 TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'HYPOTHESIS');
 CREATE TABLE IF NOT EXISTS v4_evaluations(
  experiment_id TEXT NOT NULL,split_id TEXT NOT NULL,observed_at TEXT NOT NULL,
  n_independent INTEGER NOT NULL,mean_net REAL,stderr_net REAL,
  baseline_mean_net REAL,leakage_check INTEGER NOT NULL,
  method TEXT NOT NULL,paper INTEGER NOT NULL,
  PRIMARY KEY(experiment_id,split_id));
 CREATE TABLE IF NOT EXISTS v4_promotions(
  experiment_id TEXT PRIMARY KEY,decision TEXT NOT NULL,at TEXT NOT NULL,
  reason TEXT NOT NULL);
 ''');c.commit()

def preregister(c,branch,source_id,statement,method,feature_cutoff,parameters,at):
 for v in (branch,source_id,statement,method,feature_cutoff):
  if not isinstance(v,str) or not v.strip():raise ValueError('Missing preregistration provenance')
 if dt.datetime.fromisoformat(feature_cutoff.replace('Z','+00:00'))>dt.datetime.fromisoformat(at.replace('Z','+00:00')):
  raise ValueError('Feature cutoff is future information')
 locked=hashlib.sha256(json.dumps(parameters,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 eid=hashlib.sha256((branch+'|'+source_id+'|'+statement+'|'+locked).encode()).hexdigest()[:24]
 with c:c.execute('INSERT OR IGNORE INTO v4_experiments(id,branch,source_id,statement,created_at,method,feature_cutoff,locked_params_sha256) VALUES(?,?,?,?,?,?,?,?)',
   (eid,branch,source_id,statement,at,method,feature_cutoff,locked))
 return eid

def evaluate(preregistered,folds,min_n=300,require_paper=True):
 if not preregistered:return {'state':'REJECTED','reason':'NOT_PREREGISTERED'}
 if not folds:return {'state':'REJECTED','reason':'NO_HOLDOUT_OR_FORWARD_EVIDENCE'}
 if any(not f.get('leakage_free',False) for f in folds):return {'state':'REJECTED','reason':'TIME_LEAKAGE_OR_UNKNOWN'}
 if len({f['split_id'] for f in folds})!=len(folds):return {'state':'REJECTED','reason':'REPEATED_TEST_SPLITS'}
 if require_paper and not any(f.get('paper') for f in folds):return {'state':'RESEARCH_ONLY','reason':'NO_FORWARD_PAPER'}
 n=sum(int(f['n_independent']) for f in folds if int(f['n_independent'])>=0)
 if n<min_n:return {'state':'RESEARCH_ONLY','reason':'INSUFFICIENT_INDEPENDENT_OUTCOMES','n':n}
 results=[];bench=[]
 for f in folds:
  numbers=f.get('net_excess_benchmark')
  if not isinstance(numbers,list) or len(numbers)!=int(f['n_independent']):return {'state':'REJECTED','reason':'SAMPLE_MISMATCH'}
  if any(not isinstance(x,(int,float)) or not math.isfinite(x) for x in numbers):return {'state':'REJECTED','reason':'NONFINITE_RETURN'}
  results.extend(numbers)
 if len(results)!=n:return {'state':'REJECTED','reason':'MISSING_TRADES'}
 avg=statistics.mean(results);se=statistics.stdev(results)/math.sqrt(n) if n>1 else float('inf')
 # Simplified normal lower bound is intentionally conservative gate; not a proof
 lower=avg-1.96*se
 if avg<=0 or lower<=0:return {'state':'REJECTED','reason':'NO_STATISTICAL_POSITIVE_OOS_MARGIN','n':n,'lower_bound':lower}
 return {'state':'CHALLENGER_PAPER_ELIGIBLE','reason':'FURTHER_FORWARD_MONITORING_ONLY','n':n,'lower_bound':lower}
