#!/usr/bin/env python3
"""V5.5 research-only, auditable USDT/USD-10 paper experiments and confirmed outcomes.

Never creates a real wager, never retroactively places a bet or converts a live
quote into an invented historical one. Cached original selections are immutable.
"""
from __future__ import annotations
import datetime as dt, json, math, os, sqlite3
from collections import Counter, defaultdict

STAKE = 10.0
MIN_ODDS = float(os.getenv('ORACLE_V55_MIN_PAPER_ODDS','1.7'))
MIN_EV = float(os.getenv('ORACLE_V55_MIN_RAW_EV','0'))


def setup(c):
    c.executescript('''
      CREATE TABLE IF NOT EXISTS v55_paper (
        forecast_id TEXT PRIMARY KEY, root_id TEXT NOT NULL,
        original_event_id TEXT NOT NULL, origin TEXT NOT NULL,
        market TEXT NOT NULL, selection TEXT NOT NULL, probability REAL NOT NULL,
        odds REAL NOT NULL, stake REAL NOT NULL,
        quote_key TEXT NOT NULL, quote_at TEXT NOT NULL,
        kickoff_utc TEXT NOT NULL, registered_at TEXT NOT NULL,
        expected_ev_pct REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING', result_home INTEGER,
        result_away INTEGER, result_seen TEXT, settled_at TEXT,
        profit REAL, source TEXT, reason TEXT NOT NULL DEFAULT 'VERIFIED_FORWARD_PAPER');
      CREATE INDEX IF NOT EXISTS v55_paper_status ON v55_paper(status,kickoff_utc);
      CREATE TABLE IF NOT EXISTS v55_archive_prices (
        forecast_id TEXT PRIMARY KEY, root_id TEXT NOT NULL,
        quote_key TEXT NOT NULL, price REAL NOT NULL,
        observed_at TEXT NOT NULL, origin TEXT NOT NULL,
        recorded_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS v55_settlement_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, checked_at TEXT NOT NULL,
        checked INTEGER NOT NULL, settled INTEGER NOT NULL,
        outcomes_unavailable INTEGER NOT NULL, report TEXT NOT NULL);
    ''')
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='fb_full_history' AND type='table'").fetchone():
        # Avoid repeatedly scanning the entire all-time price history per fixture.
        c.execute('CREATE INDEX IF NOT EXISTS v55_fb_history_quote_at ON fb_full_history(quote_key,changed_at)')
    c.commit()


def _at(t=None):
    import oracle_v5 as v5
    return v5.stamp(t)


def _parse(t):
    import oracle_v5 as v5
    return v5.parse(t)


def prepare(c, now=None):
    """Create forward-only, timestamp-locked paper experiments; never backfill wagers."""
    setup(c); at=_at(now); added=0; eligible=0
    rows=c.execute('''SELECT id,root_id,original_event_id,origin,market,selection,
        probability,price,quote_key,quote_snapshot,kickoff_utc,created_at
        FROM v5_forecasts WHERE root_id IS NOT NULL
        AND price_status='PRICE_ATTACHED_TIME_VALID'
        AND kickoff_utc>? AND probability IS NOT NULL AND price IS NOT NULL
        AND EXISTS (SELECT 1 FROM v5_catalog e WHERE e.root_id=v5_forecasts.root_id
          AND e.root_exists=1 AND e.active_quotes>0)
        ''',(at,)).fetchall()
    import oracle_v5 as v5
    with c:
        for fid,root,eid,origin,market,selection,p,odds,qkey,qat,kick,created in rows:
            try:
                if v5.canonical_selection(market,selection) is None:continue
                p=float(p);odds=float(odds)
                if not (math.isfinite(p) and 0<p<1 and math.isfinite(odds) and odds>=MIN_ODDS):continue
                if not (created<=qat<kick and qat<=at<kick):continue
                ev=(p*odds-1)*100
                if ev<MIN_EV:continue
                eligible+=1
                r=c.execute('''INSERT OR IGNORE INTO v55_paper(
                    forecast_id,root_id,original_event_id,origin,market,selection,
                    probability,odds,stake,quote_key,quote_at,kickoff_utc,registered_at,
                    expected_ev_pct,status,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING',?)''',
                    (fid,root,eid,origin,market,selection,p,odds,STAKE,qkey,qat,kick,at,ev,
                     'MODEL_INDEPENDENT_RAW_EV_EXPERIMENT'))
                added+=int(r.rowcount>0)
            except (ValueError,TypeError):continue
    return {'state':'OK','new_forward_paper_experiments':added,'eligible_candidates':eligible,
            'fixed_stake_usd':STAKE,'min_odds':MIN_ODDS,'minimum_raw_ev_pct':MIN_EV,
            'no_real_orders':True,'retroactive_bets_forbidden':True}


def _market_outcome(market,sel,h,a):
    """Returns WIN/LOSS/PUSH/UNKNOWN; sport-specific unsupported markets never fabricated."""
    import oracle_v5 as v5
    canonical=v5.canonical_selection(market,sel)
    if canonical is None:return 'UNKNOWN'
    kind=canonical[0]
    result='1' if h>a else 'X' if h==a else '2'
    if kind=='1X2':return 'WIN' if canonical[1]==result else 'LOSS'
    if kind=='DOUBLE_CHANCE':return 'WIN' if result in canonical[1] else 'LOSS'
    if kind=='BTTS':return 'WIN' if ((h>0 and a>0)==(canonical[1]=='YES')) else 'LOSS'
    if kind=='TOTAL':
        diff=h+a-float(canonical[2])
        if abs(diff)<1e-8:return 'PUSH'
        return 'WIN' if (diff>0)==(canonical[1]=='over') else 'LOSS'
    return 'UNKNOWN'


def _resolve_score(c,origin,eid,kick):
    """Only previously stored completed-result sources, never Fonbet settlement inference."""
    if origin=='sf_forecasts':
        row=c.execute('''SELECT home_score,away_score,last_seen,source
            FROM sf_matches WHERE id=?''',(eid,)).fetchone()
        min_hours=4 if eid.startswith('nflverse|') else 2
        if row and row[0] is not None and row[1] is not None and row[2]:
            try:
                if _parse(row[2])>=_parse(kick)+dt.timedelta(hours=min_hours):
                    return (int(row[0]),int(row[1]),row[2],str(row[3]))
            except (ValueError,TypeError):pass
    elif origin in ('v4_sport_preds','sb_independent_forecasts'):
        if origin=='sb_independent_forecasts':
            r=c.execute('SELECT oracle_event_id FROM sb_event_coverage WHERE selection_key=?',(eid,)).fetchone()
            eid=str(r[0]) if r and r[0] else ''
        if eid:
            row=c.execute('''SELECT home_score,away_score,kickoff_utc
                  FROM v4_games WHERE event_id=? AND final=1''',(eid,)).fetchone()
            if row and row[0] is not None and row[1] is not None:
                # v4.final=1 is source-marked final. Never accept match before kickoff.
                if row[2] and abs((_parse(kick)-_parse(row[2])).total_seconds())<=600:
                    return (int(row[0]),int(row[1]),_at(),'v4_games_FINAL')
    return None


def settle(c,now=None):
    setup(c); at=_at(now); checked=settled=unknown=0; tally=Counter()
    records=c.execute('''SELECT forecast_id,original_event_id,origin,market,selection,
        odds,stake,kickoff_utc FROM v55_paper
        WHERE status='PENDING' AND kickoff_utc<? ORDER BY kickoff_utc''',(at,)).fetchall()
    # Source reads are cached by event and can be retried on the next 6h cycle.
    cache={}
    with c:
        for fid,eid,origin,market,selection,odds,stake,kick in records:
            checked+=1;key=(origin,eid,kick)
            if key not in cache:cache[key]=_resolve_score(c,origin,eid,kick)
            record=cache[key]
            if record is None:unknown+=1;continue
            h,a,source_at,source=record
            # Crucial: BTTS and football double chance use only full-time final.
            state=_market_outcome(market,selection,h,a)
            if state=='UNKNOWN':unknown+=1;continue
            profit=round(float(stake)*(float(odds)-1),6) if state=='WIN' else (-float(stake) if state=='LOSS' else 0.)
            result=c.execute('''UPDATE v55_paper SET status=?,result_home=?,result_away=?,
                 result_seen=?,settled_at=?,profit=?,source=?
                 WHERE forecast_id=? AND status='PENDING' ''',
                 (state,h,a,source_at,at,profit,source,fid))
            settled+=int(result.rowcount>0);tally[state]+=int(result.rowcount>0)
        report={'state':'OK','checked_pending':checked,'settled_now':settled,
            'remaining_without_confirmed_final':unknown,'outcomes':dict(tally),
            'fixed_stake_usd':STAKE,'real_orders':0,'settled_from_final_scores_only':True}
        c.execute('''INSERT INTO v55_settlement_runs(checked_at,checked,settled,outcomes_unavailable,report)
            VALUES(?,?,?,?,?)''',(at,checked,settled,unknown,json.dumps(report,ensure_ascii=False)))
    return report


def backfill(c,now=None,limit=3000):
    """Historical quote display ONLY, never creates retrospective paper orders.

    The quote must have been OBSERVED after forecast generation, strictly before
    verified kickoff. Quote-key must refer to a documented root market+line.
    """
    import oracle_v5 as v5
    setup(c);at=_at(now)
    if not v5.has_table(c,'fb_full_history'):
        return {'state':'NO_HISTORICAL_QUOTE_SOURCE','archive_quotes_added':0}
    rows=c.execute('''SELECT id,root_id,created_at,kickoff_utc,market,selection
         FROM v5_forecasts WHERE root_id IS NOT NULL AND kickoff_utc IS NOT NULL
         AND NOT EXISTS(SELECT 1 FROM v55_archive_prices a WHERE a.forecast_id=v5_forecasts.id)
         ORDER BY kickoff_utc LIMIT ?''',(limit,)).fetchall()
    # Batch candidate source prices once per event: no fake historical re-pricing.
    cached={}; added=0
    with c:
        for fid,root,issued,kick,market,selection in rows:
            canonical=v5.canonical_selection(market,selection)
            if canonical is None:continue
            if root not in cached:
                cached[root]=c.execute('''SELECT q.quote_key,q.event_id,q.factor_id,q.point,
                        h.decimal_price,h.changed_at
                    FROM fb_full_current q JOIN fb_full_history h ON h.quote_key=q.quote_key
                    WHERE q.root_event_id=? AND q.event_id=q.root_event_id
                    AND h.active=1 ORDER BY h.changed_at ASC LIMIT 10000''',(root,)).fetchall()
            best=None
            for qkey,event,fac,point,price,observed in cached[root]:
                try:
                    if not (issued<=observed<kick):continue
                    if str(fac) in v5.KNOWN_FT_1X2:sel=('1X2',v5.KNOWN_FT_1X2[str(fac)])
                    elif str(fac) in v5.KNOWN_DC:sel=('DOUBLE_CHANCE',v5.KNOWN_DC[str(fac)])
                    elif str(fac) in v5.KNOWN_BTTS:sel=('BTTS',v5.KNOWN_BTTS[str(fac)])
                    elif str(fac) in v5.KNOWN_TOTAL:
                        line=v5.total_line(point)
                        if line is None:continue
                        sel=('TOTAL',v5.KNOWN_TOTAL[str(fac)],line)
                    else:continue
                    if sel!=canonical:continue
                    if sel[0]!='TOTAL':
                        params=json.loads(point or '{}')
                        if not isinstance(params,dict) or params.get('p') is not None or params.get('pt') is not None:continue
                    price=float(price)
                    if not math.isfinite(price) or not 1.001<price<=1001:continue
                    if best is None or observed<best[2]:best=(qkey,price,observed)
                except (ValueError,TypeError):continue
            if best:
                result=c.execute('''INSERT OR IGNORE INTO v55_archive_prices VALUES(?,?,?,?,?,?,?)''',
                    (fid,root,best[0],best[1],best[2],'fb_full_history_OBSERVED',at))
                added+=int(result.rowcount>0)
    return {'state':'OK','archive_quotes_added':added,'forecasts_examined':len(rows),
            'historical_prices_are_observed_not_retro_bets':True}


def summary(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='v55_paper'").fetchone():
        return {'state':'NOT_INITIALIZED','fixed_stake_usd':STAKE}
    states={s:n for s,n in c.execute('SELECT status,COUNT(*) FROM v55_paper GROUP BY status')}
    settled=c.execute("SELECT COUNT(*),COALESCE(SUM(stake),0),COALESCE(SUM(profit),0) FROM v55_paper WHERE status IN ('WIN','LOSS','PUSH')").fetchone()
    spent=float(settled[1]);profit=float(settled[2])
    archive=c.execute('SELECT COUNT(*) FROM v55_archive_prices').fetchone()[0]
    last=c.execute('SELECT checked_at,checked,settled,outcomes_unavailable FROM v55_settlement_runs ORDER BY id DESC LIMIT 1').fetchone()
    return {'state':'PAPER_ONLY','fixed_stake_usd':STAKE,'states':states,
            'pending':states.get('PENDING',0),'resolved':int(settled[0]),
            'settled_stake_usd':round(spent,2),'net_profit_usd':round(profit,2),
            'realized_roi_pct':round(profit/spent*100,3) if spent else None,
            'verified_historical_prices_for_display':archive,
            'last_result_check':{'at':last[0],'checked':last[1],'settled':last[2],
                                 'outcomes_unavailable':last[3]} if last else None,
            'real_bets':0,'no_betting_edge_claim':True}


def paper_page(c,limit=50,offset=0):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='v55_paper'").fetchone():return {'total':0,'items':[]}
    n=c.execute('SELECT COUNT(*) FROM v55_paper').fetchone()[0]
    rows=c.execute('''SELECT p.registered_at,p.kickoff_utc,f.home,f.away,p.market,p.selection,
           p.probability,p.odds,p.stake,p.expected_ev_pct,p.status,p.profit,p.source
           FROM v55_paper p LEFT JOIN v5_forecasts f ON f.id=p.forecast_id
           ORDER BY p.registered_at DESC,p.forecast_id LIMIT ? OFFSET ?''',(limit,offset)).fetchall()
    keys=('registered_at','kickoff_utc','home','away','market','selection','probability','odds','stake','raw_ev_pct','status','net_usd','outcome_source')
    return {'state':'OK','total':n,'items':[dict(zip(keys,r)) for r in rows]}
