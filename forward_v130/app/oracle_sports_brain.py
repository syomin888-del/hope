"""ORACLE Sports Brain v4.2: odds coverage + independent evidence-driven research.

This module does not execute wagers, infer private player mental states, claim
unverified news as facts, or train estimates using bookmaker prices.
"""
from __future__ import annotations
import argparse
from collections import defaultdict, Counter
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import time

UTC = dt.timezone.utc
DEFAULT_DB = '/app/state/oracle.sqlite3'
MODEL = 'elo_fixed_v1_UNCALIBRATED'
KNOWN = {'921':'1','922':'X','923':'2'}


def parse(s):
    val=dt.datetime.fromisoformat(str(s).replace('Z','+00:00'))
    if val.tzinfo is None: raise ValueError('Timestamp must include timezone')
    return val.astimezone(UTC)

def stamp(v=None):
    return (parse(v) if isinstance(v,str) else (v or dt.datetime.now(UTC))).astimezone(UTC).isoformat(timespec='seconds')

def exists(c,t):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone())

def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS sb_watchlist (
      fonbet_event_id TEXT PRIMARY KEY, added_at TEXT NOT NULL, note TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sb_event_coverage (
      selection_key TEXT PRIMARY KEY,oracle_event_id TEXT,fonbet_root_event_id TEXT,
      sport TEXT,home TEXT,away TEXT,kickoff_utc TEXT,odds_count INTEGER NOT NULL,
      market_codes INTEGER NOT NULL,root_1x2_count INTEGER NOT NULL,
      status TEXT NOT NULL,checked_at TEXT NOT NULL,last_packet_utc TEXT,details TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sb_dossiers (
      selection_key TEXT PRIMARY KEY,asof_utc TEXT NOT NULL,
      payload TEXT NOT NULL,sha256 TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sb_research_evidence (
      url TEXT NOT NULL,selection_key TEXT NOT NULL,headline TEXT NOT NULL,
      source TEXT NOT NULL,first_seen_utc TEXT NOT NULL,published_utc TEXT,
      evidence_type TEXT NOT NULL,verification TEXT NOT NULL,PRIMARY KEY(selection_key,url));
    CREATE INDEX IF NOT EXISTS sb_evidence_by_event ON sb_research_evidence(selection_key,first_seen_utc);
    CREATE TABLE IF NOT EXISTS sb_independent_forecasts (
      selection_key TEXT NOT NULL,model TEXT NOT NULL,asof_utc TEXT NOT NULL,
      p_home REAL NOT NULL,p_draw REAL NOT NULL,p_away REAL NOT NULL,
      input_hash TEXT NOT NULL,features_json TEXT NOT NULL, status TEXT NOT NULL,
      PRIMARY KEY(selection_key,model));
    CREATE TABLE IF NOT EXISTS sb_runs(id INTEGER PRIMARY KEY,at TEXT NOT NULL,
      state TEXT NOT NULL,events INTEGER NOT NULL,covered INTEGER NOT NULL,
      detail TEXT NOT NULL);
    ''')
    c.commit()


def packet(c):
    if not exists(c,'fb_full_packets'): return None
    row=c.execute('SELECT packet_version,imported_at FROM fb_full_packets ORDER BY imported_at DESC LIMIT 1').fetchone()
    return {'version':row[0],'at':row[1]} if row else None


def chosen(c,now=None,hours=72):
    now=parse(now) if isinstance(now,str) else (now or dt.datetime.now(UTC))
    end=stamp(now+dt.timedelta(hours=hours))
    cur=stamp(now)
    events=[]
    if exists(c,'v4_games'):
        # Any scheduled ORACLE candidate must show odds coverage, even if its model
        # has insufficient historical data to publish a probability.
        events=[dict(zip(('key','oracle_id','sport','league','home','away','kick','fb_id'),x)) for x in c.execute('''
        SELECT g.event_id,g.event_id,g.sport,g.league,g.home,g.away,g.kickoff_utc,
               l.fonbet_event_id FROM v4_games g LEFT JOIN fb_bridge_links l
               ON l.oracle_event_id=g.event_id
        WHERE g.final=0 AND g.kickoff_utc>? AND g.kickoff_utc<=?
        ORDER BY g.kickoff_utc LIMIT 250''',(cur,end))]
    if exists(c,'sb_watchlist') and exists(c,'fb_full_events'):
        for row in c.execute('''SELECT w.fonbet_event_id,e.sport,e.league,e.home,e.away,e.start_utc
            FROM sb_watchlist w JOIN fb_full_events e ON w.fonbet_event_id=e.event_id
            WHERE e.level=1 AND e.start_utc>? AND e.start_utc<=?''',(cur,end)):
            fid,sport,lg,h,a,kick=row
            if fid not in {x['fb_id'] for x in events}:
                events.append(dict(key='fonbet:'+fid,oracle_id=None,sport=sport,league=lg,
                          home=h,away=a,kick=kick,fb_id=fid))
    return events


def coverage(c,event,now,latest,max_age_min=25):
    fb=event['fb_id'];kick=event['kick'];t=stamp(now)
    count=unique=rootc=0
    if fb and exists(c,'fb_full_current'):
        count,unique=c.execute('''SELECT COUNT(*),COUNT(DISTINCT factor_id)
             FROM fb_full_current WHERE root_event_id=? AND active=1''',(fb,)).fetchone()
        rootc=c.execute('''SELECT COUNT(*) FROM fb_full_current WHERE event_id=?
             AND root_event_id=? AND factor_id IN ('921','922','923') AND active=1''',(fb,fb)).fetchone()[0]
    age=None
    if latest:
        age=round((parse(t)-parse(latest['at'])).total_seconds()/60,1)
    if not fb: status='EVENT_NOT_LINKED'
    elif not latest: status='NO_ODDS_SNAPSHOT'
    elif age is None or age>max_age_min or age< -2:status='ODDS_STALE'
    elif not count:status='NO_ACTIVE_MARKETS'
    else:status='OBSERVED_IN_FULL_FEED'
    return {'selection_key':event['key'],'oracle_event_id':event['oracle_id'],
       'fonbet_root_event_id':fb,'sport':event['sport'],'home':event['home'],'away':event['away'],
       'kickoff_utc':kick,'odds_count':count,'market_codes':unique,'root_1x2_count':rootc,
       'status':status,'checked_at':t,'last_packet_utc':latest['at'] if latest else None,
       'details':{'age_minutes':age,'packet_version':latest['version'] if latest else None,
          'scope':'all_numeric_factors_in_public_full_snapshot; special detail pages not verified',
          'unmapped_codes_are_preserved':True}}


def load_history(c,league,at,limit=2500):
    # The as-of cut is NOW, never match kickoff, preventing future result leakage.
    rows=c.execute('''SELECT kickoff_utc,home,away,home_score,away_score FROM v4_games
       WHERE league=? AND final=1 AND kickoff_utc<? AND home_score IS NOT NULL
       AND away_score IS NOT NULL ORDER BY kickoff_utc DESC LIMIT ?''',(league,stamp(at),limit)).fetchall()
    return sorted(rows,key=lambda x:x[0])


def form(rows,team,asof,n=8):
    found=[r for r in rows if team in (r[1],r[2])][-n:]
    won=drawn=losses=gf=ga=0
    for _,home,away,h,a in found:
        mine,other=(h,a) if team==home else (a,h)
        won+=mine>other;drawn+=mine==other;losses+=mine<other
        gf+=mine;ga+=other
    rest=round((parse(asof)-parse(found[-1][0])).total_seconds()/86400,2) if found else None
    return {'sample':len(found),'wins':won,'draws':drawn,'losses':losses,
            'goals_for':gf,'goals_against':ga,'days_since_last_scheduled_kickoff':rest}


def elo_p3(rows,home,away):
    """Fixed independent baseline using only past match scores. Uncalibrated."""
    if len(rows)<100: return None
    rating=defaultdict(lambda:1500.)
    counts=Counter();draws=0
    for kick,h,a,sh,sa in rows:
        ph=1/(1+10**((rating[a]-rating[h]-60)/400))
        actual=1 if sh>sa else 0 if sh<sa else .5
        delta=18*(actual-ph)
        rating[h]+=delta;rating[a]-=delta
        counts[h]+=1;counts[a]+=1;draws+=sh==sa
    if min(counts[home],counts[away])<6:return None
    d=max(.10,min(.38,draws/len(rows)))
    h=1/(1+10**((rating[away]-rating[home]-60)/400))
    p1=(1-d)*h;p2=(1-d)*(1-h)
    return {'1':round(p1,8),'X':round(d,8),'2':round(p2,8),'rating_gap':round(rating[home]-rating[away],2),
            'games_home':counts[home],'games_away':counts[away],'training_games':len(rows)}


def recent_news(c,event,asof,limit=8):
    if not exists(c,'sb_research_evidence'):return []
    return [{'title':r[0],'source':r[1],'url':r[2], 'observed_at':r[3],
            'verification':r[4]} for r in c.execute('''SELECT headline,source,url,first_seen_utc,verification
       FROM sb_research_evidence WHERE selection_key=? AND first_seen_utc<=?
       ORDER BY first_seen_utc DESC LIMIT ?''',(event['key'],stamp(asof),limit))]


def dossier(c,event,at):
    rows=load_history(c,event['league'],at) if event['oracle_id'] and event['sport']=='football' else []
    home=form(rows,event['home'],at);away=form(rows,event['away'],at)
    h2h=[{'at':r[0], 'home':r[1], 'away':r[2], 'score':f'{r[3]}:{r[4]}'} for r in rows
         if {r[1],r[2]}=={event['home'],event['away']}][-5:]
    news=recent_news(c,event,at)
    return {'event_id':event['key'],'at':stamp(at),'kickoff':event['kick'],
      'independent_of_bookmaker_prices':True,
      'historical':{'competition_matches_available':len(rows),'home_form':home,'away_form':away,'head_to_head':h2h},
      'player_level':{'roster':'NOT_VERIFIED','availability':'NOT_VERIFIED',
          'injuries':'NOT_VERIFIED','personal_mood':'UNKNOWN_NO_PRIVATE_INFERENCE'},
      'context':{'weather':'NOT_VERIFIED_NO_STADIUM_GEO','travel':'NOT_VERIFIED',
          'official_lineup':'NOT_VERIFIED','tactics':'NOT_VERIFIED','rest_days_from_matches_only':True},
      'news_candidates':news,
      'warnings':['News headlines are candidate evidence, not verified injury/lineup facts.',
                  'Model cannot assume feelings, health or motivation of individuals.',
                  'Absent evidence must not be guessed.']}


def cycle(c,at=None,hours=72,fetch_news=False):
    now=parse(at) if isinstance(at,str) else (at or dt.datetime.now(UTC))
    setup(c)
    selected=chosen(c,now,hours)
    latest=packet(c)
    info=[];new_forecasts=0
    for event in selected:
        state=coverage(c,event,now,latest)
        info.append(state)
        doc=dossier(c,event,now)
        doc_ser=json.dumps(doc,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        d_hash=hashlib.sha256(doc_ser.encode()).hexdigest()
        with c:
            c.execute('''INSERT INTO sb_event_coverage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(selection_key) DO UPDATE SET
                oracle_event_id=excluded.oracle_event_id,fonbet_root_event_id=excluded.fonbet_root_event_id,
                sport=excluded.sport,home=excluded.home,away=excluded.away,
                kickoff_utc=excluded.kickoff_utc,odds_count=excluded.odds_count,
                market_codes=excluded.market_codes,root_1x2_count=excluded.root_1x2_count,
                status=excluded.status,checked_at=excluded.checked_at,
                last_packet_utc=excluded.last_packet_utc,details=excluded.details''',
                (state['selection_key'],state['oracle_event_id'],state['fonbet_root_event_id'],state['sport'],
                 state['home'],state['away'],state['kickoff_utc'],state['odds_count'],state['market_codes'],
                 state['root_1x2_count'],state['status'],state['checked_at'],state['last_packet_utc'],
                 json.dumps(state['details'],ensure_ascii=False)))
            c.execute('''INSERT INTO sb_dossiers VALUES(?,?,?,?) ON CONFLICT(selection_key)
               DO UPDATE SET asof_utc=excluded.asof_utc,payload=excluded.payload,sha256=excluded.sha256''',
               (event['key'],stamp(now),doc_ser,d_hash))
        # Model inputs *strictly exclude* any bookmaker table, value or EV.
        if event['oracle_id'] and event['sport']=='football' and not exists_forecast(c,event['key']):
            model=elo_p3(load_history(c,event['league'],now),event['home'],event['away'])
            if model:
                features={'games':model['training_games'],'rating_gap':model['rating_gap'],
                          'home_count':model['games_home'],'away_count':model['games_away']}
                input_hash=hashlib.sha256(json.dumps(features,sort_keys=True).encode()).hexdigest()
                with c:
                    cur=c.execute('''INSERT OR IGNORE INTO sb_independent_forecasts VALUES(?,?,?,?,?,?,?,?,?)''',
                        (event['key'],MODEL,stamp(now),model['1'],model['X'],model['2'],input_hash,
                         json.dumps(features), 'PAPER_OPEN_UNCALIBRATED'))
                new_forecasts+=int(cur.rowcount>0)
    if fetch_news:
        # Cap external lookups; not a licence to infer unsupported player facts.
        discover_news(c,selected[:3],now)
    covered=sum(x['status']=='OBSERVED_IN_FULL_FEED' for x in info)
    with c:
        c.execute('INSERT INTO sb_runs(at,state,events,covered,detail) VALUES(?,?,?,?,?)',
                  (stamp(now),'OK',len(selected),covered,json.dumps({'new_independent_forecasts':new_forecasts,'packet':latest},ensure_ascii=False)))
    return {'state':'OK','selected_games':len(selected),'odds_covered':covered,
            'missing_or_stale':len(selected)-covered,'new_independent_forecasts':new_forecasts,
            'source_packet':latest,'no_bets_placed':True}


def exists_forecast(c,key):
    return bool(c.execute('SELECT 1 FROM sb_independent_forecasts WHERE selection_key=? AND model=?',(key,MODEL)).fetchone())


NEWS_GOOGLE = 'https://news.google.com/rss/search?'
NEWS_GDELT = 'https://api.gdeltproject.org/api/v2/doc/doc?'


def news_query(ev):
    """Search the named teams, never infer that the article concerns this match."""
    def quote(v):
        return '"' + str(v).replace('"', '').strip()[:100] + '"'
    return '(' + quote(ev['home']) + ' OR ' + quote(ev['away']) + ')'


def google_news_url(ev):
    q = news_query(ev) + ' when:2d'
    return NEWS_GOOGLE + urllib.parse.urlencode(
        {'q':q,'hl':'de','gl':'DE','ceid':'DE:de'})


def gdelt_news_url(ev):
    return NEWS_GDELT + urllib.parse.urlencode(
        {'query':news_query(ev),'mode':'ArtList','format':'json',
         'maxrecords':'15','timespan':'2d'})


def fetch_bytes(url,approved_prefix,limit=750_000):
    if not url.startswith(approved_prefix):
        raise ValueError('UNAPPROVED_NEWS_HOST')
    req=urllib.request.Request(url,headers={
        'User-Agent':'Mozilla/5.0 (compatible; ORACLE-Sports-Research/4.3; news metadata only)',
        'Accept':'application/rss+xml, application/xml, application/json;q=0.9'})
    with urllib.request.urlopen(req,timeout=14) as r:
        b=r.read(limit+1)
        if len(b)>limit:raise ValueError('NEWS_RESPONSE_TOO_LARGE')
        return b


def request_gdelt(url):
    data=json.loads(fetch_bytes(url,NEWS_GDELT))
    if not isinstance(data,dict):raise ValueError('GDELT_INVALID_RESPONSE')
    return data


def request_google_rss(url):
    root=ET.fromstring(fetch_bytes(url,NEWS_GOOGLE))
    if root.tag!='rss':raise ValueError('GOOGLE_RSS_INVALID_FORMAT')
    articles=[]
    for entry in root.findall('./channel/item')[:20]:
        title=(entry.findtext('title') or '').strip()
        link=(entry.findtext('link') or '').strip()
        source=(entry.findtext('source') or 'Google News').strip()
        published=None
        raw_date=(entry.findtext('pubDate') or '').strip()
        if raw_date:
            try:
                d=parsedate_to_datetime(raw_date)
                if d.tzinfo: published=d.astimezone(UTC).isoformat(timespec='seconds')
            except (TypeError,ValueError,OverflowError):pass
        articles.append({'url':link,'title':title,'domain':source,'published_utc':published})
    return {'articles':articles}


def _news_error(error):
    """No full URLs, secrets or stack traces in the compact scheduler log."""
    if isinstance(error,urllib.error.HTTPError):return 'HTTP_'+str(error.code)
    if isinstance(error,urllib.error.URLError):return 'NETWORK_'+type(error.reason).__name__
    return type(error).__name__


def _insert_news(c,ev,articles,at,provider):
    saved=0;valid=0
    for item in articles[:20]:
        if not isinstance(item,dict):continue
        url=str(item.get('url') or '').strip()
        headline=str(item.get('title') or '').strip()
        if not url.startswith('https://') or not headline:continue
        if len(url)>1800:continue
        published=item.get('published_utc')
        if not published:
            raw=str(item.get('seendate') or '')
            if len(raw)>=15:
                try:published=dt.datetime.strptime(raw[:15],'%Y%m%dT%H%M%S').replace(tzinfo=UTC).isoformat(timespec='seconds')
                except ValueError:pass
        try:
            if published and parse(published)>at+dt.timedelta(minutes=1):continue
            if published and parse(published)<at-dt.timedelta(days=3):continue
        except ValueError:continue
        valid+=1
        source=(str(item.get('domain') or provider))[:120]
        with c:
            result=c.execute("INSERT OR IGNORE INTO sb_research_evidence VALUES(?,?,?,?,?,?,?,?)",
                (url,ev['key'],headline[:350],source,stamp(at),published,
                 'NEWS_SEARCH_RESULT','UNVERIFIED_NEWS_CANDIDATE'))
        saved+=int(result.rowcount>0)
    return saved,valid


def discover_news(c,events,at,loader=None,rss_loader=None,pause=None):
    """Metadata-only news discovery for all selected games, without fabricated claims.

    Google News RSS is primary; GDELT is a fallback for empty/unavailable RSS.
    HTTP failures are counted and classified rather than silently swallowed.
    External requests are paced. The scheduler caps the number of games.
    """
    at=parse(at) if isinstance(at,str) else at
    injected_gdelt_only = loader is not None and rss_loader is None
    loader=loader or request_gdelt
    rss_loader=rss_loader or ((lambda url: {'articles': []}) if injected_gdelt_only else request_google_rss)
    pause=pause if pause is not None else ((lambda seconds: None) if injected_gdelt_only else time.sleep)
    saved=0;failures=0;covered=0;attempted=0;unsupported=0
    samples=[];sources={'google_rss':{'requests':0,'errors':0,'valid_articles':0},
                        'gdelt':{'requests':0,'errors':0,'valid_articles':0}}
    gdelt_rate_limited=False
    for ev in events:
        if ev['sport']!='football' or not ev['oracle_id']:
            unsupported+=1;continue
        attempted+=1
        accepted=0;success=False
        # One primary query per match, with conservative pacing on shared IPs.
        if attempted>1:pause(1.25)
        sources['google_rss']['requests']+=1
        try:
            data=rss_loader(google_news_url(ev))
            articles=data.get('articles',[]) if isinstance(data,dict) else []
            add,valid=_insert_news(c,ev,articles,at,'GoogleNewsRSS')
            saved+=add;accepted+=valid
            sources['google_rss']['valid_articles']+=valid
            success=True
        except Exception as exc:
            sources['google_rss']['errors']+=1
            if len(samples)<6:samples.append({'event':ev['key'],'provider':'google_rss','error':_news_error(exc)})
        if accepted==0 and not gdelt_rate_limited:
            # Fallback; a 429 blocks further GDELT calls in THIS cycle.
            pause(5.1)
            sources['gdelt']['requests']+=1
            try:
                data=loader(gdelt_news_url(ev))
                articles=data.get('articles',[]) if isinstance(data,dict) else []
                add,valid=_insert_news(c,ev,articles,at,'GDELT')
                saved+=add;accepted+=valid
                sources['gdelt']['valid_articles']+=valid
                success=True
            except Exception as exc:
                sources['gdelt']['errors']+=1
                if isinstance(exc,urllib.error.HTTPError) and exc.code==429:
                    gdelt_rate_limited=True
                if len(samples)<6:samples.append({'event':ev['key'],'provider':'gdelt','error':_news_error(exc)})
        if not success:failures+=1
        if accepted:covered+=1
    source_errors=sum(provider['errors'] for provider in sources.values())
    result={
        'state':'PARTIAL' if source_errors or failures else 'OK',
        'events_checked':attempted,'events_with_news':covered,
        'news_items_added':saved,'queries_failed':source_errors,
        'events_unreachable':failures,
        'unsupported_events':unsupported,'providers':sources,
        'error_samples':samples,'gdelt_rate_limited':gdelt_rate_limited,
        'no_injury_or_mood_claims_inferred':True}
    return result


def score(c):
    """Outcome-based, chronologically verified scores; book odds excluded."""
    if not exists(c,'v4_games') or not exists(c,'v4_sport_preds'):
        return {'n':0,'brier':None,'logloss':None,'warning':'NO_SCORE_TABLES'}
    q=c.execute('''SELECT g.event_id,g.kickoff_utc,g.home_score,g.away_score,
       p.selection,p.prob,p.issued_at
       FROM v4_games g JOIN v4_sport_preds p ON p.event_id=g.event_id
       WHERE g.final=1 AND p.market='1X2' AND p.status='PAPER_SETTLED' ''')
    by=defaultdict(dict);outcomes={}
    for eid,kick,h,a,sel,p,issued in q:
        if h is None or a is None or not issued or parse(issued)>=parse(kick):continue
        by[eid][sel]=p;outcomes[eid]='1' if h>a else '2' if a>h else 'X'
    scores=[];losses=[]
    for eid,p in by.items():
        if set(p)!={'1','X','2'} or any(not 0<=v<=1 for v in p.values()) or abs(sum(p.values())-1)>.002:continue
        y=outcomes[eid]
        scores.append(sum((p[s]-(s==y))**2 for s in ('1','X','2')))
        losses.append(-math.log(max(1e-12,p[y])))
    n=len(scores)
    return {'n':n,'brier_1x2':round(sum(scores)/n,5) if n else None,
      'logloss_1x2':round(sum(losses)/n,5) if n else None,
      'sufficient_for_claim':False,'note':'Outcome-only audit; market prices are not training targets'}


def score_independent(c):
    # Only genuinely prospective model snapshots; never benchmark on bookmaker prices.
    if not exists(c,'sb_independent_forecasts') or not exists(c,'v4_games'):
        return {'n':0,'brier_1x2':None,'logloss_1x2':None}
    rows=c.execute('''SELECT p.asof_utc,g.kickoff_utc,g.home_score,g.away_score,
      p.p_home,p.p_draw,p.p_away
      FROM sb_independent_forecasts p JOIN v4_games g ON g.event_id=p.selection_key
      WHERE g.final=1 AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL''').fetchall()
    brier=[];loss=[]
    for issued,kick,h,a,p1,px,p2 in rows:
        if not issued or parse(issued)>=parse(kick):continue
        probs={'1':p1,'X':px,'2':p2}
        if any(not 0<=v<=1 for v in probs.values()) or abs(sum(probs.values())-1)>.002:continue
        outcome='1' if h>a else '2' if a>h else 'X'
        brier.append(sum((probs[s]-(outcome==s))**2 for s in ('1','X','2')))
        loss.append(-math.log(max(1e-12,probs[outcome])))
    n=len(brier)
    return {'n':n,'brier_1x2':round(sum(brier)/n,5) if n else None,
      'logloss_1x2':round(sum(loss)/n,5) if n else None,
      'evidence_of_superiority':False,'note':'Independent paper predictions, not bookmaker price fitting'}


def summary(c,at=None,limit=35):
    if not exists(c,'sb_event_coverage'):
        return {'state':'NOT_INSTALLED','selected_games':0}
    now=stamp(at);events=[]
    for r in c.execute('''SELECT selection_key,home,away,sport,kickoff_utc,status,
      odds_count,market_codes,root_1x2_count,last_packet_utc,fonbet_root_event_id
      FROM sb_event_coverage WHERE kickoff_utc>=? ORDER BY kickoff_utc LIMIT ?''',(now,limit)):
        events.append(dict(zip(('id','home','away','sport','kickoff_utc','status',
                 'odds_count','market_codes','root_1x2_count','last_packet_utc','fonbet_id'),r)))
    fresh_packet=packet(c)
    for item in events:
        if item['status']=='OBSERVED_IN_FULL_FEED':
            age=(parse(now)-parse(item['last_packet_utc'])).total_seconds()/60 if item['last_packet_utc'] else 9999
            since_check=(parse(now)-parse(c.execute('SELECT checked_at FROM sb_event_coverage WHERE selection_key=?',(item['id'],)).fetchone()[0])).total_seconds()/60
            if age>25 or since_check>40 or age < -2:
                item['status']='ODDS_STALE'
    counts=Counter(x['status'] for x in events)
    return {'state':'RESEARCH_ONLY','selected_games':len(events),'covered':counts['OBSERVED_IN_FULL_FEED'],
      'missing_or_stale':len(events)-counts['OBSERVED_IN_FULL_FEED'],
      'research_dossiers':c.execute('SELECT COUNT(*) FROM sb_dossiers').fetchone()[0],
      'independent_elo_forecasts':c.execute('SELECT COUNT(*) FROM sb_independent_forecasts').fetchone()[0],
      'verified_player_availability':0,'evidence_candidates':c.execute('SELECT COUNT(*) FROM sb_research_evidence').fetchone()[0],
      'model_outcomes':score(c),'elo_outcomes':score_independent(c),'events':events,'note':'All observed active codes for selected Fonbet root events, including nested events; no guaranteed bookmaker market completeness.'}


def odds(c,key,limit=35,offset=0):
    event=c.execute('SELECT fonbet_root_event_id FROM sb_event_coverage WHERE selection_key=?',(key,)).fetchone()
    if not event or not event[0]:return {'state':'NO_LINK','event':key,'markets':[]}
    root=event[0]
    data=c.execute('''SELECT q.event_id,q.factor_id,q.point,q.decimal_price,q.last_seen,e.level,e.source_json
      FROM fb_full_current q LEFT JOIN fb_full_events e ON e.event_id=q.event_id
      WHERE q.active=1 AND q.root_event_id=? ORDER BY q.event_id,q.factor_id LIMIT ? OFFSET ?''',
      (root,min(limit,100),offset)).fetchall()
    markets=[]
    for eid,f,pt,price,seen,level,raw in data:
        try:description=json.loads(raw or '{}').get('name')
        except (ValueError,TypeError):description=None
        markets.append({'event_id':eid,'level':level,'event_name':description,'factor_id':f,
                        'verified_meaning':KNOWN.get(f) if str(eid)==str(root) else None,
                        'parameters':json.loads(pt or '{}'),'price':price,'last_seen':seen})
    return {'state':'OK','event':key,'root_event_id':root,'items':markets,
            'note':'Unknown factor codes remain raw, not interpreted as invented markets'}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=('once','status','dossier','odds','watch','news'))
    p.add_argument('--db',default=DEFAULT_DB);p.add_argument('--event');p.add_argument('--hours',type=int,default=72)
    p.add_argument('--limit',type=int,default=30);p.add_argument('--offset',type=int,default=0)
    p.add_argument('--fonbet-id');p.add_argument('--at')
    p.add_argument('--news-events',type=int,default=15)
    a=p.parse_args()
    if not 1<=a.hours<=336 or not 1<=a.limit<=100 or a.offset<0 or not 1<=a.news_events<=15:raise SystemExit('Invalid range')
    with sqlite3.connect(a.db,timeout=40) as c:
        c.execute('PRAGMA busy_timeout=40000')
        if a.action=='once':res=cycle(c,at=a.at,hours=a.hours)
        elif a.action=='news':
            setup(c);res=discover_news(c,chosen(c,a.at,a.hours)[:a.news_events],parse(a.at) if a.at else dt.datetime.now(UTC))
        elif a.action=='watch':
            setup(c)
            if not a.fonbet_id or not a.fonbet_id.isdigit():raise SystemExit('Use --fonbet-id <numeric root event ID>')
            e=c.execute('SELECT event_id FROM fb_full_events WHERE event_id=? AND level=1',(a.fonbet_id,)).fetchone()
            if not e:raise SystemExit('Unknown root event')
            with c:c.execute('INSERT OR IGNORE INTO sb_watchlist VALUES(?,?,?)',(a.fonbet_id,stamp(),'manual verified event ID'))
            res={'watching':a.fonbet_id}
        elif a.action=='status':res=summary(c)
        elif a.action=='odds':res=odds(c,a.event or '',a.limit,a.offset)
        else:
            row=c.execute('SELECT payload FROM sb_dossiers WHERE selection_key=?',(a.event or '',)).fetchone()
            res=json.loads(row[0]) if row else {'state':'NO_DOSSIER','event':a.event}
    print(json.dumps(res,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
