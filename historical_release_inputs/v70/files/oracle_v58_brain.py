#!/usr/bin/env python3
"""ORACLE V5.8: source-traceable multisport research engine; no real bets.

Never use book prices as model features. Never treat unvalidated predictions as
signals. Strict-asof historical validation and one per bookmaker root identity.
"""
from __future__ import annotations
import collections, datetime as dt, hashlib, json, math, os, sqlite3, time
import urllib.parse, urllib.request
from statistics import mean

UTC=dt.timezone.utc
VERSION='5.8'
# ESPN scoreboard is publicly readable but UNOFFICIAL/undocumented and can fail;
# optional switch disables third-party fetching without changing historical data.
ESPN_LEAGUES={
    'basketball':('basketball','nba'),
    'hockey':('hockey','nhl'),
    'baseball':('baseball','mlb'),
    'basketball-wnba':('basketball','wnba'),
}
DAYS_BACK=550
MODEL_NAMES=('ELO','FORM','POISSON')
MIN_HISTORY=100
MIN_OOS=40
MIN_TEAM=8
TARGET_HIT_RATE=.80

# Verified club identity candidates, not arbitrary substring/fuzzy mapping.
# Names in this map must still be paired uniquely against the result dataset.
ALIASES={
 'Бостон Селтикс':'Boston Celtics','Нью-Йорк Никс':'New York Knicks','Бруклин Нетс':'Brooklyn Nets',
 'Филадельфия Севенти Сиксерс':'Philadelphia 76ers','Торонто Рэпторс':'Toronto Raptors',
 'Майами Хит':'Miami Heat','Орландо Мэджик':'Orlando Magic','Шарлотт Хорнетс':'Charlotte Hornets',
 'Атланта Хокс':'Atlanta Hawks','Вашингтон Уизардс':'Washington Wizards',
 'Милуоки Бакс':'Milwaukee Bucks','Кливленд Кавальерс':'Cleveland Cavaliers',
 'Индиана Пэйсерс':'Indiana Pacers','Чикаго Буллз':'Chicago Bulls','Детройт Пистонс':'Detroit Pistons',
 'Лос-Анджелес Лейкерс':'Los Angeles Lakers','Лос-Анджелес Клипперс':'LA Clippers',
 'Голден Стэйт Уорриорз':'Golden State Warriors','Сакраменто Кингз':'Sacramento Kings',
 'Финикс Санз':'Phoenix Suns','Денвер Наггетс':'Denver Nuggets','Миннесота Тимбервулвз':'Minnesota Timberwolves',
 'Оклахома-Сити Тандер':'Oklahoma City Thunder','Юта Джаз':'Utah Jazz','Портленд Трэйл Блэйзерс':'Portland Trail Blazers',
 'Даллас Маверикс':'Dallas Mavericks','Хьюстон Рокетс':'Houston Rockets','Мемфис Гриззлис':'Memphis Grizzlies',
 'Нью-Орлеан Пеликанс':'New Orleans Pelicans','Сан-Антонио Сперс':'San Antonio Spurs',
 'Бостон Брюинз':'Boston Bruins','Нью-Йорк Рейнджерс':'New York Rangers',
 'Нью-Йорк Айлендерс':'New York Islanders','Нью-Джерси Девилз':'New Jersey Devils',
 'Баффало Сейбрз':'Buffalo Sabres','Торонто Мэйпл Лифс':'Toronto Maple Leafs',
 'Монреаль Канадиенс':'Montreal Canadiens','Оттава Сенаторз':'Ottawa Senators',
 'Тампа-Бэй Лайтнинг':'Tampa Bay Lightning','Флорида Пантерз':'Florida Panthers',
 'Детройт Ред Уингз':'Detroit Red Wings','Коламбус Блю Джекетс':'Columbus Blue Jackets',
 'Питтсбург Пингвинз':'Pittsburgh Penguins','Филадельфия Флайерз':'Philadelphia Flyers',
 'Вашингтон Кэпиталз':'Washington Capitals','Каролина Харрикейнз':'Carolina Hurricanes',
 'Чикаго Блэкхокс':'Chicago Blackhawks','Сент-Луис Блюз':'St. Louis Blues',
 'Нэшвилл Предаторз':'Nashville Predators','Даллас Старз':'Dallas Stars',
 'Колорадо Эвеланш':'Colorado Avalanche','Миннесота Уайлд':'Minnesota Wild',
 'Виннипег Джетс':'Winnipeg Jets','Вегас Голден Найтс':'Vegas Golden Knights',
 'Лос-Анджелес Кингз':'Los Angeles Kings','Анахайм Дакс':'Anaheim Ducks',
 'Сан-Хосе Шаркс':'San Jose Sharks','Сиэтл Кракен':'Seattle Kraken',
 'Эдмонтон Ойлерз':'Edmonton Oilers','Калгари Флэймз':'Calgary Flames',
 'Ванкувер Кэнакс':'Vancouver Canucks','Юта Маммот':'Utah Mammoth',
}


def timestamp(t=None):
    if t is None:return dt.datetime.now(UTC).isoformat(timespec='seconds')
    if isinstance(t,dt.datetime):return t.astimezone(UTC).isoformat(timespec='seconds')
    return str(t)


def tables(c):return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def init(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS v58_results(
       source_event_id TEXT PRIMARY KEY,sport TEXT NOT NULL,league TEXT NOT NULL,
       date_utc TEXT NOT NULL,home TEXT NOT NULL,away TEXT NOT NULL,
       home_score INTEGER NOT NULL,away_score INTEGER NOT NULL,source TEXT NOT NULL,
       source_url TEXT NOT NULL,observed_at TEXT NOT NULL,final_provenance TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS v58_results_sport_date ON v58_results(sport,league,date_utc);
    CREATE TABLE IF NOT EXISTS v58_source_runs(
       source_id TEXT PRIMARY KEY,last_attempt TEXT,last_ok TEXT,next_due TEXT,
       status TEXT NOT NULL,details TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v58_validation(
       sport TEXT NOT NULL,league TEXT NOT NULL,model TEXT NOT NULL,
       updated_at TEXT NOT NULL,oos_n INTEGER NOT NULL,
       brier REAL,baseline_brier REAL,calibration_error REAL,
       hit_rate REAL,high_conf_n INTEGER,high_conf_hit_rate REAL,
       temperature REAL NOT NULL,passed INTEGER NOT NULL,reason TEXT NOT NULL,
       PRIMARY KEY(sport,league,model));
    CREATE TABLE IF NOT EXISTS v58_coverage(
       root_id TEXT PRIMARY KEY,sport TEXT NOT NULL,league TEXT NOT NULL,
       kickoff_utc TEXT,home TEXT,away TEXT,readiness TEXT NOT NULL,
       available_models INTEGER NOT NULL,reason TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v58_forecasts(
       forecast_id TEXT PRIMARY KEY,root_id TEXT NOT NULL,sport TEXT NOT NULL,
       league TEXT NOT NULL,kickoff_utc TEXT NOT NULL,model TEXT NOT NULL,
       selection TEXT NOT NULL,probability REAL NOT NULL,created_at TEXT NOT NULL,
       training_games INTEGER NOT NULL,oos_n INTEGER NOT NULL,
       validation_brier REAL,calibration_temperature REAL NOT NULL,
       source_set TEXT NOT NULL,validated INTEGER NOT NULL,
       UNIQUE(root_id,model,selection));
    CREATE INDEX IF NOT EXISTS v58_forecast_root ON v58_forecasts(root_id);
    CREATE TABLE IF NOT EXISTS v58_work_runs(
       id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL,kind TEXT NOT NULL,
       report_json TEXT NOT NULL);
    ''');c.commit()


def _norm(s):
    import oracle_v5 as v5
    return v5.norm(str(s or ''))


def same_team(a,b):
    import oracle_v5 as v5
    return bool(v5.name_match(a,b)[1]=='EXPLICIT_ALIAS_OR_EXACT' or
                v5.name_match(b,a)[1]=='EXPLICIT_ALIAS_OR_EXACT' or
                (_norm(ALIASES.get(a,a))==_norm(b) and _norm(b)) or
                (_norm(ALIASES.get(b,b))==_norm(a) and _norm(a)))


def sport_of(s):
    s=str(s or '').lower()
    if s in ('americanfootball','am-football','american-football'):return 'americanfootball'
    return s


def collect_history(c,asof=None):
    """Final scores, no future matches or unsupported/unknown sources."""
    at=timestamp(asof);end=at[:10]
    result=[];seen=set();names=tables(c)
    if 'sf_matches' in names:
        sql='''SELECT id,sport,league,match_date,home,away,home_score,away_score,source,source_url
             FROM sf_matches WHERE match_date<? AND home_score IS NOT NULL AND away_score IS NOT NULL'''
        for row in c.execute(sql,(end,)):
            eid,s,league,date,h,a,hs,aw,src,url=row
            if not (str(src).startswith('openfootball') or str(src).startswith('nflverse')):continue
            if sport_of(s)=='football' and (not str(src).startswith('openfootball')):continue
            try:
                hs=int(hs);aw=int(aw)
                if not (0<=hs<=200 and 0<=aw<=200):continue
                t=(date,hs,aw,str(h),str(a));key=('sf',eid)
                if key in seen:continue
                seen.add(key)
                result.append((sport_of(s),str(league),str(date),str(h),str(a),hs,aw,str(src),str(url)))
            except (TypeError,ValueError):continue
    if 'v58_results' in names:
        for sport,league,day,h,a,hs,aw,src,url in c.execute('''SELECT sport,league,substr(date_utc,1,10),home,away,home_score,away_score,source,source_url
           FROM v58_results WHERE date_utc<? AND final_provenance='SOURCE_MARKED_FINAL' ''',(at,)):
            result.append((sport_of(sport),str(league),str(day),str(h),str(a),int(hs),int(aw),str(src),str(url)))
    # Some providers can represent the same result; prefer the first known source.
    uniq={}
    for r in result:
        key=(r[0],r[1],r[2],_norm(r[3]),_norm(r[4]))
        uniq.setdefault(key,r)
    return sorted(uniq.values(),key=lambda r:(r[2],r[0],r[1],r[3],r[4]))


def parse_espn(payload,sport,league,observed=None):
    """ESPN status.type.completed MUST be true. Scores alone prove nothing."""
    at=timestamp(observed);events=payload.get('events',[])
    if not isinstance(events,list):raise ValueError('BAD_SCOREBOARD_EVENTS')
    rows=[]
    for ev in events:
        if not isinstance(ev,dict):continue
        eid=ev.get('id');comps=ev.get('competitions') or []
        if not eid or not comps or not isinstance(comps[0],dict):continue
        competition=comps[0]
        status=competition.get('status') or ev.get('status') or {}
        tp=status.get('type') if isinstance(status,dict) else None
        if not isinstance(tp,dict) or tp.get('completed') is not True:continue
        if str(tp.get('name','')).upper() not in ('STATUS_FINAL','STATUS_FULL_TIME','STATUS_FINAL_OVERTIME','STATUS_FINAL_PENALTY_SHOOTOUT'):
            # Explicit final + completed required, not score-only.
            continue
        sides={}
        for x in competition.get('competitors',[]):
            if not isinstance(x,dict) or x.get('homeAway') not in ('home','away'):continue
            team=x.get('team') or {}
            name=team.get('displayName') or team.get('name')
            if not name:continue
            score=x.get('score')
            try:
                if isinstance(score,dict):score=score.get('value')
                n=float(score)
                if not n.is_integer() or not 0<=n<=250:continue
                sides[x['homeAway']]=(str(name),int(n))
            except (TypeError,ValueError):continue
        if 'home' not in sides or 'away' not in sides:continue
        date=str(ev.get('date') or competition.get('date') or '')
        try:
            start=dt.datetime.fromisoformat(date.replace('Z','+00:00')).astimezone(UTC)
            if start>dt.datetime.fromisoformat(at):continue
        except ValueError:continue
        h,hs=sides['home'];a,aw=sides['away']
        rows.append(('espn:'+sport+':'+league+':'+str(eid),sport,league,
            start.isoformat(timespec='seconds'),h,a,hs,aw,'ESPN_UNOFFICIAL_SCOREBOARD',
            'https://site.api.espn.com/apis/site/v2/sports/'+ESPN_LEAGUES[sport][0]+'/'+league+'/scoreboard',
            at,'SOURCE_MARKED_FINAL'))
    return rows


def fetch_json(url,timeout=11):
    req=urllib.request.Request(url,headers={'User-Agent':'OracleSportsResearch/5.8 (read-only; contact: local project)',
                                            'Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=timeout) as res:
        if res.status!=200:raise ValueError('HTTP_NON_200')
        size=int(res.headers.get('Content-Length') or '0')
        if size>2_000_000:raise ValueError('SOURCE_TOO_LARGE')
        payload=res.read(2_000_001)
    if len(payload)>2_000_000:raise ValueError('SOURCE_TOO_LARGE')
    return json.loads(payload)


def ingest_espn(c,now=None,limit=3,fetcher=fetch_json):
    """Bounded backfill requests; no accounts, scraping, or hidden access tokens."""
    init(c);at=timestamp(now)
    if str(os.getenv('ORACLE_V58_ESPN_ENABLED','1')).lower() in ('0','false','off'):
        return {'state':'DISABLED','attempted':0,'new_final_results':0}
    limit=max(0,min(int(limit),12));today=dt.date.fromisoformat(at[:10]);added=0;attempted=0;errors=[]
    # Spread bootstrap across runs, prioritize last 14d, then older days.
    # Work keys are persistent, so restarts do not reset the backfill cursor.
    tasks=[]
    for sport in ESPN_LEAGUES:
        candidates=[0,1,2,3,4,5,6,7,10,14,21,28,35,49,70,90,120,150,180,210,240,270,300,365,420,500]
        for age in candidates:
            day=today-dt.timedelta(days=age)
            sid=f'espn|{sport}|{day:%Y%m%d}'
            prev=c.execute('SELECT next_due FROM v58_source_runs WHERE source_id=?',(sid,)).fetchone()
            if prev and prev[0] and str(prev[0])>at:continue
            tasks.append((age,sport,day,sid))
    # Stable round robin with local precedence; bounded time.
    tasks.sort(key=lambda x:(x[0],x[1]))
    for age,sport,day,sid in tasks[:limit]:
        category,league=ESPN_LEAGUES[sport]
        url=f'https://site.api.espn.com/apis/site/v2/sports/{category}/{league}/scoreboard?dates={day:%Y%m%d}&limit=100'
        status='OK';msg='';n=0
        try:
            data=fetcher(url)
            records=parse_espn(data,sport,league,at)
            with c:
                for r in records:
                    c.execute('''INSERT INTO v58_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(source_event_id) DO UPDATE SET
                      home_score=excluded.home_score,away_score=excluded.away_score,
                      observed_at=excluded.observed_at,final_provenance=excluded.final_provenance''',r)
                n=len(records);added+=n
            due=dt.datetime.fromisoformat(at)+dt.timedelta(hours=12 if age<3 else 24*30)
        except Exception as exc:
            status='ERROR';msg=type(exc).__name__+': '+str(exc)[:110]
            errors.append({'source_id':sid,'error':msg})
            due=dt.datetime.fromisoformat(at)+dt.timedelta(hours=8)
        with c:c.execute('''INSERT INTO v58_source_runs VALUES(?,?,?,?,?,?)
           ON CONFLICT(source_id) DO UPDATE SET last_attempt=excluded.last_attempt,
             last_ok=COALESCE(excluded.last_ok,v58_source_runs.last_ok),next_due=excluded.next_due,
             status=excluded.status,details=excluded.details''',
           (sid,at,at if status=='OK' else None,due.isoformat(timespec='seconds'),status,
            json.dumps({'url':url,'events':n,'error':msg})))
        attempted+=1
        if fetcher is fetch_json:time.sleep(.3)
    return {'state':'OK' if not errors else 'PARTIAL','attempted':attempted,
            'new_final_results':added,'errors':errors[:3],'source':'ESPN_UNOFFICIAL_FINAL_ONLY'}


def league_games(history,sport,league,date):
    key=(sport_of(sport),str(league))
    return [r for r in history if (r[0],r[1])==key and r[2]<date]


def _sigmoid(x):
    if x>30:return 1.
    if x< -30:return 0.
    return 1/(1+math.exp(-x))


def _soften(vec,temp):
    vals=[max(1e-6,v)**(1.0/float(temp)) for v in vec]
    sm=sum(vals)
    return tuple(v/sm for v in vals)


def predict(method,games,home,away,sport,league=None):
    """Returns (home/draw/away vector, training-game count), or None.

    Distinct methods share a result dataset and are therefore correlated;
    independent MODEL_NAMES are not independent experimental evidence.
    """
    if len(games)<MIN_HISTORY:return None
    is_football=(sport_of(sport)=='football')
    games=sorted(games,key=lambda g:g[2])
    hp=[r for r in games if r[3]==home or r[4]==home]
    ap=[r for r in games if r[3]==away or r[4]==away]
    if len(hp)<MIN_TEAM or len(ap)<MIN_TEAM:return None
    draws=sum(r[5]==r[6] for r in games)
    d=min(.40,max(.12,(draws+12)/(len(games)+48))) if is_football else 0.
    if method=='ELO':
        ratings=collections.defaultdict(lambda:1500.)
        for r in games:
            h,a=_norm(r[3]),_norm(r[4]);ha=ratings[h];aa=ratings[a]
            exp=_sigmoid((ha-aa+60)/170)
            score=1 if r[5]>r[6] else .5 if r[5]==r[6] else 0
            change=23*(score-exp)
            ratings[h]+=change;ratings[a]-=change
        hn=_norm(home);an=_norm(away)
        if not any(r[3]==home or r[4]==home for r in games[-100:]):return None
        if not any(r[3]==away or r[4]==away for r in games[-100:]):return None
        ph=_sigmoid((ratings[hn]-ratings[an]+60)/170)
    elif method=='FORM':
        def form(rows,name):
            seq=rows[-18:];num=den=0.
            for age,r in enumerate(reversed(seq)):
                w=.93**age
                gd=(r[5]-r[6]) if r[3]==name else (r[6]-r[5])
                win=1 if gd>0 else .5 if gd==0 else 0
                num+=w*win;den+=w
            return (num+5)/(den+10)
        ph=min(.88,max(.12,.5+form(hp,home)-form(ap,away)+.05))
    elif method=='POISSON' and is_football:
        home_goals=mean(r[5] for r in games);away_goals=mean(r[6] for r in games)
        h=[r for r in games if r[3]==home]
        a=[r for r in games if r[4]==away]
        if len(h)<5 or len(a)<5:return None
        def shr(values,prior,n=15):return (sum(values)+prior*n)/(len(values)+n)
        ha=shr([r[5] for r in h],home_goals);hd=shr([r[6] for r in h],away_goals)
        aa=shr([r[6] for r in a],away_goals);ad=shr([r[5] for r in a],home_goals)
        lamh=max(.20,min(4.8,ha*ad/max(.5,home_goals)))
        lama=max(.20,min(4.8,aa*hd/max(.5,away_goals)))
        def pmf(lam):
            probs=[math.exp(-lam)]
            for k in range(1,13):probs.append(probs[-1]*lam/k)
            s=sum(probs)
            return [x/s for x in probs]
        x,y=pmf(lamh),pmf(lama)
        p1=sum(p*q for i,p in enumerate(x) for j,q in enumerate(y) if i>j)
        px=sum(p*q for i,p in enumerate(x) for j,q in enumerate(y) if i==j)
        return (p1,px,1-p1-px),len(games)
    else:return None
    return ((1-d)*ph,d,(1-d)*(1-ph)),len(games)


def _brier(p,actual):return sum((x-(i==actual))**2 for i,x in enumerate(p))/3


def _temp_tune(samples):
    if len(samples)<25:return 1.
    return min((.75,.9,1.,1.15,1.3,1.6),key=lambda t:mean(_brier(_soften(s[0],t),s[1]) for s in samples))


def validate_sport(method,games,sport,league,at=None,max_eval=120):
    """Chronological expanding-window walk-forward with disjoint tune/test periods."""
    hist=sorted(games,key=lambda r:r[2]);previous=[]
    # Use only the tail; each score is predicted with strict previous-day history.
    end=hist[-max_eval:] if len(hist)>max_eval else hist
    for row in end:
        past=[r for r in hist if r[2]<row[2]]
        pred=predict(method,past,row[3],row[4],sport,league)
        if not pred:continue
        actual=0 if row[5]>row[6] else 1 if row[5]==row[6] else 2
        if sport!='football' and actual==1:continue # reject 2-way draws
        if len(past)<100:continue
        base_draw=sum(r[5]==r[6] for r in past)/len(past) if sport=='football' else 0
        if sport=='football':
            h=sum(r[5]>r[6] for r in past)/len(past);a=1-h-base_draw
        else:
            h=sum(r[5]>r[6] for r in past)/len(past);a=1-h
        previous.append((pred[0],actual,(h,base_draw,a),row[2]))
    if len(previous)<2*MIN_OOS:
        return {'passed':False,'reason':'INSUFFICIENT_WALKFORWARD','oos_n':max(0,len(previous)//2),
            'temperature':1.,'brier':None,'baseline_brier':None,'calibration_error':None,
            'hit_rate':None,'high_conf_n':0,'high_conf_hit_rate':None}
    cut=max(25,len(previous)//2)
    temp=_temp_tune(previous[:cut]);test=previous[cut:]
    preds=[(_soften(p,temp),a,base) for p,a,base,_ in test]
    bm=mean(_brier(p,a) for p,a,b in preds)
    bb=mean(_brier(base,a) for p,a,base in preds)
    hit=mean(int(max(range(3),key=lambda i:p[i])==a) for p,a,b in preds)
    bins=collections.defaultdict(list)
    for p,a,_ in preds:
        i=max(range(3),key=lambda z:p[z]);bins[int(min(9,p[i]*10))].append((p[i],int(i==a)))
    ece=sum(len(vals)*abs(mean(x[0] for x in vals)-mean(x[1] for x in vals)) for vals in bins.values())/len(preds)
    high=[int(max(range(3),key=lambda i:p[i])==a) for p,a,b in preds if max(p)>=.8]
    # True historical edge cannot be inferred from accuracy alone or bookmaker line.
    passed=len(preds)>=MIN_OOS and bm+0.005<bb and ece<=.14
    return {'passed':passed,'reason':'PASSED' if passed else 'NOT_BETTER_THAN_BASELINE_OR_NOT_CALIBRATED',
        'oos_n':len(preds),'temperature':temp,'brier':round(bm,6),'baseline_brier':round(bb,6),
        'calibration_error':round(ece,6),'hit_rate':round(hit,6),
        'high_conf_n':len(high),'high_conf_hit_rate':round(mean(high),6) if high else None}


def evaluate(c,now=None,history=None,force=False):
    init(c);at=timestamp(now);hist=history if history is not None else collect_history(c,at)
    parts=collections.defaultdict(list)
    for r in hist:parts[(r[0],r[1])].append(r)
    out={'state':'OK','studied_leagues':0,'validated_models':0,'tested_models':0,'insufficient_leagues':0,'deferred_leagues':0}
    max_leagues=max(1,min(5,int(os.getenv('ORACLE_V58_LEAGUES_PER_RUN','2'))))
    updated_leagues=0
    for (sport,league),matches in sorted(parts.items()):
        if len(matches)<MIN_HISTORY:out['insufficient_leagues']+=1;continue
        out['studied_leagues']+=1
        due=c.execute('SELECT MIN(updated_at) FROM v58_validation WHERE sport=? AND league=?',(sport,league)).fetchone()[0]
        if due and not force and (dt.datetime.fromisoformat(at)-dt.datetime.fromisoformat(due)).total_seconds()<12*3600:
            continue
        if updated_leagues>=max_leagues:out['deferred_leagues']+=1;continue
        updated_leagues+=1
        for method in MODEL_NAMES:
            if method=='POISSON' and sport!='football':continue
            old=c.execute('SELECT updated_at FROM v58_validation WHERE sport=? AND league=? AND model=?',(sport,league,method)).fetchone()
            if old and not force and (dt.datetime.fromisoformat(at)-dt.datetime.fromisoformat(old[0])).total_seconds()<12*3600:
                continue
            report=validate_sport(method,matches,sport,league,at)
            out['tested_models']+=1;out['validated_models']+=int(report['passed'])
            with c:c.execute('''INSERT INTO v58_validation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(sport,league,model) DO UPDATE SET updated_at=excluded.updated_at,
                oos_n=excluded.oos_n,brier=excluded.brier,baseline_brier=excluded.baseline_brier,
                calibration_error=excluded.calibration_error,hit_rate=excluded.hit_rate,
                high_conf_n=excluded.high_conf_n,high_conf_hit_rate=excluded.high_conf_hit_rate,
                temperature=excluded.temperature,passed=excluded.passed,reason=excluded.reason''',
                (sport,league,method,at,report['oos_n'],report['brier'],report['baseline_brier'],
                 report['calibration_error'],report['hit_rate'],report['high_conf_n'],
                 report['high_conf_hit_rate'],report['temperature'],int(report['passed']),report['reason']))
    return out


def _match_teams(games,home,away):
    hset={r[3] for r in games}|{r[4] for r in games}
    hs=[h for h in hset if same_team(h,home)]
    aas=[a for a in hset if same_team(a,away)]
    if len(hs)!=1 or len(aas)!=1 or hs[0]==aas[0]:return None
    return hs[0],aas[0]


def _identity_index(by_league):
    import oracle_v5 as v5
    idx=collections.defaultdict(set)
    for (sport,league),records in by_league.items():
        teams={r[3] for r in records}|{r[4] for r in records}
        for name in teams:
            keys=set(v5.name_sets(name))|{_norm(ALIASES.get(name,name))}
            for k in keys:
                if k:idx[(sport,k)].add((league,name))
    return idx


def _lookup(index,sport,name):
    import oracle_v5 as v5
    keys={v5.norm(name),v5.translit(name),v5.norm(ALIASES.get(name,name))}
    found=set()
    for key in keys:
        found.update(index.get((sport,key),()))
    return found


def forecast(c,now=None,history=None,max_events=8000):
    """All quoted upcoming Fonbet roots get a coverage record, even if blocked."""
    init(c);at=timestamp(now);hist=history if history is not None else collect_history(c,at)
    by_sport=collections.defaultdict(list);by_league=collections.defaultdict(list)
    for g in hist:
        by_sport[g[0]].append(g);by_league[(g[0],g[1])].append(g)
    identity_index=_identity_index(by_league)
    validations={(sp,l,m):dict(zip(('oos_n','brier','temp','passed'),(n,b,t,ok)))
                 for sp,l,m,n,b,t,ok in c.execute('''SELECT sport,league,model,oos_n,brier,temperature,passed FROM v58_validation''')}
    quote_candidates=c.execute('''SELECT root_id,sport,league,home,away,kickoff_utc
       FROM v5_catalog WHERE root_exists=1 AND active_quotes>0 AND kickoff_utc>?
       ORDER BY kickoff_utc,root_id LIMIT ?''',(at,int(max_events))).fetchall()
    report=collections.Counter();created=0;live_football=[]
    with c:
        for root,sp,league,home,away,kick in quote_candidates:
            sport=sport_of(sp);root=str(root);day=str(kick)[:10]
            models=[];reason='';code='NO_HISTORY_FOR_SPORT';choices=[]
            import oracle_v5 as _v5
            if not _v5.is_fixture_market({'league':league,'root_exists':True}):code='NON_FIXTURE_MARKET'
            elif not home or not away:code='MISSING_PARTICIPANTS'
            elif sport not in ('football','americanfootball',*ESPN_LEAGUES) and sport not in by_sport:code='UNSUPPORTED_SPORT_HISTORY'
            else:
                home_names=_lookup(identity_index,sport,home)
                away_names=_lookup(identity_index,sport,away)
                for l,hname in home_names:
                    for al,aname in away_names:
                        if l!=al or hname==aname:continue
                        gs=[r for r in by_league[(sport,l)] if r[2]<day]
                        if len(gs)<MIN_HISTORY:continue
                        choices.append((l,gs,(hname,aname)))
                choices=list({(l,pair): (l,gs,pair) for l,gs,pair in choices}.values())
                if len(choices)>1:code='AMBIGUOUS_LEAGUE_OR_TEAM'
                elif len(choices)==0:code='INSUFFICIENT_VERIFIED_TEAM_HISTORY'
                else:
                    l,gs,pair=choices[0]
                    for m in MODEL_NAMES:
                        v=validations.get((sport,l,m))
                        if not v or not v['passed']:continue
                        p=predict(m,gs,pair[0],pair[1],sport,l)
                        if p:models.append((m,_soften(p[0],v['temp']),p[1],v,l))
                    code='VALIDATED_RESEARCH_READY' if models else 'NO_MODEL_PASSED_WALKFORWARD'
            report[code]+=1
            c.execute('''INSERT INTO v58_coverage VALUES(?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(root_id) DO UPDATE SET sport=excluded.sport,league=excluded.league,
              kickoff_utc=excluded.kickoff_utc,home=excluded.home,away=excluded.away,
              readiness=excluded.readiness,available_models=excluded.available_models,
              reason=excluded.reason,updated_at=excluded.updated_at''',
              (root,sp,league,kick,home,away,code,len(models),reason or code,at))
            for method,probs,n,v,l in models:
                for i,s in enumerate(('1','X','2')):
                    if sport!='football' and s=='X':continue
                    probability=probs[i]
                    if not (0<probability<1):continue
                    fid=hashlib.sha256(('v58|'+root+'|'+method+'|'+s).encode()).hexdigest()[:32]
                    cur=c.execute('''INSERT OR IGNORE INTO v58_forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (fid,root,sp,l,kick,'v58_'+method,s,probability,at,n,v['oos_n'],
                           v['brier'],v['temp'],'SPORTS_HISTORICAL_SCORES_ONLY',1))
                    created+=int(cur.rowcount>0)
                    # Football: documented 1X2 full-time market only; do NOT
                    # push NHL/NBA/NFL moneyline because period semantics differ.
                    if sport=='football':live_football.append((fid,root,sp,league,home,away,kick,method,s,probability,l,n,v))
    # Write validated model outputs into existing V5 pipeline. No direct bets.
    with c:
        for fid,root,sp,league,home,away,kick,method,s,p,l,n,v in live_football:
            first=c.execute('SELECT created_at,probability FROM v58_forecasts WHERE forecast_id=?',(fid,)).fetchone()
            if not first:continue
            issued,locked_p=first
            details={'v58_validation':'WALKFORWARD_PASSED','history_league':l,
                     'oos_n':v['oos_n'],'brier':v['brier'],'no_book_odds_as_features':True}
            c.execute('''INSERT OR IGNORE INTO v5_forecasts(
              id,origin,original_event_id,created_at,sport,league,home,away,match_date,
              kickoff_utc,market,selection,probability,model,root_id,match_rule,price,
              quote_key,quote_snapshot,price_status,evaluated_at,details_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (fid,'v58_brain','v58|'+root,issued,sp,league,home,away,kick[:10],kick,
               '1X2',s,locked_p,'v58_'+method+'_WALKFORWARD',root,'VERIFIED_FONBET_ROOT',
               None,None,None,'AWAITING_POST_FORECAST_QUOTE',at,json.dumps(details)))
    return {'state':'OK','fonbet_quoted_upcoming_scanned':len(quote_candidates),
            'coverage_states':dict(report),'new_model_forecasts':created,
            'no_fabricated_probabilities':True,'all_sports_visible_even_if_not_ready':True}


def attach(c,now=None):
    """Require bookmaker quote captured AFTER model creation and before kickoff."""
    import oracle_v5 as v5
    at=timestamp(now)
    pk=v5.source_packet(c)
    fresh=pk and -2<=(v5.parse(at)-v5.parse(pk['at'])).total_seconds()/60<=v5.SNAPSHOT_MAX_AGE_MIN
    if not fresh:return {'state':'SOURCE_STALE','attached':0}
    rows=c.execute('''SELECT f.id,f.root_id,f.market,f.selection,f.created_at,
        f.kickoff_utc FROM v5_forecasts f WHERE f.origin='v58_brain'
        AND f.price_status='AWAITING_POST_FORECAST_QUOTE' AND f.kickoff_utc>?''',(at,)).fetchall()
    qcache=v5.current_quotes(c,list({r[1] for r in rows}));count=0
    with c:
        for fid,root,market,sel,issued,kick in rows:
            selection=v5.canonical_selection(market,sel)
            if not selection:continue
            q=qcache.get(root,{}).get(selection)
            if not q:continue
            price,key,observed=q
            if not (issued<=observed<=at<kick):continue
            # packet update may coincide with forecast timestamp; ties count
            # only if same-time event snapshot actually persisted and verified.
            c.execute('''UPDATE v5_forecasts SET price=?,quote_key=?,quote_snapshot=?,
                price_status='PRICE_ATTACHED_TIME_VALID',evaluated_at=?
                WHERE id=? AND price_status='AWAITING_POST_FORECAST_QUOTE' ''',
                (price,key,observed,at,fid))
            c.execute('''INSERT OR IGNORE INTO v5_price_captures
                VALUES(?,?,?,?,?,?,?)''',(fid,key,observed,price,sel,str(selection),at))
            count+=1
    return {'state':'OK','attached':count,'awaiting_quote':len(rows)-count,
        'rule':'post_forecast_and_pre_kickoff_only'}


def _wilson_lower(successes,n,z=1.95996398454):
    if n<=0:return 0.
    p=successes/n;den=1+z*z/n
    center=p+z*z/(2*n)
    err=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)
    return max(0.,(center-err)/den)


def portfolio_goal(c):
    if 'v56_portfolio' not in tables(c):
        return {'settled_n':0,'win_n':0,'hit_rate':None,'wilson_lower_95':None,'goal_80_proven':False}
    r=c.execute('''SELECT COUNT(*),COALESCE(SUM(CASE WHEN p.status='WIN' THEN 1 ELSE 0 END),0),
        COALESCE(SUM(p.profit),0),COALESCE(SUM(p.stake),0)
        FROM v56_portfolio x JOIN v55_paper p ON p.forecast_id=x.forecast_id
        WHERE p.status IN ('WIN','LOSS','PUSH')''').fetchone()
    n,win,profit,stake=map(float,r);n=int(n);win=int(win)
    lower=_wilson_lower(win,n) if n else None
    roi=profit/stake if stake else None
    return {'settled_n':n,'win_n':win,'hit_rate':round(win/n,5) if n else None,
       'wilson_lower_95':round(lower,5) if lower is not None else None,
       'net_profit_usd':round(profit,2),'roi':round(roi,5) if roi is not None else None,
       'goal_80_proven':bool(n>=100 and lower>.8 and profit>0),
       'minimum_evidence_for_goal':100,'never_target_train_on_test':True}


def report(c,limit=30):
    init(c)
    statuses={k:n for k,n in c.execute('SELECT readiness,COUNT(*) FROM v58_coverage GROUP BY readiness')}
    evaluated=[dict(zip(('sport','league','model','oos_n','brier','baseline_brier','ece',
        'hit_rate','high_conf_n','high_conf_hit_rate','temperature','passed','reason'),r))
        for r in c.execute('''SELECT sport,league,model,oos_n,brier,baseline_brier,calibration_error,
         hit_rate,high_conf_n,high_conf_hit_rate,temperature,passed,reason
         FROM v58_validation ORDER BY passed DESC,oos_n DESC LIMIT ?''',(limit,))]
    return {'version':VERSION,'state':'RESEARCH_ONLY','goal_hit_rate':TARGET_HIT_RATE,
        'goal_verified':portfolio_goal(c)['goal_80_proven'],'empirical_portfolio_accuracy':portfolio_goal(c),'fonbet_coverage':statuses,
        'forecast_probabilities':c.execute('SELECT COUNT(*) FROM v58_forecasts').fetchone()[0],
        'football_market_candidates_with_history':c.execute("SELECT COUNT(*) FROM v5_forecasts WHERE origin='v58_brain'").fetchone()[0],
        'validation':evaluated,'source_results':c.execute('SELECT COUNT(*) FROM v58_results').fetchone()[0],
        'real_orders':0,'explicit_warnings':['Models may share historical scores; not statistically independent.',
        'Backtest accuracy never guarantees 80 percent future hits.',
        'Only football full-time 1X2 outputs join the paper pipeline when market identity and prices are verified.',
        'Some sports have no verified history or market semantics and remain research-only.']}


def sync(c,now=None,fetch_limit=2,force_eval=False):
    init(c);at=timestamp(now);res={}
    res['sources']=ingest_espn(c,at,fetch_limit)
    history=collect_history(c,at)
    res['history']={'state':'OK','verified_completed_results':len(history),
                    'sports':dict(collections.Counter(r[0] for r in history))}
    res['validation']=evaluate(c,at,history,force_eval)
    res['coverage']=forecast(c,at,history)
    res['prices']=attach(c,at)
    with c:c.execute('INSERT INTO v58_work_runs(at,kind,report_json) VALUES(?,?,?)',
                    (at,'sync',json.dumps(res,ensure_ascii=False)[:12000]))
    return {'state':'OK' if res['sources']['state'] in ('OK','DISABLED') else 'PARTIAL',
            **res,'real_orders':0}


def resolve_final_for_portfolio(c,original_id,kick,now=None):
    """Only a UNIQUE identity-verified finalized game; never based on clocks alone."""
    if not str(original_id).startswith('v58|'):return None
    root=original_id.split('|',1)[1];at=timestamp(now)
    ev=c.execute('SELECT sport,home,away,kickoff_utc FROM v5_catalog WHERE root_id=?',(root,)).fetchone()
    if not ev or ev[3]!=kick:return None
    sport,h,a,_=ev;target=kick[:10];matches=[]
    if 'sf_matches' in tables(c):
        for day,h1,a1,hs,aw,source,seen in c.execute('''SELECT match_date,home,away,home_score,away_score,source,last_seen
             FROM sf_matches WHERE sport=? AND match_date BETWEEN ? AND ?
             AND home_score IS NOT NULL AND away_score IS NOT NULL''',
             ('football',target,target)):
            if not str(source).startswith('openfootball'):continue
            if same_team(h,h1) and same_team(a,a1) and seen and seen<=at:
                if dt.datetime.fromisoformat(at)>=dt.datetime.fromisoformat(kick)+dt.timedelta(hours=3):
                    matches.append((int(hs),int(aw),seen,source))
    if len(matches)!=1:return None
    return matches[0]
