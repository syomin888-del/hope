#!/usr/bin/env python3
"""V5.6: one consolidated research-only portfolio selection per Fonbet root.

Existing V5.5 experiment rows are immutable, retained for per-model evaluation.
V5.6 selects at most ONE source experiment per root BEFORE kickoff.  It never
creates retrospective paper bets and never uses bookmaker prices to generate
model probabilities.  Portfolio P&L is a non-additive VIEW of source experiments.
"""
from __future__ import annotations
import datetime as dt
import json
import math
from collections import defaultdict, Counter
from statistics import median


def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS v56_portfolio (
        root_id TEXT PRIMARY KEY,
        forecast_id TEXT NOT NULL UNIQUE,
        canonical_key TEXT NOT NULL,
        selected_at TEXT NOT NULL,
        source_registered_at TEXT NOT NULL,
        independent_models INTEGER NOT NULL,
        consensus_ev_pct REAL NOT NULL,
        reason TEXT NOT NULL,
        FOREIGN KEY(forecast_id) REFERENCES v55_paper(forecast_id)
    );
    CREATE INDEX IF NOT EXISTS v56_portfolio_selected_at ON v56_portfolio(selected_at);
    ''')
    c.commit()


def key_for(market,selection):
    import oracle_v5 as v5
    val=v5.canonical_selection(market,selection)
    if val is None:return None
    return json.dumps(val,ensure_ascii=False,separators=(',',':'))


def sync(c,now=None,min_independent_models=2):
    """Consolidate already recorded experiments, never fabricate old portfolio stakes.

    Require >=2 DISTINCT model implementations per outcome (same model repeated on
    several days counts once), and choose only one outcome per game.  Fix selection
    irrevocably; later prices/results never change the selected source forecast.
    """
    import oracle_v5 as v5
    setup(c)
    at=v5.stamp(now)
    data=c.execute('''SELECT p.forecast_id,p.root_id,p.market,p.selection,p.odds,
        p.probability,p.expected_ev_pct,p.quote_at,p.registered_at,p.kickoff_utc,
        f.model,f.origin
      FROM v55_paper p
      JOIN v5_forecasts f ON f.id=p.forecast_id
      JOIN v5_catalog e ON e.root_id=p.root_id
      WHERE p.kickoff_utc>? AND p.registered_at<=?
        AND p.quote_at<p.kickoff_utc
        AND e.root_exists=1 AND e.active_quotes>0
      ORDER BY p.root_id,p.registered_at,p.forecast_id''',(at,at)).fetchall()
    existing={r[0] for r in c.execute('SELECT root_id FROM v56_portfolio')}
    outcomes=defaultdict(lambda:defaultdict(list))
    for fid,root,market,sel,odds,prob,ev,qat,registered,kick,model,origin in data:
        try:
            canonical=key_for(market,sel)
            if not canonical or v5.parse(registered)>=v5.parse(kick):continue
            if v5.parse(qat)>v5.parse(registered):continue
            odds=float(odds);prob=float(prob);ev=float(ev)
            if not (math.isfinite(odds) and math.isfinite(prob) and math.isfinite(ev)):continue
            if not (0<prob<1 and odds>=1.7 and ev>0):continue
        except (ValueError,TypeError):continue
        # Model name is the method identity.  Repeated forecasts from same model
        # cannot vote several times to fabricate ensemble agreement.
        family=str(model or origin or 'UNKNOWN')
        outcomes[str(root)][canonical].append((fid,family,ev,odds,registered))
    selected=0; abstained=0
    with c:
        for root,variants in sorted(outcomes.items()):
            if root in existing:continue
            options=[]
            for canonical,rows in variants.items():
                by_model={}
                for row in rows:
                    prev=by_model.get(row[1])
                    if prev is None or (row[4],row[0])<(prev[4],prev[0]):
                        by_model[row[1]]=row
                if len(by_model)<min_independent_models:continue
                consensus_ev=median(x[2] for x in by_model.values())
                if consensus_ev<=0:continue
                # Anchor to the earliest *actually recorded* positive experiment
                # with this exact selection, not an invented new price or time.
                anchor=min(rows,key=lambda x:(x[4],x[0]))
                options.append((len(by_model),consensus_ev,-anchor[3],canonical,anchor))
            if not options:
                abstained+=1;continue
            options.sort(reverse=True)
            n,consensus,_,canonical,anchor=options[0]
            fid,_,_,_,registered=anchor
            r=c.execute('''INSERT OR IGNORE INTO v56_portfolio
                (root_id,forecast_id,canonical_key,selected_at,
                 source_registered_at,independent_models,consensus_ev_pct,reason)
                VALUES(?,?,?,?,?,?,?,?)''',
                (root,fid,canonical,at,registered,n,round(consensus,6),
                 'CONSOLIDATED_PREKICKOFF_SOURCE_EXPERIMENT_NOT_NEW_WAGER'))
            selected+=int(r.rowcount>0)
    return {'state':'OK','new_portfolio_selections':selected,
            'roots_with_eligible_experiments':len(outcomes),
            'roots_without_multi_model_agreement':abstained,
            'independent_models_required':min_independent_models,
            'one_selection_per_event':True,'new_bets_backdated':0,
            'underlying_model_experiments_untouched':True,'real_orders':0}


def summary(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='v56_portfolio'").fetchone():
        return {'state':'NOT_INITIALIZED','portfolio_selections':0,'real_orders':0}
    stats={state:n for state,n in c.execute('''
        SELECT p.status,COUNT(*) FROM v56_portfolio x
        JOIN v55_paper p ON p.forecast_id=x.forecast_id
        GROUP BY p.status''')}
    settled=c.execute('''SELECT COUNT(*),COALESCE(SUM(p.stake),0),
         COALESCE(SUM(p.profit),0) FROM v56_portfolio x
         JOIN v55_paper p ON p.forecast_id=x.forecast_id
         WHERE p.status IN ('WIN','LOSS','PUSH')''').fetchone()
    total=c.execute('SELECT COUNT(*) FROM v56_portfolio').fetchone()[0]
    raw=c.execute('SELECT COUNT(*) FROM v55_paper').fetchone()[0] if c.execute("SELECT 1 FROM sqlite_master WHERE name='v55_paper'").fetchone() else 0
    denom=float(settled[1]);net=float(settled[2])
    return {'state':'PAPER_ONLY','portfolio_selections':total,
            'model_experiments_retained':raw,'states':stats,
            'pending':stats.get('PENDING',0),'resolved':int(settled[0]),
            'settled_stake_usd':round(denom,2),'net_profit_usd':round(net,2),
            'realized_roi_pct':round(net/denom*100,3) if denom else None,
            'stake_per_selection_usd':10.0,'one_selection_per_event':True,
            'counts_are_not_additive_to_model_experiments':True,
            'real_orders':0,'no_edge_proven':True}


def page(c,limit=50,offset=0):
    total=c.execute('SELECT COUNT(*) FROM v56_portfolio').fetchone()[0] if c.execute("SELECT 1 FROM sqlite_master WHERE name='v56_portfolio'").fetchone() else 0
    rows=c.execute('''SELECT x.selected_at,x.source_registered_at,
      f.home,f.away,f.sport,p.market,p.selection,p.odds,p.probability,
      x.independent_models,x.consensus_ev_pct,p.status,p.profit,p.source,
      x.root_id,x.forecast_id
      FROM v56_portfolio x
      JOIN v55_paper p ON x.forecast_id=p.forecast_id
      LEFT JOIN v5_forecasts f ON f.id=p.forecast_id
      ORDER BY x.selected_at DESC,x.root_id
      LIMIT ? OFFSET ?''',(limit,offset)).fetchall() if total else []
    keys=('selected_at','source_registered_at','home','away','sport','market',
        'selection','odds','probability','independent_models','consensus_raw_ev_pct',
        'status','net_usd','outcome_source','root_id','source_forecast_id')
    return {'state':'OK','total':total,'items':[dict(zip(keys,r)) for r in rows],
        'one_selection_per_event':True,'real_orders':0}
