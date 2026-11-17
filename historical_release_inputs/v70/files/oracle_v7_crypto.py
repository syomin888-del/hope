#!/usr/bin/env python3
"""ORACLE V7 crypto forensic research.  No orders or retroactive trades.

Chronological model selection on training+calibration; ONE untouched holdout.
Raw observed open/close/volume only; fees+slippage via oracle_profit.costs.
Survivorship and cross-asset dependence are explicitly unresolved.
"""
from __future__ import annotations
import datetime as dt, json, math, sqlite3, statistics
from collections import Counter, defaultdict

UTC=dt.timezone.utc
HOURLY=3600000
NAMES=('BASE_VOL_RSI_EMA','BTC_REGIME_TREND','BTC_DIP_REVERSION','BTC_BREAKOUT')

def tables(c):return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

def has(c,t):return t in tables(c)

def last_failure(c):
    if 'pf_backtests' not in tables(c):return {'state':'NO_HISTORICAL_BACKTEST'}
    row=c.execute('SELECT run_at,method,training_json,test_json,warning FROM pf_backtests ORDER BY run_at DESC LIMIT 1').fetchone()
    if not row:return {'state':'NOT_YET_TESTED'}
    a,b=json.loads(row[2]),json.loads(row[3]);score=b.get('mean_net_pct')
    reason=[]
    if score is None:reason.append('NO_OUT_OF_SAMPLE_NET_RETURN')
    elif score<=0:reason.append('NEGATIVE_OUT_OF_SAMPLE_AFTER_COSTS')
    if b.get('positive_frac') is not None and b['positive_frac']<.5:reason.append('MORE_LOSERS_THAN_WINNERS')
    if b.get('n',0)<100:reason.append('SMALL_HOLDOUT')
    return {'state':'BLOCKED_NO_PROVEN_EDGE' if reason else 'POSITIVE_SAMPLE_NOT_INDEPENDENTLY_PROVEN',
       'last_backtest_at':row[0],'method':row[1],'training':a,'out_of_sample':b,
       'blockers':reason,'notes':['Fixed 24h hold and one long-only entry rule is not adaptive.',
       'Exchange survivor/universe selection and cross-coin correlation can inflate results.',
       'Costs use modeled rather than measured slippage; order book unavailable.',
       'A hit rate or historical return cannot establish +3% consistently every day.']}

def _rsi(v,n=14):
    change=[v[i]-v[i-1] for i in range(-n,0)];pos=sum(max(0,x) for x in change)/n;neg=sum(max(0,-x) for x in change)/n
    return 100. if neg==0 else 100-100/(1+pos/neg)

def _ema(v,n):
    a=2/(n+1);x=v[0]
    for y in v[1:]:x=a*y+(1-a)*x
    return x

def _features(b,i):
    xs=[b[k][2] for k in range(i-39,i+1)]
    if min(xs)<=0:return None
    recent=[float(b[k][3]) for k in range(i-29,i+1)]
    prev=sum(recent[:24])/24
    if prev<=0:return None
    return {'rsi':_rsi(xs),'volume':sum(recent[-6:])/6/prev,
       'momentum':(xs[-1]/xs[-25]-1)*100,'h1':(xs[-1]/xs[-2]-1)*100,
       'ema_up':_ema(xs,12)>_ema(xs,26)}

def _rule(name,f,btc_up):
    if name=='BASE_VOL_RSI_EMA':return f['volume']>=1.7 and 42<=f['rsi']<=64 and f['ema_up'] and -8<=f['momentum']<=3 and -5<=f['h1']<=1.5
    if not btc_up:return False
    if name=='BTC_REGIME_TREND':return f['volume']>=1.3 and 45<=f['rsi']<=65 and f['ema_up'] and -1<=f['momentum']<=5
    if name=='BTC_DIP_REVERSION':return f['volume']>=1.1 and 23<=f['rsi']<=37 and -15<=f['momentum']<=-2
    if name=='BTC_BREAKOUT':return f['volume']>=1.5 and 52<=f['rsi']<=73 and f['ema_up'] and 2<=f['momentum']<=10
    return False

def _bars(c,symbol,max_rows):
    rows=c.execute('''SELECT open_ms,open,close,volume,provider FROM candles WHERE symbol=? AND timeframe='1h' AND provider IN ('binance_archive','binance_watch') ORDER BY open_ms DESC LIMIT ?''',(symbol,max_rows*2)).fetchall()
    uniq={}
    for t,o,cl,v,src in rows:
        if t not in uniq or src=='binance_watch':uniq[t]=(int(t),float(o),float(cl),float(v))
    return sorted(uniq.values())[-max_rows:]

def _cost(x,y):
    # Fixed explicitly modeled taker fee 10 bps and adverse slippage 20 bps per side.
    buy=x*1.002*1.001;sell=y*.998*.999
    return 100*(sell/buy-1)

def hypotheses(c,max_symbols=45,max_rows=2500,asof=None):
    """Research-only, no write; returns independent experiment samples."""
    t=tables(c)
    if not {'candles','mw_symbols'}.issubset(t):return {'state':'MISSING_CANDLE_ARCHIVE'}
    symbols=[x[0] for x in c.execute('SELECT symbol FROM mw_symbols WHERE eligible=1 AND symbol NOT IN (\'USDCUSDT\',\'FDUSDUSDT\') ORDER BY symbol LIMIT ?', (max_symbols,))]
    if 'BTCUSDT' not in symbols:symbols.append('BTCUSDT')
    raw={s:_bars(c,s,max_rows) for s in symbols}
    btc={x[0]:x[2] for x in raw.get('BTCUSDT',[])}
    cutoff=int((asof or dt.datetime.now(UTC)).timestamp()*1000)-HOURLY
    samples=defaultdict(list);counts=Counter()
    for sym,b in raw.items():
        if sym=='BTCUSDT' or len(b)<110:continue
        last={m:-1 for m in NAMES}
        for i in range(55,len(b)-26):
            if b[i+25][0]>=cutoff or b[i+25][0]-b[i-40][0]!=65*HOURLY:continue
            feat=_features(b,i)
            if feat is None:continue
            bt=btc.get(b[i][0]);bp=btc.get(b[i][0]-24*HOURLY)
            btc_up=bool(bt and bp and bt/bp>1.005)
            entry=b[i+1][1];exit_=b[i+25][1]
            if min(entry,exit_)<=0:continue
            for method in NAMES:
                if i<=last[method] or not _rule(method,feat,btc_up):continue
                samples[method].append((b[i+1][0],_cost(entry,exit_),sym))
                last[method]=i+25
                counts[method]+=1
    return {'state':'OK','samples':samples,'symbols_scanned':len(raw),'trades_found':dict(counts),
        'warning':'Historical archive survivor bias; simulated execution; correlated symbols'}

def _summary(rows):
    arr=[v for t,v,s in rows];n=len(arr)
    if not n:return {'n':0,'mean_net_pct':None,'positive_fraction':None,'net_10_usdt':0,'unique_days':0}
    return {'n':n,'mean_net_pct':round(statistics.mean(arr),5),
       'positive_fraction':round(sum(v>0 for v in arr)/n,5),
       'net_10_usdt':round(sum(v*.1 for v in arr),3),
       'unique_days':len({t//86400000 for t,v,s in rows}),
       'worst_trade_net_pct':round(min(arr),4)}

def run_hypothesis_test(c,max_symbols=45,max_rows=2500,asof=None):
    d=hypotheses(c,max_symbols,max_rows,asof)
    if d['state']!='OK':return d
    all_times=sorted(set(t for rows in d['samples'].values() for t,_,_ in rows))
    if len(all_times)<120:return {'state':'INSUFFICIENT_CHRONOLOGICAL_COVERAGE',
       'timepoints':len(all_times),'trades_found':d['trades_found']}
    split1=all_times[len(all_times)*6//10];split2=all_times[len(all_times)*8//10]
    result={};rank=[]
    for method in NAMES:
        arr=d['samples'].get(method,[])
        train=[x for x in arr if x[0]<split1-24*HOURLY]
        cal=[x for x in arr if split1<=x[0]<split2-24*HOURLY]
        test=[x for x in arr if x[0]>=split2]
        a,b,cval=map(_summary,(train,cal,test))
        result[method]={'train':a,'calibration':b,'holdout':cval}
        if a['n']>=30 and b['n']>=15 and a['mean_net_pct'] is not None and b['mean_net_pct'] is not None:
            rank.append((b['mean_net_pct'],method))
    chosen=max(rank)[1] if rank else None
    final=result[chosen]['holdout'] if chosen else None
    # Simulated *fixed $1000* bankroll: each trade uses only $10; no leverage.
    # Calendar days without any paper trades count as zero, not magically +3%.
    daily=None
    if chosen:
        trades=result.get(chosen)
        holdout=[x for x in d['samples'].get(chosen,[]) if x[0]>=split2]
        by_day=defaultdict(float)
        for epoch,ret,sym in holdout:by_day[epoch//86400000]+=.1*ret
        first=split2//86400000;last=(all_times[-1]//86400000)
        daily_vals=[by_day.get(day,0.)/1000*100 for day in range(first,last+1)]
        daily={'bankroll_usdt':1000,'fixed_per_trade_usdt':10,'days':len(daily_vals),
          'mean_net_daily_pct':round(statistics.mean(daily_vals),5) if daily_vals else None,
          'days_at_least_plus_3pct':sum(x>=3 for x in daily_vals),
          'days_negative':sum(x<0 for x in daily_vals),
          'min_day_pct':round(min(daily_vals),5) if daily_vals else None}
    approved=bool(chosen and final['n']>=100 and final['unique_days']>=30 and final['mean_net_pct']>0 and final['positive_fraction']>.5)
    return {'state':'RESEARCH_ONLY','selection_method':'CALIBRATION_ONLY','holdout_untouched_for_selection':True,
        'chosen_by_calibration':chosen,'approved_for_real_orders':False,'candidate_for_forward_paper':approved,
        'out_of_sample':final,'daily_fixed_1000_bankroll':daily,'all_hypotheses':result,'train_end_ms':split1,'test_start_ms':split2,
        'fee_bps_each_side':10,'slippage_bps_each_side':20,'target_3pct_daily_proven':False,
        'warning':'No independence across coins, survivor bias, no book-depth fills. Never treat holdout as live profit.'}

def scheduled(c,at=None,max_symbols=35,max_rows=2500):
    """At most once daily, autonomous hypothesis research; NO orders or signal issuance.
    Never optimize the selection on test-set outcomes. Always preserve losing trials.
    """
    at=at or dt.datetime.now(UTC)
    c.execute('''CREATE TABLE IF NOT EXISTS v7_crypto_runs(
        ran_at TEXT PRIMARY KEY,kind TEXT NOT NULL,report_json TEXT NOT NULL)''')
    latest=c.execute('SELECT ran_at FROM v7_crypto_runs ORDER BY ran_at DESC LIMIT 1').fetchone()
    if latest:
        try:
            if (at-dt.datetime.fromisoformat(latest[0])).total_seconds()<24*3600:
                return {'state':'OK','not_due':True,'last_research_at':latest[0]}
        except (TypeError,ValueError):pass
    try:
        result=run_hypothesis_test(c,max_symbols,max_rows,asof=at)
    except (sqlite3.Error,ValueError,OverflowError) as exc:
        result={'state':'ERROR','kind':type(exc).__name__,'message':str(exc)[:120]}
    with c:c.execute('INSERT INTO v7_crypto_runs VALUES(?,?,?)',(at.isoformat(timespec='seconds'),'model_hypothesis_test',json.dumps(result,ensure_ascii=False,default=str)[:40000]))
    return {'state':'OK' if result['state']!='ERROR' else 'ERROR','research_state':result['state'],
        'selected_hypothesis':result.get('chosen_by_calibration'),
        'prospective_signals_created':0,'actual_orders':0}

def recent_hypotheses(c):
    if 'v7_crypto_runs' not in tables(c):return {'state':'NOT_RUN'}
    r=c.execute('SELECT ran_at,report_json FROM v7_crypto_runs ORDER BY ran_at DESC LIMIT 1').fetchone()
    if not r:return {'state':'NOT_RUN'}
    payload=json.loads(r[1]);return {'ran_at':r[0],**payload}
