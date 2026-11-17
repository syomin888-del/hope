#!/usr/bin/env python3
"""ORACLE v2.7: conservative prospective paper trading + predeclared historical test.
No live orders. No artificial 'profit' from 24h peaks. No fabricated bookmaker odds.
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, os, sqlite3, statistics, time
from pathlib import Path
import collect
UTC=dt.timezone.utc
FEE_BPS=max(0,min(500,int(os.getenv('ORACLE_PAPER_FEE_BPS','10'))))       # fee per side
SLIPPAGE_BPS=max(0,min(500,int(os.getenv('ORACLE_PAPER_SLIPPAGE_BPS','20'))))  # adverse movement per side, not measured
STAKE_USDT=10.0  # simulated fixed stake; $1000 virtual bankroll * 1%
GAP_LIMIT=90*60
SECONDS_H=3600

def as_utc(s):
    x=dt.datetime.fromisoformat(str(s).replace('Z','+00:00'))
    if x.tzinfo is None:raise ValueError('Missing timezone')
    return x.astimezone(UTC)
def iso(x):return x.astimezone(UTC).isoformat(timespec='seconds')
def now():return iso(dt.datetime.now(UTC))
def tabs(c):return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
def costs(entry_mid,exit_mid):
    if entry_mid<=0 or exit_mid<=0 or not all(map(math.isfinite,(entry_mid,exit_mid))):return None
    buy=entry_mid*(1+SLIPPAGE_BPS/10000)*(1+FEE_BPS/10000)
    sell=exit_mid*(1-SLIPPAGE_BPS/10000)*(1-FEE_BPS/10000)
    return {'entry_exec':buy,'exit_exec':sell,'gross_pct':(exit_mid/entry_mid-1)*100,'net_pct':(sell/buy-1)*100}

def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS pf_trades (
      source_id INTEGER PRIMARY KEY,symbol TEXT NOT NULL, signal_at TEXT NOT NULL,
      source_quote REAL NOT NULL, first_fill_at TEXT, first_fill_price REAL,
      due_at TEXT NOT NULL, status TEXT NOT NULL,
      exit_at TEXT, exit_price REAL, entry_exec REAL, exit_exec REAL,
      gross_pct REAL,net_pct REAL,btc_net_pct REAL,
      tp10_sl5_pct REAL,observed_peak_pct REAL,coverage_hours INTEGER,
      updated_at TEXT NOT NULL,reason TEXT);
    CREATE TABLE IF NOT EXISTS pf_runs (id INTEGER PRIMARY KEY,at TEXT NOT NULL,
      module TEXT NOT NULL,result_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pf_backtests (
      run_at TEXT PRIMARY KEY,method TEXT NOT NULL,training_json TEXT NOT NULL,
      test_json TEXT NOT NULL,warning TEXT NOT NULL);
    ''');c.commit()

def quotes(c,sym,a,b):
    return [(as_utc(t),float(p)) for t,p in c.execute('''SELECT observed_at,price
     FROM mw_quotes WHERE symbol=? AND observed_at>? AND observed_at<=? AND price>0
     ORDER BY observed_at ASC''',(sym,iso(a),iso(b))).fetchall()]

def nearest_prior(c,symbol,at):
    r=c.execute('''SELECT observed_at,price FROM mw_quotes WHERE symbol=? AND observed_at<=?
     ORDER BY observed_at DESC LIMIT 1''',(symbol,iso(at))).fetchone()
    return float(r[1]) if r and dt.timedelta(0)<=at-as_utc(r[0])<=dt.timedelta(minutes=20) else None

def btc_net(c,entry_at,exit_at):
    first=nearest_prior(c,'BTCUSDT',entry_at);last=nearest_prior(c,'BTCUSDT',exit_at)
    x=costs(first,last) if first and last else None
    return x['net_pct'] if x else None

def one_trade(c,rec,at):
    """No-fill if no timely next quote, no hindsight buying signal's stale quote.
    One fixed-horizon 24h hold, plus preregistered TP10/SL5 sensitivity.
    """
    sid,sym,start,recorded,deadline=rec
    start=as_utc(start);due=as_utc(deadline)
    if at<start+dt.timedelta(minutes=15):return None
    fill_quotes=quotes(c,sym,start,start+dt.timedelta(minutes=15))
    if not fill_quotes:
        return (sid,sym,iso(start),float(recorded),None,None,iso(due),'NO_FILL',None,None,
                None,None,None,None,None,None,None,0,iso(at),'NO_FRESH_EXECUTABLE_QUOTE')
    ft,fp=fill_quotes[0]
    if at<due:return (sid,sym,iso(start),float(recorded),iso(ft),fp,iso(due),'OPEN',
                None,None,None,None,None,None,None,None,None,0,iso(at),None)
    sample=quotes(c,sym,ft,due)
    before=quotes(c,sym,start,ft)
    sample=before+sample
    hours={int((t-start).total_seconds()//3600) for t,p in sample}
    max_gap=max([(sample[i+1][0]-sample[i][0]).total_seconds() for i in range(len(sample)-1)] or [999999])
    valid=(len(hours)>=16 and max_gap<=GAP_LIMIT and sample and
           (due-sample[-1][0])<=dt.timedelta(minutes=30))
    if not valid:
        return (sid,sym,iso(start),float(recorded),iso(ft),fp,iso(due),'UNVERIFIED_DATA',
                None,None,None,None,None,None,None,None,None,len(hours),iso(at),'MISSING_INTERVALS_OR_CLOSE')
    xt,xp=sample[-1]
    result=costs(fp,xp)
    peak=max(v for t,v in sample if t>=ft)
    tp_result=None
    for t,v in sample:
        if t<=ft:continue
        if v>=fp*1.10 or v<=fp*.95:
            tp_result=costs(fp,v)['net_pct'];break
    if tp_result is None:tp_result=result['net_pct']
    return (sid,sym,iso(start),float(recorded),iso(ft),fp,iso(due),'SETTLED',iso(xt),xp,
            result['entry_exec'],result['exit_exec'],result['gross_pct'],result['net_pct'],
            btc_net(c,ft,xt),tp_result,100*(peak/fp-1),len(hours),iso(at),None)

def update(c,at=None):
    at=as_utc(at or now());setup(c)
    if 'ub_paper' not in tabs(c) or 'mw_quotes' not in tabs(c):return {'state':'MISSING_SOURCE_TABLES'}
    # Only actual recorded prospective scenarios, never synthetic history as a 'live fill'.
    rows=c.execute('''SELECT id,symbol,created_at,entry_price,horizon_end FROM ub_paper
          WHERE id NOT IN (SELECT source_id FROM pf_trades)
             OR id IN (SELECT source_id FROM pf_trades WHERE status='OPEN')
             OR id IN (SELECT source_id FROM pf_trades WHERE status='UNVERIFIED_DATA' AND updated_at<?)
          ORDER BY created_at LIMIT 2000''',(iso(at-dt.timedelta(hours=12)),)).fetchall()
    totals={'processed':0,'open':0,'settled':0,'no_fill':0,'unverified':0}
    for rec in rows:
        value=one_trade(c,rec,at)
        if value is None:continue
        with c:c.execute('''INSERT INTO pf_trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(source_id) DO UPDATE SET
           first_fill_at=excluded.first_fill_at,first_fill_price=excluded.first_fill_price,
           status=excluded.status,exit_at=excluded.exit_at,exit_price=excluded.exit_price,
           entry_exec=excluded.entry_exec,exit_exec=excluded.exit_exec,
           gross_pct=excluded.gross_pct,net_pct=excluded.net_pct,
           btc_net_pct=excluded.btc_net_pct,tp10_sl5_pct=excluded.tp10_sl5_pct,
           observed_peak_pct=excluded.observed_peak_pct,coverage_hours=excluded.coverage_hours,
           updated_at=excluded.updated_at,reason=excluded.reason''',value)
        totals['processed']+=1
        totals[{'OPEN':'open','SETTLED':'settled','NO_FILL':'no_fill','UNVERIFIED_DATA':'unverified'}[value[7]]]+=1
    with c:c.execute('INSERT INTO pf_runs(at,module,result_json) VALUES(?,?,?)',
                     (iso(at),'prospective_ledger',json.dumps(totals)))
    return totals

def report(c):
    setup(c)
    t=tabs(c)
    records=c.execute('''SELECT symbol,signal_at,source_quote,first_fill_at,first_fill_price,
      due_at,status,exit_at,exit_price,gross_pct,net_pct,btc_net_pct,tp10_sl5_pct,
      observed_peak_pct,coverage_hours,reason FROM pf_trades ORDER BY signal_at DESC LIMIT 100''').fetchall()
    keys=['symbol','signal_at','signal_quote','fill_at','fill_price','due_at','status','exit_at','exit_price',
          'gross_pct','net_pct','btc_net_pct','tp10_sl5_pct','observed_peak_pct','coverage_hours','reason']
    settled=c.execute("SELECT net_pct,btc_net_pct,tp10_sl5_pct FROM pf_trades WHERE status='SETTLED' ORDER BY signal_at").fetchall()
    returns=[r[0] for r in settled];btc=[r[0]-r[1] for r in settled if r[1] is not None]
    agg={'paper_fills':c.execute("SELECT COUNT(*) FROM pf_trades WHERE first_fill_at IS NOT NULL").fetchone()[0],
         'pending':c.execute("SELECT COUNT(*) FROM pf_trades WHERE status='OPEN'").fetchone()[0],
         'settled':len(returns),
         'no_fill':c.execute("SELECT COUNT(*) FROM pf_trades WHERE status='NO_FILL'").fetchone()[0],
         'unverified':c.execute("SELECT COUNT(*) FROM pf_trades WHERE status='UNVERIFIED_DATA'").fetchone()[0],
         'avg_net_pct':statistics.mean(returns) if returns else None,
         'win_rate':sum(x>0 for x in returns)/len(returns) if returns else None,
         'net_usdt_fixed_10_stake':sum(STAKE_USDT*x/100 for x in returns) if returns else None,
         'avg_vs_btc_pct':statistics.mean(btc) if btc else None,
         'tp10_sl5_avg_pct':statistics.mean(x[2] for x in settled) if settled else None,
         'entry_fee_bps':FEE_BPS,'entry_slippage_bps':SLIPPAGE_BPS,
         'test_note':'One-way fees/slippage simulated and not measured. Equal $10 imaginary positions, not a real portfolio. No execution/orderbook simulation.'}
    agg['claimable_edge']='NOT_VERIFIED' if len(returns)<30 or agg['avg_net_pct'] is None else ('UNVERIFIED_POSITIVE_SAMPLE' if agg['avg_net_pct']>0 else 'NEGATIVE_PAPER_SAMPLE')
    sports={'odds_usable':False,'model_outcomes_settled':0,'independent_matches':0,
            'binary_brier':None,'money_profit':None,'reason':'No verified priced official sports bets.'}
    if 'sf_forecasts' in t:
        from oracle_scorecard import _group_summary, _official_sport
        sf=_group_summary(c)
        sp=_official_sport(c) if 'signals' in t else {}
        sports.update(model_outcomes_settled=sf['settled_rows'],
                      independent_matches=sf['unique_matches'],
                      binary_brier=sf['brier_1x2'],
                      researched_1x2_matches=sf['independent_1x2_matches'],
                      top_pick_win_rate=sf['top_pick_win_rate'],
                      money_profit=sp.get('profit_units'),
                      money_roi=sp.get('roi'),
                      priced_official_settled=sp.get('settled_bets',0),
                      odds_usable=sp.get('settled_bets',0)>0,
                      reason=sp.get('reason','NO_PRICED_SETTLED_BETS'))
    back=c.execute('SELECT run_at,method,training_json,test_json,warning FROM pf_backtests ORDER BY run_at DESC LIMIT 1').fetchone()
    return {'module':'ORACLE PROFIT AUDIT 2.7','mode':'PAPER_ONLY_NOT_A_TRADING_SYSTEM',
            'crypto':agg,'trades':[dict(zip(keys,r)) for r in records],
            'sports':sports,'backtest':({'at':back[0],'method':back[1], 'training':json.loads(back[2]),
                      'out_of_sample':json.loads(back[3]),'warning':back[4]} if back else None)}

# Fixed, preregistered hourly research rule. This is not a discovered trading edge.
def rsi(closes,n=14):
    changes=[closes[i]-closes[i-1] for i in range(len(closes)-n,len(closes))]
    up=sum(max(x,0) for x in changes)/n;down=sum(max(-x,0) for x in changes)/n
    return 100.0 if down==0 else 100-100/(1+up/down)
def ema(values,n):
    a=2/(n+1);out=values[0]
    for v in values[1:]:out=a*v+(1-a)*out
    return out

def history_for(c,sym,max_rows=12000):
    # Coin hourly history from existing Binance archive/watch. Must use completed candles.
    rows=c.execute('''SELECT open_ms,open,close,volume,provider FROM candles
      WHERE symbol=? AND timeframe='1h' AND provider IN ('binance_archive','binance_watch')
      ORDER BY open_ms DESC LIMIT ?''',(sym,max_rows*2)).fetchall()
    seen={}
    for ms,op,close,vol,src in rows:
        if ms not in seen or src=='binance_watch':seen[ms]=(ms,op,close,vol)
    return sorted(seen.values())[-max_rows:]

def signals_from_hourly(bars):
    sig=[];last_end=-1
    for i in range(59,len(bars)-25):
        if i<=last_end:continue
        # Requires continuous hourly history across feature and 24h outcome window.
        if bars[i+25][0]-bars[i-30][0] != 55*3600*1000:continue
        recent=[x[2] for x in bars[i-35:i+1]]
        if any(not math.isfinite(x) or x<=0 for x in recent):continue
        v6=sum(float(x[3]) for x in bars[i-5:i+1])/6
        v24=sum(float(x[3]) for x in bars[i-29:i-5])/24
        if not v24 or v6/v24<1.7:continue
        if not 42<=rsi(recent)<=64:continue
        if not ema(recent,12)>ema(recent,26):continue
        mom=100*(bars[i][2]/bars[i-24][2]-1)
        h=100*(bars[i][2]/bars[i-1][2]-1)
        if not -8<=mom<=3 or h>1.5 or h< -5:continue
        # Feature observation at close of i, entry at *next hour opening*, exit at 24h after entry.
        entry=float(bars[i+1][1]);exit_=float(bars[i+25][1])
        result=costs(entry,exit_)
        if result is None:continue
        sig.append({'signal_ms':bars[i][0]+3600000,'entry_ms':bars[i+1][0],
                    'entry':entry,'exit':exit_,'net_pct':result['net_pct'],
                    'gross_pct':result['gross_pct']})
        last_end=i+25
    return sig

def aggregate(items):
    r=[x['net_pct'] for x in items]
    paired=[x['net_pct']-x['btc_net_pct'] for x in items if x.get('btc_net_pct') is not None]
    return {'n':len(items),'mean_net_pct':round(statistics.mean(r),4) if r else None,
            'avg_excess_vs_btc_pct':round(statistics.mean(paired),4) if paired else None,
            'btc_aligned_trades':len(paired),
            'median_net_pct':round(statistics.median(r),4) if r else None,
            'positive_frac':round(sum(x>0 for x in r)/len(r),4) if r else None,
            'sum_fixed_10_usdt':round(sum(x*STAKE_USDT/100 for x in r),4) if r else None}

def backtest(c,at=None,max_symbols=120,max_rows=12000):
    """No parameter search. 70/30 time split with 24h embargo; 1h OHLCV data only."""
    at=as_utc(at or now());setup(c)
    if not {'mw_symbols','candles'}.issubset(tabs(c)):return {'state':'MISSING_SOURCE_TABLES'}
    # Universe exclusion must match live marketwatch classification, including pegged assets.
    from market_watch import STABLES, TOKENIZED_SECURITIES
    syms=[r[0] for r in c.execute('SELECT symbol FROM mw_symbols WHERE eligible=1 ORDER BY symbol').fetchall()
          if r[0].endswith('USDT') and r[0][:-4] not in STABLES | TOKENIZED_SECURITIES][:max_symbols]
    # Exclude bars not known closed at backtest run-time.
    cutoff=int((at-dt.timedelta(hours=1)).timestamp()*1000)
    alltrades=[];coverage=0
    for sym in syms:
        bars=[v for v in history_for(c,sym,max_rows) if v[0]<cutoff]
        if len(bars)<100:continue
        coverage+=1
        for x in signals_from_hourly(bars):x['symbol']=sym;alltrades.append(x)
    # Benchmark is evaluated only AFTER the hypothetical exit, never a feature.
    btc_history={r[0]:r[1] for r in history_for(c,'BTCUSDT',max_rows)}
    for row in alltrades:
        first=btc_history.get(row['entry_ms'])
        last=btc_history.get(row['entry_ms']+24*3600000)
        bench=costs(float(first),float(last)) if first and last else None
        row['btc_net_pct']=bench['net_pct'] if bench else None
    alltrades.sort(key=lambda x:x['signal_ms'])
    if len(alltrades)>=2:
        bound=alltrades[int(len(alltrades)*.7)]['signal_ms']
        train=[x for x in alltrades if x['signal_ms']<bound-24*3600*1000]
        test=[x for x in alltrades if x['signal_ms']>=bound]
    else:train=alltrades;test=[]
    a=aggregate(train);b=aggregate(test)
    a['symbols_with_100h_history']=coverage;a['symbols_requested']=len(syms)
    b['minimum_evidence_reached']=len(test)>=30
    b['claim']='NO_PROVEN_EDGE' if len(test)<30 or b['mean_net_pct'] is None or b['mean_net_pct']<=0 else 'POSITIVE_IN_SAMPLE_ONLY_REQUIRES_LIVE_FORWARD_TEST'
    warning='Only candles known by backtest time; model uses fixed unoptimized rule. Historical exchange survivors, correlated trades, simulated fills/fees. Paper results are not money earned.'
    with c:c.execute('INSERT INTO pf_backtests(run_at,method,training_json,test_json,warning) VALUES(?,?,?,?,?)',
                     (iso(at),'PREREGISTERED_HOURLY_VOL_RSI_EMA_24H',json.dumps(a),json.dumps(b),warning))
    return {'training':a,'out_of_sample':b,'warning':warning}

def run():
    c=collect.db_open();setup(c)
    print('ORACLE PROFIT LAB: paper ledger, no real orders, no unsupported odds',flush=True)
    while True:
        started=time.monotonic()
        try:
            r=update(c)
            if not c.execute('SELECT 1 FROM pf_backtests WHERE run_at>=? LIMIT 1',
              (iso(dt.datetime.now(UTC)-dt.timedelta(hours=24)),)).fetchone():
                r['backtest']=backtest(c)['out_of_sample']
            print(json.dumps(r),flush=True)
        except Exception as e:print('PROFIT LAB ERROR',type(e).__name__,str(e)[:250],flush=True)
        time.sleep(max(1,300-(time.monotonic()-started)))

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['run','once','status','backtest']);a=p.parse_args()
    if a.action=='run':run();return
    c=collect.db_open();setup(c)
    fn={'once':lambda:update(c),'status':lambda:report(c),'backtest':lambda:backtest(c)}[a.action]
    print(json.dumps(fn(),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
