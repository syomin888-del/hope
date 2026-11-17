#!/usr/bin/env python3
"""ORACLE evidence scorecard. Read-only evaluation; never imputes bookmaker odds.

Sports probabilities are evaluated as one market per independent match, not as
thousands of independent (often complementary) selections. Economic P&L is only
computed for a preregistered bookmaker quote stored with an official signal.
"""
from __future__ import annotations
import datetime as dt
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

UTC = dt.timezone.utc

def _tables(c):
    return {x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

def _group_summary(c):
    stats = {'forecast_rows':0,'settled_rows':0,'unique_matches':0,
             'independent_1x2_matches':0,'brier_1x2':None,'logloss_1x2':None,
             'top_pick_win_rate':None,'top_pick_wins':0,'market_groups':0,
             'by_league':[], 'by_market':[], 'unsettled_rows':0,
             'data_quality_warnings':[]}
    stats['forecast_rows'] = c.execute('SELECT COUNT(*) FROM sf_forecasts').fetchone()[0]
    stats['unsettled_rows'] = c.execute("SELECT COUNT(*) FROM sf_forecasts WHERE status NOT IN ('PAPER_SETTLED')").fetchone()[0]
    stats['settled_rows'] = c.execute("SELECT COUNT(*) FROM sf_forecasts WHERE status='PAPER_SETTLED' AND hit IS NOT NULL").fetchone()[0]
    rows = c.execute("""SELECT match_id,asof_day,market,selection,probability,hit,league,created_at,match_date
        FROM sf_forecasts WHERE status='PAPER_SETTLED' AND hit IN (0,1) AND created_at < match_date||'T23:59:59'
        ORDER BY match_id,asof_day,market""").fetchall()
    # Only the earliest forecast per market/match is evaluated, avoiding re-forecasts.
    groups = defaultdict(list)
    for match,asof,market,selection,p,hit,league,created,date in rows:
        if str(created)[:10]>=str(date):
            continue
        groups[(match,asof,market)].append((selection,p,hit,league))
    first={}
    for (match,asof,market),arr in sorted(groups.items(),key=lambda x:x[0][1]):
        first.setdefault((match,market),arr)
    valid=[]
    for (match,market),arr in first.items():
        if len({a[0] for a in arr})!=len(arr):continue
        if market=='1X2' and set(a[0] for a in arr)!={'П1','П2','Ничья'}:continue
        if market!='1X2' and (market not in ('ТОТАЛ 2.5','ТОТАЛ 44.5','ОБЕ ЗАБЬЮТ') or len(arr)!=2):continue
        if any(p is None or not math.isfinite(p) or not 0<=p<=1 for _,p,_,_ in arr):continue
        if not .98<=sum(a[1] for a in arr)<=1.02 or sum(a[2] for a in arr)!=1:continue
        winner=next(a for a in arr if a[2]==1)
        best=max(arr,key=lambda a:a[1]);league=arr[0][3]
        brier=sum((p-hit)**2 for _,p,hit,_ in arr)/len(arr)
        valid.append({'match':match,'market':market,'league':league,'brier':brier,
                      'logloss':-math.log(max(1e-9,winner[1])),
                      'top_hit':int(best[2])})
    stats['market_groups']=len(valid)
    stats['unique_matches']=len({a['match'] for a in valid})
    x=[a for a in valid if a['market']=='1X2']
    stats['independent_1x2_matches']=len(x)
    if x:
        stats['brier_1x2']=round(statistics.mean(a['brier'] for a in x),6)
        stats['logloss_1x2']=round(statistics.mean(a['logloss'] for a in x),6)
        stats['top_pick_wins']=sum(a['top_hit'] for a in x)
        stats['top_pick_win_rate']=round(stats['top_pick_wins']/len(x),4)
    for field,label in [('league','by_league'),('market','by_market')]:
        bins=defaultdict(list)
        for entry in valid: bins[entry[field]].append(entry)
        stats[label]=[{'name':k,'n_market_groups':len(v),
                      'n_unique_matches':len({a['match'] for a in v}),
                      'mean_brier':round(statistics.mean(a['brier'] for a in v),6)}
                     for k,v in sorted(bins.items(),key=lambda kv:-len(kv[1]))]
    if stats['settled_rows'] and not valid:
        stats['data_quality_warnings'].append('NO_COMPLETE_INDEPENDENT_OUTCOME_MARKETS')
    if stats['forecast_rows'] and not stats['settled_rows']:
        stats['data_quality_warnings'].append('NO_SETTLED_PROSPECTIVE_SPORTS_FORECASTS')
    stats['economic_roi'] = None
    stats['note']='Exploratory probabilities; no bookmaker pricing is inferred from match scores.'
    return stats

def _official_sport(c):
    out={'settled_bets':0,'priced_bets':0,'profit_units':None,'roi':None,'win_rate':None,
         'winning_bets':0,'total_registered':0,'reason':'NO_SETTLED_PRICED_OFFICIAL_SPORTS_SIGNALS'}
    rows=c.execute("SELECT kind,status,issued_at,payload FROM signals WHERE kind='sport'").fetchall()
    out['total_registered']=len(rows)
    pnl=[];wins=0
    for _,status,issued,payload in rows:
        if status not in ('win','loss','push'):continue
        try:
            p=json.loads(payload)
            odds=float(p['odds'])
            stamp=dt.datetime.fromisoformat(issued.replace('Z','+00:00'))
            checked=dt.datetime.fromisoformat(p['odds_checked_at'].replace('Z','+00:00'))
            if stamp.tzinfo is None or checked.tzinfo is None:continue
            age=(stamp-checked).total_seconds()
            if not 0<=age<=300 or not 1.01<odds<=100:continue
            if not p.get('bookmaker'):continue
            pnl.append(odds-1 if status=='win' else -1 if status=='loss' else 0)
            wins+=status=='win'
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError):continue
    if pnl:
        out.update(settled_bets=len(pnl),priced_bets=len(pnl),profit_units=round(sum(pnl),4),
                   roi=round(statistics.mean(pnl),6),win_rate=round(wins/len(pnl),6),winning_bets=wins,
                   reason='PAPER_RESULT_FROM_RECORDED_QUOTES_NOT_REAL_MONEY')
    return out

def _crypto(c):
    out={'state':'NO_LEDGER','paper_trades':0,'open':0,'settled':0,'unverified':0,
         'no_fill':0,'net_profit_fixed_10_usdt':None,'mean_net_pct':None,
         'win_rate':None,'out_of_sample':None}
    for state,n in c.execute('SELECT status,COUNT(*) FROM pf_trades GROUP BY status'):
        out['paper_trades']+=n
        if state=='OPEN':out['open']+=n
        elif state=='SETTLED':out['settled']+=n
        elif state=='NO_FILL':out['no_fill']+=n
        elif state=='UNVERIFIED_DATA':out['unverified']+=n
    vals=[v for (v,) in c.execute("SELECT net_pct FROM pf_trades WHERE status='SETTLED' AND net_pct IS NOT NULL")]
    if vals:
        out.update(state='PAPER_ESTIMATES_ONLY',mean_net_pct=round(statistics.mean(vals),5),
                   win_rate=round(sum(x>0 for x in vals)/len(vals),5),
                   net_profit_fixed_10_usdt=round(sum(v*.1 for v in vals),5))
    if c.execute('SELECT 1 FROM pf_backtests LIMIT 1').fetchone():
        test=c.execute('SELECT test_json FROM pf_backtests ORDER BY run_at DESC LIMIT 1').fetchone()[0]
        try:out['out_of_sample']=json.loads(test)
        except (ValueError,TypeError):pass
    return out

def summary(path):
    p=Path(path)
    out={'module':'ORACLE_INTEGRATED_SCORECARD','generated_at':dt.datetime.now(UTC).isoformat(timespec='seconds'),
         'mode':'PAPER_RESEARCH_NO_LIVE_EXECUTION', 'database_path':str(p),
         'sports':{},'official_sports':{},'crypto':{},'source_coverage':{},'warnings':[]}
    if not p.exists():
        out['warnings'].append('DATABASE_NOT_FOUND');return out
    with sqlite3.connect('file:'+p.resolve().as_posix()+'?mode=ro',uri=True,timeout=15) as c:
        t=_tables(c)
        if 'sf_forecasts' in t:
            out['sports']=_group_summary(c)
            if 'v55_paper' in t and 'v56_portfolio' in t:
                out['sports']['data_quality_warnings']=[x for x in out['sports']['data_quality_warnings'] if x!='NO_SETTLED_PROSPECTIVE_SPORTS_FORECASTS']
                if not c.execute("SELECT 1 FROM v55_paper WHERE status IN ('WIN','LOSS','PUSH') LIMIT 1").fetchone():
                    out['sports']['data_quality_warnings'].append('AWAITING_SETTLEMENT_OF_VERIFIED_FORWARD_PAPER')
        else:out['warnings'].append('SPORTS_FORECASTS_TABLE_MISSING')
        if 'signals' in t:out['official_sports']=_official_sport(c)
        else:out['warnings'].append('OFFICIAL_SIGNAL_LEDGER_MISSING')
        if {'pf_trades','pf_backtests'}.issubset(t):out['crypto']=_crypto(c)
        else:out['warnings'].append('CRYPTO_PROFIT_LEDGER_MISSING')
        if 'sf_matches' in t:
            out['source_coverage']['sports_match_rows']=c.execute('SELECT COUNT(*) FROM sf_matches').fetchone()[0]
        if 'raw_snapshots' in t:
            out['source_coverage']['actual_bookmaker_snapshot_versions']=c.execute("SELECT COUNT(*) FROM raw_snapshots WHERE provider LIKE '%odds%'").fetchone()[0]
        if 'source_runs' in t:
            out['source_coverage']['odds_last_jobs']=[{'job':j,'at':a,'state':s} for j,a,s in c.execute("SELECT job,run_at,state FROM source_runs WHERE job LIKE '%odds%' ORDER BY id DESC LIMIT 5")]
    # Legacy `signals` ledger and new v56 research portfolio are separate.
    # A missing legacy official ledger is NOT an error in modern research portfolio.
    if 'v56_portfolio' in t and 'v55_paper' in t:
        import oracle_v7_core
        with sqlite3.connect('file:'+p.resolve().as_posix()+'?mode=ro',uri=True) as current:
            unified=oracle_v7_core.scorecard(current)
        out['modern_portfolio']=unified['official_sports']
        out['official_sports']['legacy_ledger_only']=True
        out['warnings'].append('AWAITING_VERIFIED_PORTFOLIO_SETTLEMENT' if not unified['official_sports']['settled_bets'] else 'LEGACY_LEDGER_SEPARATE_FROM_PORTFOLIO')
    elif out['official_sports'].get('roi') is None:
        out['warnings'].append('LEGACY_OFFICIAL_BET_LEDGER_HAS_NO_SETTLED_ODDS')
    if out['crypto'].get('mean_net_pct') is None:
        out['warnings'].append('NO_SETTLED_CRYPTO_PAPER_RETURNS')
    return out

if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument('--db',default='/app/state/oracle.sqlite3')
    args=ap.parse_args()
    print(json.dumps(summary(args.db),ensure_ascii=False,indent=2))
