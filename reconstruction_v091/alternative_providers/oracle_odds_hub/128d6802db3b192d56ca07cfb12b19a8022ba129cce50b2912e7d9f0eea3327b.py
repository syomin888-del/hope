"""ORACLE Odds Hub 4.4. Guest bookmaker quotations from two documented sources.

OddsLink: main pre-match h2h/spreads/totals across sportsbook brands (American odds).
MoviOdds: one bookmaker (bet365), many sport markets, decimal odds.
All raw/source-labeled price changes retained; conservative 1X2 bridge into v4_book_odds.
No authenticated extraction, prediction-market substitution, or real bet execution.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import unicodedata
import urllib.error
import urllib.request

UTC=dt.timezone.utc
SPORTS_ODDSLINK=('soccer','nba','nhl','tennis','nfl','mlb','wnba','mma','ncaaf')
SPORTS_MOVI=('soccer','tennis','basketball','ice_hockey')
BASE_ODDSLINK='https://oddslink.app/v1/public/board/'
BASE_MOVI='https://moviodds.com/odds?sport='
UA='ORACLE-Research-OddsHub/4.4 (public data, no wagers)'

ALIASES={'fc augsburg':'augsburg','1 fc union berlin':'union berlin','fc union berlin':'union berlin',
'1 fsv mainz 05':'mainz','fsv mainz 05':'mainz','mainz 05':'mainz',
'sv werder bremen':'werder bremen','bvb dortmund':'dortmund','borussia dortmund':'dortmund',
'eintracht frankfurt':'frankfurt','sc freiburg':'freiburg','fc bayern munchen':'bayern munich',
'bayern munchen':'bayern munich','vfb stuttgart':'stuttgart','borussia monchengladbach':'monchengladbach',
'1 fc magdeburg':'magdeburg','fc magdeburg':'magdeburg','karlsruher sc':'karlsruher',
'hertha bsc':'hertha berlin','dsc arminia bielefeld':'arminia bielefeld',
'1 fc nurnberg':'nurnberg','1 fc kaiserslautern':'kaiserslautern',
'tsg hoffenheim':'hoffenheim','bayer 04 leverkusen':'leverkusen',
'eintracht braunschweig':'braunschweig','hannover 96':'hannover','holstein kiel':'holstein kiel',
'dynamo dresden':'dynamo dresden'}

def utc(value):
    if isinstance(value,dt.datetime): d=value
    else:d=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('Time zone is mandatory')
    return d.astimezone(UTC)

def stamp(v=None): return utc(v or dt.datetime.now(UTC)).isoformat(timespec='seconds')

def norm(s):
    s=unicodedata.normalize('NFKD',str(s)).casefold().replace('ß','ss')
    s=''.join(c for c in s if not unicodedata.combining(c))
    s=re.sub('[^a-z0-9]+',' ',s).strip()
    return ALIASES.get(s,s)

def dec_american(value):
    if type(value)!=int or not (100<=abs(value)<=10000):raise ValueError('Invalid American odds')
    result=(1+value/100) if value>0 else (1+100/abs(value))
    if not 1.01<=result<=101:raise ValueError('Invalid decimal odds')
    return round(result,6)

def dec_price(value):
    if type(value) not in (float,int) or not math.isfinite(value) or not 1.01<=value<=1001:
        raise ValueError('Invalid decimal odds')
    return float(value)

def fetch_json(url,max_bytes=22_000_000):
    if not (url.startswith(BASE_ODDSLINK) or url.startswith(BASE_MOVI)):
        raise ValueError('Unexpected domain/endpoint')
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=12) as resp:
        if resp.status != 200:raise ValueError('Non-200 response')
        body=resp.read(max_bytes+1)
        if len(body)>max_bytes:raise ValueError('Response exceeds configured memory limit')
    return json.loads(body)

def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS odds_hub_events(
       provider TEXT NOT NULL, source_event_id TEXT NOT NULL,sport TEXT NOT NULL,league TEXT,
       kickoff_utc TEXT NOT NULL,home TEXT NOT NULL,away TEXT NOT NULL,in_play INTEGER NOT NULL,
       last_seen TEXT NOT NULL,PRIMARY KEY(provider,source_event_id));
    CREATE INDEX IF NOT EXISTS idx_oh_events ON odds_hub_events(sport,kickoff_utc);
    CREATE TABLE IF NOT EXISTS odds_hub_quotes(
       quote_id TEXT PRIMARY KEY,provider TEXT NOT NULL,source_event_id TEXT NOT NULL,
       bookmaker TEXT NOT NULL,market TEXT NOT NULL,period TEXT NOT NULL,
       selection TEXT NOT NULL,point TEXT NOT NULL,price REAL NOT NULL,
       observed_at TEXT NOT NULL,price_updated_at TEXT NOT NULL,polled_at TEXT NOT NULL,
       in_play INTEGER NOT NULL,source_url TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_oh_quotes ON odds_hub_quotes(provider,source_event_id,market,bookmaker);
    CREATE TABLE IF NOT EXISTS odds_hub_runs(
       id INTEGER PRIMARY KEY,checked_at TEXT NOT NULL,provider TEXT NOT NULL,sport TEXT NOT NULL,
       state TEXT NOT NULL,events_seen INTEGER NOT NULL,quotes_saved INTEGER NOT NULL,
       matches_linked INTEGER NOT NULL,detail TEXT NOT NULL);
    ''');c.commit()

def find_existing_soccer(c,home,away,kickoff):
    try:
        opts=c.execute('''SELECT event_id,home,away,kickoff_utc FROM v4_games
        WHERE sport='football' AND final=0 AND kickoff_utc BETWEEN ? AND ?''',
        (stamp(utc(kickoff)-dt.timedelta(minutes=16)),stamp(utc(kickoff)+dt.timedelta(minutes=16)))).fetchall()
    except sqlite3.OperationalError:return None
    hit=[eid for eid,h,a,k in opts if norm(home)==norm(h) and norm(away)==norm(a)
         and (abs((utc(kickoff)-utc(k)).total_seconds())<=15*60)]
    return hit[0] if len(hit)==1 else None

def persist_event(c,pid,eid,sport,league,kick,home,away,inplay,now):
    if not eid or not home or not away or norm(home)==norm(away):return False
    c.execute('''INSERT INTO odds_hub_events VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,source_event_id)
    DO UPDATE SET sport=excluded.sport,league=excluded.league,kickoff_utc=excluded.kickoff_utc,
     home=excluded.home,away=excluded.away,in_play=excluded.in_play,last_seen=excluded.last_seen''',
     (pid,str(eid)[:120],sport,str(league or '')[:150],stamp(kick),home,away,int(inplay),stamp(now)))
    return True

def persist_quote(c,pid,eid,book,market,period,selection,point,price,now,updated,polled,inplay,url):
    qid=hashlib.sha256(json.dumps([pid,str(eid),book,market,period,selection,str(point),price,stamp(updated)],
                                    ensure_ascii=False).encode()).hexdigest()
    r=c.execute('''INSERT OR IGNORE INTO odds_hub_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
      (qid,pid,str(eid),book,str(market),str(period),str(selection),str(point),price,
       stamp(now),stamp(updated),stamp(polled),int(inplay),url))
    return max(0,r.rowcount)

def bridge_1x2(c,sport,eid,home,away,kick,book,prices,now,updated,src,pid):
    if sport!='soccer' or len(prices)!=3 or set(prices)!={'1','X','2'} or utc(kick)<=now:return 0
    old=find_existing_soccer(c,home,away,kick)
    if old is None:return 0
    if (now-utc(updated)).total_seconds()>1800 or utc(updated)>now+dt.timedelta(minutes=2):return 0
    linked=0
    for sel,val in prices.items():
        try:
            r=c.execute('''INSERT OR IGNORE INTO v4_book_odds(event_id,bookmaker,selection,price,
                captured_at,book_last_update,source,origin_id) VALUES(?,?,?,?,?,?,?,?)''',
                (old,book,sel,val,stamp(now),stamp(updated),src,str(eid)))
            linked+=max(r.rowcount,0)
        except sqlite3.OperationalError:break
    return linked

def parse_oddslink(c,sport,data,now):
    if not isinstance(data,dict) or not isinstance(data.get('fixtures'),list) or data.get('sport_key')!=sport:
        raise ValueError('OddsLink unexpected schema')
    generated=utc(data['generated_at'])
    if not -120<=(now-generated).total_seconds()<=900:raise ValueError('Outdated OddsLink board')
    count=saved=linked=0
    for game in data['fixtures'][:60]:
        if not isinstance(game,dict):continue
        try:
            kick=utc(game['commence_time']);eid=str(game['id'])
            home=str(game['home_team']).strip();away=str(game['away_team']).strip()
            inplay=bool(game.get('in_play',False)) or kick<=now
            if not persist_event(c,'oddslink',eid,sport,game.get('sport_title',''),kick,home,away,inplay,now):continue
            count+=1
            for book in (game.get('bookmakers') or [])[:40]:
                if not isinstance(book,dict) or book.get('kind')!='sportsbook':continue
                bookkey=str(book.get('key') or book.get('title') or '').lower().strip()[:90]
                if not bookkey:continue
                try:updated=utc(book['last_update'])
                except (KeyError,TypeError,ValueError):continue
                if updated>now+dt.timedelta(minutes=2):continue
                # Reject stale source; odds could remain unchanged, but we require book-confirmed freshness.
                if (now-updated).total_seconds()>1800:continue
                rawmarkets=book.get('markets') or {}
                if not isinstance(rawmarkets,dict):continue
                outcome3={}
                for family in ('h2h','spreads','totals'):
                    outcomes=rawmarkets.get(family,[])
                    if isinstance(outcomes,dict):outcomes=outcomes.get('outcomes',[])
                    if not isinstance(outcomes,list):continue
                    for item in outcomes[:28]:
                        if not isinstance(item,dict):continue
                        try:price=dec_american(item['price'])
                        except (KeyError,ValueError,TypeError):continue
                        sel=str(item.get('name','')).strip()[:100]
                        point=item.get('point')
                        point='' if point is None else str(point)
                        saved+=persist_quote(c,'oddslink',eid,bookkey,family,'FT',sel,point,price,
                                             now,updated,updated,inplay,BASE_ODDSLINK+sport)
                        if family=='h2h' and not inplay and not point:
                            x=norm(sel)
                            if x==norm(home):outcome3['1']=price
                            elif x==norm(away):outcome3['2']=price
                            elif x in ('draw','tie','x'):outcome3['X']=price
                if not inplay:
                    linked+=bridge_1x2(c,sport,eid,home,away,kick,bookkey,outcome3,now,updated,
                                       'ODDSLINK_PUBLIC_BOARD','oddslink')
        except (ValueError,TypeError,KeyError):continue
    return {'events_seen':count,'quotes_saved':saved,'matches_linked':linked}

def parse_movi(c,sport,data,now):
    if not isinstance(data,dict) or not isinstance(data.get('matches'),list):raise ValueError('MoviOdds unexpected schema')
    sv=utc(data.get('server_time',stamp(now)))
    if (now-sv).total_seconds()>900 or (sv-now).total_seconds()>120:raise ValueError('MoviOdds stale server clock')
    count=saved=linked=0
    for game in data['matches'][:400]:
        if not isinstance(game,dict):continue
        try:
            eid=str(game['id']);kick=utc(game['kickoff_utc'])
            home=str(game['home']).strip();away=str(game['away']).strip()
            inplay=bool(game.get('in_play')) or kick<=now
            if not persist_event(c,'moviodds',eid,sport,game.get('league'),kick,home,away,inplay,now):continue
            count+=1;three={};three_updated=[]
            polled=utc(game['polled_at'])
            if (now-polled).total_seconds()>900 or (polled-now).total_seconds()>120:continue
            for q in (game.get('quotes') or [])[:1500]:
                if not isinstance(q,dict) or q.get('suspended') is True:continue
                try:price=dec_price(q.get('odds_dec'));updated=utc(q['ts'])
                except (KeyError,ValueError,TypeError):continue
                if (updated-now).total_seconds()>120:continue
                group=str(q.get('group_id') or '')
                market=str(q.get('group_name') or group)[:100]
                selection=str(q.get('n2') or q.get('label') or q.get('side') or q.get('sel_id') or '')[:100]
                point=str(q.get('line') if q.get('line') is not None else q.get('handicap') or '')
                period='LIVE' if inplay else 'FT'
                saved+=persist_quote(c,'moviodds',eid,'bet365',market,period,selection,point,price,now,
                                     updated,polled,inplay,BASE_MOVI+sport)
                if sport=='soccer' and not inplay and group=='40' and str(q.get('n2')) in ('1','X','2'):
                    three[str(q['n2'])]=price;three_updated.append(updated)
            if len(three)==3 and three_updated:
                linked+=bridge_1x2(c,sport,eid,home,away,kick,'bet365',three,now,
                                   polled,'MOVIODDS_BET365_PUBLIC','moviodds')
        except (KeyError,ValueError,TypeError):continue
    return {'events_seen':count,'quotes_saved':saved,'matches_linked':linked}

def default_sports(name):
    cfg=os.getenv('ORACLE_ODDS_HUB_SPORTS','soccer,nba,nhl,tennis,nfl,mlb,wnba,mma,ncaaf') if name=='oddslink' else os.getenv('ORACLE_MOVI_SPORTS','soccer,tennis,basketball,ice_hockey')
    allowed=SPORTS_ODDSLINK if name=='oddslink' else SPORTS_MOVI
    return [x for x in allowed if x in {v.strip().lower() for v in cfg.split(',')}]

def poll(c,now=None,loader=fetch_json,providers=('oddslink','moviodds'),sports=None):
    setup(c);now=utc(now or dt.datetime.now(UTC));results=[]
    for provider in providers:
        if provider not in ('oddslink','moviodds'):continue
        provider_down=False
        choices=sports if sports is not None else default_sports(provider)
        for sport in choices:
            if provider_down:break
            if sport not in (SPORTS_ODDSLINK if provider=='oddslink' else SPORTS_MOVI):continue
            url=(BASE_ODDSLINK if provider=='oddslink' else BASE_MOVI)+sport
            try:
                data=loader(url)
                with c:
                    v=parse_oddslink(c,sport,data,now) if provider=='oddslink' else parse_movi(c,sport,data,now)
                state='OK' if v['quotes_saved'] or v['events_seen'] else 'EMPTY'
                detail=''
            except urllib.error.HTTPError as e:
                if e.code in (429,500,502,503,504):provider_down=True
                v={'events_seen':0,'quotes_saved':0,'matches_linked':0}
                state='RATE_LIMITED' if e.code==429 else 'HTTP_'+str(e.code)
                detail='HTTP '+str(e.code)
            except Exception as e:
                if isinstance(e,(urllib.error.URLError,TimeoutError,ConnectionError)):provider_down=True
                v={'events_seen':0,'quotes_saved':0,'matches_linked':0}
                state='ERROR';detail=type(e).__name__+': '+str(e)[:130]
            with c:c.execute('''INSERT INTO odds_hub_runs(checked_at,provider,sport,state,events_seen,
                    quotes_saved,matches_linked,detail) VALUES(?,?,?,?,?,?,?,?)''',
                    (stamp(now),provider,sport,state,v['events_seen'],v['quotes_saved'],v['matches_linked'],detail))
            results.append({'provider':provider,'sport':sport,'state':state,**v,'error':detail or None})
    return {'state':'OK' if any(x['state']=='OK' for x in results) else 'NO_ACTIVE_FEEDS',
            'runs':results,'total_events_seen':sum(x['events_seen'] for x in results),
            'new_prices':sum(x['quotes_saved'] for x in results),
            'odds_matched_existing_football':sum(x['matches_linked'] for x in results)}

def summary(c):
    existing={x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'odds_hub_runs' not in existing:return {'state':'NOT_INITIALIZED','providers':[]}
    last=c.execute('''SELECT r.provider,r.sport,r.state,r.checked_at,r.events_seen,r.quotes_saved,r.detail
          FROM odds_hub_runs r WHERE r.id=(SELECT MAX(r2.id) FROM odds_hub_runs r2
          WHERE r2.provider=r.provider AND r2.sport=r.sport) ORDER BY r.provider,r.sport''').fetchall()
    counts=c.execute('SELECT COUNT(*),COUNT(DISTINCT provider||":"||source_event_id),COUNT(DISTINCT bookmaker) FROM odds_hub_quotes').fetchone()
    recent=c.execute('''SELECT e.sport,e.kickoff_utc,e.home,e.away,q.bookmaker,q.market,q.selection,q.price,q.observed_at
       FROM odds_hub_events e JOIN odds_hub_quotes q ON e.provider=q.provider AND e.source_event_id=q.source_event_id
       WHERE e.kickoff_utc>? AND q.in_play=0 ORDER BY q.observed_at DESC LIMIT 15''',(stamp(),)).fetchall()
    return {'state':'OBSERVED' if counts[0] else 'NO_PRICES_YET',
      'stored_quote_versions':counts[0],'quoted_events':counts[1],'bookmakers_observed':counts[2],
      'sources':[{'provider':p,'sport':s,'state':st,'checked_at':at,'events_last':ne,'quotes_last':nq,'error':er or None}
                 for p,s,st,at,ne,nq,er in last],
      'sample_prices':[{'sport':sp,'kickoff_utc':ki,'home':h,'away':a,'bookmaker':b,'market':m,
                        'selection':sl,'price':pr,'seen':at} for sp,ki,h,a,b,m,sl,pr,at in recent]}

def probe(loader=fetch_json):
    results=[]
    for p,s in [('oddslink','soccer'),('oddslink','nba'),('moviodds','soccer'),('moviodds','tennis')]:
        url=(BASE_ODDSLINK if p=='oddslink' else BASE_MOVI)+s
        try:
            data=loader(url)
            games=data.get('fixtures') if p=='oddslink' else data.get('matches')
            if not isinstance(games,list):raise ValueError('Unexpected schema')
            books={str(b.get('key') or b.get('title')) for g in games[:60] for b in g.get('bookmakers',[]) if isinstance(b,dict) and b.get('kind')=='sportsbook'} if p=='oddslink' else {'bet365'}
            results.append({'provider':p,'sport':s,'state':'OK','events':len(games),'bookmakers':sorted(books)[:25]})
        except Exception as e:results.append({'provider':p,'sport':s,'state':'ERROR','detail':str(e)[:180]})
    return results

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=('probe','once','status'))
    p.add_argument('--db',default=os.getenv('ORACLE_DB','/app/state/oracle.sqlite3'))
    a=p.parse_args()
    if a.command=='probe':print(json.dumps(probe(),ensure_ascii=False,indent=2))
    else:
        c=sqlite3.connect(a.db,timeout=30)
        print(json.dumps(poll(c) if a.command=='once' else summary(c),ensure_ascii=False,indent=2))
        c.close()
