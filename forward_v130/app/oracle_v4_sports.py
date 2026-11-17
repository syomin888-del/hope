"""Daily prospective sport research + price provenance. No betting execution.

OpenLigaDB: public live schedule/final scores, 60 rpm max. The Odds API: optional
real timestamped bookmaker odds. No bookmaker data => no EV, no bet.
"""
from __future__ import annotations
import datetime as dt, hashlib, json, math, os, sqlite3, urllib.parse, urllib.request
from collections import defaultdict
UTC=dt.timezone.utc
LEAGUES={'bl1':'de.1','bl2':'de.2'}
OPENLIGA='https://api.openligadb.de'
ODDSAPI='https://api.the-odds-api.com/v4'

def stamp(at=None):
    if isinstance(at,str):at=parse_time(at)
    return (at or dt.datetime.now(UTC)).astimezone(UTC).isoformat(timespec='seconds')
def parse_time(raw):
    d=dt.datetime.fromisoformat(str(raw).replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('UTC offset required')
    return d.astimezone(UTC)
def get_json(url, headers=None):
    if not (url.startswith(OPENLIGA+'/') or url.startswith(ODDSAPI+'/')):raise ValueError('Unapproved origin')
    req=urllib.request.Request(url,headers={'User-Agent':'ORACLE-Research-V4/1.0','Accept':'application/json',**(headers or {})})
    with urllib.request.urlopen(req,timeout=18) as response:
        blob=response.read(3_000_000)
        if response.read(1):raise ValueError('Response too large')
        return json.loads(blob),dict(response.headers)

def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS v4_games(event_id TEXT PRIMARY KEY,provider TEXT NOT NULL,sport TEXT NOT NULL,
      league TEXT NOT NULL,kickoff_utc TEXT NOT NULL,home TEXT NOT NULL,away TEXT NOT NULL,
      home_score INTEGER,away_score INTEGER,final INTEGER NOT NULL DEFAULT 0,observed_at TEXT NOT NULL,
      source_url TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS v4_games_kickoff ON v4_games(kickoff_utc);
    CREATE TABLE IF NOT EXISTS v4_sport_preds(event_id TEXT NOT NULL,market TEXT NOT NULL,selection TEXT NOT NULL,
      issued_at TEXT NOT NULL,prob REAL NOT NULL,model TEXT NOT NULL,status TEXT NOT NULL,
      hit INTEGER,settled_at TEXT, PRIMARY KEY(event_id,market,selection));
    CREATE TABLE IF NOT EXISTS v4_book_odds(event_id TEXT NOT NULL,bookmaker TEXT NOT NULL,selection TEXT NOT NULL,
      price REAL NOT NULL,captured_at TEXT NOT NULL,book_last_update TEXT NOT NULL,
      source TEXT NOT NULL,origin_id TEXT NOT NULL,
      PRIMARY KEY(event_id,bookmaker,selection,captured_at));
    CREATE TABLE IF NOT EXISTS v4_paper_bets(
      event_id TEXT PRIMARY KEY,issued_at TEXT NOT NULL,selection TEXT NOT NULL,
      bookmaker TEXT NOT NULL,odds REAL NOT NULL,raw_model_probability REAL NOT NULL,
      raw_ev REAL NOT NULL,status TEXT NOT NULL,stake_units REAL NOT NULL,
      pnl_units REAL,settled_at TEXT,origin TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v4_jobs(job TEXT PRIMARY KEY,last_run TEXT NOT NULL,status TEXT NOT NULL,
      details TEXT NOT NULL);
    ''');c.commit()

def log(c,job,status,details,at=None):
    c.execute('''INSERT INTO v4_jobs VALUES(?,?,?,?) ON CONFLICT(job) DO UPDATE SET
      last_run=excluded.last_run,status=excluded.status,details=excluded.details''',
      (job,stamp(at),status,json.dumps(details,ensure_ascii=False)[:2000]));c.commit()

def due(c,job,seconds,at=None):
    row=c.execute('SELECT last_run FROM v4_jobs WHERE job=?',(job,)).fetchone()
    return not row or (parse_time(stamp(at))-parse_time(row[0])).total_seconds()>=seconds

def fulltime(m):
    """OpenLigaDB: only After90Minutes or verified terminal result, no half time."""
    if not m.get('matchIsFinished'):return None
    results=m.get('matchResults') or []
    for r in results:
        typ=r.get('resultTypeID') or (r.get('resultType') or {}).get('resultTypeID')
        name=str(r.get('resultName','')).lower()
        kind=str(r.get('resultTypeKind') or '')
        if kind and kind!='After90Minutes':continue
        if kind=='After90Minutes' or (not kind and (typ==2 or 'endergebnis' in name or '90' in name)):
            h,a=r.get('pointsTeam1'),r.get('pointsTeam2')
            if type(h)==int and type(a)==int and 0<=h<=50 and 0<=a<=50:return (h,a)
    return None

def normalize_game(m,league,source,at=None):
    raw=str(m['matchDateTimeUTC'])
    kick=stamp(parse_time(raw+'+00:00') if len(raw)==19 and 'Z' not in raw else parse_time(raw))
    home=str(m['team1']['teamName']).strip();away=str(m['team2']['teamName']).strip()
    if not home or not away or home==away:raise ValueError('Invalid teams')
    final_score=fulltime(m)
    ident='openligadb:'+str(int(m['matchID']))
    return (ident,'OpenLigaDB','football',league,kick,home,away,
            final_score[0] if final_score else None,final_score[1] if final_score else None,
            int(final_score is not None),stamp(at),source)

def ingest_openliga(c,at=None,loader=get_json,seasons=None,leagues=None):
    now=at or dt.datetime.now(UTC)
    leagues=leagues or ('bl1','bl2')
    # Current and two historic seasons, limiting requests to 6 per 12-hour cycle.
    years=seasons or (now.year-2,now.year-1,now.year)
    stats={'requests':0,'games_seen':0,'errors':[]}
    for league in leagues:
        for year in years:
            url=f'{OPENLIGA}/getmatchdata/{league}/{year}'
            try:
                try:items,_=loader(url)
                except TypeError:items,_=loader(url,{})
                if not isinstance(items,list):raise ValueError('Expected list')
                rows=[]
                for m in items:
                    try:rows.append(normalize_game(m,LEAGUES.get(league,league),url,at))
                    except (KeyError,ValueError,TypeError):continue
                with c:
                    for item in rows:
                        oldkick=c.execute('SELECT kickoff_utc FROM v4_games WHERE event_id=?',(item[0],)).fetchone()
                        if oldkick and oldkick[0]!=item[4]:
                            c.execute("UPDATE v4_sport_preds SET status='INVALID_SCHEDULE_CHANGE' WHERE event_id=? AND status='PAPER_OPEN'",(item[0],))
                            c.execute("UPDATE v4_paper_bets SET status='VOID_SCHEDULE_CHANGE',pnl_units=NULL WHERE event_id=? AND status='PAPER_OPEN_UNCALIBRATED'",(item[0],))
                    c.executemany('''INSERT INTO v4_games VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET
                     home_score=COALESCE(excluded.home_score,v4_games.home_score),
                     away_score=COALESCE(excluded.away_score,v4_games.away_score),
                     final=CASE WHEN excluded.final=1 THEN 1 ELSE v4_games.final END,
                     observed_at=excluded.observed_at,
                     kickoff_utc=excluded.kickoff_utc''',rows)
                stats['games_seen']+=len(rows)
            except Exception as e:stats['errors'].append(f'{league}/{year}: {type(e).__name__}: {str(e)[:90]}')
            stats['requests']+=1
    log(c,'openliga','OK' if not stats['errors'] else 'PARTIAL',stats,at);return stats

def _poisson_cdf(lam,limit=11):
    arr=[math.exp(-lam)]
    for i in range(1,limit):arr.append(arr[-1]*lam/i)
    return arr

def estimate(c,league,home,away,kickoff):
    # No lookahead: only historical scores from matches completed before as-of kickoff.
    rows=c.execute('''SELECT home,away,home_score,away_score FROM v4_games
       WHERE league=? AND final=1 AND kickoff_utc<? AND home_score IS NOT NULL
       ORDER BY kickoff_utc DESC LIMIT 2500''',(league,kickoff)).fetchall()
    if len(rows)<100:return None
    home_games=[r for r in rows if r[0]==home];away_games=[r for r in rows if r[1]==away]
    if len(home_games)<6 or len(away_games)<6:return None
    avg_h=sum(r[2] for r in rows)/len(rows);avg_a=sum(r[3] for r in rows)/len(rows)
    if min(avg_h,avg_a)<.3:return None
    k=16
    h_att=(sum(r[2] for r in home_games)+k*avg_h)/(len(home_games)+k)
    a_def=(sum(r[2] for r in away_games)+k*avg_h)/(len(away_games)+k)
    a_att=(sum(r[3] for r in away_games)+k*avg_a)/(len(away_games)+k)
    h_def=(sum(r[3] for r in home_games)+k*avg_a)/(len(home_games)+k)
    lh=min(5,max(.2,h_att*a_def/avg_h));la=min(5,max(.2,a_att*h_def/avg_a))
    hp,ap=_poisson_cdf(lh),_poisson_cdf(la)
    ph=sum(x*y for i,x in enumerate(hp) for j,y in enumerate(ap) if i>j)
    pd=sum(x*y for i,x in enumerate(hp) for j,y in enumerate(ap) if i==j)
    pa=sum(x*y for i,x in enumerate(hp) for j,y in enumerate(ap) if i<j)
    total=ph+pd+pa
    return {'1':ph/total,'X':pd/total,'2':pa/total}

def propose(c,at=None,horizon_days=2):
    at=stamp(at);limit=stamp(parse_time(at)+dt.timedelta(days=horizon_days))
    rows=c.execute('''SELECT event_id,league,kickoff_utc,home,away FROM v4_games
       WHERE final=0 AND kickoff_utc>? AND kickoff_utc<=? ORDER BY kickoff_utc LIMIT 200''',(at,limit)).fetchall()
    n=0;missing=0
    for event,league,kick,home,away in rows:
        probs=estimate(c,league,home,away,kick)
        if probs is None:missing+=1;continue
        # Use model's fair probabilities as RESEARCH; no bookmaker price => not a bet.
        with c:
            for pick,p in probs.items():
                cur=c.execute('''INSERT OR IGNORE INTO v4_sport_preds
                   (event_id,market,selection,issued_at,prob,model,status) VALUES(?,?,?,?,?,?,?)''',
                   (event,'1X2',pick,at,round(p,7),'poisson_shrink_UNCALIBRATED','PAPER_OPEN'))
                n+=int(cur.rowcount>0)
    result={'upcoming_fixtures':len(rows),'new_forecast_rows':n,'without_history':missing,
       'preregistered_before_kickoff':True,'note':'Not betting advice; 3 selections per event are not 3 bets.'}
    log(c,'predictions','OK',result,at);return result

def settle(c,at=None):
    at=stamp(at);matches=0;rows=0
    for eid,kick,h,a in c.execute('''SELECT event_id,kickoff_utc,home_score,away_score FROM v4_games
          WHERE final=1 AND kickoff_utc<? AND home_score IS NOT NULL AND away_score IS NOT NULL''',(at,)):
        outcome='1' if h>a else '2' if h<a else 'X'
        # Do not settle post-kickoff predictions as genuine forward observations.
        preds=c.execute("SELECT selection,issued_at FROM v4_sport_preds WHERE event_id=? AND status='PAPER_OPEN'",(eid,)).fetchall()
        if not preds:continue
        with c:
            for sel,issued in preds:
                status='PAPER_SETTLED' if parse_time(issued)<parse_time(kick) else 'INVALID_LATE'
                c.execute('''UPDATE v4_sport_preds SET status=?,hit=?,settled_at=?
                  WHERE event_id=? AND selection=? AND market='1X2' AND status='PAPER_OPEN' ''',
                  (status,int(sel==outcome) if status=='PAPER_SETTLED' else None,at,eid,sel))
                rows+=1
        matches+=1
    result={'matches_settled_or_rejected':matches,'forecast_rows_processed':rows}
    log(c,'settlement','OK',result,at);return result

def normalized(s):
    return ''.join(x for x in s.lower() if x.isalnum())

def match_candidate(c,game,asof):
    # Do NOT guess teams: exact normalized home AND away and kickoff within 90 min.
    home,away=normalized(game['home_team']),normalized(game['away_team'])
    kick=parse_time(game['commence_time'])
    for eid,h,a,t in c.execute('SELECT event_id,home,away,kickoff_utc FROM v4_games WHERE final=0 AND kickoff_utc>? AND kickoff_utc<?',
        (stamp(kick-dt.timedelta(minutes=90)),stamp(kick+dt.timedelta(minutes=90)))):
        if (normalized(h),normalized(a))==(home,away):return eid
    return None

def normalize_odds(c,data,at=None):
    captured=stamp(at);n=0;unmatched=0
    if not isinstance(data,list):raise ValueError('Odds API data must be list')
    for game in data:
        try:event=match_candidate(c,game,captured)
        except (KeyError,ValueError,TypeError):continue
        if not event:unmatched+=1;continue
        if parse_time(game['commence_time'])<=parse_time(captured):continue
        for book in game.get('bookmakers',[]):
            name=book.get('key') or book.get('title')
            for market in book.get('markets',[]):
                if market.get('key')!='h2h':continue
                try:book_at=stamp(parse_time(market.get('last_update') or book['last_update']))
                except (KeyError,ValueError,TypeError):continue
                if parse_time(book_at)>parse_time(captured):continue
                for outcome in market.get('outcomes',[]):
                    game_home,game_away=normalized(game['home_team']),normalized(game['away_team'])
                    picked=normalized(outcome.get('name',''))
                    selection='1' if picked==game_home else '2' if picked==game_away else 'X' if picked in ('draw','tie') else None
                    price=outcome.get('price')
                    if selection is None or not isinstance(price,(int,float)) or not 1.01<price<=100:continue
                    with c:
                        c.execute('''INSERT OR IGNORE INTO v4_book_odds VALUES(?,?,?,?,?,?,?,?)''',
                           (event,name,selection,float(price),captured,book_at,'THE_ODDS_API',game.get('id','')))
                    n+=1
    return {'odds_rows_examined':n,'events_unmatched':unmatched,'capture_at':captured}

def ingest_odds(c,key,at=None,loader=get_json,league_key='soccer_germany_bundesliga'):
    if not key:return {'state':'NOT_CONFIGURED','reason':'ORACLE_ODDS_KEY missing; no fabricated odds'}
    url=f'{ODDSAPI}/sports/{league_key}/odds/?'+urllib.parse.urlencode({'regions':'eu','markets':'h2h','oddsFormat':'decimal','dateFormat':'iso','apiKey':key})
    try:
        try:payload,headers=loader(url)
        except TypeError:payload,headers=loader(url)
        if not isinstance(payload,list):raise ValueError('Unexpected odds format')
        result=normalize_odds(c,payload,at)
        result['quota_remaining']=headers.get('x-requests-remaining')
        result['state']='OK';log(c,'odds','OK',result,at);return result
    except Exception as e:
        result={'state':'ERROR','error':type(e).__name__} # never log URL or credentials
        log(c,'odds','ERROR',result,at);return result

def paper_candidates(c,at=None,min_odds=1.7,min_raw_ev=.08):
    """One research-only paper candidate per match from recorded bookmaker odds.

    p is UNCALIBRATED; not a real betting recommendation. Timestamped only.
    """
    at=stamp(at);created=0
    games=c.execute('SELECT event_id,kickoff_utc FROM v4_games WHERE final=0 AND kickoff_utc>?',(at,)).fetchall()
    for eid,kick in games:
        ranked=[]
        for sel,p,model_at in c.execute("SELECT selection,prob,issued_at FROM v4_sport_preds WHERE event_id=? AND market='1X2' AND status='PAPER_OPEN'",(eid,)):
            if parse_time(model_at)>=parse_time(kick):continue
            rows=c.execute('SELECT bookmaker,price,captured_at,book_last_update,source FROM v4_book_odds WHERE event_id=? AND selection=? AND captured_at<=? ORDER BY captured_at DESC',(eid,sel,at)).fetchall()
            for book,price,checked,book_time,quote_source in rows:
                if not (parse_time(model_at)<=parse_time(checked)<parse_time(kick)):continue
                if not 0<=(parse_time(checked)-parse_time(book_time)).total_seconds()<=900:continue
                if (parse_time(at)-parse_time(checked)).total_seconds()>900:continue
                ev=p*price-1
                if p>=.45 and price>=min_odds and ev>=min_raw_ev:
                    ranked.append((ev,sel,book,price,p,checked,quote_source))
        if not ranked:continue
        ev,sel,book,price,p,checked,quote_source=max(ranked,key=lambda a:a[0])
        with c:
            cur=c.execute('INSERT OR IGNORE INTO v4_paper_bets(event_id,issued_at,selection,bookmaker,odds,raw_model_probability,raw_ev,status,stake_units,origin) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (eid,at,sel,book,price,p,round(ev,6),'PAPER_OPEN_UNCALIBRATED',1.0,'REAL_SNAPSHOT_'+quote_source))
            created+=int(cur.rowcount>0)
    stats={'paper_candidates_created':created,'note':'Uncalibrated model; not recommended real bets'}
    log(c,'paper_candidates','OK',stats,at);return stats

def settle_paper_bets(c,at=None):
    at=stamp(at);completed=0
    rows=c.execute("SELECT b.event_id,b.selection,b.odds,b.issued_at,g.kickoff_utc,g.home_score,g.away_score FROM v4_paper_bets b JOIN v4_games g ON g.event_id=b.event_id WHERE b.status='PAPER_OPEN_UNCALIBRATED' AND g.final=1 AND g.kickoff_utc<?",(at,)).fetchall()
    for eid,sel,price,issued,kick,h,a in rows:
        if parse_time(issued)>=parse_time(kick):status='INVALID_LATE';pnl=None
        elif h is None or a is None:continue
        else:
            success=sel==('1' if h>a else '2' if a>h else 'X')
            status='PAPER_SETTLED';pnl=price-1 if success else -1.0
        with c:c.execute("UPDATE v4_paper_bets SET status=?,pnl_units=?,settled_at=? WHERE event_id=? AND status='PAPER_OPEN_UNCALIBRATED'",(status,pnl,at,eid))
        completed+=1
    return {'settled_or_invalid':completed}

def sports_summary(c,at=None):
    at=stamp(at);today=at[:10]
    from zoneinfo import ZoneInfo
    local_today=parse_time(at).astimezone(ZoneInfo('Asia/Makassar')).date().isoformat()
    stats={'today_utc':today,'today_bali':local_today,'games_today_bali':sum(1 for (k,) in c.execute("SELECT kickoff_utc FROM v4_games WHERE kickoff_utc>? AND kickoff_utc<=?",(stamp(parse_time(at)-dt.timedelta(days=1)),stamp(parse_time(at)+dt.timedelta(days=2)))) if parse_time(k).astimezone(ZoneInfo('Asia/Makassar')).date().isoformat()==local_today), 'research_today':c.execute('''SELECT COUNT(*) FROM v4_sport_preds
         WHERE substr(issued_at,1,10)=?''',(today,)).fetchone()[0],
       'games_today':c.execute('SELECT COUNT(*) FROM v4_games WHERE substr(kickoff_utc,1,10)=?', (today,)).fetchone()[0],
       'games_next_48h':c.execute('SELECT COUNT(*) FROM v4_games WHERE kickoff_utc>? AND kickoff_utc<=?',(at,stamp(parse_time(at)+dt.timedelta(hours=48)))).fetchone()[0],
       'pred_rows':c.execute('SELECT COUNT(*) FROM v4_sport_preds').fetchone()[0],
       'paper_bets':c.execute('SELECT COUNT(*) FROM v4_paper_bets').fetchone()[0],
       'paper_bets_settled':c.execute("SELECT COUNT(*) FROM v4_paper_bets WHERE status='PAPER_SETTLED'").fetchone()[0],
       'settled_matches':c.execute("SELECT COUNT(DISTINCT event_id) FROM v4_sport_preds WHERE status='PAPER_SETTLED'").fetchone()[0],
       'odds_snapshots':c.execute('SELECT COUNT(*) FROM v4_book_odds').fetchone()[0],
       'booked_bets':0,'roi':None,'data_integrity':'REAL_TIMESTAMPS_PAPER_ONLY'}
    vals=[x[0] for x in c.execute("SELECT pnl_units FROM v4_paper_bets WHERE status='PAPER_SETTLED' AND pnl_units IS NOT NULL")]
    stats['paper_roi']=round(sum(vals)/len(vals),6) if vals else None
    stats['paper_pnl_units']=round(sum(vals),5) if vals else None
    rows=c.execute('''SELECT g.kickoff_utc,g.home,g.away,p.selection,p.prob,p.status,
           (SELECT MAX(price) FROM v4_book_odds o WHERE o.event_id=p.event_id AND o.selection=p.selection
            AND o.captured_at<=p.issued_at) AS odds
       FROM v4_sport_preds p JOIN v4_games g ON p.event_id=g.event_id
       WHERE g.kickoff_utc>? AND p.issued_at < g.kickoff_utc ORDER BY g.kickoff_utc,p.prob DESC LIMIT 90''',(at,)).fetchall()
    stats['upcoming']=[{'kickoff_utc':k,'home':h,'away':a,'selection':sel,'probability':p,
         'state':state,'odds':od,'ev':round(p*od-1,5) if od is not None else None}
         for k,h,a,sel,p,state,od in rows]
    return stats
