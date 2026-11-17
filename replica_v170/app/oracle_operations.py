#!/usr/bin/env python3
"""Read-only evidence ledger for the legacy laboratory and Unified Console.

Everything is drawn from actual stored records. This endpoint never constructs
investment/betting instructions from heuristic priorities or raw probabilities.
"""
from __future__ import annotations
import datetime as dt
import json
import sqlite3
from pathlib import Path
UTC=dt.timezone.utc


def operations(path:Path,clock=None):
    at=clock or dt.datetime.now(UTC)
    if at.tzinfo is None:at=at.replace(tzinfo=UTC)
    today=at.astimezone(UTC).date().isoformat()
    since=(at-dt.timedelta(hours=24)).isoformat(timespec='seconds')
    res={'generated_at':at.isoformat(timespec='seconds'),'day_utc':today,
         'research_only':True,'approved_actions':0,
         'notice':'Наблюдения и предварительные гипотезы — не рекомендации купить или поставить.',
         'metrics':{},'crypto':[],'sports':[],'recent_events':[], 'sports_results':[],
         'models':[],'sources':[],'sport_coverage':[],'backtests':[], 'activity':[]}
    if not path.exists():return {**res,'error':'Нет файла общей базы'}
    conn=sqlite3.connect('file:'+path.resolve().as_posix()+'?mode=ro',uri=True,timeout=10)
    conn.row_factory=sqlite3.Row
    try:
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        def scalar(tbl,sql,params=()):
            return conn.execute(sql,params).fetchone()[0] if tbl in tables else None
        def get(tbl,sql,params=()):
            return [dict(r) for r in conn.execute(sql,params).fetchall()] if tbl in tables else []
        m=res['metrics']
        for k,t,sql in [
           ('spot_quotes','mw_quotes','SELECT COUNT(*) FROM mw_quotes'),
           ('crypto_symbols','mw_quotes','SELECT COUNT(DISTINCT symbol) FROM mw_quotes'),
           ('technical_snapshots','oc_technical','SELECT COUNT(*) FROM oc_technical'),
           ('technical_symbols','oc_technical','SELECT COUNT(DISTINCT symbol) FROM oc_technical'),
           ('dex_tokens','radar_tokens','SELECT COUNT(*) FROM radar_tokens'),
           ('news_events','og_events','SELECT COUNT(*) FROM og_events'),
           ('news_articles','oi_articles','SELECT COUNT(*) FROM oi_articles'),
           ('headlines','oc_news','SELECT COUNT(*) FROM oc_news'),
           ('sports_matches','sf_matches','SELECT COUNT(*) FROM sf_matches'),
           ('sports_completed','sf_matches','SELECT COUNT(*) FROM sf_matches WHERE home_score IS NOT NULL'),
           ('sports_hypotheses','sf_forecasts','SELECT COUNT(*) FROM sf_forecasts'),
           ('sports_settled','sf_forecasts',"SELECT COUNT(*) FROM sf_forecasts WHERE status='PAPER_SETTLED'"),
           ('crypto_hypotheses','od_exploratory','SELECT COUNT(*) FROM od_exploratory'),
           ('crypto_settled','od_exploratory',"SELECT COUNT(*) FROM od_exploratory WHERE state='PAPER_SETTLED'"),
           ('watchlist','od_watch','SELECT COUNT(*) FROM od_watch'),
           ('news_links','og_edges','SELECT COUNT(*) FROM og_edges'),
           ('bookmaker_odds','odds_snapshots','SELECT COUNT(*) FROM odds_snapshots')]:
            m[k]=scalar(t,sql)
        m['crypto_24h']=scalar('od_exploratory','SELECT COUNT(*) FROM od_exploratory WHERE created_at>=?',(since,))
        m['sports_24h']=scalar('sf_forecasts','SELECT COUNT(*) FROM sf_forecasts WHERE created_at>=?',(since,))
        m['articles_24h']=scalar('oc_news','SELECT COUNT(*) FROM oc_news WHERE first_seen_at>=?',(since,))
        m['missing_actual_odds']=True  # v2.3 does not infer available prices from bookmaker-independent scores
        res['crypto']=get('od_exploratory','''SELECT symbol,created_at,target_pct,entry_price,target_price,
           horizon_end,state,hit,max_observed,quote_count,evidence FROM od_exploratory
           ORDER BY created_at DESC,id DESC LIMIT 80''')
        for r in res['crypto']:
            try:r['evidence']=json.loads(r['evidence'])
            except (ValueError,TypeError):r['evidence']={}
            r['category']='crypto';r['evidence_type']='uncalibrated_heuristic'
        # Distinguish the one selected outcome per match/market from complementary selections.
        # SQL groups by match, model as-of date, market; show all candidates in expandable evidence only.
        allrows=get('sf_forecasts','''SELECT f.match_id,f.asof_day,f.created_at,f.match_date,f.sport,f.league,
           f.home,f.away,f.market,f.selection,f.probability,f.quality,f.training_games,
           f.status,f.hit,f.settled_at,m.home_score,m.away_score
           FROM sf_forecasts f LEFT JOIN sf_matches m ON m.id=f.match_id
           WHERE f.created_at>=? OR (f.match_date>=? AND f.match_date<=date(?,'+14 days'))
           ORDER BY f.created_at DESC, f.probability DESC LIMIT 8000''',( (at-dt.timedelta(days=8)).isoformat(timespec='seconds'),today,today))
        selected={}
        for r in allrows:
            key=(r['match_id'],r['asof_day'],r['market'])
            if key not in selected or float(r['probability'])>float(selected[key]['probability']):selected[key]=r
        sports=sorted(selected.values(),key=lambda r:(r['created_at'],r['match_date']),reverse=True)
        res['sports']=sports[:100]
        for r in res['sports']:
            r['category']='sport';r['evidence_type']='raw_uncalibrated_probability'
            r['actual_bookmaker_odds']=None
            r['estimated_value']=None
        res['recent_events']=get('og_events','''SELECT first_seen_at,representative_title,article_count,
             publisher_labels_unverified FROM og_events ORDER BY first_seen_at DESC LIMIT 18''')
        res['sports_results']=get('sf_matches','''SELECT match_date,league,sport,home,away,
             home_score,away_score,source FROM sf_matches WHERE home_score IS NOT NULL
             ORDER BY match_date DESC LIMIT 14''')
        res['models']=get('od_model_audit','''SELECT threshold_pct,trained_at,state,reason,dataset_json,
             metrics_json FROM od_model_audit WHERE id IN
             (SELECT MAX(id) FROM od_model_audit GROUP BY threshold_pct)
             ORDER BY threshold_pct''')
        for r in res['models']:
            for key in ('dataset_json','metrics_json'):
                try:r[key]=json.loads(r[key])
                except (ValueError,TypeError):r[key]={}
        res['sources']=get('sf_sources','''SELECT source_id,status,last_attempt,last_success,
                last_error,records,next_due FROM sf_sources
                ORDER BY CASE status WHEN 'error' THEN 0 WHEN 'missing' THEN 1 ELSE 2 END,
                last_attempt DESC LIMIT 120''')
        res['sport_coverage']=get('sf_matches','''SELECT sport,league,COUNT(*) AS matches,
               SUM(CASE WHEN home_score IS NOT NULL THEN 1 ELSE 0 END) AS completed,
               SUM(CASE WHEN home_score IS NULL AND match_date>=date('now') THEN 1 ELSE 0 END) AS upcoming,
               MIN(match_date) AS oldest,MAX(match_date) AS newest
               FROM sf_matches GROUP BY sport,league ORDER BY matches DESC''')
        res['backtests']=get('sf_eval','''SELECT league,asof_day,evaluated,model_brier,baseline_brier,
             skill_pct,interpretation FROM sf_eval WHERE asof_day=(SELECT MAX(asof_day) FROM sf_eval)
             ORDER BY evaluated DESC LIMIT 25''')
        # Data pipeline facts are meaningful even when there is no approved model.
        for table,job_col,time_col,status_col,label in [
             ('od_runs','job','at','status','Криптоаналитика'),
             ('sf_runs','job','at','status','Спортивные исследования'),
             ('oc_runs','job','at','status','Контекстный анализ'),
             ('og_runs','job','at','status','Граф событий')]:
            if table in tables:
                cols={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
                if {job_col,time_col,status_col}.issubset(cols):
                    res['activity'].extend([{'component':label,**r} for r in get(table,
                       f'SELECT {time_col} AS time,{job_col} AS job,{status_col} AS status FROM {table} ORDER BY {time_col} DESC LIMIT 5')])
        res['activity'].sort(key=lambda r:r['time'],reverse=True)
        res['activity']=res['activity'][:25]
    except sqlite3.Error as e:res['error']='Ошибка чтения общей базы: '+str(e)[:160]
    finally:conn.close()
    return res
