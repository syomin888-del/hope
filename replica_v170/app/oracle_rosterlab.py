#!/usr/bin/env python3
"""Bounded, attributed research importer for StatsBomb public OPEN-DATA subset.

Not a comprehensive five-year professional lineup database. Never uses match
results or post-game player information to issue a historical pre-game forecast.
No paywall bypass, credentials or unauthorized scraping.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import sqlite3
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
import collect

UTC=dt.timezone.utc
ROOT='https://raw.githubusercontent.com/hudl/open-data/master/data/'
INTERVAL=max(900,int(os.getenv('ORACLE_ROSTER_INTERVAL','3600')))
USER_AGENT='OracleSportsOpenResearch/2.5 (+https://github.com/hudl/open-data)'

def now():return dt.datetime.now(UTC).isoformat(timespec='seconds')
def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS sr_sources(
      source_id TEXT PRIMARY KEY, last_attempt TEXT, last_success TEXT,
      error TEXT, next_attempt TEXT, item_count INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS sr_matches(
      match_id INTEGER PRIMARY KEY, competition_id INTEGER NOT NULL, season_id INTEGER NOT NULL,
      competition TEXT, season TEXT, match_day TEXT, home TEXT, away TEXT,
      home_score INTEGER, away_score INTEGER, source_url TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sr_lineups(
      match_id INTEGER NOT NULL, team_id INTEGER NOT NULL, team TEXT NOT NULL,
      player_id INTEGER NOT NULL, player TEXT NOT NULL, jersey INTEGER,
      position_text TEXT, source_url TEXT NOT NULL,
      PRIMARY KEY(match_id,team_id,player_id));
    CREATE TABLE IF NOT EXISTS sr_cards(
      match_id INTEGER NOT NULL, event_id TEXT NOT NULL, minute INTEGER,
      team TEXT, player TEXT, card TEXT,
      PRIMARY KEY(match_id,event_id));
    CREATE TABLE IF NOT EXISTS sr_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,
      at TEXT NOT NULL, detail TEXT NOT NULL);
    ''');c.commit()

def get_json(relative,loader=None):
    url=ROOT+relative
    if loader is not None:return loader(url)
    with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':USER_AGENT}),timeout=20) as f:
        b=f.read(4_000_001)
    if len(b)>4_000_000:raise ValueError('SOURCE_OVERSIZED')
    return json.loads(b)

def due(c,key,at):
    row=c.execute('SELECT next_attempt FROM sr_sources WHERE source_id=?',(key,)).fetchone()
    return not row or not row[0] or row[0]<=at

def mark(c,key,at,n=0,error=None,delay_hours=24):
    nxt=(dt.datetime.fromisoformat(at)+dt.timedelta(hours=delay_hours)).isoformat(timespec='seconds')
    with c:c.execute('''INSERT INTO sr_sources(source_id,last_attempt,last_success,error,next_attempt,item_count)
        VALUES(?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
        last_attempt=excluded.last_attempt,
        last_success=CASE WHEN excluded.error IS NULL THEN excluded.last_success ELSE sr_sources.last_success END,
        error=excluded.error,next_attempt=excluded.next_attempt,item_count=excluded.item_count''',
        (key,at,at if error is None else None,error,nxt,n))

def available_comps(c,at,loader=None):
    if not due(c,'catalog',at):return {'skipped':True}
    try:
        comp=get_json('competitions.json',loader)
        if not isinstance(comp,list):raise ValueError('Malformed competitions')
        recent=[]
        for item in comp:
            if not isinstance(item,dict):continue
            sid=item.get('season_id');cid=item.get('competition_id')
            name=item.get('season_name','')
            try:
                if int(str(name)[:4])<2020:continue
                if not isinstance(cid,int) or not isinstance(sid,int):continue
            except ValueError:continue
            recent.append((cid,sid))
        # Persist catalog in sr_sources due table; use explicit catalog key in next pass.
        with c:
            for cid,sid in recent:
                c.execute('INSERT OR IGNORE INTO sr_sources(source_id,next_attempt) VALUES(?,?)',(f'comp:{cid}:{sid}',at))
        mark(c,'catalog',at,len(recent),delay_hours=72)
        return {'catalog_seasons':len(recent)}
    except Exception as e:
        mark(c,'catalog',at,error=type(e).__name__+': '+str(e)[:140],delay_hours=3)
        return {'error':str(e)[:150]}

def match_list(c,at,loader=None,max_comps=2):
    keys=[r[0] for r in c.execute("SELECT source_id FROM sr_sources WHERE source_id LIKE 'comp:%' AND (next_attempt IS NULL OR next_attempt<=?) ORDER BY source_id LIMIT ?",(at,max_comps))]
    out={'attempted':0,'saved':0,'failed':0}
    for key in keys:
        _,cid,sid=key.split(':');rel=f'matches/{cid}/{sid}.json'
        out['attempted']+=1
        try:
            games=get_json(rel,loader)
            if not isinstance(games,list):raise ValueError('Malformed match list')
            saved=0
            with c:
                for x in games:
                    try:
                        mid=int(x['match_id']);day=str(x.get('match_date',''))[:10]
                        dt.date.fromisoformat(day)
                        hc=x['home_team'];ac=x['away_team']
                        h=str(hc['home_team_name']);a=str(ac['away_team_name'])
                        c.execute('''INSERT OR IGNORE INTO sr_matches(match_id,competition_id,season_id,
                           competition,season,match_day,home,away,home_score,away_score,source_url)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                           (mid,int(cid),int(sid),str(x.get('competition',{}).get('competition_name','')),
                            str(x.get('season',{}).get('season_name','')),day,h,a,
                            x.get('home_score'),x.get('away_score'),ROOT+rel))
                        saved+=1
                    except (KeyError,ValueError,TypeError):continue
            mark(c,key,at,saved,delay_hours=24*21)
            out['saved']+=saved
        except Exception as e:
            mark(c,key,at,error=type(e).__name__+': '+str(e)[:130],delay_hours=24)
            out['failed']+=1
        if loader is None:time.sleep(.2)
    return out

def player_details(c,at,loader=None,max_matches=4):
    # Old games only. Lineups not proof of lineups announced prior to match.
    rows=c.execute('''SELECT m.match_id FROM sr_matches m
       LEFT JOIN sr_sources s ON s.source_id='roster:'||m.match_id
       WHERE m.match_day<date(?) AND (s.source_id IS NULL OR s.next_attempt<=?)
       ORDER BY m.match_day DESC LIMIT ?''',(at[:10],at,max_matches)).fetchall()
    out={'attempted':0,'lineups':0,'cards':0,'failed':0}
    for (mid,) in rows:
        out['attempted']+=1;key='roster:'+str(mid)
        try:
            ro=get_json(f'lineups/{mid}.json',loader)
            if not isinstance(ro,list):raise ValueError('Malformed lineups')
            n=0
            with c:
                for team in ro:
                    try:
                        name=str(team['team_name']);teamid=int(team['team_id'])
                    except (TypeError,ValueError,KeyError):continue
                    for p in team.get('lineup',[]):
                        try:
                            pid=int(p['player_id']);pname=str(p['player_name'])
                            pos=','.join(str(z.get('position','')) for z in p.get('positions',[]))[:250]
                            c.execute('''INSERT OR IGNORE INTO sr_lineups VALUES(?,?,?,?,?,?,?,?)''',
                                      (mid,teamid,name,pid,pname,p.get('jersey_number'),pos,ROOT+f'lineups/{mid}.json'))
                            n+=1
                        except (KeyError,TypeError,ValueError):continue
            mark(c,key,at,n,delay_hours=24*90)
            out['lineups']+=n
            # Fetch only bounded historical events; independent and nonblocking.
            # Use optional published event data if available, never invent cards.
            try:
                ev=get_json(f'events/{mid}.json',loader)
                if isinstance(ev,list):
                    with c:
                        for e in ev:
                            card=((e.get('bad_behaviour') or {}).get('card') or (e.get('foul_committed') or {}).get('card') or {}).get('name')
                            if card and e.get('id'):
                                c.execute('INSERT OR IGNORE INTO sr_cards VALUES(?,?,?,?,?,?)',
                                          (mid,str(e['id']),e.get('minute'),(e.get('team') or {}).get('name'),
                                           (e.get('player') or {}).get('name'),str(card)))
                                out['cards']+=1
            except Exception:pass
        except Exception as e:
            mark(c,key,at,error=type(e).__name__+': '+str(e)[:130],delay_hours=48)
            out['failed']+=1
        if loader is None:time.sleep(.2)
    return out

def cycle(c,at=None,loader=None):
    at=at or now()
    result={'catalog':available_comps(c,at,loader), 'matches':match_list(c,at,loader),
            'details':player_details(c,at,loader)}
    with c:c.execute('INSERT INTO sr_runs(at,detail) VALUES(?,?)',(at,json.dumps(result)))
    return result

def status(c):
    setup(c)
    return {'matches':c.execute('SELECT COUNT(*) FROM sr_matches').fetchone()[0],
            'players_in_lineups':c.execute('SELECT COUNT(*) FROM sr_lineups').fetchone()[0],
            'disciplinary_cards':c.execute('SELECT COUNT(*) FROM sr_cards').fetchone()[0],
            'source':'StatsBomb/Hudl open-data: subset of available competitions',
            'does_not_include_every_league':True}

def dashboard(db):
    result={'source':'StatsBomb/Hudl open-data (public research subset)','matches':0,
            'lineups':0,'cards':0,'recent_matches':[],'error':None}
    if not Path(db).exists():return result
    c=sqlite3.connect('file:'+Path(db).resolve().as_posix()+'?mode=ro',uri=True,timeout=5)
    try:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'sr_matches' not in tables:return result
        result['matches']=c.execute('SELECT COUNT(*) FROM sr_matches').fetchone()[0]
        result['lineups']=c.execute('SELECT COUNT(*) FROM sr_lineups').fetchone()[0]
        result['cards']=c.execute('SELECT COUNT(*) FROM sr_cards').fetchone()[0]
        result['recent_matches']=[{'date':r[0],'home':r[1],'away':r[2],'competition':r[3],'players':r[4],'cards':r[5]}
          for r in c.execute('''SELECT m.match_day,m.home,m.away,m.competition,
              (SELECT COUNT(*) FROM sr_lineups WHERE match_id=m.match_id),
              (SELECT COUNT(*) FROM sr_cards WHERE match_id=m.match_id)
              FROM sr_matches m WHERE m.match_day<date('now') ORDER BY m.match_day DESC LIMIT 8''')]
    finally:c.close()
    return result

def main():
    import argparse
    p=argparse.ArgumentParser();p.add_argument('action',choices=('run','once','status'));args=p.parse_args()
    c=collect.db_open();setup(c)
    if args.action=='status':r=status(c)
    elif args.action=='once':r=cycle(c)
    else:
        print('ORACLE RosterLab started: public research subset, not all matches',flush=True)
        while True:
            try:print(json.dumps(cycle(c),ensure_ascii=False),flush=True)
            except Exception as e:
                print(type(e).__name__,str(e)[:250],flush=True);traceback.print_exc()
                try:c.close()
                except Exception:pass
                c=collect.db_open();setup(c)
            time.sleep(INTERVAL)
        return
    print(json.dumps(r,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
