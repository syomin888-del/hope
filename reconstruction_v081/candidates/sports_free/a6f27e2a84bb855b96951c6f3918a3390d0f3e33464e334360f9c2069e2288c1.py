#!/usr/bin/env python3
"""ORACLE Sports Open Lab: CC0 football results/fixtures and time-safe exploratory forecasts.
No bookmaker lines, no paid API, no live bets, no assertions of calibration.
"""
from __future__ import annotations
import csv, datetime as dt, io, json, math, os, sqlite3, sys, time, urllib.error, urllib.request
from collections import defaultdict
from pathlib import Path

SOURCE='https://github.com/openfootball/football.json'
NFL_URL='https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv'
NFL_ATTRIBUTION='nflverse (CC BY 4.0): https://github.com/nflverse/nflverse-data'
RAW='https://raw.githubusercontent.com/openfootball/football.json/master/{season}/{league}.json'
LEAGUES={'en.1':'Англия · Премьер-лига','en.2':'Англия · Чемпионшип',
         'de.1':'Германия · Бундеслига','es.1':'Испания · Ла Лига',
         'it.1':'Италия · Серия А','fr.1':'Франция · Лига 1',
         'nl.1':'Нидерланды · Эредивизи','pt.1':'Португалия · Примейра',
         'sco.1':'Шотландия · Премьершип','tr.1':'Турция · Суперлига',
         'en.3':'Англия · Первая лига','de.2':'Германия · Вторая Бундеслига'}
MODEL='openfootball-poisson-shrink-0.1-UNCALIBRATED'
UTC=dt.timezone.utc

def now():return dt.datetime.now(UTC).isoformat(timespec='seconds')
def day():return dt.datetime.now(UTC).date().isoformat()
def db():
    f=Path(os.environ.get('ORACLE_DB',str(Path(__file__).resolve().parent/'oracle.sqlite3')))
    f.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(str(f),timeout=30)
    c.execute('PRAGMA busy_timeout=30000')
    c.execute('PRAGMA journal_mode=WAL')
    initialize(c)
    return c

def initialize(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS sf_matches(
      id TEXT PRIMARY KEY, sport TEXT NOT NULL,league TEXT NOT NULL,season TEXT NOT NULL,
      match_date TEXT NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL,
      home_score INTEGER,away_score INTEGER,source TEXT NOT NULL,source_url TEXT NOT NULL,
      last_seen TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS sf_matches_league_date ON sf_matches(league,match_date);
    CREATE TABLE IF NOT EXISTS sf_sources(
      source_id TEXT PRIMARY KEY,source_url TEXT NOT NULL,status TEXT NOT NULL,
      last_attempt TEXT,last_success TEXT,next_due TEXT,last_error TEXT,records INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS sf_forecasts(
      match_id TEXT NOT NULL,asof_day TEXT NOT NULL,created_at TEXT NOT NULL,
      sport TEXT NOT NULL,league TEXT NOT NULL,match_date TEXT NOT NULL,
      home TEXT NOT NULL,away TEXT NOT NULL,market TEXT NOT NULL,selection TEXT NOT NULL,
      probability REAL NOT NULL,quality TEXT NOT NULL,training_games INTEGER NOT NULL,
      model_version TEXT NOT NULL,status TEXT NOT NULL,
      hit INTEGER,settled_at TEXT,
      PRIMARY KEY(match_id,asof_day,market,selection));
    CREATE INDEX IF NOT EXISTS sf_fcast_latest ON sf_forecasts(asof_day,match_date);
    CREATE TABLE IF NOT EXISTS sf_eval(
      league TEXT NOT NULL,asof_day TEXT NOT NULL, evaluated INTEGER NOT NULL,
      model_brier REAL, baseline_brier REAL, skill_pct REAL,
      interpretation TEXT NOT NULL,PRIMARY KEY(league,asof_day));
    CREATE TABLE IF NOT EXISTS sf_runs(
      id INTEGER PRIMARY KEY,at TEXT NOT NULL,job TEXT NOT NULL,status TEXT NOT NULL,details TEXT NOT NULL);
    ''')
    # Safe additive migration for any previously initialized research DB.
    existing={r[1] for r in c.execute('PRAGMA table_info(sf_forecasts)')}
    if 'hit' not in existing:c.execute('ALTER TABLE sf_forecasts ADD COLUMN hit INTEGER')
    if 'settled_at' not in existing:c.execute('ALTER TABLE sf_forecasts ADD COLUMN settled_at TEXT')
    c.commit()

def normalize(payload,season,code,when=None):
    """Public-domain format: date, team1, team2, score.ft (possibly absent)."""
    if not isinstance(payload,dict) or not isinstance(payload.get('matches'),list):raise ValueError('Missing football match list')
    when=when or now()
    out=[];seen=set()
    for m in payload['matches']:
        try:
            if not isinstance(m,dict):continue
            date=str(m.get('date',''))
            dt.date.fromisoformat(date)
            home,away=m.get('team1'),m.get('team2')
            if not (isinstance(home,str) and isinstance(away,str) and home.strip() and away.strip() and home!=away):continue
            score=m.get('score')
            ft=score.get('ft') if isinstance(score,dict) else score if isinstance(score,list) else None
            hs=vs=None
            if isinstance(ft,list) and len(ft)==2 and all(type(x)==int and 0<=x<=40 for x in ft):hs,vs=ft
            ident='|'.join((code,date,home.strip(),away.strip()))
            if ident in seen:continue
            seen.add(ident)
            out.append((ident,'football',code,season,date,home.strip(),away.strip(),hs,vs,
                        'openfootball_CC0',RAW.format(season=season,league=code),when))
        except (TypeError,ValueError):continue
    if not out:raise ValueError('No valid football records')
    return out

def save_matches(c,rows):
    with c:
        c.executemany('''INSERT INTO sf_matches VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET home_score=COALESCE(excluded.home_score,sf_matches.home_score),
        away_score=COALESCE(excluded.away_score,sf_matches.away_score),
        last_seen=excluded.last_seen,source_url=excluded.source_url''',rows)
    return len(rows)

def schedule_sources():
    year=dt.datetime.now(UTC).year
    # Include last 5 COMPLETE seasons and current (season rolls over in July).
    end=year if dt.datetime.now(UTC).month>=7 else year-1
    seasons=[f'{y}-{str(y+1)[2:]}' for y in range(end-5,end+1)]
    # Current season first; smaller priority tiers naturally accumulate later.
    return [(s,l) for s in reversed(seasons) for l in LEAGUES]

def fetch(url,timeout=14):
    req=urllib.request.Request(url,headers={'User-Agent':'OracleOpenSportsResearch/2.2 (+https://github.com/openfootball/football.json)','Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        b=resp.read(3_000_000)
        if resp.read(1):raise ValueError('Response exceeded 3 MB safe cap')
    return json.loads(b)

def ingest(c,limit=12,loader=fetch,clock=None):
    at=clock or now();today=at[:10];pairs=schedule_sources()
    states={r[0]:r[1] for r in c.execute('SELECT source_id,next_due FROM sf_sources')}
    ready=[(s,l) for s,l in pairs if not states.get(s+'|'+l) or states[s+'|'+l]<=at]
    stats={'attempted':0,'ok':0,'not_found':0,'error':0,'imported_rows':0,'remaining_eligible':max(0,len(ready)-limit)}
    for season,code in ready[:max(1,min(48,int(limit)))]:
        url=RAW.format(season=season,league=code);sid=season+'|'+code
        try:
            data=loader(url)
            records=normalize(data,season,code,at)
            count=save_matches(c,records)
            status='ok';err=None;stats['ok']+=1;stats['imported_rows']+=count
            recent=season==schedule_sources()[0][0]
            due=dt.datetime.fromisoformat(at)+(dt.timedelta(hours=6) if recent else dt.timedelta(days=30))
        except urllib.error.HTTPError as e:
            status='missing' if e.code==404 else 'error'
            stats['not_found' if status=='missing' else 'error']+=1
            err=f'HTTP {e.code}';count=0
            due=dt.datetime.fromisoformat(at)+dt.timedelta(days=14 if e.code==404 else 1)
        except Exception as e:
            status='error';stats['error']+=1;err=f'{type(e).__name__}: {str(e)[:125]}';count=0
            due=dt.datetime.fromisoformat(at)+dt.timedelta(hours=3)
        with c:c.execute('''INSERT INTO sf_sources VALUES(?,?,?,?,?,?,?,?)
          ON CONFLICT(source_id) DO UPDATE SET status=excluded.status,last_attempt=excluded.last_attempt,
          last_success=COALESCE(excluded.last_success,sf_sources.last_success),next_due=excluded.next_due,
          last_error=excluded.last_error,records=CASE WHEN excluded.status='ok' THEN excluded.records ELSE sf_sources.records END''',
          (sid,url,status,at,at if status=='ok' else None,due.isoformat(timespec='seconds'),err,count))
        stats['attempted']+=1
        # Be polite to GitHub raw cache; bounded API usage.
        if loader is fetch:time.sleep(.4)
    log(c,'ingest','ok' if stats['error']==0 else 'partial',stats)
    return stats

def normalize_nfl_csv(content, observed=None):
    """nflverse schedules release: season, gameday, home_team, away_team, home_score, away_score."""
    observed=observed or now()
    if isinstance(content,bytes):content=content.decode('utf-8-sig')
    rows=list(csv.DictReader(io.StringIO(content)))
    if not rows or not {'game_id','gameday','home_team','away_team','season'}.issubset(rows[0]):
        raise ValueError('NFL schedule CSV format unexpected')
    out=[];seen=set();current=dt.datetime.now(UTC).year
    for x in rows:
        try:
            year=int(x['season'])
            if year<current-6 or year>current:continue
            date=str(x['gameday'])[:10];dt.date.fromisoformat(date)
            h=x['home_team'].strip();a=x['away_team'].strip()
            mid=str(x['game_id']).strip()
            if not all((h,a,mid)) or mid in seen or h==a:continue
            seen.add(mid)
            home=away=None
            if str(x.get('home_score','')).strip().lower() not in ('','na','nan','null','none') and str(x.get('away_score','')).strip().lower() not in ('','na','nan','null','none'):
                home=int(float(x['home_score']));away=int(float(x['away_score']))
                if not (0<=home<=100 and 0<=away<=100):continue
            out.append(('nflverse|'+mid,'americanfootball','nfl',str(year),date,h,a,home,away,
                        'nflverse_CC_BY_4.0',NFL_URL,observed))
        except (TypeError,ValueError,KeyError,AttributeError):continue
    if not out:raise ValueError('No valid recent NFL games')
    return out

def ingest_nfl(c,loader=None,at=None):
    at=at or now();sid='nflverse|games'
    existing=c.execute('SELECT next_due FROM sf_sources WHERE source_id=?',(sid,)).fetchone()
    if existing and existing[0] and existing[0]>at:return {'attempted':0,'status':'not_due'}
    status='error';count=0;error=None
    try:
        if loader is None:
            req=urllib.request.Request(NFL_URL,headers={'User-Agent':'OracleOpenSportsResearch/2.2','Accept':'text/csv'})
            with urllib.request.urlopen(req,timeout=20) as response:
                payload=response.read(4_500_000)
                if response.read(1):raise ValueError('NFL CSV exceeds 4.5 MB')
        else:payload=loader(NFL_URL)
        count=save_matches(c,normalize_nfl_csv(payload,at));status='ok'
        due=dt.datetime.fromisoformat(at)+dt.timedelta(hours=12)
    except Exception as e:
        error=type(e).__name__+': '+str(e)[:150]
        due=dt.datetime.fromisoformat(at)+dt.timedelta(hours=3)
    with c:c.execute("""INSERT INTO sf_sources VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id)
      DO UPDATE SET status=excluded.status,last_attempt=excluded.last_attempt,
       last_success=COALESCE(excluded.last_success,sf_sources.last_success),next_due=excluded.next_due,
       last_error=excluded.last_error,records=CASE WHEN excluded.status='ok' THEN excluded.records ELSE sf_sources.records END""",
       (sid,NFL_URL,status,at,at if status=='ok' else None,due.isoformat(timespec='seconds'),error,count))
    return {'attempted':1,'status':status,'records':count,'error':error}

def estimate_nfl(c,home,away,date):
    since=(dt.date.fromisoformat(date)-dt.timedelta(days=5*365+2)).isoformat()
    matches=c.execute("""SELECT home,away,home_score,away_score FROM sf_matches WHERE league='nfl'
        AND match_date<? AND match_date>=? AND home_score IS NOT NULL AND away_score IS NOT NULL
        ORDER BY match_date DESC LIMIT 1600""",(date,since)).fetchall()
    if len(matches)<250:return None
    hs=[x for x in matches if x[0]==home];aws=[x for x in matches if x[1]==away]
    if min(len(hs),len(aws))<6:return None
    avg_margin=sum(x[2]-x[3] for x in matches)/len(matches)
    avg_total=sum(x[2]+x[3] for x in matches)/len(matches)
    # Heuristic shrinkage, not calibrated model.
    own_home=(sum(x[2]-x[3] for x in hs)+16*avg_margin)/(len(hs)+16)
    own_away=(sum(x[2]-x[3] for x in aws)-16*avg_margin)/(len(aws)+16)
    expected_margin=max(-30,min(30,avg_margin+.5*(own_home-avg_margin)-.5*(own_away+avg_margin)))
    sigma=14.2
    cdf=lambda z:.5*(1+math.erf(z/math.sqrt(2)))
    away_p=cdf((-0.5-expected_margin)/sigma)
    home_p=1-cdf((0.5-expected_margin)/sigma)
    tie_p=max(0,1-home_p-away_p)
    total=avg_total;total_p=1-cdf((44.5-total)/14.0)
    market={'1X2':{'П1':home_p,'Ничья':tie_p,'П2':away_p},
            'ТОТАЛ 44.5':{'Больше 44.5':total_p,'Меньше 44.5':1-total_p}}
    return market,'EXPLORATORY',len(matches)

def poisson_probs(mean):
    if not math.isfinite(mean) or not 0.15<=mean<=5.5:raise ValueError('Unreliable goal mean')
    ps=[math.exp(-mean)];
    for k in range(1,14):ps.append(ps[-1]*mean/k)
    total=sum(ps)
    return [x/total for x in ps]

def probability_markets(home,away):
    ph,pa=poisson_probs(home),poisson_probs(away)
    h=d=a=over=btts=0.0
    for i,p in enumerate(ph):
        for j,q in enumerate(pa):
            z=p*q
            if i>j:h+=z
            elif i==j:d+=z
            else:a+=z
            if i+j>=3:over+=z
            if i>0 and j>0:btts+=z
    return {'1X2':{'П1':h,'Ничья':d,'П2':a},'ТОТАЛ 2.5':{'Больше 2.5':over,'Меньше 2.5':1-over},
            'ОБЕ ЗАБЬЮТ':{'Да':btts,'Нет':1-btts},'ДВОЙНОЙ ШАНС':{'1X':h+d,'X2':d+a,'12':h+a}}

def estimate(c,league,home,away,match_date):
    # Crucial: game reports on match_date or later cannot influence model for that date.
    since=(dt.date.fromisoformat(match_date)-dt.timedelta(days=5*365+2)).isoformat()
    games=c.execute('''SELECT home,away,home_score,away_score,match_date FROM sf_matches
        WHERE league=? AND match_date<? AND match_date>=? AND home_score IS NOT NULL AND away_score IS NOT NULL
        ORDER BY match_date DESC''',(league,match_date,since)).fetchall()
    if len(games)<120:return None
    total_h=sum(x[2] for x in games)/len(games);total_a=sum(x[3] for x in games)/len(games)
    if total_h<.4 or total_a<.4:return None
    hx=[x for x in games if x[0]==home];ay=[x for x in games if x[1]==away]
    # Enough prior same-league games to avoid extreme extrapolation for promoted teams.
    if len(hx)<6 or len(ay)<6:return None
    # Home scored / Away conceded mean, weighted toward league-level prior (12 games).
    n=12
    h_attack=(sum(x[2] for x in hx)+n*total_h)/(len(hx)+n)/total_h
    a_defense=(sum(x[2] for x in ay)+n*total_h)/(len(ay)+n)/total_h
    a_attack=(sum(x[3] for x in ay)+n*total_a)/(len(ay)+n)/total_a
    h_defense=(sum(x[3] for x in hx)+n*total_a)/(len(hx)+n)/total_a
    lhome=max(.25,min(4.9,total_h*h_attack*a_defense))
    laway=max(.25,min(4.9,total_a*a_attack*h_defense))
    quality='EXPLORATORY' if min(len(hx),len(ay))<18 else 'RESEARCH'
    return probability_markets(lhome,laway),quality,len(games)

def propose(c,max_fixtures=60,at=None):
    at=at or now();today=at[:10];until=(dt.date.fromisoformat(today)+dt.timedelta(days=7)).isoformat()
    # Date only; today excluded because unknown local kickoff timezone.
    upcoming=c.execute('''SELECT id,sport,league,match_date,home,away FROM sf_matches
         WHERE match_date>? AND match_date<=? AND home_score IS NULL AND away_score IS NULL
         ORDER BY match_date,id LIMIT ?''',(today,until,max_fixtures)).fetchall()
    n=0;empty=0
    with c:
        for mid,sport,league,date,home,away in upcoming:
            model=estimate_nfl(c,home,away,date) if sport=='americanfootball' else estimate(c,league,home,away,date)
            if model is None:empty+=1;continue
            markets,quality,games=model
            for market,outs in markets.items():
                for selection,p in outs.items():
                    # Each is a published hypothetical probability, NEVER a priced bet.
                    c.execute('''INSERT OR IGNORE INTO sf_forecasts(match_id,asof_day,created_at,sport,league,match_date,home,away,market,selection,probability,quality,training_games,model_version,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (mid,today,at,sport,league,date,home,away,market,selection,
                         round(p,6),quality,games,MODEL,'RESEARCH_ONLY'))
                    n+=1
    stats={'upcoming_in_feed':len(upcoming),'without_training_data':empty,'outcomes_examined':n,'has_live_bookmaker_odds':False}
    log(c,'propose','ok',stats);return stats

def validate_historical(c, max_games_per_league=65):
    """Chronological 1X2 check: train only on matches with date strictly earlier.

    Never claims live accuracy or skill from training-set fit; compared to a
    prior-frequency league baseline (no knowledge of future matches).
    """
    results=[];on=day(); cutoff=(dt.date.fromisoformat(on)-dt.timedelta(days=365)).isoformat()
    for league in LEAGUES:
        rows=c.execute("""SELECT match_date,home,away,home_score,away_score FROM sf_matches
             WHERE league=? AND match_date>=? AND match_date<? AND home_score IS NOT NULL
             ORDER BY match_date DESC LIMIT ?""",(league,cutoff,on,max_games_per_league)).fetchall()
        sq_mod=[];sq_base=[]
        for date,home,away,hs,vs in rows:
            result=estimate(c,league,home,away,date)
            if not result:continue
            probs=result[0]['1X2']
            prior=c.execute("""SELECT home_score,away_score FROM sf_matches WHERE league=?
                AND match_date<? AND home_score IS NOT NULL AND away_score IS NOT NULL
                ORDER BY match_date DESC LIMIT 300""",(league,date)).fetchall()
            if len(prior)<100:continue
            counts={'П1':0,'Ничья':0,'П2':0}
            for a,b in prior:counts['П1' if a>b else 'Ничья' if a==b else 'П2']+=1
            base={k:(v+3)/(len(prior)+9) for k,v in counts.items()}
            actual='П1' if hs>vs else 'Ничья' if hs==vs else 'П2'
            sq_mod.append(sum((probs[k]-(1 if k==actual else 0))**2 for k in probs)/3)
            sq_base.append(sum((base[k]-(1 if k==actual else 0))**2 for k in base)/3)
        n=len(sq_mod)
        bm=sum(sq_mod)/n if n else None
        bb=sum(sq_base)/n if n else None
        skill=round(100*(bb-bm)/bb,2) if n and bb>0 else None
        interpretation='INSUFFICIENT_EVIDENCE' if n<40 else 'RESEARCH_BEATS_BASELINE' if skill>0 else 'RESEARCH_BELOW_BASELINE'
        with c:c.execute("""INSERT INTO sf_eval VALUES(?,?,?,?,?,?,?) ON CONFLICT(league,asof_day)
          DO UPDATE SET evaluated=excluded.evaluated,model_brier=excluded.model_brier,
          baseline_brier=excluded.baseline_brier,skill_pct=excluded.skill_pct,
          interpretation=excluded.interpretation""",(league,on,n,bm,bb,skill,interpretation))
        results.append({'league':league,'walk_forward_cases':n,'model_brier':bm,'baseline_brier':bb,
          'skill_pct':skill,'state':interpretation})
    log(c,'historical_validation','ok',{'evaluated_leagues':len(results),
       'leagues_with_min_40':sum(x['walk_forward_cases']>=40 for x in results)})
    return results

def settle_sports(c,at=None):
    """Check previously timestamped exploratory projections using final scores only.
    Game date must be after prediction day. No retrofitted/hindsight results.
    """
    at=at or now();n=hit=0
    rows=c.execute("""SELECT f.match_id,f.asof_day,f.market,f.selection,m.home_score,m.away_score
      FROM sf_forecasts f JOIN sf_matches m ON m.id=f.match_id
      WHERE f.status='RESEARCH_ONLY' AND f.asof_day<m.match_date
      AND m.match_date<? AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL""",(at[:10],)).fetchall()
    for mid,asof,market,selection,h,a in rows:
        actual=None
        if market=='1X2': actual='П1' if h>a else 'П2' if h<a else 'Ничья'
        elif market.startswith('ТОТАЛ '):
            try:
                threshold=float(market.split(' ',1)[1]);actual=('Больше ' if h+a>threshold else 'Меньше ')+str(threshold)
            except (TypeError,ValueError):pass
        elif market=='ОБЕ ЗАБЬЮТ':actual='Да' if h>0 and a>0 else 'Нет'
        elif market=='ДВОЙНОЙ ШАНС':
            actual='1X' if h>a else 'X2' if h<a else '1X|X2' # draw qualifies 1X and X2
        if actual is None:continue
        success=selection in actual.split('|')
        if market=='ДВОЙНОЙ ШАНС' and selection=='12':success=h!=a
        with c:c.execute("""UPDATE sf_forecasts SET status='PAPER_SETTLED',hit=?,settled_at=?
              WHERE match_id=? AND asof_day=? AND market=? AND selection=? AND status='RESEARCH_ONLY' """,
              (int(success),at,mid,asof,market,selection))
        n+=1;hit+=int(success)
    return {'checked_outcomes':n,'hits':hit,'past_matches_only':True}

def log(c,job,status,details):
    with c:c.execute('INSERT INTO sf_runs(at,job,status,details) VALUES(?,?,?,?)',(now(),job,status,json.dumps(details,ensure_ascii=False)))

def status(c):
    scalar=lambda sql:c.execute(sql).fetchone()[0]
    return {'sport':'football','connected_source':'openfootball_CC0',
      'historical_and_fixture_matches':scalar('SELECT COUNT(*) FROM sf_matches'),
      'completed_matches':scalar('SELECT COUNT(*) FROM sf_matches WHERE home_score IS NOT NULL'),
      'scheduled_matches':scalar('SELECT COUNT(*) FROM sf_matches WHERE home_score IS NULL'),
      'leagues_with_data':scalar('SELECT COUNT(DISTINCT league) FROM sf_matches'),
      'feeds_successful':scalar("SELECT COUNT(*) FROM sf_sources WHERE status='ok'"),
      'feeds_error':scalar("SELECT COUNT(*) FROM sf_sources WHERE status='error'"),
      'total_hypotheses_saved':scalar('SELECT COUNT(*) FROM sf_forecasts'),
      'research_outcomes_settled':scalar("SELECT COUNT(*) FROM sf_forecasts WHERE status='PAPER_SETTLED'"),
      'leagues_backtested':scalar('SELECT COUNT(*) FROM sf_eval'),
      'future_matches_in_next_7d':scalar("SELECT COUNT(DISTINCT match_id) FROM sf_forecasts WHERE asof_day >= date('now','-1 day') AND match_date>date('now')"),
      'last_jobs':[{'job':r[0],'at':r[1],'status':r[2],'details':json.loads(r[3])} for r in c.execute('SELECT job,at,status,details FROM sf_runs ORDER BY id DESC LIMIT 3')],
      'sports_supported_by_current_free_ingest':['football','americanfootball_nfl'],
      'other_sports_ingest':'NOT_CONNECTED; no fake claims of complete sports coverage',
      'signal_state':'RESEARCH_ONLY; no odds, no betting edge, no calibrated probabilities'}

def retry_known_parser_errors(c):
    """One-shot retry of failed OpenFootball payloads after score-list parser repair.

    Never retry 404/missing blindly and never erase historical games.
    """
    with c:
        cur=c.execute("""UPDATE sf_sources SET next_due=?
             WHERE status='error' AND source_id LIKE '%|%'
             AND last_error LIKE '%attribute%get%' AND next_due>?""",(now(),now()))
    return cur.rowcount


def run():
    wait=max(300,int(os.getenv('ORACLE_SPORTSLAB_INTERVAL','300')))
    print('ORACLE Sports Open Lab starting, CC0 public football, NO real bets',flush=True)
    while True:
        try:
            c=db()
            try:
                retry_known_parser_errors(c)
                batch=ingest(c,limit=int(os.getenv('ORACLE_SPORTSLAB_BATCH','12')))
                nfl=ingest_nfl(c)
                hypotheses=propose(c)
                settlement=settle_sports(c)
                evaluated=c.execute('SELECT 1 FROM sf_runs WHERE job=? AND at>=? LIMIT 1',('historical_validation',day())).fetchone()
                if not evaluated and c.execute('SELECT COUNT(*) FROM sf_matches').fetchone()[0]>=600:
                    validate_historical(c,max_games_per_league=50)
                print(json.dumps({'at':now(),'import':batch,'nfl':nfl,'hypotheses':hypotheses,'settlement':settlement},ensure_ascii=False),flush=True)
            finally:c.close()
        except Exception as e:print('SportsLab cycle error:',repr(e),flush=True)
        time.sleep(wait)

if __name__=='__main__':
    cmd=sys.argv[1] if len(sys.argv)>1 else 'status'
    if cmd=='run':run()
    else:
        c=db()
        try:
            if cmd=='status':res=status(c)
            elif cmd=='ingest':res=ingest(c,limit=16)
            elif cmd=='propose':res=propose(c)
            elif cmd=='settle':res=settle_sports(c)
            elif cmd=='nfl':res=ingest_nfl(c)
            elif cmd=='validate':res=validate_historical(c)
            else:raise SystemExit('Commands: run, status, ingest, nfl, propose, settle, validate')
            print(json.dumps(res,ensure_ascii=False,indent=2))
        finally:c.close()
