"""ORACLE V4 unified research orchestrator, invoked by existing sportslab loop.

No autonomous account creation, scraping forbidden sites, self-modifying code or
live orders. All data written into the active shared SQLite volume.
"""
from __future__ import annotations
import datetime as dt, json, os, sqlite3, time
from pathlib import Path
import oracle_v4_sports as sports
import oracle_v4_discovery as research
import oracle_fonbet_bridge
import oracle_sports_brain
import oracle_v5
import oracle_causal, oracle_equities, oracle_library,oracle_v4_books,oracle_v4_learning,oracle_open_sources,oracle_v4_bookies,oracle_v4_fulltext,oracle_odds_hub,oracle_fonbet_full
ROOT=Path(__file__).resolve().parent

def setup(c):
 sports.setup(c);research.setup(c)
 dbfile=c.execute('PRAGMA database_list').fetchone()[2]
 causal=oracle_causal.open_db(dbfile);causal.close()
 equ=oracle_equities.conn_db(dbfile);equ.close()
 oracle_library.setup(c)
 oracle_v4_books.setup(c);oracle_v4_learning.setup(c);oracle_open_sources.setup(c);oracle_v4_bookies.setup(c);oracle_v4_fulltext.setup(c);oracle_odds_hub.setup(c);oracle_fonbet_full.setup(c)
 c.execute('''CREATE TABLE IF NOT EXISTS v4_orchestrator_log(id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,component TEXT NOT NULL,state TEXT NOT NULL,detail TEXT NOT NULL)''');c.commit()

def audit_log(c,module,state,data,at=None):
 c.execute('INSERT INTO v4_orchestrator_log(at,component,state,detail) VALUES(?,?,?,?)',
   (sports.stamp(at),module,state,json.dumps(data,ensure_ascii=False)[:2500]));c.commit()

def catalog(c):
 path=ROOT/'BOOKS_100_ORACLE.csv'
 if path.exists():return oracle_library.ingest_catalog(c,path)
 return 0

def stocks_once(c):
    tickers=[x.strip().upper() for x in os.getenv('ORACLE_V4_STOCK_TICKERS','AAPL,MSFT,NVDA').split(',') if x.strip()][:3]
    agent=os.getenv('SEC_USER_AGENT','')
    if not agent or '@' not in agent or 'example.com' in agent:
        return {'status':'NOT_CONFIGURED','source':'SEC','note':'Set SEC_USER_AGENT with a real contact email'}
    mapped=oracle_equities.sec_tickers(c,agent,tickers)
    results=[]
    for name,_ in mapped:
        try:results.append({'ticker':name,'filings':oracle_equities.sec_filings(c,agent,name)})
        except Exception as e:results.append({'ticker':name,'state':'ERROR','error':type(e).__name__})
        time.sleep(.12)
    key=os.getenv('ORACLE_STOCKS_AV_KEY','')
    if key:
        for name,_ in mapped[:2]:
            try:oracle_equities.av_prices(c,agent,name,key)
            except Exception:pass
    return {'status':'OK','issuers':len(mapped),'filing_checks':results,'unadjusted_prices_enabled':bool(key)}

def news_claims(c,at=None):
    """Discover typed events from existing news; do not infer company exposures."""
    rows=c.execute("""SELECT title,url,source,first_seen_at,published_at FROM oc_news
       WHERE first_seen_at>=? ORDER BY first_seen_at DESC LIMIT 400""",
       (sports.stamp((at or dt.datetime.now(dt.timezone.utc))-dt.timedelta(days=2)),)).fetchall()
    items=0
    for title,url,source,first,published in rows:
        try:
            if oracle_causal.ingest_event(c,headline=title,url=url,source=source or 'oc_news',
                 first_seen_at=first,published_at=published):items+=1
        except (ValueError,TypeError):continue
    return {'news_reviewed':len(rows),'events_classified_without_claiming_truth':items}

def cycle(c,at=None):
 setup(c);output={};now=at or dt.datetime.now(dt.timezone.utc)
 tasks=[
  ('openliga',lambda:sports.ingest_openliga(c,at=now),12*3600),
  ('open_worldbank',lambda:oracle_open_sources.ingest_worldbank(c,at=now),24*3600),
  ('open_gleif',lambda:oracle_open_sources.ingest_gleif(c,at=now),7*86400),
  ('open_cftc',lambda:oracle_open_sources.ingest_cftc(c,at=now),24*3600),
  ('open_crossref',lambda:oracle_open_sources.ingest_crossref(c,at=now),24*3600),
  ('open_openalex',lambda:oracle_open_sources.ingest_openalex(c,at=now),24*3600),
  ('open_binance_spot',lambda:oracle_open_sources.ingest_binance(c,at=now),3600),
  ('open_prediction_market',lambda:oracle_open_sources.ingest_polymarket(c,at=now),3600),
  ('sports_propose',lambda:sports.propose(c,at=now),30*60),
  ('bookmaker_public_feed',lambda:oracle_v4_bookies.poll(c,os.getenv('ORACLE_PUBLIC_BOOKMAKER_FEEDS',''),at=now),2*3600),
  ('sports_settle',lambda:sports.settle(c,at=now),15*60),
  ('sports_paper_settle',lambda:sports.settle_paper_bets(c,at=now),15*60),
  ('sports_paper_candidates',lambda:sports.paper_candidates(c,at=now),30*60),
  ('research_discovery',lambda:research.ingest(c,at=now),6*3600),
  ('video_discovery',lambda:research.ingest_video_feeds(c,os.getenv('ORACLE_V4_YOUTUBE_CHANNELS','') or 'UCEBb1b_L6zDS3xTUrIALZOw',at=now),6*3600),
  ('new_book_discovery',lambda:research.discover_book_metadata(c,os.getenv('ORACLE_BOOKS_API_KEY',''),at=now),24*3600),
  ('library_catalog',lambda:catalog(c),7*86400),
  ('licensed_fulltext',lambda:oracle_v4_fulltext.ingest(c,at=now,max_new=4),6*3600),
  ('news_claims',lambda:news_claims(c,now),6*3600),
  ('stocks',lambda:stocks_once(c),24*3600),
  ('multi_sport_odds_hub',lambda:oracle_odds_hub.poll(c,now=now),20*60),
  ('fonbet_first_party',lambda:oracle_fonbet_full.once(c,now=now),max(300,int(os.getenv('ORACLE_FONBET_POLL_SECONDS','600')))),
  ('fonbet_bridge',lambda:oracle_fonbet_bridge.run(c,at=sports.stamp(now)),max(300,int(os.getenv('ORACLE_FONBET_POLL_SECONDS','600')))),
  ('sports_brain',lambda:oracle_sports_brain.cycle(c,at=now),600),
  # ORACLE V5 indexes the ENTIRE source root-event universe, not 15 fixtures.
  ('v5_full_universe',lambda:oracle_v5.run(c,now=now),max(300,int(os.getenv('ORACLE_FONBET_POLL_SECONDS','600')))),
  # Bounded staged public news search; no headline becomes a verified roster claim.
  ('v5_paper_settlement_6h',lambda:__import__('oracle_v5_settlement').settle(c,now=now),6*3600),
  ('v5_evidence_search',lambda:oracle_v5.check_public_evidence(c,now=now,max_events=max(1,min(80,int(os.getenv('ORACLE_V5_EVIDENCE_BATCH','48'))))),max(900,int(os.getenv('ORACLE_V5_EVIDENCE_INTERVAL_SEC','1800')))),
 ]
 if os.getenv('ORACLE_ODDS_KEY'):
  tasks.append(('sports_odds',lambda:sports.ingest_odds(c,os.environ['ORACLE_ODDS_KEY'],at=now),3*3600))
 for name,worker,interval in tasks:
  if not sports.due(c,'v4_'+name,interval,at=now):continue
  try:
   value=worker();output[name]=value
   state=(value.get('state') or value.get('status') or ('PARTIAL' if value.get('errors') else 'OK')) if isinstance(value,dict) else 'OK'
  except Exception as e:
   value={'error':type(e).__name__};state='ERROR';output[name]=value
  sports.log(c,'v4_'+name,state,value,at=now)
  audit_log(c,name,state,value,at=now)
 return output

def safe_count(c,table):
 try:return c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
 except sqlite3.Error:return 0

def summary(path):
 out={'system':'ORACLE_V4','state':'RESEARCH_ONLY','live_orders':False,
      'promise_positive_profit':False,'at':sports.stamp(),'crypto_oos_edge':'NOT_PROVEN',
      'source_notes':'Optional providers never simulated when not configured.'}
 c=sqlite3.connect('file:'+Path(path).resolve().as_posix()+'?mode=ro',uri=True,timeout=10)
 try:
  tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  out['sports']=sports.sports_summary(c) if 'v4_games' in tables else {'state':'NOT_INITIALIZED','research_today':0,'upcoming':[]}
  out['research']=research.status(c) if 'v4_discoveries' in tables else {'discovered_items':0,'unverified_claims':0,'recent':[]}
  out['books']={'catalog_entries':safe_count(c,'kb_books'),
                'notes_with_explicit_provenance':safe_count(c,'kb_principles'),
                'books_verified_fully_read':0,'licensed_study_fragments':safe_count(c,'v4_study_fragments')}
  out['wall_street']={'sec_filings':safe_count(c,'eq_filings'),
                       'listed_issuers':safe_count(c,'eq_issuers'),
                       'daily_price_bars_unadjusted':safe_count(c,'eq_prices'),
                       'signal_accuracy':None,'paper_roi':None,
                       'state':'RESEARCH_DATA_ONLY'}
  out['open_sources']=oracle_open_sources.summary(c)
  out['bookmaker_sources']=oracle_v4_bookies.summary(c)
  out['multi_sport_odds']=oracle_odds_hub.summary(c)
  out['fonbet_direct']=oracle_fonbet_full.status(c) if 'fb_full_runs' in tables else {'state':'NOT_CHECKED','events':0,'factor_quotes_current':0}
  out['sports_brain']=oracle_sports_brain.summary(c)
  out['v5']=oracle_v5.status(c)
  out['fonbet_bridge']={'linked_events':safe_count(c,'fb_bridge_links'),'pre_kickoff_1x2':safe_count(c,'fb_bridge_1x2')}
  out['licensed_fulltext']=oracle_v4_fulltext.summary(c)
  out['causal']={'exposure_edges':safe_count(c,'ci_exposures'),
                 'event_types_registered':safe_count(c,'ci_events'),
                 'untested_hypothesis_paths':safe_count(c,'ci_paths')}
  out['runs']=[{'at':r[0],'module':r[1],'state':r[2]} for r in c.execute('SELECT at,component,state FROM v4_orchestrator_log ORDER BY id DESC LIMIT 12')] if 'v4_orchestrator_log' in tables else []
 finally:c.close()
 return out

if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('command',choices=('once','status','init'));p.add_argument('--db',default=os.getenv('ORACLE_DB','/app/state/oracle.sqlite3'))
 args=p.parse_args()
 c=sqlite3.connect(args.db,timeout=30)
 if args.command in ('once','init'):
  setup(c);print(json.dumps(cycle(c) if args.command=='once' else {'init':'OK'},ensure_ascii=False,indent=2))
 else:print(json.dumps(summary(args.db),ensure_ascii=False,indent=2))
 c.close()
