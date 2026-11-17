#!/usr/bin/env python3
"""Oracle V7: traceable integration health and unified sports/crypto evaluation.
Read-only dashboard path. API credentials are NEVER returned. No real orders.
"""
from __future__ import annotations
import datetime as dt, json, os, sqlite3, urllib.request, urllib.error
from pathlib import Path
from oracle_v7_crypto import last_failure, recent_hypotheses

UTC=dt.timezone.utc
AI=(('openai','OpenAI API','OPENAI_API_KEY','https://api.openai.com/v1/models'),
    ('anthropic','Claude API','ANTHROPIC_API_KEY','https://api.anthropic.com/v1/models'),
    ('gemini','Gemini API','GEMINI_API_KEY','https://generativelanguage.googleapis.com/v1beta/models'))

def tables(c):return {x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

def init(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS v7_connection_checks(
    provider TEXT PRIMARY KEY,checked_at TEXT NOT NULL,state TEXT NOT NULL,
    http_status INTEGER,details TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v7_audit_runs(
    run_at TEXT PRIMARY KEY,kind TEXT NOT NULL,report_json TEXT NOT NULL);
    ''');c.commit()

def timestamp():return dt.datetime.now(UTC).isoformat(timespec='seconds')

def _age(s):
    try:return max(0,(dt.datetime.now(UTC)-dt.datetime.fromisoformat(s.replace('Z','+00:00'))).total_seconds())
    except (ValueError,TypeError,AttributeError):return None

def _one(c,sql,args=(),default=None):
    try:
        v=c.execute(sql,args).fetchone();return v[0] if v else default
    except sqlite3.Error:return default

def _safe_db_counts(c):
    t=tables(c)
    f=lambda tab,q: _one(c,q) if tab in t else None
    return {'fonbet_events':f('v5_catalog','SELECT COUNT(*) FROM v5_catalog'),
      'fonbet_active_quotes':f('fb_full_current','SELECT COUNT(*) FROM fb_full_current WHERE active=1'),
      'sport_forecasts':f('v5_forecasts','SELECT COUNT(*) FROM v5_forecasts'),
      'sport_research_events':f('v58_coverage','SELECT COUNT(*) FROM v58_coverage'),
      'historical_final_results':f('v58_results','SELECT COUNT(*) FROM v58_results'),
      'validated_model_rows':f('v58_validation','SELECT COUNT(*) FROM v58_validation WHERE passed=1'),
      'paper_experiments':f('v55_paper','SELECT COUNT(*) FROM v55_paper'),
      'portfolio_signals':f('v56_portfolio','SELECT COUNT(*) FROM v56_portfolio'),
      'crypto_quotes':f('mw_quotes','SELECT COUNT(*) FROM mw_quotes'),
      'crypto_hourly_bars':f('candles',"SELECT COUNT(*) FROM candles WHERE timeframe='1h'"),
      'public_research_articles':f('v54_public_evidence','SELECT COUNT(*) FROM v54_public_evidence'),
    }

def _conn(c,provider,env=None):
    conf=next((x for x in AI if x[0]==provider),None)
    if not conf:raise ValueError('UNKNOWN_PROVIDER')
    key=os.environ.get(conf[2],'') if env is None else env.get(conf[2],'')
    if not key:return {'id':provider,'name':conf[1],'state':'NOT_CONFIGURED','checked_at':None,
                      'detail':'Server-side key not configured; no connection established'}
    checked=None
    if 'v7_connection_checks' in tables(c):
        checked=c.execute('SELECT checked_at,state,http_status,details FROM v7_connection_checks WHERE provider=?',(provider,)).fetchone()
    if checked and _age(checked[0]) is not None and _age(checked[0])<48*3600:
        return {'id':provider,'name':conf[1],'state':checked[1], 'checked_at':checked[0],
                'detail':checked[3]}
    return {'id':provider,'name':conf[1],'state':'CONFIGURED_UNVERIFIED','checked_at':None,
            'detail':'Key present; run python oracle_v7.py probe to verify credentials'}

def probe(c,provider=None,transport=None):
    """Explicit read-only models-list test. No secret is logged or returned."""
    init(c);out={}
    for pid,name,env,url in AI:
        if provider and provider!=pid:continue
        key=os.environ.get(env)
        if not key:
            out[pid]={'state':'NOT_CONFIGURED','detail':'Missing server environment key'};continue
        headers={'Accept':'application/json','User-Agent':'ORACLE-Research/7.0'}
        if pid=='openai':headers['Authorization']='Bearer '+key
        elif pid=='anthropic':headers.update({'x-api-key':key,'anthropic-version':'2023-06-01'})
        elif pid=='gemini':headers['x-goog-api-key']=key
        req=urllib.request.Request(url,headers=headers,method='GET')
        code=None
        try:
            if transport:
                status=transport(req)
                code=int(status)
            else:
                with urllib.request.urlopen(req,timeout=12) as response:
                    code=response.status;response.read(1024)
            state='WORKING' if code==200 else 'HTTP_ERROR';details='Credential tested; read-only model listing'
        except urllib.error.HTTPError as e:
            code=e.code;state='AUTH_FAILED' if code in (401,403) else 'RATE_LIMITED' if code==429 else 'HTTP_ERROR'
            details='Model listing returned HTTP '+str(code)
        except Exception as e:
            state='NETWORK_ERROR';details=type(e).__name__+' (redacted)'
        at=timestamp()
        with c:c.execute('''INSERT INTO v7_connection_checks VALUES(?,?,?,?,?)
          ON CONFLICT(provider) DO UPDATE SET checked_at=excluded.checked_at,state=excluded.state,
          http_status=excluded.http_status,details=excluded.details''',(pid,at,state,code,details))
        out[pid]={'state':state,'http_status':code,'checked_at':at,'detail':details}
    return out

def crypto_market(c):
    if 'mw_quotes' not in tables(c):return {'state':'NOT_CONFIGURED','last_observation':None}
    latest=_one(c,'SELECT MAX(observed_at) FROM mw_quotes')
    age=_age(latest)
    return {'state':'WORKING' if age is not None and age<1200 else 'STALE' if latest else 'NO_OBSERVED_QUOTES', 'last_observation':latest,'age_seconds':round(age) if age is not None else None}

def sports(c):
    t=tables(c)
    if 'v5_catalog' not in t:return {'state':'NOT_INITIALIZED','detail':'Fonbet catalog absent'}
    counts=_safe_db_counts(c)
    source=_one(c,"SELECT MAX(imported_at) FROM fb_full_packets") if 'fb_full_packets' in t else None
    age=_age(source)
    healthy=bool(age is not None and age<20*60 and (counts['fonbet_active_quotes'] or 0)>0)
    return {'state':'WORKING' if healthy else 'STALE_OR_NO_QUOTED_MARKETS',
       'last_fonbet_packet_at':source,'packet_age_seconds':round(age) if age is not None else None,
       'counts':counts, 'brain_state':'VALIDATED_RESEARCH' if (counts['validated_model_rows'] or 0)>0 else 'AWAITING_MODEL_VALIDATION',
       'player_rosters':'NOT_VERIFIED'}

def portfolio(c):
    if 'v56_portfolio' not in tables(c):return {'state':'NOT_INITIALIZED','signals':0,'settled':0,'roi_pct':None}
    t=tables(c)
    # v56 selects forecast_id; v55 holds outcomes; compute only settled outcomes.
    rows=c.execute('''SELECT x.root_id,p.market,p.selection,p.status,p.profit,p.stake,
       p.forecast_id FROM v56_portfolio x JOIN v55_paper p ON p.forecast_id=x.forecast_id''').fetchall()
    conflicts=[];root_outcomes={}
    for root,market,selection,status,profit,stake,fid in rows:
        identity=(str(market),str(selection))
        if root in root_outcomes and root_outcomes[root]!=identity:conflicts.append(str(root))
        root_outcomes[root]=identity
    selected=[r for r in rows if r[3] in ('WIN','LOSS','PUSH')]
    pnl=sum(float(x[4] or 0) for x in selected);staking=sum(float(x[5] or 0) for x in selected)
    return {'state':'PAPER_ONLY','signals':len(rows),'settled':len(selected),
       'pending':len(rows)-len(selected),'net_usd':round(pnl,2),'roi_pct':round(100*pnl/staking,3) if staking else None,
       'stake_settled_usd':round(staking,2),'conflicting_portfolio_events':sorted(set(conflicts)),
       'unique_event_constraint_ok':len({r[0] for r in rows})==len(rows),
       'note':'Separate from all model experiments. No real orders.'}

def scorecard(c):
    p=portfolio(c)
    reason='AWAITING_VERIFIED_SETTLEMENT' if not p.get('settled') else 'SETTLED_PAPER_RESULTS_AVAILABLE'
    return {'official_sports':{'state':reason,'settled_bets':p.get('settled',0),'paper_roi_pct':p.get('roi_pct'),
        'net_usd':p.get('net_usd'),'pending':p.get('pending',0),'price_verified_in_model_ledger':True,
        'note':'Original legacy signal ledger cannot be merged with v56 paper results as actual bets.'},
        'crypto':last_failure(c)}

def dashboard(c):
    return {'state':'PAPER_RESEARCH_ONLY','version':'7.0','generated_at':timestamp(),
       'sports':sports(c),'portfolio':portfolio(c),
       'crypto':last_failure(c), 'crypto_hypotheses':recent_hypotheses(c),
       'integrations':{'sports':'WORKING' if sports(c)['state']=='WORKING' else 'PARTIAL',
         'crypto_market':crypto_market(c)['state'],
         'statistical_models':'RESEARCH_NOT_PROVEN',
         'ai':[ _conn(c,name) for name,*_ in AI]},
       'scorecard':scorecard(c),
       'safety':{'real_orders_placed_by_v7':0,'target_crypto_3pct_daily_proven':False,
                 'target_sport_win_rate_80pct_proven':False,
                 'no_two_opposing_official_selections_per_event':portfolio(c)['unique_event_constraint_ok']},
       'issues':['No live AI provider is claimed without a successful credential probe',
           'Only source-confirmed final outcomes count as settled',
           'All events can be classified; missing history means no invented probability']}
