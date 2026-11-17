#!/usr/bin/env python3
"""Unified read-only command center on the existing SSH-only dashboard port."""
from __future__ import annotations
import argparse, datetime as dt, json, os, sqlite3
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from http.server import ThreadingHTTPServer
import dashboard
from oracle_operations import operations
from oracle_scorecard import summary as scorecard_summary
from oracle_v4 import summary as v4_summary
import oracle_sports_brain
import oracle_v5

ROOT=Path(__file__).resolve().parent

def fetch_status(db):
    out={'system':'ORACLE Unified Research 2.3','read_only':True,
         'verified_buy_signals':0, 'message':'Research and paper forecasts; no trading API connected.'}
    if not db.exists():return {**out,'error':'Database not yet created'}
    c=sqlite3.connect(f'file:{db}?mode=ro',uri=True,timeout=5)
    try:
        t={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        def val(sql,params=()):return c.execute(sql,params).fetchone()[0]
        def rows(sql,params=(),fields=None):
            r=c.execute(sql,params).fetchall()
            if fields:return [dict(zip(fields,x)) for x in r]
            return r
        if 'mw_quotes' in t:
            out['observed_spot_coins']=val('SELECT COUNT(DISTINCT symbol) FROM mw_quotes')
            out['quote_rows']=val('SELECT COUNT(*) FROM mw_quotes')
            out['quote_last_at']=val('SELECT MAX(observed_at) FROM mw_quotes')
        if 'radar_tokens' in t:
            out['dex_discovered']=val('SELECT COUNT(*) FROM radar_tokens')
        if 'oc_technical' in t:
            out['technical_coins']=val('SELECT COUNT(DISTINCT symbol) FROM oc_technical')
            out['technical_last_at']=val('SELECT MAX(evaluated_at) FROM oc_technical')
        if 'oi_source_state' in t:
            out['configured_news_feeds']=val('SELECT COUNT(*) FROM oi_source_state')
            cutoff=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=24)).isoformat(timespec='seconds')
            out['working_news_feeds']=val('SELECT COUNT(*) FROM oi_source_state WHERE last_success>=?',(cutoff,))
            out['feeds'] = rows('''SELECT name,topic,successes,errors,last_success,last_error
                FROM oi_source_state ORDER BY COALESCE(last_success,'') DESC LIMIT 50''',fields=('name','topic','successes','errors','last_success','last_error'))
        if 'oc_news' in t:
            out['articles']=val('SELECT COUNT(*) FROM oc_news')
            out['latest_articles']=rows('''SELECT first_seen_at,title,topic,source,url
                FROM oc_news ORDER BY first_seen_at DESC LIMIT 25''',fields=('first_seen','title','topic','source','url'))
        if 'oi_clusters' in t:out['clusters']=val('SELECT COUNT(*) FROM oi_clusters')
        if 'og_events' in t:
            out['graph_events']=val('SELECT COUNT(*) FROM og_events')
            out['graph_edges']=val('SELECT COUNT(*) FROM og_edges')
            out['event_examples']=rows('''SELECT e.first_seen_at,e.representative_title,e.article_count,
               GROUP_CONCAT(x.entity,' | ') FROM og_events e LEFT JOIN og_edges x
               ON x.cluster_id=e.cluster_id GROUP BY e.cluster_id
               ORDER BY e.first_seen_at DESC LIMIT 15''',fields=('first_seen','title','article_count','entities'))
        if 'og_followups' in t:
            out['graph_followups']=val('SELECT COUNT(*) FROM og_followups')
            out['graph_followup_status']=rows('SELECT status,COUNT(*) FROM og_followups GROUP BY status',fields=('status','count'))
        if 'od_watch' in t:
            out['watchlist']=rows('''SELECT symbol,seen_at,quote_at,quote_price,quote_volume,
             change_24h,priority,patterns,evidence,blockers,news_mentions,model_state,
             model_probability FROM od_watch ORDER BY priority DESC,quote_volume DESC LIMIT 60''',
             fields=('symbol','seen_at','quote_at','price','volume_usdt','change_24h_pct',
                     'priority','patterns','evidence','blockers','news_mentions','model_state','model_probability'))
            for item in out['watchlist']:
                for key in ('patterns','evidence','blockers'):
                    try:item[key]=json.loads(item[key])
                    except (ValueError,TypeError):item[key]=None
            out['watchlist_count']=val('SELECT COUNT(*) FROM od_watch')
        if 'od_model_audit' in t:
            out['models']=rows('''SELECT threshold_pct,trained_at,state,reason,dataset_json,metrics_json
                FROM od_model_audit WHERE id IN (SELECT MAX(id) FROM od_model_audit GROUP BY threshold_pct)
                ORDER BY threshold_pct''',fields=('threshold','at','state','reason','dataset','metrics'))
            for m in out['models']:
                for key in ('dataset','metrics'):
                    try:m[key]=json.loads(m[key])
                    except (ValueError,TypeError):m[key]={}
        if 'od_paper_signals' in t:
            out['paper_forecasts']=rows('''SELECT symbol,created_at,threshold_pct,entry,target,
              horizon_end,estimated_probability,state,hit,observations
              FROM od_paper_signals ORDER BY created_at DESC LIMIT 35''',
              fields=('symbol','created_at','threshold','entry','target','horizon_end',
                      'estimated_probability','state','hit','observations'))
        if 'od_runs' in t:
            out['decision_last_run']=rows('SELECT at,job,status,details FROM od_runs ORDER BY id DESC LIMIT 1',fields=('at','job','status','details'))
        if 'od_exploratory' in t:
            out['hypotheses']=rows('''SELECT symbol,created_at,target_pct,entry_price,target_price,
                  horizon_end,state,hit,max_observed,quote_count,evidence FROM od_exploratory
                  ORDER BY id DESC LIMIT 60''',fields=('symbol','created_at','target_pct','entry','target',
                  'horizon_end','state','hit','peak','quote_count','evidence'))
            for item in out['hypotheses']:
                try:item['evidence']=json.loads(item['evidence'])
                except (ValueError,TypeError):item['evidence']={}
            out['hypothesis_summary']=rows('''SELECT target_pct,state,COUNT(*) FROM od_exploratory
                 GROUP BY target_pct,state''',fields=('target_pct','state','count'))
        out['signals_are_paper_only']=True
    except sqlite3.Error as e:
        out['error']='Reading current database: '+str(e)[:160]
    finally:c.close()
    return out

def fetch_sports(db,models):
    """Existing sport-research database, exposed in the same read-only console."""
    state=dashboard.snapshot(db,models)
    signals=[x for x in state.get('signals',[]) if x.get('kind')=='sport']
    jobs=[x for x in state.get('source_jobs',[]) if
          x.get('job') in ('nba_games','nba_odds','nba_scores','sports_multi_odds')]
    providers=[x for x in state.get('source_status',[]) if
               any(token in str(x.get('provider','')).lower() for token in
                   ('nba','odds','sport','soccer','nfl','nhl'))]
    historical=sorted(signals,key=lambda x:x.get('issued_at') or '',reverse=True)
    current=[x for x in historical if x.get('status') in ('open','pending','paper_open')]
    return {
      'module':'sports','read_only':True, 'signals':historical[:100],
      'total_signals':len(signals), 'pending_signals':len(current),
      'source_jobs':jobs,'sport_source_snapshots':providers,
      'models':{k:v for k,v in state.get('models',{}).items() if v.get('kind')=='sport'},
      'source_key_state':{
        'ORACLE_ODDS_KEY':bool(os.getenv('ORACLE_ODDS_KEY')),
        'ORACLE_NBA_KEY':bool(os.getenv('ORACLE_NBA_KEY'))},
      'data_state':('NO_SPORTS_HISTORY' if not signals and not providers else 'HISTORY_AVAILABLE'),
      'note':'Sports evidence and paper history, not current bookable odds. Missing API key is not evidence of no opportunities.'
    }

def fetch_health(db,models):
    data=fetch_status(db)
    sports=fetch_sports(db,models)
    current=dt.datetime.now(dt.timezone.utc)
    checks=[]
    def check(k,state,description):checks.append({'component':k,'state':state,'detail':description})
    if data.get('error'):check('Shared SQLite data','ERROR',data['error'])
    def since(raw):
        if not raw:return None
        try:
            v=dt.datetime.fromisoformat(raw.replace('Z','+00:00'))
            if v.tzinfo is None:v=v.replace(tzinfo=dt.timezone.utc)
            return max(0,round((current-v.astimezone(dt.timezone.utc)).total_seconds()))
        except (TypeError,ValueError):return None
    for name,key,lim in [('Spot quotes','quote_last_at',1200),('Technical research','technical_last_at',3600)]:
        sec=since(data.get(key))
        check(name,'OK' if sec is not None and sec<=lim else 'STALE',
              ('Last record '+str(sec)+' seconds ago' if sec is not None else 'Not initialized'))
    total=data.get('configured_news_feeds',0)
    success=data.get('working_news_feeds',0)
    check('Global news feeds','OK' if success and total and success>=min(6,total) else 'PARTIAL',
          str(success)+' working feeds in last 24h out of '+str(total)+' configured/registered')
    check('Sport odds API','READY' if sports.get('source_key_state',{}).get('ORACLE_ODDS_KEY') else 'NOT_CONFIGURED',
          'Key present' if sports.get('source_key_state',{}).get('ORACLE_ODDS_KEY') else 'Missing ORACLE_ODDS_KEY — not a market result')
    check('NBA API','READY' if sports.get('source_key_state',{}).get('ORACLE_NBA_KEY') else 'NOT_CONFIGURED',
          'Key present' if sports.get('source_key_state',{}).get('ORACLE_NBA_KEY') else 'Missing ORACLE_NBA_KEY')
    approved=[x for x in data.get('models',[]) if x.get('state')=='APPROVED_PAPER']
    check('Crypto forecast model','RESEARCH' if not approved else 'PAPER_TESTED',
          'No independently approved model yet' if not approved else 'Approved paper models, NOT actual execution readiness')
    return {'checked_at':current.isoformat(timespec='seconds'),'checks':checks,
       'verdict':'ACTION_REQUIRED' if any(x['state'] in ('ERROR','STALE','PARTIAL','NOT_CONFIGURED') for x in checks) else 'RESEARCH_ONLY',
       'warning':'Status reflects observation freshness and configuration, not verified prediction accuracy or uptime.'}

def fetch_open_sports(db):
    """Read-only CC0 football history/projections; missing is not success."""
    result={'state':'NOT_READY','source':'OpenFootball CC0','sport':'football',
            'fixtures':[], 'model_calibration':'NOT_VALIDATED',
            'odds_ready':False,'recommended_bets':0,
            'notice':'Исследовательские вероятности, не проверенная модель и не рекомендация ставить.'}
    if not db.exists():return result
    c=sqlite3.connect(f'file:{db}?mode=ro',uri=True,timeout=7)
    try:
        names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'sf_matches','sf_forecasts','sf_sources'}.issubset(names):return result
        def scalar(sql):return c.execute(sql).fetchone()[0]
        result.update(state='COLLECTING',matches=scalar('SELECT COUNT(*) FROM sf_matches'),
            completed=scalar('SELECT COUNT(*) FROM sf_matches WHERE home_score IS NOT NULL'),
            per_sport={r[0]:{'matches':r[1],'completed':r[2]} for r in c.execute("SELECT sport,COUNT(*),SUM(CASE WHEN home_score IS NOT NULL THEN 1 ELSE 0 END) FROM sf_matches GROUP BY sport")},
            future=scalar("SELECT COUNT(*) FROM sf_matches WHERE match_date>date('now') AND home_score IS NULL"),
            leagues=scalar('SELECT COUNT(DISTINCT league) FROM sf_matches'),
            successful_feeds=scalar("SELECT COUNT(*) FROM sf_sources WHERE status='ok'"),
            failed_feeds=scalar("SELECT COUNT(*) FROM sf_sources WHERE status='error'"),
            hypothesis_rows=scalar('SELECT COUNT(*) FROM sf_forecasts'))
        # Only forthcoming matches; never treat an old result as a future projection.
        rows=c.execute("""SELECT a.match_id,a.match_date,a.home,a.away,a.league,a.sport,a.market,a.selection,
             a.probability,a.quality,a.training_games,a.created_at
             FROM sf_forecasts a WHERE a.match_date>date('now')
             AND a.match_date<=date('now','+14 days') AND a.asof_day>=date('now','-7 days')
             AND a.market IN ('1X2','ТОТАЛ 2.5','ТОТАЛ 44.5','ОБЕ ЗАБЬЮТ')
             ORDER BY a.match_date,a.match_id,a.market,a.probability DESC LIMIT 1200""").fetchall()
        by_event={}
        for mid,date,home,away,league,sport,market,selection,prob,quality,n,asof in rows:
            x=by_event.setdefault(mid,{'date':date,'home':home,'away':away,'league':league,'sport':sport,
              'historical_games_used':n,'quality':quality,'asof':asof,'markets':{}})
            if market not in x['markets']:
                x['markets'][market]={'selection':selection,'raw_probability':prob}
        result['fixtures']=list(by_event.values())[:50]
        if result['fixtures']:result['state']='RESEARCH_PROJECTIONS_AVAILABLE'
        rows=c.execute('''SELECT f.created_at,f.match_date,f.home,f.away,f.league,f.market,
            f.selection,f.probability,f.hit,f.settled_at FROM sf_forecasts f
            WHERE f.status='PAPER_SETTLED' ORDER BY f.settled_at DESC LIMIT 40''').fetchall()
        result['outcomes']=[dict(zip(('predicted_at','match_date','home','away','league','market',
                 'selection','raw_probability','hit','settled_at'),r)) for r in rows]
        result['settled_count']=scalar("SELECT COUNT(*) FROM sf_forecasts WHERE status='PAPER_SETTLED'")
        result['unsettled_count']=scalar("SELECT COUNT(*) FROM sf_forecasts WHERE status='RESEARCH_ONLY'")
        result['settled_hit_rate']=round(100*scalar("SELECT COUNT(*) FROM sf_forecasts WHERE status='PAPER_SETTLED' AND hit=1")/result['settled_count'],2) if result['settled_count'] else None
        # All-prediction hit rate is *not* evidence of betting edge; complementary outcomes may both be saved.
        if 'sf_eval' in names:
            result['backtests']=[dict(zip(('league','cases','model_brier','baseline_brier','skill_pct','state'),r)) for r in c.execute('''SELECT league,evaluated,model_brier,baseline_brier,skill_pct,interpretation
              FROM sf_eval WHERE asof_day=(SELECT MAX(asof_day) FROM sf_eval)
              ORDER BY evaluated DESC LIMIT 15''')]
        if 'sf_runs' in names:
            rows=c.execute('SELECT at,job,status,details FROM sf_runs ORDER BY id DESC LIMIT 3').fetchall()
            result['recent_jobs']=[{'at':r[0],'job':r[1],'status':r[2],'details':json.loads(r[3])} for r in rows]
    finally:c.close()
    return result

def handler_factory(db,models):
    Base=dashboard.handler_factory(db,models)
    class Handler(Base):
        def do_GET(self):
            route=urlsplit(self.path).path
            if route=='/':
                self.send_response(302)
                self.send_header('Location','/command')
                self.send_header('Cache-Control','no-store')
                self.end_headers();return
            if route in ('/v4','/v4/'):
                body=(ROOT/'oracle_v4.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'")
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route in ('/api/v4/sports-brain/odds','/api/v4/sports-brain/dossier'):
                try:
                    query=parse_qs(urlsplit(self.path).query)
                    key=query.get('event',[''])[0][:100]
                    if not key or not all(ch.isalnum() or ch in '_:-.' for ch in key):
                        raise ValueError('Invalid event ID')
                    with sqlite3.connect('file:'+Path(db).resolve().as_posix()+'?mode=ro',uri=True,timeout=12) as conn:
                        if route.endswith('/odds'):
                            limit=min(100,max(1,int(query.get('limit',['50'])[0])))
                            offset=max(0,min(100000,int(query.get('offset',['0'])[0])))
                            result=oracle_sports_brain.odds(conn,key,limit=limit,offset=offset)
                        else:
                            match=conn.execute('SELECT payload FROM sb_dossiers WHERE selection_key=?',(key,)).fetchone()
                            result=json.loads(match[0]) if match else {'state':'NO_DOSSIER','event':key}
                except (ValueError,sqlite3.Error) as exc:
                    result={'state':'ERROR','reason':type(exc).__name__}
                body=json.dumps(result,ensure_ascii=False,default=str).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route in ('/v5','/v5/'):
                body=(ROOT/'oracle_v5.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'")
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route in ('/api/v5','/api/v5/doctor','/api/v5/events',
                         '/api/v5/forecasts','/api/v5/odds','/api/v5/dossier','/api/v5/paper'):
                try:
                    query=parse_qs(urlsplit(self.path).query)
                    def param(name,default='',maxlen=100):
                        return str(query.get(name,[default])[0])[:maxlen]
                    lim=max(1,min(200,int(param('limit','50',8))))
                    off=max(0,min(1000000,int(param('offset','0',9))))
                    rid=param('event','',100)
                    if rid and not all(ch.isalnum() or ch in '_:-.' for ch in rid):
                        raise ValueError('Invalid event ID')
                    with sqlite3.connect('file:'+Path(db).resolve().as_posix()+'?mode=ro',uri=True,timeout=20) as conn:
                        if route=='/api/v5':result=oracle_v5.status(conn)
                        elif route=='/api/v5/doctor':result=oracle_v5.doctor(conn)
                        elif route=='/api/v5/events':
                            result=oracle_v5.catalog_page(conn,param('sport','',50),param('q','',70),lim,off,param('history')=='1')
                        elif route=='/api/v5/forecasts':
                            result=oracle_v5.forecast_page(conn,param('status','',70),lim,off,param('history')!='1')
                        elif route=='/api/v5/odds':result=oracle_v5.price_page(conn,rid,lim,off)
                        elif route=='/api/v5/paper':result=__import__('oracle_v5_settlement').paper_page(conn,lim,off)
                        else:
                            r=conn.execute('SELECT payload FROM v5_dossiers WHERE root_id=?',(rid,)).fetchone()
                            result=json.loads(r[0]) if r else {'state':'NO_VERIFIED_DOSSIER','event':rid}
                except (ValueError,sqlite3.Error,TypeError,KeyError) as exc:
                    result={'state':'ERROR','kind':type(exc).__name__,'message':str(exc)[:150]}
                body=json.dumps(result,ensure_ascii=False,default=str).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route=='/api/v4':
                try:result=v4_summary(db)
                except Exception as e:result={'error':type(e).__name__}
                body=json.dumps(result,ensure_ascii=False,default=str).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route in ('/scorecard','/scorecard/'):
                body=(ROOT/'oracle_scorecard.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'")
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route=='/api/scorecard':
                try: result=scorecard_summary(db)
                except Exception as e: result={'error':type(e).__name__+': '+str(e)[:130]}
                body=json.dumps(result,ensure_ascii=False,default=str).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route in ('/command','/command/'):
                body=(ROOT/'oracle_console.html').read_bytes()
                if b'/v4' not in body:
                    nav=('<a href="/v4" style="position:fixed;right:18px;bottom:74px;z-index:9999;'+
                         'background:#122441;color:#eaf4ff;padding:11px 15px;border:1px solid #466084;'+
                         'border-radius:12px;font-family:system-ui;text-decoration:none">ORACLE V4: Три рынка</a>').encode('utf8')
                    body=body.replace(b'</body>',nav+b'</body>')
                if b'/scorecard' not in body:
                    link=('<a href="/scorecard" style="position:fixed;right:18px;bottom:18px;'
                          'z-index:9999;background:#101b31;color:white;padding:11px 15px;'
                          'border:1px solid #385171;border-radius:12px;font-family:system-ui;'
                          'text-decoration:none">ORACLE: Статистика</a>').encode('utf-8')
                    body=body.replace(b'</body>',link+b'</body>')
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'")
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route=='/api/operations':
                try:result=operations(db)
                except Exception as e:result={'error':type(e).__name__+': '+str(e)[:140]}
                body=json.dumps(result,ensure_ascii=False,default=str).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route=='/api/health':
                try:result=fetch_health(db,models)
                except Exception as e:result={'error':type(e).__name__+': '+str(e)[:140]}
                body=json.dumps(result,ensure_ascii=False).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route=='/api/sports-research':
                try:result=fetch_open_sports(db)
                except Exception as e:result={'state':'ERROR','error':type(e).__name__+': '+str(e)[:160]}
                body=json.dumps(result,ensure_ascii=False,default=str).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route=='/api/sports':
                try:result=fetch_sports(db,models)
                except Exception as e:result={'module':'sports','error':type(e).__name__+': '+str(e)[:140]}
                body=json.dumps(result,ensure_ascii=False,default=str).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route in ('/api/presignal','/api/rosters'):
                try:
                    if route=='/api/presignal':
                        from oracle_presignal import dashboard as endpoint
                    else:
                        from oracle_rosterlab import dashboard as endpoint
                    result=endpoint(db)
                except Exception as exc:
                    result={'error':type(exc).__name__+': '+str(exc)[:140]}
                body=json.dumps(result,ensure_ascii=False,default=str).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route=='/api/profit':
                try:
                    import oracle_profit
                    import sqlite3
                    cc=sqlite3.connect('file:'+db.resolve().as_posix()+'?mode=ro',uri=True,timeout=5)
                    try: result=oracle_profit.report(cc)
                    finally: cc.close()
                except Exception as exc:
                    result={'error':type(exc).__name__+': '+str(exc)[:160]}
                body=json.dumps(result,ensure_ascii=False,default=str).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route=='/api/universe':
                try:
                    from oracle_universe import dashboard as read_universe
                    result=read_universe(db)
                except Exception as exc:
                    result={'error':type(exc).__name__+': '+str(exc)[:120]}
                body=json.dumps(result,ensure_ascii=False,default=str).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route=='/api/command':
                try:result=fetch_status(db)
                except Exception as e:result={'error':type(e).__name__+': '+str(e)[:130]}
                body=json.dumps(result,ensure_ascii=False,default=str).encode('utf8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            return super().do_GET()
    return Handler

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=8765)
    p.add_argument('--db',default=os.getenv('ORACLE_DB',str(ROOT/'oracle.sqlite3')))
    p.add_argument('--models',default=str(ROOT/'approved_models.json'))
    a=p.parse_args()
    server=ThreadingHTTPServer((a.host,a.port),handler_factory(Path(a.db).resolve(),Path(a.models).resolve()))
    print(f'ORACLE unified command center 2.3 at http://127.0.0.1:{a.port}/command (SSH tunnel required)',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
if __name__=='__main__':main()
