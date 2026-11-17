#!/usr/bin/env python3
"""HOPE V4 isolated supplementary result bridge (paper only).

V3 strict+simulation is always executed first. Then V4 attempts new independent
ESPN/TheSportsDB verification for unresolved, non-NFL football. V4 never promotes
single-source, guessed or contradictory results, and never alters production.
"""
from __future__ import annotations
import collections, datetime as dt, fcntl, hashlib, importlib.util, json, math, os
import pathlib, re, shutil, sqlite3, sys, time, unicodedata, urllib.request, urllib.parse, uuid

BASE=pathlib.Path('/opt/ORACLE_EVA/hope_ops')
STATE=BASE/'state'
V3=BASE/'releases/v3_0_paper_finality/finality.py'
EXPECTED_V3_SHA='26cb79f1f358f840d42c867e759ea63651df81bdffd5e09b3fe6d0489d7bcc7c'
DATA=pathlib.Path('/opt/ORACLE_EVA/replica_v170/state')
DB=DATA/'oracle.sqlite3'
BACKUP=DATA/'eva-backups'
SOURCE='HOPE_V4_INDEPENDENT_SCORE_AS_RECORDED_ODDS_NOT_BOOKMAKER_VERIFIED'
UTC=dt.timezone.utc
MAX_TSDB_DAYS=32
MAX_ESPN_CALLS=48
MAX_HTTP_BYTES=2_000_000
LEAGUES={'en.1':'eng.1','en.2':'eng.2','de.1':'ger.1','de.2':'ger.2',
         'es.1':'esp.1','fr.1':'fra.1','it.1':'ita.1','nl.1':'ned.1',
         'pt.1':'por.1','tr.1':'tur.1'}
# Only unambiguous FULL names; never accept ambiguous generic "Premier League".
# Explicit, audited canonical club aliases from the installed R10 matching policy.
TEAM_ALIASES={
 'man utd':'manchester united','man united':'manchester united',
 'man city':'manchester city','manchester city':'manchester city',
 'nottm forest':'nottingham forest','nottingham forest':'nottingham forest',
 'spurs':'tottenham','tottenham hotspur':'tottenham',
 'wolves':'wolverhampton','wolverhampton wanderers':'wolverhampton',
 'inter milan':'inter','internazionale milano':'inter','fc internazionale milano':'inter',
 'atletico de madrid':'atletico madrid','stade rennais':'rennes',
 'olympique lyonnais':'lyon','olympique de marseille':'marseille',
 'juventus':'juventus','juventus fc':'juventus',
 'bayer 04 leverkusen':'bayer leverkusen',
}
TSDB_LEAGUES={
 'en.1':{'english premier league'},'en.2':{'english league championship','english championship'},
 'de.1':{'german bundesliga'},'de.2':{'german 2 bundesliga','german bundesliga 2'},
 'es.1':{'spanish la liga'},'fr.1':{'french ligue 1'},'it.1':{'italian serie a'},
 'nl.1':{'dutch eredivisie'},'pt.1':{'portuguese primeira liga'},
 'tr.1':{'turkish super lig'},
}

def stamp():return dt.datetime.now(UTC).isoformat(timespec='seconds')
def now():return dt.datetime.now(UTC)
def emit(label,data):print('HOPE_V4_'+label+' '+json.dumps(data,sort_keys=True,ensure_ascii=False,default=str),flush=True)
def sha12(s):return hashlib.sha256(str(s).encode('utf-8')).hexdigest()[:12]
def clock(s):
    try:
        d=dt.datetime.fromisoformat(str(s).replace('Z','+00:00'))
        return d.astimezone(UTC) if d.tzinfo else None
    except (ValueError,TypeError,OverflowError):return None

def norm(s):
    s=unicodedata.normalize('NFKD',str(s or ''))
    s=''.join(x for x in s if not unicodedata.combining(x))
    words=re.findall(r'[a-z0-9]+',s.casefold())
    # Suffix FC is purely structural, but don't remove meaningful words.
    while words and words[-1] in ('fc','afc'):words.pop()
    n=' '.join(words)
    return TEAM_ALIASES.get(n,n)

def load_v3():
    if hashlib.sha256(V3.read_bytes()).hexdigest()!=EXPECTED_V3_SHA:
        raise RuntimeError('V3_SOURCE_HASH_CHANGED')
    spec=importlib.util.spec_from_file_location('hope_v3_trusted',str(V3))
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    if mod.DB!=DB:raise RuntimeError('V3_DB_IDENTITY_CHANGED')
    return mod

def safe_get(url, getter=None):
    u=urllib.parse.urlsplit(url)
    if u.scheme!='https' or u.port not in (None,443) or u.username or u.password or u.fragment:
        raise RuntimeError('UNSAFE_PUBLIC_URL')
    if u.hostname=='www.thesportsdb.com':
        if u.path!='/api/v1/json/123/eventsday.php' or not re.fullmatch(r'd=20\d{2}-\d{2}-\d{2}&s=Soccer',u.query):
            raise RuntimeError('TSDB_PATH_DENIED')
    elif u.hostname=='site.api.espn.com':
        if not re.fullmatch(r'/apis/site/v2/sports/soccer/[a-z]{3}\.\d/scoreboard',u.path) or not re.fullmatch(r'dates=20\d{6}&limit=150',u.query):
            raise RuntimeError('ESPN_PATH_DENIED')
    else:raise RuntimeError('HOST_DENIED')
    if getter is not None:
        b=getter(url)
        if isinstance(b,str):b=b.encode('utf-8')
        if not isinstance(b,bytes) or len(b)>MAX_HTTP_BYTES:raise RuntimeError('MOCK_PAYLOAD_TOO_BIG')
        return b
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):raise RuntimeError('REDIRECT_DENIED')
    req=urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'HOPE-EVA-PaperBridge/4.0'})
    with urllib.request.build_opener(NoRedirect()).open(req,timeout=12) as response:
        if response.status!=200 or response.geturl()!=url:raise RuntimeError('UNEXPECTED_SOURCE_RESPONSE')
        b=response.read(MAX_HTTP_BYTES+1)
    if len(b)>MAX_HTTP_BYTES:raise RuntimeError('SOURCE_SIZE_LIMIT')
    return b

def tsdb_url(day):return 'https://www.thesportsdb.com/api/v1/json/123/eventsday.php?d='+day+'&s=Soccer'
def espn_url(league,day):
    return 'https://site.api.espn.com/apis/site/v2/sports/soccer/'+LEAGUES[league]+'/scoreboard?dates='+day.replace('-','')+'&limit=150'

def tsdb_rows(data):
    payload=json.loads(data)
    if not isinstance(payload,dict) or not isinstance(payload.get('events'),(list,type(None))):
        raise RuntimeError('INVALID_TSDB_RESPONSE')
    rows=[]
    for x in payload.get('events') or []:
        if not isinstance(x,dict):continue
        status=str(x.get('strStatus') or '').strip().lower()
        # A match marked 'finished' may include penalties: for basic FT markets require FT/AOT.
        # Only normal full-time; AOT/penalties need market-specific settlement rules.
        if status!='ft':continue
        if str(x.get('strSport') or '').lower() not in ('soccer','football'):continue
        try:hs=int(x['intHomeScore']);aw=int(x['intAwayScore'])
        except (ValueError,TypeError,KeyError):continue
        if hs<0 or aw<0 or hs>40 or aw>40:continue
        day=str(x.get('dateEvent') or '')
        try:dt.date.fromisoformat(day)
        except ValueError:continue
        t=clock(x.get('strTimestamp'))
        rows.append({'provider':'thesportsdb','id':str(x.get('idEvent') or ''),
          'home':str(x.get('strHomeTeam') or ''),'away':str(x.get('strAwayTeam') or ''),
          'league':norm(x.get('strLeague')),'day':day,'kickoff':t,'score':[hs,aw],
          'status':status})
    return rows

def espn_rows(data):
    obj=json.loads(data)
    if not isinstance(obj,dict) or not isinstance(obj.get('events'),(list,type(None))):
        raise RuntimeError('INVALID_ESPN_RESPONSE')
    result=[]
    for ev in obj.get('events') or []:
        st=(ev.get('status') or {}).get('type') or {}
        if st.get('completed') is not True or st.get('state')!='post':continue
        kickoff=clock(ev.get('date'))
        sides={}
        for e in (ev.get('competitions') or [{}])[0].get('competitors',[]):
            team=e.get('team') or {};ha=e.get('homeAway')
            try:s=int(e.get('score'))
            except (TypeError,ValueError):continue
            if ha in ('home','away') and 0<=s<=40:
                sides[ha]=(team.get('displayName') or team.get('name') or '',s)
        if set(sides)!={'home','away'} or not kickoff:continue
        result.append({'provider':'espn','id':str(ev.get('id') or ''),
             'home':sides['home'][0],'away':sides['away'][0],
             'day':kickoff.date().isoformat(),'kickoff':kickoff,
             'score':[sides['home'][1],sides['away'][1]]})
    return result

def matching(t, rows, require_league=False):
    match_date=dt.date.fromisoformat(t['match_date'])
    out=[]
    for r in rows:
        if norm(t['home'])!=norm(r['home']) or norm(t['away'])!=norm(r['away']):continue
        if require_league and r['league'] not in TSDB_LEAGUES.get(t['league'],set()):continue
        day=dt.date.fromisoformat(r['day'])
        if abs((match_date-day).days)>1:continue
        out.append(r)
    # ESPN same event is often present for 2 adjacent date queries; dedupe ID.
    result={}
    for r in out:
        k=r['id']
        if not k or (k in result and result[k]['score']!=r['score']):return None,'SOURCE_ID_CONFLICT'
        result[k]=r
    if len(result)!=1:return None,'NOT_FOUND' if len(result)==0 else 'AMBIGUOUS_IDENTITIES'
    return next(iter(result.values())),'MATCHED'

def round_robin(items,limit,index):
    items=sorted(items)
    if not items:return []
    return [items[(index*limit+i)%len(items)] for i in range(min(limit,len(items)))]

def fetch_sources(targets, getter=None, cycle_index=0):
    # Rotate source windows on each successful cycle; old dates cannot starve
    # newer dates when the pending backlog grows beyond a single request cap.
    all_days={(dt.date.fromisoformat(t['match_date'])+dt.timedelta(days=d)).isoformat()
        for t in targets.values() for d in (-1,0,1)}
    days=round_robin(all_days,MAX_TSDB_DAYS,cycle_index)
    tsdb={};errors=[];requests=0
    for day in days:
        try:tsdb[day]=tsdb_rows(safe_get(tsdb_url(day),getter));requests+=1
        except Exception as exc:errors.append({'source':'thesportsdb','day':day,'error':type(exc).__name__})
    todo=set()
    for t in targets.values():
        es=(t.get('providers') or {}).get('espn') or {}
        if es.get('state')=='FINAL_SCORE_MATCHED':continue
        for d in (-2,-1,0,1,2):
            day=(dt.date.fromisoformat(t['match_date'])+dt.timedelta(days=d)).isoformat()
            todo.add((t['league'],day))
    espn={}
    for lg,day in round_robin(todo,MAX_ESPN_CALLS,cycle_index):
        try:espn[lg,day]=espn_rows(safe_get(espn_url(lg,day),getter));requests+=1
        except Exception as exc:errors.append({'source':'espn','league':lg,'day':day,'error':type(exc).__name__})
    return {'tsdb':tsdb,'espn':espn,'requests':requests,'errors':errors,
            'requests_suppressed':max(0,len(todo)-MAX_ESPN_CALLS),
            'days_suppressed':max(0,len(all_days)-MAX_TSDB_DAYS),'cycle_index':cycle_index}

def score_proof(t, evidence):
    if t.get('match_state') in ('SOURCE_CONFLICT','AMBIGUOUS_PROVIDER_IDENTITY'):
        return None,'R10_CONFLICTING_PROVIDER_EVIDENCE'
    day=dt.date.fromisoformat(t['match_date'])
    matches=[]
    for diff in (-1,0,1):
        matches+=evidence['tsdb'].get((day+dt.timedelta(days=diff)).isoformat(),[])
    secondary,reason=matching(t,matches,True)
    if secondary is None:return None,'TSDB_'+reason
    old=(t.get('providers') or {}).get('espn') or {}
    if old.get('state')=='FINAL_SCORE_MATCHED':
        k=clock(old.get('kickoff_utc'))
        raw_score=old.get('score')
        if not k or not str(old.get('event_id') or '').isdigit() or not isinstance(raw_score,list) or len(raw_score)!=2:
            return None,'R10_ESPN_EVIDENCE_CORRUPT'
        esp={'id':str(old['event_id']),'kickoff':k,'score':raw_score,'provider':'espn'}
    else:
        rows=[]
        for shift in (-2,-1,0,1,2):
            rows+=evidence['espn'].get((t['league'],(day+dt.timedelta(days=shift)).isoformat()),[])
        esp,reason=matching(t,rows)
        if esp is None:return None,'ESPN_'+reason
    if not str(esp['id']).isdigit() or not str(secondary['id']).isdigit():
        return None,'INVALID_PROVIDER_EVENT_ID'
    if secondary['score']!=esp['score']:return None,'TWO_SOURCE_SCORE_CONFLICT'
    for key in ('football_data','openfootball'):
        old_provider=(t.get('providers') or {}).get(key) or {}
        if old_provider.get('state')=='FINAL_SCORE_MATCHED' and old_provider.get('score')!=esp['score']:
            return None,'THIRD_SOURCE_SCORE_CONFLICT'
    # If TSDB has a UTC timestamp it must corroborate ESPN's kickoff; otherwise
    # original kickoff must agree with ESPN <=10min (no speculative timezone fix).
    correction_allowed=False
    if secondary['kickoff']:
        if abs((secondary['kickoff']-esp['kickoff']).total_seconds())>600:
            return None,'KICKOFF_PROVIDERS_DISAGREE'
        correction_allowed=True
    return {'score':esp['score'],'espn_id':esp['id'],'tsdb_id':secondary['id'],
        'espn_kickoff':esp['kickoff'],'tsdb_kickoff':secondary['kickoff'],
        'corrected_kickoff_allowed':correction_allowed,
        'grade':'ESPN_AND_THESPORTSDB_FINAL_SCORES'},'TWO_SOURCE_FINAL'

def plan(report,connection,evidence,now_dt=None):
    now_dt=now_dt or now()
    details=report.get('details')
    if not isinstance(details,list) or len(details)!=report.get('pending_total'):
        raise RuntimeError('INCOMPLETE_R10_REPORT')
    targets={k:v for k,v in (report.get('event_evidence') or {}).items()
             if v.get('league') in LEAGUES and isinstance(v.get('match_date'),str) and
                 isinstance(v.get('home'),str) and isinstance(v.get('away'),str) and
                 v['home'] and v['away'] and v.get('league') in TSDB_LEAGUES}
    # Read-only PENDING paper: V3 may have closed some of the R10 census after
    # report generation. Protect by verifying exact snapshots in each candidate.
    from collections import defaultdict
    v3=load_v3();papers=defaultdict(list)
    for record in connection.execute('SELECT '+','.join(v3.PAPER_COLS)+' FROM v55_paper'):
        p=dict(zip(v3.PAPER_COLS,record));papers[sha12(p['forecast_id'])].append(p)
    if any(len(v)>1 for v in papers.values()):raise RuntimeError('FORECAST_DIGEST_COLLISION')
    proof_cache={};new=[];refusal=collections.Counter();source_stats=collections.Counter();seen=set()
    for item in details:
        key=str(item['forecast_sha12']);arr=papers.get(key,[])
        if len(arr)!=1:raise RuntimeError('CENSUS_FORECAST_DIGEST_MISSING')
        p=arr[0];fid=p['forecast_id']
        if fid in seen:raise RuntimeError('REPEATED_R10_FORECAST')
        seen.add(fid)
        if p['status']!='PENDING':refusal['ALREADY_SETTLED']+=1;continue
        evkey=sha12(p['original_event_id']);t=targets.get(evkey)
        if not t:refusal['NO_SUPPORTED_IDENTIFIED_FOOTBALL_EVENT']+=1;continue
        if evkey not in proof_cache:
            proof_cache[evkey]=score_proof(t,evidence)
        proof,state=proof_cache[evkey];source_stats[state]+=1
        if not proof:refusal[state]+=1;continue
        if (str(p['origin'])!=item.get('origin') or evkey!=item.get('original_event_sha12') or
            p['market']!=item.get('market') or p['selection']!=item.get('selection')):
            refusal['PAPER_IDENTITY_OR_MARKET_CHANGED']+=1;continue
        if (not isinstance(p['odds'],(int,float)) or isinstance(p['odds'],bool) or
            not math.isfinite(p['odds']) or not 1.001<p['odds']<=1001 or
            not isinstance(p['stake'],(int,float)) or isinstance(p['stake'],bool) or
            not math.isfinite(p['stake']) or p['stake']<=0 or
            p['odds']!=item.get('odds') or p['stake']!=item.get('stake')):
            refusal['PAPER_PRICE_OR_STAKE_CHANGED']+=1;continue
        kick=clock(p['kickoff_utc']);quote=clock(p['quote_at']);registered=clock(p['registered_at'])
        actual=proof['espn_kickoff']
        if not kick or not quote or not registered or not quote<registered<kick or registered>=actual:
            refusal['UNVERIFIED_PREKICKOFF_TIMING']+=1;continue
        if clock(item.get('kickoff_utc'))!=kick:
            refusal['R10_KICKOFF_CHANGED']+=1;continue
        offset=abs((actual-kick).total_seconds())
        if offset>600 and not (proof['corrected_kickoff_allowed'] and offset<=7200):
            refusal['KICKOFF_IDENTITY_NOT_PROVEN']+=1;continue
        if now_dt<actual+dt.timedelta(minutes=110):
            refusal['FINAL_TOO_EARLY']+=1;continue
        if p['origin']=='sf_forecasts':
            sf=connection.execute('SELECT home_score,away_score FROM sf_matches WHERE id=?',(p['original_event_id'],)).fetchall()
            if len(sf)!=1 or (sf[0][0] is None)!=(sf[0][1] is None) or (sf[0][0] is not None and list(sf[0])!=proof['score']):
                refusal['NATIVE_FIXTURE_CONFLICT']+=1;continue
        elif p['origin']=='v58_brain':
            if not str(p['original_event_id']).startswith('v58|') or str(p['root_id'])!=str(p['original_event_id']).split('|',1)[1]:
                refusal['V58_ROOT_IDENTITY_MISMATCH']+=1;continue
        else:refusal['UNSUPPORTED_ORIGIN']+=1;continue
        other=connection.execute("""SELECT COUNT(*) FROM v55_paper WHERE original_event_id=? AND origin=?
          AND status IN ('WIN','LOSS','PUSH') AND result_home IS NOT NULL AND result_away IS NOT NULL
          AND (result_home!=? OR result_away!=?)""",
          (p['original_event_id'],p['origin'],proof['score'][0],proof['score'][1])).fetchone()[0]
        if other:refusal['PREEXISTING_SETTLEMENT_SCORE_CONFLICT']+=1;continue
        outcome=v3.normalized_market_result(p['market'],p['selection'],*proof['score'])
        if outcome not in ('WIN','LOSS','PUSH'):refusal['UNSUPPORTED_MARKET']+=1;continue
        net=round(float(p['stake']*(p['odds']-1) if outcome=='WIN' else (-p['stake'] if outcome=='LOSS' else 0)),6)
        new.append({'forecast_id':fid,'original':p,'status':outcome,'profit':net,
          'result_home':proof['score'][0],'result_away':proof['score'][1],
          'event_sha12':evkey,'source_grade':proof['grade'],'sources':{
            'ESPN_EVENT_ID':proof['espn_id'],'TSDB_EVENT_ID':proof['tsdb_id']},
          'kickoff_delta_seconds':round(offset,1)})
    return new,dict(refusal),dict(source_stats)

def atomic_json(path,obj):
    path=pathlib.Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    with tmp.open('x',encoding='utf-8') as f:
        os.chmod(tmp,0o600);json.dump(obj,f,ensure_ascii=False,sort_keys=True,indent=2,default=str)
        f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def apply(candidates):
    if not candidates:return {'newly_closed':0,'state':'NO_NEW_CONFIRMED_RESULTS','backups_created':0}
    # Uses V3 tested online SQLite backup, proven disk-space budget and fail-closed identity.
    v3=load_v3();backup,tag=v3.backup_database()
    manifest=BACKUP/('hope_v4_'+tag+'.manifest.json')
    at=stamp();source=SOURCE
    item={'release':'HOPE_V4_RESULT_BRIDGE','state':'PREPARED','source':source,
          'created_at':at,'candidates':candidates,'backup':str(backup),
          'price_verification':'UNVERIFIED_BOOKMAKER_AS_RECORDED_ONLY','real_orders':0}
    atomic_json(manifest,item)
    db=sqlite3.connect(str(DB),timeout=30,isolation_level=None)
    try:
        db.execute('BEGIN IMMEDIATE')
        for x in candidates:
            old=db.execute('SELECT '+','.join(v3.PAPER_COLS)+' FROM v55_paper WHERE forecast_id=?',(x['forecast_id'],)).fetchall()
            if len(old)!=1 or dict(zip(v3.PAPER_COLS,old[0]))!=x['original']:
                raise RuntimeError('CHANGED_SINCE_SOURCE_CHECK')
        for x in candidates:
            r=db.execute('''UPDATE v55_paper SET status=?,result_home=?,result_away=?,result_seen=?,
                        settled_at=?,profit=?,source=? WHERE forecast_id=? AND status='PENDING' ''',
                        (x['status'],x['result_home'],x['result_away'],at,at,x['profit'],source,
                        x['forecast_id']))
            if r.rowcount!=1:raise RuntimeError('ATOMIC_UPDATE_COUNT_MISMATCH')
        db.execute('''INSERT INTO v55_settlement_runs(checked_at,checked,settled,outcomes_unavailable,report)
                     VALUES(?,?,?,?,?)''',(at,len(candidates),len(candidates),0,json.dumps({
                         'bridge':'HOPE_V4','tag':tag,'source':source,
                         'not_a_verified_bookmaker_quote':True,'real_orders':0})))
        db.execute('COMMIT')
    except Exception:
        if db.in_transaction:db.execute('ROLLBACK')
        raise
    finally:db.close()
    item['state']='APPLIED';atomic_json(manifest,item)
    ro=sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True)
    try:
        for x in candidates:
            row=ro.execute('SELECT status,result_home,result_away,profit,source,settled_at FROM v55_paper WHERE forecast_id=?',(x['forecast_id'],)).fetchone()
            if row!=(x['status'],x['result_home'],x['result_away'],x['profit'],source,at):
                raise RuntimeError('POSTCOMMIT_PROOF_INCONSISTENT')
    finally:ro.close()
    item['state']='APPLIED_VERIFIED';atomic_json(manifest,item)
    output={'newly_closed':len(candidates),'backups_created':1,'state':'APPLIED_VERIFIED',
            'net_simulated_usd':round(sum(x['profit'] for x in candidates),4),
            'outcomes':dict(collections.Counter(x['status'] for x in candidates)),
            'backup':str(backup),'manifest':str(manifest),'real_orders':0}
    emit('BRIDGE_APPLIED',output);return output

def reconcile_manifests():
    if not BACKUP.is_dir():return
    v3=load_v3()
    for path in BACKUP.glob('hope_v4_*.manifest.json'):
        data=json.loads(path.read_text())
        if data.get('state') not in ('PREPARED','APPLIED'):continue
        if data.get('release')!='HOPE_V4_RESULT_BRIDGE' or data.get('source')!=SOURCE:
            raise RuntimeError('UNRECOGNIZED_PENDING_V4_MANIFEST')
        states=set()
        ro=sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True)
        try:
            for x in data['candidates']:
                old=ro.execute('SELECT '+','.join(v3.PAPER_COLS)+' FROM v55_paper WHERE forecast_id=?',(x['forecast_id'],)).fetchall()
                if len(old)!=1:states.add('UNKNOWN');continue
                vals=dict(zip(v3.PAPER_COLS,old[0]))
                if vals==x['original']:states.add('OLD')
                elif tuple(vals[k] for k in v3.OUTCOME_COLS)==(x['status'],x['result_home'],x['result_away'],data['created_at'],data['created_at'],x['profit'],SOURCE):states.add('NEW')
                else:states.add('UNKNOWN')
        finally:ro.close()
        if states=={'OLD'}:data['state']='NOT_APPLIED'
        elif states=={'NEW'}:data['state']='APPLIED_VERIFIED'
        else:raise RuntimeError('AMBIGUOUS_PERSISTED_TRANSACTION')
        atomic_json(path,data)

def rollback_data():
    v3=load_v3();manifests=[]
    for p in BACKUP.glob('hope_v4_*.manifest.json'):
        d=json.loads(p.read_text())
        if d.get('release')=='HOPE_V4_RESULT_BRIDGE' and d.get('source')==SOURCE and d.get('state')=='APPLIED_VERIFIED':
            manifests.append((p,d))
    if not manifests:emit('NO_V4_DATA_TO_ROLLBACK',{});return
    path,data=sorted(manifests,key=lambda z:z[1]['created_at'])[-1]
    c=sqlite3.connect(str(DB),isolation_level=None,timeout=30)
    try:
        c.execute('BEGIN IMMEDIATE')
        for x in data['candidates']:
            got=c.execute('SELECT '+','.join(v3.OUTCOME_COLS)+' FROM v55_paper WHERE forecast_id=?',(x['forecast_id'],)).fetchall()
            want=(x['status'],x['result_home'],x['result_away'],data['created_at'],data['created_at'],x['profit'],SOURCE)
            if len(got)!=1 or got[0]!=want:raise RuntimeError('ROLLBACK_DATA_CHANGED_REFUSE')
        for x in data['candidates']:
            original=x['original'];values=[original[k] for k in v3.OUTCOME_COLS]+[x['forecast_id']]
            q='UPDATE v55_paper SET '+','.join(k+'=?' for k in v3.OUTCOME_COLS)+' WHERE forecast_id=?'
            if c.execute(q,values).rowcount!=1:raise RuntimeError('ROLLBACK_COUNT')
        c.execute('COMMIT')
    except Exception:
        if c.in_transaction:c.execute('ROLLBACK')
        raise
    finally:c.close()
    data['state']='ROLLED_BACK';atomic_json(path,data)
    emit('DATA_ROLLBACK',{'rows':len(data['candidates']),'only_v4_settlements':True,'backup_retained':data['backup']})

def classify():
    c=sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True)
    try:
        paper=collections.Counter();pnl=collections.defaultdict(lambda:{'count':0,'stake':0.0,'net':0.0})
        for status,src,stake,profit,kick in c.execute('SELECT status,source,stake,profit,kickoff_utc FROM v55_paper'):
            if status=='PENDING':kind='FUTURE_PENDING' if clock(kick) and clock(kick)>now() else 'PAST_DUE_PENDING'
            elif status in ('WIN','LOSS','PUSH'):
                if src==SOURCE:kind='V4_RECONCILED_SIMULATION'
                elif str(src or '').startswith('HOPE_V3_AS_RECORDED_ODDS_NOT_BOOKMAKER_VERIFIED'):
                    kind='V3_AS_RECORDED_SIMULATION'
                else:kind='PRE_V3_SETTLEMENT'
                r=pnl[kind];r['count']+=1;r['stake']+=float(stake or 0);r['net']+=float(profit or 0)
            else:kind='OTHER_'+str(status)
            paper[kind]+=1
        portfolio=collections.Counter()
        for (st,src) in c.execute('''SELECT p.status,p.source FROM v56_portfolio f
                 LEFT JOIN v55_paper p ON p.forecast_id=f.forecast_id'''):
            if st is None:portfolio['UNLINKED']+=1
            elif src==SOURCE:portfolio['V4_RECONCILED_SIMULATION']+=1
            elif str(src or '').startswith('HOPE_V3_'):portfolio['V3_AS_RECORDED_SIMULATION']+=1
            else:portfolio[str(st)]+=1
        return {'paper':dict(paper),'portfolio':dict(portfolio),
          'pnl_separated':{k:{'rows':v['count'],'stake_usd':round(v['stake'],4),
              'net_usd':round(v['net'],4),'roi_pct':round(v['net']/v['stake']*100,4) if v['stake'] else None}
              for k,v in pnl.items()},'real_orders':0}
    finally:c.close()

def run_cycle():
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(str(STATE/'bridge_v4.lock'),os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        v3=load_v3();v2=v3.v2_module();v2.load_v1().runtime_identity()
        reconcile_manifests()
        v3.run_cycle()  # R10 strict + V2 ledger + V3 provenance-separated paper simulation.
        snapshot=json.loads((STATE/'reconciliation_v2_last.json').read_text())
        report=json.loads(v3.assert_r10_report(snapshot['r10_report']).read_text())
        targets={k:v for k,v in report.get('event_evidence',{}).items()
                 if v.get('league') in LEAGUES and v.get('match_date') and v.get('home') and v.get('away')}
        cursor_path=STATE/'source_cursor_v4.json'
        try:cursor=json.loads(cursor_path.read_text()).get('cycle_index',0) if cursor_path.is_file() else 0
        except (OSError,ValueError):cursor=0
        if type(cursor)!=int or not 0<=cursor<1_000_000:raise RuntimeError('UNTRUSTED_SOURCE_CYCLE_CURSOR')
        supplement=fetch_sources(targets,cycle_index=cursor)
        ro=v2.load_v1().ro_connection()
        try:candidates,refusals,proofs=plan(report,ro,supplement)
        finally:ro.close()
        emit('BRIDGE_PREVIEW',{'pending_in_r10':report['pending_total'],
             'candidate_count':len(candidates),'rejections':refusals,
             'external_requests':supplement['requests'],'provider_errors':supplement['errors'][:12],
             'requests_suppressed':supplement['requests_suppressed'],
             'days_suppressed':supplement['days_suppressed'],'real_orders':0})
        written=apply(candidates)
        status=classify()
        obj={'at':stamp(),'scheduler':'EVERY_3_HOURS','v3_processed_first':True,
             'bridge_settled':written,'source_requests':supplement['requests'],
             'source_errors':supplement['errors'][:15],
             'request_limits':{'espn_suppressed':supplement['requests_suppressed'],
                'tsdb_days_suppressed':supplement['days_suppressed']},
             'rejections':refusals,'status':status,
             'paper_only':True,'unverified_odds_disclosed':True,
             'ui_modified':False,'production_modified':False,'real_orders':0}
        atomic_json(STATE/'result_bridge_v4_last.json',obj)
        atomic_json(cursor_path,{'cycle_index':(cursor+1)%1_000_000,'last_completed':stamp()})
        emit('CYCLE_VERIFIED',obj)
        return obj
    except Exception as exc:
        atomic_json(STATE/'result_bridge_v4_error.json',{'at':stamp(),'type':type(exc).__name__,
           'error':str(exc)[:260],'next':'NEXT_3_HOUR_RETRY','real_orders':0})
        emit('CYCLE_FAILED',{'exception':type(exc).__name__,'message':str(exc)[:220]})
        raise
    finally:os.close(fd)

def main():
    action=sys.argv[1] if len(sys.argv)>1 else 'run'
    if action=='run':run_cycle()
    elif action=='status':
        p=STATE/'result_bridge_v4_last.json'
        print(p.read_text() if p.is_file() else 'NO_COMPLETED_BRIDGE_CYCLE')
    elif action=='rollback-data':
        v3=load_v3();v3.v2_module().load_v1().runtime_identity()
        fd=os.open(str(STATE/'bridge_v4.lock'),os.O_CREAT|os.O_RDWR,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            reconcile_manifests();rollback_data()
        finally:os.close(fd)
    elif action=='selftest':
        assert norm('Manchester City FC')=='manchester city'
        assert norm('Manchester United')!='manchester city'
        assert re.fullmatch(r'20\d{2}-\d{2}-\d{2}','2026-11-17')
        emit('SELFTEST_PASS',{'real_orders':0})
    else:raise RuntimeError('UNKNOWN_ACTION')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        emit('ABORTED',{'reason':type(exc).__name__+': '+str(exc)[:300],
             'real_orders':0});sys.exit(1)
