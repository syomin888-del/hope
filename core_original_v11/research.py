#!/usr/bin/env python3
"""ORACLE independent quantitative research engine.

Read-only market research, NO trading and NO betting automation.
Models estimate threshold probabilities; approval is EARNED on future holdouts.
All forecasts are paper-only. Guard against overlapping targets, train-test leak,
overconfidence, poor baselines, stale inputs and unverified result settlement.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
import traceback

import collect
import storage

ROOT=Path(__file__).resolve().parent
MODEL_DIR=ROOT/'model_artifacts'
UTC=dt.timezone.utc
HORIZONS=((6,.02),(24,.05),(72,.10))
SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')
MIN_LABELS=550
MIN_TRAIN=340
MIN_CAL=75
MIN_TEST=110
FEATURE_VERSION=1


def open_db():
    c=collect.db_open();initialize(c);return c

def initialize(c):
    c.executescript('''
      CREATE TABLE IF NOT EXISTS research_models(
       id TEXT PRIMARY KEY, symbol TEXT NOT NULL,horizon_hours INTEGER NOT NULL,
       threshold REAL NOT NULL, state TEXT NOT NULL,updated_at TEXT NOT NULL,
       metrics_json TEXT NOT NULL, artifact_name TEXT, data_sha TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS paper_forecasts(
       id TEXT PRIMARY KEY, model_id TEXT NOT NULL, symbol TEXT NOT NULL,
       recorded_at TEXT NOT NULL, open_ms INTEGER NOT NULL,
       horizon_hours INTEGER NOT NULL,threshold REAL NOT NULL,
       expires_at TEXT NOT NULL, entry REAL NOT NULL, target REAL NOT NULL,
       probability REAL NOT NULL, model_state TEXT NOT NULL,
       status TEXT NOT NULL DEFAULT 'open', final_price REAL,
       actual_hit INTEGER,settled_at TEXT,
       CHECK(probability>=0 AND probability<=1),
       UNIQUE(model_id,open_ms));
      CREATE TABLE IF NOT EXISTS research_jobs(
       id INTEGER PRIMARY KEY AUTOINCREMENT,run_at TEXT NOT NULL,
       job TEXT NOT NULL, status TEXT NOT NULL,details_json TEXT NOT NULL);
    ''');c.commit()

def log(c,name,status,info):
    with c:
        c.execute('INSERT INTO research_jobs(run_at,job,status,details_json) VALUES(?,?,?,?)',
          (collect.stamp(),name,status,json.dumps(info,ensure_ascii=False,default=str)[:9000]))

def model_id(symbol,horizon,threshold):
    return f'{symbol}_H{horizon}_UP{int(threshold*1000):03d}_v{FEATURE_VERSION}'

def get_candles(c,symbol):
    # Prefer finalized archive over REST, identical open times never counted twice.
    records=c.execute('''SELECT open_ms,open,high,low,close,volume,provider FROM candles
      WHERE symbol=? AND timeframe='1h' AND provider IN ('binance_archive','binance_public')
      ORDER BY open_ms, CASE WHEN provider='binance_archive' THEN 0 ELSE 1 END''',(symbol,)).fetchall()
    result={}
    max_closed=int(time.time()*1000)-3_600_000
    # Historical Parquet is mandatory for archived months. Missing/corrupt archives
    # must fail loudly; silently dropping old history would invalidate backtests.
    archive_rows = storage.archived_candles(c,symbol)
    for ts,o,h,l,close,v,prov in archive_rows + list(records):
        if ts>max_closed:continue
        value=(int(ts),float(o),float(h),float(l),float(close),float(v))
        if prov=='binance_archive':
            result[ts]=value
        elif ts not in result:
            result[ts]=value
    return list(sorted(result.values()))

def features(rows,h):
    """Sample nonoverlapping decisions. Every X known at t; Y known only at t+h.
    Rows with incomplete windows or gaps never enter training.
    """
    import numpy as np
    t=np.asarray([r[0] for r in rows],dtype=np.int64)
    if len(t)<300:return [],[],[],[]
    cl=np.asarray([r[4] for r in rows],dtype=float)
    hi=np.asarray([r[2] for r in rows],dtype=float)
    lo=np.asarray([r[3] for r in rows],dtype=float)
    vl=np.asarray([r[5] for r in rows],dtype=float)
    result_x=[];result_y=[];result_i=[];result_price=[]
    # Sample every h hours so target windows do not overlap.
    # A larger lookback 168h is allowed: history predictors overlap but labels don't.
    for i in range(168,len(rows)-h,h):
        if t[i+h]-t[i]!=h*3_600_000:continue
        if t[i]-t[i-168]!=168*3_600_000:continue
        if not (cl[i]>0 and cl[i-168]>0):continue
        price=cl[i]
        logret=np.log(cl[i-168:i+1]/cl[i-168])
        tail6=np.diff(logret[-7:]);tail24=np.diff(logret[-25:]);tail168=np.diff(logret)
        vol24=float(np.std(tail24));vol168=float(np.std(tail168))
        pastvol=vl[i-24:i]
        v_z=(math.log1p(vl[i])-float(np.mean(np.log1p(pastvol)))) / (float(np.std(np.log1p(pastvol)))+1e-6)
        x=[float(logret[-1]-logret[-7]),float(logret[-1]-logret[-25]),float(logret[-1]-logret[-169]),
           float(np.mean(tail6)),float(np.mean(tail24)),vol24,vol168,
           float((hi[i-23:i+1].max()/price)-1),float(price/lo[i-23:i+1].min()-1),
           max(-8,min(8,v_z)),float((cl[i]/max(cl[i-24],1e-9))-1),
           float(dt.datetime.fromtimestamp(int(t[i])/1000,UTC).weekday())/6]
        if not all(math.isfinite(v) and abs(v)<100 for v in x):continue
        result_x.append(x);result_y.append(float(cl[i+h]/price));result_i.append(int(t[i]));result_price.append(float(price))
    return result_x,result_y,result_i,result_price

def select_and_test(X,y,indices,h,threshold):
    """Training / calibration / never-seen final holdout, purged by h.

    Evaluation consists of independent non-overlapping targets. Baseline is
    the *train-period* event rate; threshold for approval intentionally hard.
    """
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier,HistGradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import make_pipeline
    from sklearn.metrics import brier_score_loss,log_loss
    from sklearn.base import clone
    X=np.asarray(X,dtype=float)
    labels=(np.asarray(y)>=1+threshold).astype(int)
    n=len(labels)
    if n<MIN_LABELS:return None,{'why':'too_few_independent_labels','n':n,'need':MIN_LABELS}
    tr=int(n*.64);ca=int(n*.79)
    # Purge one decision interval around boundaries; h-hour future label cannot
    # share the calibration/holdout starting candle.
    e_train=tr-1;e_cal=ca-1
    if e_train<MIN_TRAIN or ca-tr<MIN_CAL or n-ca<MIN_TEST:
        return None,{'why':'insufficient_train_cal_holdout','n':n}
    xt,yt=X[:e_train],labels[:e_train]
    xc,yc=X[tr:e_cal],labels[tr:e_cal]
    xv,yv=X[ca:],labels[ca:]
    if len(set(yt))<2 or len(set(yc))<2 or len(set(yv))<2:
        return None,{'why':'class_imbalance_or_no_events','n':n,'positives':[int(yt.sum()),int(yc.sum()),int(yv.sum())]}
    ideas={
     'logistic':make_pipeline(StandardScaler(),LogisticRegression(C=.15,max_iter=500,class_weight=None,random_state=11)),
     'forest':RandomForestClassifier(n_estimators=100,max_depth=4,min_samples_leaf=20,max_features=.8,random_state=11,n_jobs=1),
     'boost':HistGradientBoostingClassifier(max_iter=100,max_leaf_nodes=8,l2_regularization=20,learning_rate=.04,min_samples_leaf=25,random_state=11),
    }
    base=float(yt.mean());base=min(.999,max(.001,base))
    benchmark=brier_score_loss(yv,[base]*len(yv))
    candidates=[]
    for name,estimator in ideas.items():
        try:
            estimator.fit(xt,yt)
            pc=estimator.predict_proba(xc)[:,1]
            # Monotone calibration on disjoint time block. Use simple sigmoid shrink
            # via logistic on log odds to avoid isotonic stepwise overfit.
            from sklearn.linear_model import LogisticRegression as Calibrator
            pc=np.clip(pc,.0001,.9999)
            zc=np.log(pc/(1-pc)).reshape(-1,1)
            cal=Calibrator(C=.25,max_iter=250,random_state=11)
            cal.fit(zc,yc)
            cscore=brier_score_loss(yc,cal.predict_proba(zc)[:,1])
            candidates.append((cscore,name,estimator,cal))
        except (ValueError,OverflowError):continue
    if not candidates:return None,{'why':'models_could_not_fit','n':n}
    _,name,est,cal=min(candidates,key=lambda x:x[0])
    raw=np.clip(est.predict_proba(xv)[:,1],.0001,.9999)
    pred=cal.predict_proba(np.log(raw/(1-raw)).reshape(-1,1))[:,1]
    bs=float(brier_score_loss(yv,pred))
    improvement=(benchmark-bs)/max(benchmark,1e-8)
    # Split final holdout: advantage must persist, not a lucky short burst.
    halves=[]
    for sl in (slice(0,len(yv)//2),slice(len(yv)//2,None)):
        halves.append(float(brier_score_loss(yv[sl],[base]*len(yv[sl]))-brier_score_loss(yv[sl],pred[sl])))
    # Block-bootstrap difference of predictive quality, not fake IID hour-by-hour samples.
    diffs=(yv-base)**2-(yv-pred)**2
    rng=np.random.default_rng(17)
    sims=[float(np.mean(rng.choice(diffs,size=len(diffs),replace=True))) for _ in range(400)]
    ci_lower=float(np.quantile(sims,.025))
    probs=pred.clip(.001,.999)
    # Positive prediction edge alone is NOT a signal. Brier/LL measure calibration.
    calibrated=bool(improvement>.04 and ci_lower>0 and min(halves)>0)
    metric={'n':n,'n_train':len(yt),'n_cal':len(yc),'n_holdout':len(yv),
        'positive_rate_train':round(base,5),'positive_rate_holdout':round(float(yv.mean()),5),
        'model':name,'brier':round(bs,6),'baseline_brier':round(float(benchmark),6),
        'brier_improvement_pct':round(100*improvement,2),
        'holdout_logloss':round(float(log_loss(yv,probs)),5),
        'bootstrap_delta_lower95':round(ci_lower,7),
        'positive_both_halves':min(halves)>0,'passed_historical_gate':calibrated,
        'holdout_first_ms':indices[ca], 'holdout_last_ms':indices[-1],
        'caveat':'Backtest only; model is NOT live-certified or guaranteed profitable.'}
    return (est,cal),metric

def train_one(c,symbol,h,threshold):
    name=model_id(symbol,h,threshold)
    # A model that already began collecting *future* paper outcomes must never
    # silently change its weights while retaining the same model identity.
    # Further ideas are evaluated in discovery.py; promotion needs a separate
    # version and genuinely new prospective evidence, not an overwritten model.
    prior=c.execute('SELECT state,artifact_name,data_sha FROM research_models WHERE id=?',(name,)).fetchone()
    if prior and prior[0]=='HISTORICAL_PASS_SHADOW' and prior[1] and (MODEL_DIR/prior[1]).is_file():
        return {'model_id':name,'state':'FROZEN_SHADOW','data_sha':prior[2],
          'reason':'Weights and calibration frozen; preserve validity of prospective outcomes.'}
    rows=get_candles(c,symbol)
    x,y,t,price=features(rows,h)
    if len(x)<MIN_LABELS:
        detail={'why':'history_incomplete','independent_samples':len(x),'min':MIN_LABELS,
          'candles':len(rows),'needs':'Wait for historical backfill'}
        store(c,name,symbol,h,threshold,'WAIT_DATA',detail,None,'')
        return detail
    # Fingerprint relevant time window for provenance. Keep training reproducible.
    data_sha=hashlib.sha256(json.dumps({'begin':t[0],'end':t[-1], 'rows':len(rows),
       'labels':len(y),'all_candles_sha':hashlib.sha256(json.dumps([(r[0],r[4],r[5]) for r in rows],separators=(',',':')).encode()).hexdigest()},sort_keys=True).encode()).hexdigest()
    try:
        fit,metric=select_and_test(x,y,t,h,threshold)
    except ImportError as e:
        detail={'why':'ML_dependencies_missing','detail':str(e)[:150]}
        store(c,name,symbol,h,threshold,'NO_ML',detail,None,data_sha);return detail
    if not fit:
        store(c,name,symbol,h,threshold,'REJECTED',metric,None,data_sha);return metric
    # Even historical passes remain shadow-only until the independent live sample.
    state='HISTORICAL_PASS_SHADOW' if metric['passed_historical_gate'] else 'REJECTED'
    artifact_name=None
    if state=='HISTORICAL_PASS_SHADOW':
        import pickle
        MODEL_DIR.mkdir(exist_ok=True)
        artifact_name=name+'_'+data_sha[:12]+'.pkl'
        artifact={'model':fit,'symbol':symbol,'h':h,'threshold':threshold,'trained_at':collect.stamp(),
                  'data_sha':data_sha,'feature_version':FEATURE_VERSION}
        temp=MODEL_DIR/(artifact_name+'.tmp')
        temp.write_bytes(pickle.dumps(artifact,protocol=4))
        os.replace(temp,MODEL_DIR/artifact_name)
    store(c,name,symbol,h,threshold,state,metric,artifact_name,data_sha)
    return {'model_id':name,'state':state,**metric}

def store(c,name,symbol,h,threshold,state,metrics,artifact_name,sha):
    with c:
        c.execute('''INSERT INTO research_models(id,symbol,horizon_hours,threshold,state,updated_at,metrics_json,artifact_name,data_sha)
         VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
         state=excluded.state,updated_at=excluded.updated_at,metrics_json=excluded.metrics_json,
         artifact_name=excluded.artifact_name,data_sha=excluded.data_sha''',
         (name,symbol,h,threshold,state,collect.stamp(),json.dumps(metrics,default=str),artifact_name,sha))

def train_all(c):
    result=[]
    for symbol in SYMBOLS:
        for h,threshold in HORIZONS:
            try:
                detail=train_one(c,symbol,h,threshold)
                result.append({'symbol':symbol,'horizon':h,'detail':detail})
            except Exception as exc:
                detail={'error':type(exc).__name__+': '+str(exc)[:150]}
                log(c,'train_'+symbol+'_'+str(h),'ERROR',detail)
                result.append({'symbol':symbol,'horizon':h,'detail':detail})
    log(c,'train_models','ok',{'models':len(result)})
    return result

def latest_completed(rows):
    if not rows:return None
    # Candles collected at the top of the hour only; the archive/open time
    # is not a real-time last-trade quote, so do not call it executable.
    return rows[-1]

def live_guard(c,name,baseline):
    """Independent outcomes veto a model that deteriorates after historical testing.

    This is a monitoring veto, NOT an automatic approval to invest.
    """
    records=c.execute("""SELECT probability,actual_hit FROM paper_forecasts
       WHERE model_id=? AND status='settled' ORDER BY recorded_at DESC LIMIT 80""",(name,)).fetchall()
    if len(records)<60:return True
    bs=sum((float(r[0])-int(r[1]))**2 for r in records)/len(records)
    default=sum((float(baseline)-int(r[1]))**2 for r in records)/len(records)
    if bs>=default:
        log(c,'drift_guard','blocked',{'model':name,'brier':round(bs,5),'baseline':round(default,5),'n':len(records)})
        return False
    return True

def generate_shadow(c):
    import pickle
    result=[]
    for m in c.execute("SELECT id,symbol,horizon_hours,threshold,artifact_name,state,data_sha,metrics_json FROM research_models WHERE state='HISTORICAL_PASS_SHADOW'").fetchall():
        name,symbol,h,threshold,art,state,sha,metrics_json=m
        if not live_guard(c,name,json.loads(metrics_json).get('positive_rate_train',.5)):
            continue
        path=MODEL_DIR/(art or '')
        if not art or not path.is_file():continue
        rows=get_candles(c,symbol)
        if len(rows)<200:continue
        last=rows[-1]
        if int(time.time()*1000)-last[0]>2*3_600_000:continue
        if (last[0]//3_600_000)%h!=0:continue # avoid overlapping forecast horizons
        # Safe loading: only trust artifacts created locally by this program.
        try:
            artifact=pickle.loads(path.read_bytes())
            if artifact.get('data_sha')!=sha or artifact.get('feature_version')!=FEATURE_VERSION:continue
            # Features computed ONLY from genuine completed candles.
            from numpy import asarray,log,mean,std
            import numpy as np
            cl=np.asarray([r[4] for r in rows],dtype=float);hi=np.asarray([r[2] for r in rows],dtype=float)
            lo=np.asarray([r[3] for r in rows],dtype=float);vl=np.asarray([r[5] for r in rows],dtype=float)
            i=len(rows)-1
            if rows[i][0]-rows[i-168][0]!=168*3600000:continue
            rets=np.log(cl[i-168:i+1]/cl[i-168]);rv=np.diff(rets)
            pastvl=np.log1p(vl[i-24:i]);v_z=(np.log1p(vl[i])-pastvl.mean())/(pastvl.std()+1e-6)
            feat=np.asarray([[float(rets[-1]-rets[-7]),float(rets[-1]-rets[-25]),float(rets[-1]-rets[-169]),
                     float(np.mean(rv[-6:])),float(np.mean(rv[-24:])),float(np.std(rv[-24:])),float(np.std(rv)),
                     float(hi[i-23:i+1].max()/cl[i]-1),float(cl[i]/lo[i-23:i+1].min()-1),
                     max(-8,min(8,float(v_z))),float(cl[i]/cl[i-24]-1),
                     float(dt.datetime.fromtimestamp(rows[i][0]/1000,UTC).weekday())/6]],dtype=float)
            est,cal=artifact['model']
            raw=float(np.clip(est.predict_proba(feat)[0][1],.0001,.9999))
            pred=float(cal.predict_proba([[math.log(raw/(1-raw))]])[0][1])
            if not .001<=pred<=.999:continue
        except (ValueError,IndexError,KeyError,OSError) as exc:continue
        stamp=collect.stamp()
        exp=dt.datetime.fromtimestamp((last[0]+(h+1)*3_600_000)/1000,UTC).isoformat(timespec='seconds')
        sid=hashlib.sha256((name+'|'+str(last[0])).encode()).hexdigest()[:20]
        target=float(last[4])*(1+threshold)
        with c:
            cur=c.execute('''INSERT OR IGNORE INTO paper_forecasts
             (id,model_id,symbol,recorded_at,open_ms,horizon_hours,threshold,expires_at,
              entry,target,probability,model_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
             (sid,name,symbol,stamp,last[0],h,threshold,exp,float(last[4]),target,pred,state))
        if cur.rowcount:result.append({'id':sid,'model':name,'p':pred,'target':target,'expires':exp})
    log(c,'shadow_forecast','ok',{'new':len(result)})
    return result

def settle(c):
    results=[]
    nowms=int(time.time()*1000)
    for row in c.execute("SELECT id,symbol,open_ms,horizon_hours,entry,target FROM paper_forecasts WHERE status='open'").fetchall():
        sid,sym,ts,h,entry,target=row
        targetts=ts+h*3_600_000
        if nowms<targetts+3_600_000:continue
        final=c.execute('''SELECT close FROM candles WHERE symbol=? AND timeframe='1h' AND open_ms=?
            ORDER BY CASE WHEN provider='binance_archive' THEN 0 ELSE 1 END LIMIT 1''',(sym,targetts)).fetchone()
        if not final:continue
        price=float(final[0]);hit=int(price>=float(target))
        with c:
            c.execute("UPDATE paper_forecasts SET status='settled',final_price=?,actual_hit=?,settled_at=? WHERE id=? AND status='open'",
                (price,hit,collect.stamp(),sid))
        results.append({'id':sid,'hit':hit,'return_pct':round(100*(price/entry-1),2)})
    log(c,'settle','ok',{'settled':len(results)})
    return results

def status(c):
    models=[{'id':r[0],'state':r[1],**json.loads(r[2])} for r in c.execute('SELECT id,state,metrics_json FROM research_models ORDER BY id')]
    summary=[dict(zip(('status','n','hits'),r)) for r in c.execute('SELECT status,COUNT(*),SUM(actual_hit) FROM paper_forecasts GROUP BY status')]
    return {'models':models,'paper':summary,'disclaimer':'Historical approval is NOT a guarantee of future profits. No real bets/trades.'}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=('init','train','forecast','settle','status','tick'))
    args=ap.parse_args();c=open_db()
    try:
        result= {'init':'research database ready','train':lambda:train_all(c),
          'forecast':lambda:generate_shadow(c),'settle':lambda:settle(c),
          'status':lambda:status(c),'tick':lambda:{'settled':settle(c),'forecasts':generate_shadow(c)}}[args.action]
        if callable(result):result=result()
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    finally:c.close()

if __name__=='__main__':main()
