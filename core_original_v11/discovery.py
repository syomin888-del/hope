#!/usr/bin/env python3
"""Autonomous, bounded hypothesis lab. Research hypotheses, NOT trading signals.

All hypotheses use pre-registered rules, point-in-time closed candles, contiguous
history, chronologically purged splits, multiple-test penalty and append-only
experiment history. Historical success never enables investing or betting.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import statistics

import collect
import research

# Precommitted tests: the next revision may expand the library only with a
# new revision identifier. No ex-post threshold tuning against test outcomes.
REVISION='2026-10-21-b'
TESTS={
 'momentum6':lambda c,v,i:c[i]/c[i-6]-1>0.008,
 'drop24':lambda c,v,i:c[i]/c[i-24]-1 < -0.025,
 'volume_spike':lambda c,v,i:v[i]>1.8*statistics.fmean(v[i-24:i]),
 'quiet6':lambda c,v,i:max(c[i-6:i+1])/min(c[i-6:i+1])-1<.013,
 'strong24':lambda c,v,i:c[i]/c[i-24]-1>.035,
}
# A constrained, deterministic hypothesis grammar automatically composes
# pairs of observable conditions. All combinations are registered *before*
# looking at results and counted in the multiple-testing correction.
CONJUNCTIONS=(('momentum6','volume_spike'),('drop24','volume_spike'),
              ('quiet6','volume_spike'),('strong24','momentum6'),
              ('drop24','quiet6'),('momentum6','quiet6'))
for left,right in CONJUNCTIONS:
    rule_left,rule_right=TESTS[left],TESTS[right]
    TESTS[f'{left}__AND__{right}']=(lambda c,v,i,a=rule_left,b=rule_right:a(c,v,i) and b(c,v,i))
HORIZONS=((6,.01),(24,.025))
MIN_SAMPLES=230
MIN_EXPOSED_TEST=18

def setup(c):
    c.executescript('''
      CREATE TABLE IF NOT EXISTS hypothesis_registry(
       key TEXT PRIMARY KEY, revision TEXT NOT NULL, symbol TEXT NOT NULL,
       horizon INTEGER NOT NULL, threshold REAL NOT NULL, hypothesis TEXT NOT NULL,
       declared_at TEXT NOT NULL, declaration_sha TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS hypothesis_experiments(
       id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,
       run_at TEXT NOT NULL, first_ms INTEGER, last_ms INTEGER,
       sample_sha TEXT NOT NULL, status TEXT NOT NULL, results_json TEXT NOT NULL,
       FOREIGN KEY(key) REFERENCES hypothesis_registry(key),UNIQUE(key,sample_sha));
      CREATE INDEX IF NOT EXISTS lab_history ON hypothesis_experiments(key,run_at);
    ''')
    c.commit()

def register(c,symbol,h,threshold,test):
    setup(c)
    declaration={'symbol':symbol,'hours':h,'target_return':threshold,'rule':test,'revision':REVISION,
       'no_tuning':'Fixed rules before dataset evaluation'}
    sha=hashlib.sha256(json.dumps(declaration,sort_keys=True).encode()).hexdigest()
    key=f'{symbol}:{h}:{test}:{REVISION}'
    with c:
        c.execute('INSERT OR IGNORE INTO hypothesis_registry VALUES(?,?,?,?,?,?,?,?)',
            (key,REVISION,symbol,h,threshold,test,collect.stamp(),sha))
    return key

def samples(rows,h,threshold):
    # At decision i, the last observed candle is complete. No data beyond i
    # contributes to features. Outcomes are close at i+h (future).
    if len(rows)<MIN_SAMPLES*h+100:return []
    prices=[float(r[4]) for r in rows];vols=[float(r[5]) for r in rows]
    samples=[]
    for i in range(48,len(rows)-h,h):
        if rows[i][0]-rows[i-48][0]!=48*3600_000:continue
        if rows[i+h][0]-rows[i][0]!=h*3600_000:continue
        if not all(x>0 and math.isfinite(x) for x in prices[i-24:i+h+1]):continue
        try:
            signals={k:bool(rule(prices,vols,i)) for k,rule in TESTS.items()}
        except (ValueError,ZeroDivisionError,OverflowError):continue
        samples.append((int(rows[i][0]),int(prices[i+h]/prices[i]>=1+threshold),signals))
    return samples

def _wilson(successes,n,z=2.576):
    """Wilson interval avoids zero-variance certainty for rare outcomes."""
    if n<=0:return (0.0,1.0)
    p=successes/n
    den=1+z*z/n
    center=(p+z*z/(2*n))/den
    radius=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return max(0.,center-radius),min(1.,center+radius)

def score(events,base_events):
    # Conservative exploratory comparison, not a causal estimate. Samples are
    # market-dependent; these p-values are approximate and paper-only.
    a=len(events);b=len(base_events)
    if min(a,b)<MIN_EXPOSED_TEST:return {'pass':False,'why':'low_event_coverage','exposed':a,'control':b}
    p1=sum(events)/a;p0=sum(base_events)/b
    pooled=(sum(events)+sum(base_events))/(a+b)
    se=math.sqrt(max(1e-12,pooled*(1-pooled)*(1/a+1/b)))
    z=(p1-p0)/se
    pval=.5*math.erfc(z/math.sqrt(2))
    adjusted=min(1.,pval*len(TESTS)*len(HORIZONS))
    low1,_=_wilson(sum(events),a)
    _,upper0=_wilson(sum(base_events),b)
    conservative_delta=low1-upper0
    return {'pass':bool(p1>p0 and adjusted<.01 and conservative_delta>0),
       'exposed':a,'control':b,'event_rate_exposed':round(p1,5),
       'event_rate_control':round(p0,5),'uplift':round(p1-p0,5),
       'lower_delta':round(conservative_delta,5),'p_adjusted':round(adjusted,7),
       'warning':'Approximate correlated-market test, research only; not a probability calibration or expected trading return.'}

def assess(c,symbol,h,threshold,test,records):
    key=register(c,symbol,h,threshold,test)
    if len(records)<MIN_SAMPLES:
        return {'key':key,'status':'WAIT_DATA','samples':len(records)}
    n=len(records);cut1=int(n*.65);cut2=int(n*.82)
    # Purge gap at both boundaries to avoid crossing decision/outcome points.
    # Non-overlapping h-hour sample indices plus 1-sample boundary embargo.
    train=records[:cut1-1];validation=records[cut1+1:cut2-1];hold=records[cut2+1:]
    def parts(chunk):
        return ([outcome for _,outcome,x in chunk if x[test]],
                [outcome for _,outcome,x in chunk if not x[test]])
    preliminary=score(*parts(train))
    val=score(*parts(validation))
    final=score(*parts(hold))
    status='REJECTED'
    if preliminary['pass'] and val['pass'] and final['pass']:
        status='RESEARCH_CANDIDATE_PAPER_ONLY'
    # An overlapping future re-run is NOT another independent holdout.
    past=c.execute('SELECT COUNT(*) FROM hypothesis_experiments WHERE key=?',(key,)).fetchone()[0]
    if past and status=='RESEARCH_CANDIDATE_PAPER_ONLY':status='RESEARCH_CANDIDATE_RETESTED'
    digest=hashlib.sha256(json.dumps([(ts,y,int(x[test])) for ts,y,x in records],separators=(',',':')).encode()).hexdigest()
    report={'n':n,'first_ms':records[0][0],'last_ms':records[-1][0],
        'train':preliminary,'validation':val,'holdout':final,
        'holdout_reused_before':bool(past),'revision':REVISION,
        'caution':'Multiple correlated experiments & backtests do not prove future predictive edge.'}
    with c:
        c.execute('''INSERT OR IGNORE INTO hypothesis_experiments
            (key,run_at,first_ms,last_ms,sample_sha,status,results_json)
            VALUES(?,?,?,?,?,?,?)''',(key,collect.stamp(),records[0][0],records[-1][0],digest,status,
             json.dumps(report,ensure_ascii=False)))
    return {'key':key,'status':status,'samples':n,'holdout':final,'retested':bool(past)}

def run(c,symbols=None):
    setup(c)
    result=[]
    for symbol in symbols or research.SYMBOLS:
        rows=research.get_candles(c,symbol)
        for h,threshold in HORIZONS:
            records=samples(rows,h,threshold)
            for rule in TESTS:
                result.append(assess(c,symbol,h,threshold,rule,records))
    return {'experiments_checked':len(result), 'potential_research_only':sum(x['status'].startswith('RESEARCH_CANDIDATE') for x in result),
      'wait_data':sum(x['status']=='WAIT_DATA' for x in result),'examples':result[:8]}

def status(c):
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'hypothesis_registry' not in tables:
        return {'registered_hypotheses':0,'experiments':0,'candidates':0,
                'autonomous_trading':False,'certified_profitable_models':0,'initialized':False}
    return {'registered_hypotheses':c.execute('SELECT COUNT(*) FROM hypothesis_registry').fetchone()[0],
        'experiments':c.execute('SELECT COUNT(*) FROM hypothesis_experiments').fetchone()[0],
        'candidates':c.execute("SELECT COUNT(*) FROM hypothesis_experiments WHERE status LIKE 'RESEARCH_CANDIDATE%'").fetchone()[0],
        'autonomous_trading':False,'certified_profitable_models':0}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('command',choices=['run','status']);a=ap.parse_args()
    c=research.open_db()
    try:print(json.dumps(run(c) if a.command=='run' else status(c),ensure_ascii=False,indent=2))
    finally:c.close()
if __name__=='__main__':main()
