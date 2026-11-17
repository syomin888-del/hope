#!/usr/bin/env python3
"""ORACLE audit of data freshness, completeness and contradictory market observations.

A clean audit is not evidence of a profitable model. It only establishes input health.
"""
import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path
import collect
import sources

UTC=dt.timezone.utc

def audit(conn,now=None):
    now=now or dt.datetime.now(UTC)
    reports=[]
    for job,(interval,_) in sources.SCHEDULE.items():
        row=conn.execute('SELECT run_at,state,new_rows,message FROM source_runs WHERE job=? ORDER BY id DESC LIMIT 1',(job,)).fetchone()
        if not row:
            status='NEVER_RUN';last=None;lag=None
        else:
            last=dt.datetime.fromisoformat(row[0]);lag=round((now-last).total_seconds()/3600,2)
            if row[1]=='skip':status='NO_KEY'
            elif row[1]=='error':status='ERROR'
            elif lag*3600>max(2*interval,7200):status='STALE'
            else:status='OK'
        reports.append({'job':job,'state':status,'last_run':last.isoformat() if last else None,'age_hours':lag})
    candles=[]
    for provider,symbol,tf,count,mn,mx in conn.execute('''SELECT provider,symbol,timeframe,COUNT(*),MIN(open_ms),MAX(open_ms)
        FROM candles GROUP BY provider,symbol,timeframe ORDER BY provider,symbol'''):
        span_hours=(mx-mn)/3600000 if count>1 else 0
        expected_hours={'1h':1,'1d':24}.get(tf)
        if expected_hours and span_hours>0:
            missing=max(0,round(span_hours/expected_hours)+1-count)
        else:missing=None
        candles.append({'provider':provider,'symbol':symbol,'timeframe':tf,
          'count':count,'first_ms':mn,'last_ms':mx,'estimated_missing_bars':missing})
    # Compare only aligned completed hourly closes from 2 different exchanges.
    divergence=[]
    rows=conn.execute("""SELECT symbol,open_ms,provider,close FROM candles
        WHERE timeframe='1h' AND provider IN ('binance_public','kraken_public') ORDER BY open_ms DESC LIMIT 3000""").fetchall()
    grouped={}
    for sym,ts,provider,close in rows:
        canonical='BTC' if sym.startswith('BTC') else 'ETH' if sym.startswith('ETH') else 'SOL' if sym.startswith('SOL') else None
        if canonical:grouped.setdefault((canonical,ts),{})[provider]=close
    for (sym,ts),both in grouped.items():
        if len(both)<2:continue
        v=list(both.values());ratio=abs(v[0]-v[1])/max(min(v),1e-9)
        if ratio>.008:divergence.append({'symbol':sym,'open_ms':ts,'gap_pct':round(ratio*100,3)})
    return {'generated_at':now.isoformat(),'sources':reports,'candles':candles,
        'price_disagreements_above_0_8pct':divergence[:50],
        'prediction_authorized':False,
        'note':'Input quality only. No certified statistical forecasting model is installed.'}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--json',action='store_true')
    args=ap.parse_args()
    cn=collect.db_open();sources.create_tables(cn)
    x=audit(cn)
    if args.json: print(json.dumps(x,ensure_ascii=False,indent=2))
    else:
        print('ORACLE INPUT QUALITY — THIS IS NOT A SIGNAL')
        for r in x['sources']:
            print(f"{r['job']:<23} {r['state']:<10} age-hours={r['age_hours']}")
        for c in x['candles']:
            print(f"{c['provider']:<20} {c['symbol']:<9} {c['timeframe']}: {c['count']} bars; missing est: {c['estimated_missing_bars']}")
        print('Market disagreements >0.8%:',len(x['price_disagreements_above_0_8pct']))
        print('Prediction authorized: NO. Models not independently validated.')
    cn.close()

if __name__=='__main__':main()
