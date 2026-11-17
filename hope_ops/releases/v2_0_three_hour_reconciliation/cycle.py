#!/usr/bin/env python3
"""HOPE v2: every-three-hours R10 audit and durable, non-forged resolution ledger.

Never changes UI, native forecast engine, or original paper statuses independently.
Native R10 may atomically settle exact-evidence paper bets. All other outcomes
remain classified/retried; provisional outcomes never contaminate official PnL.
"""
from __future__ import annotations
import contextlib, collections, datetime as dt, fcntl, hashlib, importlib.util, json
import os, pathlib, re, sqlite3, sys, traceback, urllib.request, urllib.parse
import unicodedata, uuid, time

BASE=pathlib.Path('/opt/ORACLE_EVA/hope_ops')
V1=BASE/'releases/v1_0_autopilot/hope_ops.py'
EXPECTED_V1_SHA='4fab239e79c74581962185fd333e0fc827e42769fb292fe6ba984797c9fe14e7'
STATE=BASE/'state'
LEDGER=STATE/'resolution_ledger.sqlite3'
DATA=pathlib.Path('/opt/ORACLE_EVA/replica_v170/state')
CURRENT_DB=DATA/'oracle.sqlite3'
TSDB='https://www.thesportsdb.com/api/v1/json/123/eventsday.php'
UTC=dt.timezone.utc


def stamp():return dt.datetime.now(UTC).isoformat(timespec='seconds')

def echo(tag,obj):print('HOPE_V2_'+tag+' '+json.dumps(obj,ensure_ascii=False,sort_keys=True,default=str),flush=True)

def sha12(x):return hashlib.sha256(str(x).encode('utf-8')).hexdigest()[:12]

def atomic_json(p,obj):
    p=pathlib.Path(p);p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    tmp=p.with_name('.'+p.name+'.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(str(tmp),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(obj,f,ensure_ascii=False,sort_keys=True,indent=2,default=str)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,p)
    finally:
        if tmp.exists():tmp.unlink()


def load_v1():
    if hashlib.sha256(V1.read_bytes()).hexdigest()!=EXPECTED_V1_SHA:
        raise RuntimeError('V1_ORIGINAL_HASH_CHANGED')
    spec=importlib.util.spec_from_file_location('hope_v1_protected',str(V1))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.DB!=CURRENT_DB:raise RuntimeError('V1_DATABASE_PATH_CHANGED')
    return module


def assert_report_path(s):
    # /data mapped onto known host directory; no arbitrary traversal.
    if not re.fullmatch(r'/data/eva-reports/eva_r10_census_\d{8}T\d{6}Z_[a-z0-9]{6,20}\.json',str(s)):
        raise RuntimeError('UNTRUSTED_R10_REPORT_PATH')
    p=DATA/str(s)[6:]
    root=(DATA/'eva-reports').resolve(strict=True)
    if p.resolve().parent!=root or not p.is_file() or p.stat().st_size>20*1024*1024:
        raise RuntimeError('MISSING_OR_UNSAFE_REPORT')
    return p


def norm_team(name):
    # Conservative exact matching: accented spelling only. Never guess aliases.
    s=unicodedata.normalize('NFKD',str(name or ''))
    s=''.join(ch for ch in s if not unicodedata.combining(ch))
    return ' '.join(re.findall(r'[a-z0-9]+',s.casefold()))


def parse_tsdb(raw):
    ob=json.loads(raw)
    if not isinstance(ob,dict):raise RuntimeError('BAD_SPORTSDB_FORMAT')
    results=[]
    for ev in ob.get('events') or []:
        if not isinstance(ev,dict):continue
        status=str(ev.get('strStatus') or '').casefold().strip()
        if status not in ('match finished','finished','ft','final'):
            continue
        if str(ev.get('strSport') or '').casefold() not in ('soccer','football'):
            continue
        try:hs=int(ev.get('intHomeScore'));aw=int(ev.get('intAwayScore'))
        except (ValueError,TypeError):continue
        if any(type(x) is not int or x<0 or x>40 for x in (hs,aw)):continue
        day=str(ev.get('dateEvent') or '')
        if not re.fullmatch(r'20\d\d-\d\d-\d\d',day):continue
        results.append({'id':str(ev.get('idEvent') or ''), 'day':day,
             'home':str(ev.get('strHomeTeam') or ''),'away':str(ev.get('strAwayTeam') or ''),
             'score':[hs,aw]})
    return results


def tsdb_research(event_evidence, getter=None, max_days=24):
    """Extra independent read-only results. Never authorizes official settlement.
    The free API may return incomplete schedules: absence isn't a missing game.
    """
    target={k:v for k,v in (event_evidence or {}).items()
            if v.get('match_state') in ('NO_PROVIDER_FINAL','ESPN_ONLY','NO_ESPN_FINAL_KICKOFF')
            and v.get('home') and v.get('away') and v.get('match_date')}
    days=sorted({str(v['match_date']) for v in target.values()
                 if re.fullmatch(r'20\d\d-\d\d-\d\d',str(v['match_date']))})[:max_days]
    output={};errors=[];requests=0
    for day in days:
        url=TSDB+'?'+urllib.parse.urlencode({'d':day,'s':'Soccer'})
        try:
            if getter:raw=getter(url)
            else:
                req=urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'HOPE-EVA-V2-3h/1.0'})
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self,*args,**kwargs):raise RuntimeError('TSDB_REDIRECT_BLOCKED')
                with urllib.request.build_opener(NoRedirect()).open(req,timeout=9) as res:
                    if res.status!=200 or res.geturl()!=url:raise RuntimeError('TSDB_BAD_RESPONSE')
                    raw=res.read(1250001)
                    if len(raw)>1250000:raise RuntimeError('TSDB_RESPONSE_TOO_BIG')
            events=parse_tsdb(raw)
            requests+=1
        except Exception as ex:
            errors.append({'date':day,'reason':type(ex).__name__+':'+str(ex)[:90]})
            continue
        for eid,t in target.items():
            if str(t['match_date'])!=day:continue
            matching=[e for e in events if norm_team(e['home'])==norm_team(t['home'])
                      and norm_team(e['away'])==norm_team(t['away'])]
            if len(matching)!=1:continue
            ev=matching[0];espn=(t.get('providers') or {}).get('espn') or {}
            if espn.get('state')=='FINAL_SCORE_MATCHED':
                score=espn.get('score')
                state=('INDEPENDENT_SCORE_AGREES' if score==ev['score']
                       else 'INDEPENDENT_SCORE_CONFLICT')
            else:state='SPORTSDB_FINAL_ONLY'
            output[eid]={'state':state,'score':ev['score'],'provider':'THESPORTSDB',
                         'event_id':ev['id'],'day':day,'source_is_partial_free_api':True}
    return {'matches':output,'requests':requests,'errors':errors[:30],
            'note':'TheSportsDB free endpoint may return incomplete events; absence is inconclusive'}


def paper_and_portfolio(conn):
    paper={}
    cols={r[1] for r in conn.execute('PRAGMA table_info(v55_paper)')}
    required={'forecast_id','status','origin','original_event_id','profit','source','stake'}
    if not required.issubset(cols):raise RuntimeError('PAPER_SCHEMA_UNVERIFIED')
    for row in conn.execute('SELECT forecast_id,status,origin,original_event_id,profit,source,stake FROM v55_paper'):
        fid,st,origin,event,profit,source,stake=row
        if fid in paper:raise RuntimeError('DUPLICATE_PAPER_ID')
        paper[fid]={'status':str(st),'origin':origin,'event':str(event),
                    'profit':profit,'source':source,'stake':stake}
    portfolio=collections.Counter();orphans=[];status_count=collections.Counter()
    port_exists=conn.execute("SELECT 1 FROM sqlite_master WHERE name='v56_portfolio' AND type='table'").fetchone()
    if port_exists:
        pc={r[1] for r in conn.execute('PRAGMA table_info(v56_portfolio)')}
        if 'forecast_id' not in pc:raise RuntimeError('PORTFOLIO_ID_SCHEMA_UNVERIFIED')
        for (fid,) in conn.execute('SELECT forecast_id FROM v56_portfolio'):
            portfolio[str(fid)]+=1
            if fid not in paper:orphans.append(str(fid))
            else:status_count[paper[fid]['status']]+=1
    return paper,portfolio,orphans,status_count


def outcome_ledger_rows(report,papers,portfolio,tsdb=None):
    """Each paper has one status. Locked-in result != theoretical result."""
    details=report.get('details')
    if not isinstance(details,list):raise RuntimeError('R10_MISSING_DETAILS')
    if len(details)!=report.get('pending_total'):raise RuntimeError('R10_DETAILS_CENSUS_MISMATCH')
    ids=collections.defaultdict(list)
    for fid in papers:ids[sha12(fid)].append(fid)
    records=[];seen=set();g=collections.Counter()
    for item in details:
        matches=ids.get(str(item.get('forecast_sha12')),[])
        if len(matches)!=1:raise RuntimeError('R10_HASH_NOT_UNIQUE_OR_ID_MISSING')
        fid=matches[0]
        if fid in seen:raise RuntimeError('DUPLICATE_R10_DETAIL')
        seen.add(fid);p=papers[fid]
        if p['status'] in ('WIN','LOSS','PUSH'):
            category='OFFICIAL_SETTLED';outcome=p['status'];profit=p['profit'];grade='DATABASE_COMMITTED'
        else:
            r=(item.get('evidence') or {}).get('research_only') or {}
            if r.get('state')=='THEORETICAL_ONLY' and r.get('outcome') in ('WIN','LOSS','PUSH'):
                category='SCORE_KNOWN_UNVERIFIED_PRICE'
                outcome=r['outcome'];profit=r.get('theoretical_pnl_usd');grade=r.get('grade')
            elif str(item.get('reason'))=='NOT_YET_DUE':
                category='AWAITING_EVENT';outcome=None;profit=None;grade=None
            else:
                category='RETRY_UNRESOLVED';outcome=None;profit=None;grade=None
        rec={'forecast_id':fid,'origin':p['origin'],'original_event_id':p['event'],
             'category':category,'outcome':outcome,'hypothetical_profit':profit,
             'grade':grade,'reason':item.get('reason'),'in_portfolio':fid in portfolio,
             'provider_extra':(tsdb or {}).get(str(item.get('original_event_sha12')))}
        g[category]+=1;records.append(rec)
    # Include all previously settled rows (not in current R10 census), for audit.
    for fid,p in papers.items():
        if fid in seen:continue
        if p['status'] in ('WIN','LOSS','PUSH'):
            cat='OFFICIAL_SETTLED';outcome=p['status'];profit=p['profit']
        else:cat='NEW_OR_OTHER_STATUS';outcome=None;profit=None
        records.append({'forecast_id':fid,'origin':p['origin'],
             'original_event_id':p['event'],'category':cat,'outcome':outcome,
             'hypothetical_profit':profit,'grade':p.get('source'),
             'reason':'NOT_IN_R10_PENDING_CENSUS','in_portfolio':fid in portfolio,
             'provider_extra':None})
        g[cat]+=1
    return records,dict(g)


def store_ledger(records,portfolio_orphans,stats):
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    db=sqlite3.connect(str(LEDGER),timeout=30)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS paper_resolution(
          forecast_id TEXT PRIMARY KEY,origin TEXT,original_event_id TEXT,
          category TEXT NOT NULL,outcome TEXT,hypothetical_profit REAL,
          evidence_grade TEXT,last_reason TEXT,in_portfolio INTEGER NOT NULL,
          extra_evidence_json TEXT,first_seen TEXT NOT NULL,last_checked TEXT NOT NULL,
          cycles_seen INTEGER NOT NULL DEFAULT 1)''')
        db.execute('''CREATE TABLE IF NOT EXISTS portfolio_unlinked(
          forecast_id TEXT PRIMARY KEY,first_seen TEXT NOT NULL,
          last_checked TEXT NOT NULL,cycles_seen INTEGER NOT NULL DEFAULT 1,
          classification TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS cycles(
          at TEXT PRIMARY KEY,summary_json TEXT NOT NULL)''')
        now=stamp()
        with db:
            for r in records:
                db.execute('''INSERT INTO paper_resolution
                  (forecast_id,origin,original_event_id,category,outcome,hypothetical_profit,
                   evidence_grade,last_reason,in_portfolio,extra_evidence_json,first_seen,last_checked,cycles_seen)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)
                  ON CONFLICT(forecast_id) DO UPDATE SET
                  category=excluded.category,outcome=excluded.outcome,
                  hypothetical_profit=excluded.hypothetical_profit,
                  evidence_grade=excluded.evidence_grade,last_reason=excluded.last_reason,
                  in_portfolio=excluded.in_portfolio,extra_evidence_json=excluded.extra_evidence_json,
                  last_checked=excluded.last_checked,cycles_seen=paper_resolution.cycles_seen+1''',
                  (r['forecast_id'],r['origin'],r['original_event_id'],r['category'],r['outcome'],
                   r['hypothetical_profit'],r['grade'],r['reason'],1 if r['in_portfolio'] else 0,
                   json.dumps(r['provider_extra'],sort_keys=True) if r['provider_extra'] else None,now,now))
            for fid in portfolio_orphans:
                db.execute('''INSERT INTO portfolio_unlinked VALUES(?,?,?,1,'UNLINKED_FROM_PAPER')
                        ON CONFLICT(forecast_id) DO UPDATE SET last_checked=excluded.last_checked,
                        cycles_seen=portfolio_unlinked.cycles_seen+1''',(fid,now,now))
            db.execute('INSERT OR REPLACE INTO cycles(at,summary_json) VALUES(?,?)',
                       (now,json.dumps(stats,sort_keys=True)))
        return {'paper_rows_total':db.execute('SELECT COUNT(*) FROM paper_resolution').fetchone()[0],
                'portfolio_unlinked_total':db.execute('SELECT COUNT(*) FROM portfolio_unlinked').fetchone()[0]}
    finally:db.close()


def run_once(settle=True,extra_source=True):
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(str(STATE/'cycle_v2.lock'),os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        v1=load_v1()
        if not CURRENT_DB.is_file():raise RuntimeError('EVA_DB_NOT_FOUND')
        v1.runtime_identity()
        result=v1.settlement_cycle() if settle else json.loads((STATE/'settlement_last.json').read_text())
        if result.get('result')!='PASS':raise RuntimeError('R10_SETTLEMENT_FAILED')
        summary=result.get('summary') or {}
        report_path=assert_report_path(summary.get('report'))
        report=json.loads(report_path.read_text(encoding='utf-8'))
        # Extra source failure cannot block the core reporting loop.
        try:
            extra=tsdb_research(report.get('event_evidence'),max_days=24) if extra_source else {'matches':{},'requests':0,'errors':[]}
        except Exception as exc:
            extra={'matches':{},'requests':0,'errors':[{'reason':type(exc).__name__+':'+str(exc)[:120]}]}
        ro=v1.ro_connection()
        try:papers,portfolio,orphans,pst=paper_and_portfolio(ro)
        finally:ro.close()
        rows,categories=outcome_ledger_rows(report,papers,portfolio,extra.get('matches'))
        stats={'at':stamp(),'mode':'PAPER_ONLY','scheduler':'EVERY_3_HOURS','real_orders':0,
          'r10_report':str(report_path),'r10_newly_settled':summary.get('newly_settled',0),
          'pending_in_r10':report.get('pending_total'),'categories':categories,
          'portfolio_unique_ids':len(portfolio),'portfolio_rows':sum(portfolio.values()),
          'portfolio_by_paper_status':dict(pst),'portfolio_unlinked':len(orphans),
          'independent_extra_matches':collections.Counter(v['state'] for v in extra.get('matches',{}).values()),
          'extra_source_requests':extra.get('requests'), 'extra_source_errors':extra.get('errors'),
          'unresolved_remain_in_auto_retry':True,
          'no_forged_win_loss':True,'official_pnl_only_native_r10':True,
          'native_r10_summary':{'previous_settled_pnl':report.get('previous_settled_pnl'),
             'research_only_preview':report.get('research_only_preview')}}
        stats['ledger']=store_ledger(rows,orphans,stats)
        atomic_json(STATE/'reconciliation_v2_last.json',stats)
        echo('CYCLE_VERIFIED',stats)
        return stats
    except Exception as exc:
        fail={'at':stamp(),'state':'FAILED','error_type':type(exc).__name__,
              'error':str(exc)[:350],'next_retry':'NEXT_3H_SCHEDULE','real_orders':0}
        atomic_json(STATE/'reconciliation_v2_failure.json',fail)
        echo('CYCLE_FAILED',fail)
        raise
    finally:os.close(fd)


def main():
    cmd=sys.argv[1] if len(sys.argv)>1 else 'run'
    if cmd=='selftest':
        assert norm_team('Atlético Madrid')=='atletico madrid'
        assert not norm_team('Team FC')==norm_team('Team')
        echo('SELFTEST_PASS',{'status':'PASS','scheduler':'EVERY_3_HOURS'})
    elif cmd=='run':run_once(True,True)
    elif cmd=='status':
        p=STATE/'reconciliation_v2_last.json'
        if p.is_file():print(p.read_text())
        else:echo('NO_SUCCESSFUL_CYCLE',{})
    else:raise RuntimeError('UNKNOWN_ACTION')

if __name__=='__main__':
    try:main()
    except Exception as e:
        echo('FAIL_CLOSED',{'error':type(e).__name__+':'+str(e)[:250]})
        sys.exit(1)
