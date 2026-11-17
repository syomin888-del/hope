#!/usr/bin/env python3
"""HOPE V3: R10 strict first, then as-recorded (UNVERIFIED ODDS) paper settlement.

The only new real-DB mutations are distinctly labeled paper simulations based on
R10's independently validated ESPN final + built-in native market semantics.
Never assert an executable bookmaker quote. Never assert unknown event outcomes.
"""
from __future__ import annotations
import collections, datetime as dt, fcntl, hashlib, importlib.util, json, math, os
import pathlib, sqlite3, sys, time, uuid, shutil

BASE=pathlib.Path('/opt/ORACLE_EVA/hope_ops')
STATE=BASE/'state'
V2=BASE/'releases/v2_0_three_hour_reconciliation/cycle.py'
V1=BASE/'releases/v1_0_autopilot/hope_ops.py'
DATA=pathlib.Path('/opt/ORACLE_EVA/replica_v170/state')
DB=DATA/'oracle.sqlite3'
BACKUP=DATA/'eva-backups'
SOURCE_PREFIX='HOPE_V3_AS_RECORDED_ODDS_NOT_BOOKMAKER_VERIFIED'
V2_SHA='afa726b3a61acbb4d2bbc511784fe7e83124e409e193e06e3aa6265eb744a23e'
UTC=dt.timezone.utc
OUTCOME_COLS=('status','result_home','result_away','result_seen','settled_at','profit','source')
PAPER_COLS=('forecast_id','root_id','original_event_id','origin','market','selection','odds','stake',
            'quote_key','quote_at','registered_at','kickoff_utc',
            'status','result_home','result_away','result_seen','settled_at','profit','source')


def now():return dt.datetime.now(UTC)
def stamp():return now().isoformat(timespec='seconds')
def sha12(x):return hashlib.sha256(str(x).encode('utf-8')).hexdigest()[:12]
def emit(kind,data):print('HOPE_V3_'+kind+' '+json.dumps(data,sort_keys=True,ensure_ascii=False,default=str),flush=True)
def clock(v):
    try:
        x=dt.datetime.fromisoformat(str(v).replace('Z','+00:00'))
        return x.astimezone(UTC) if x.tzinfo is not None else None
    except (ValueError,TypeError,OverflowError):return None

def write_atomic(path,obj):
    path=pathlib.Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(str(temp),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(obj,f,ensure_ascii=False,sort_keys=True,indent=2,default=str)
            f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if temp.exists():temp.unlink()

def v2_module():
    if hashlib.sha256(V2.read_bytes()).hexdigest()!=V2_SHA:
        raise RuntimeError('INSTALLED_V2_SOURCE_SHA_CHANGED')
    spec=importlib.util.spec_from_file_location('hope_v2_trusted',str(V2))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def assert_r10_report(s):
    import re
    if not re.fullmatch(r'/opt/ORACLE_EVA/replica_v170/state/eva-reports/eva_r10_census_\d{8}T\d{6}Z_[a-f0-9]{8}\.json',str(s)):
        raise RuntimeError('INVALID_R10_REPORT_LOCATION')
    p=pathlib.Path(s)
    if p.is_symlink() or p.resolve().parent!=(DATA/'eva-reports').resolve() or p.stat().st_size>22_000_000:
        raise RuntimeError('UNTRUSTED_R10_REPORT_FILE')
    return p

def normalized_market_result(market,selection,hs,aw):
    """Independent check of R10 native outcome for a conservative subset only."""
    if type(hs)!=int or type(aw)!=int or hs<0 or aw<0 or hs>40 or aw>40:return None
    m=str(market).strip().upper();s=str(selection).strip().upper()
    if m=='1X2':
        if s in ('П1','1'):return 'WIN' if hs>aw else 'LOSS'
        if s in ('П2','2'):return 'WIN' if aw>hs else 'LOSS'
        if s in ('X','Х','НИЧЬЯ'):return 'WIN' if hs==aw else 'LOSS'
    if m in ('ОБЕ ЗАБЬЮТ','BTTS'):
        if s in ('ДА','YES'):return 'WIN' if hs>0 and aw>0 else 'LOSS'
        if s in ('НЕТ','NO'):return 'WIN' if hs==0 or aw==0 else 'LOSS'
    if m in ('ТОТАЛ 2.5','TOTAL 2.5'):
        if s in ('МЕНЬШЕ 2.5','UNDER 2.5'):return 'WIN' if hs+aw<2.5 else 'LOSS'
        if s in ('БОЛЬШЕ 2.5','OVER 2.5'):return 'WIN' if hs+aw>2.5 else 'LOSS'
    return None

def candidate_plan(report,c,cutoff=None):
    """Fail closed: require an ESPN final validated by R10 and identity-bound paper.
    No unknown/ambiguous, delayed kickoff, future event, contradictory score or unsupported market.
    """
    cutoff=cutoff or now()
    details=report.get('details')
    if not isinstance(details,list) or len(details)!=report.get('pending_total'):
        raise RuntimeError('R10_CENSUS_INCOMPLETE')
    event_evidence=report.get('event_evidence')
    if not isinstance(event_evidence,dict):raise RuntimeError('R10_EVENT_EVIDENCE_MISSING')
    db_cols={x[1] for x in c.execute('PRAGMA table_info(v55_paper)')}
    if not set(PAPER_COLS).issubset(db_cols):raise RuntimeError('UNVERIFIED_PAPER_SCHEMA')
    papers={}
    for vals in c.execute('SELECT '+','.join(PAPER_COLS)+' FROM v55_paper'):
        row=dict(zip(PAPER_COLS,vals))
        papers.setdefault(sha12(row['forecast_id']),[]).append(row)
    if any(len(v)>1 for v in papers.values()):raise RuntimeError('FORECAST_HASH_COLLISION')
    candidates=[];rejections=collections.Counter();seen=set()
    for d in details:
        dig=str(d.get('forecast_sha12',''))
        rows=papers.get(dig) or []
        if len(rows)!=1:raise RuntimeError('REPORT_PAPER_IDENTIFICATION_FAILED')
        p=rows[0];fid=p['forecast_id']
        if fid in seen:raise RuntimeError('DUPLICATE_R10_PAPER_DETAIL')
        seen.add(fid)
        if p['status']!='PENDING':
            rejections['NO_LONGER_PENDING']+=1;continue
        r=(d.get('evidence') or {}).get('research_only') or {}
        if r.get('state')!='THEORETICAL_ONLY' or r.get('grade') not in ('ESPN_FINAL_ONLY','TWO_PROVIDER_SCORES'):
            rejections['NO_FINISHED_VALIDATED_ESPN_MATCH']+=1;continue
        if r.get('outcome') not in ('WIN','LOSS','PUSH'):
            rejections['RESEARCH_OUTCOME_MISSING']+=1;continue
        if str(p['origin'])!=str(d.get('origin')) or sha12(p['original_event_id'])!=str(d.get('original_event_sha12')):
            rejections['ORIGINAL_EVENT_IDENTITY_MISMATCH']+=1;continue
        if p['market']!=d.get('market') or p['selection']!=d.get('selection'):
            rejections['PAPER_MARKET_CHANGED']+=1;continue
        stake,odds=p['stake'],p['odds']
        if (not isinstance(odds,(float,int)) or isinstance(odds,bool) or not math.isfinite(odds) or not 1.001<odds<=1001
          or not isinstance(stake,(float,int)) or isinstance(stake,bool) or not math.isfinite(stake) or stake<=0
          or not isinstance(d.get('odds'),(float,int)) or abs(odds-d['odds'])>1e-9
          or not isinstance(d.get('stake'),(float,int)) or abs(stake-d['stake'])>1e-9):
            rejections['PAPER_PRICE_OR_STAKE_CHANGED']+=1;continue
        q=clock(p['quote_at']);reg=clock(p['registered_at']);kick=clock(p['kickoff_utc'])
        if not q or not reg or not kick or not q<reg<kick or clock(d.get('kickoff_utc'))!=kick:
            rejections['UNTRUSTED_PREKICKOFF_TIMES']+=1;continue
        ev=event_evidence.get(sha12(p['original_event_id'])) or {}
        if ev.get('match_state') not in ('ESPN_ONLY','TWO_SOURCE_AGREEMENT'):
            rejections['EVENT_EVIDENCE_CONFLICT_OR_ABSENT']+=1;continue
        es=(ev.get('providers') or {}).get('espn') or {}
        if es.get('state')!='FINAL_SCORE_MATCHED' or not str(es.get('event_id','')).isdigit():
            rejections['ESPN_EVENT_FINAL_ID_MISSING']+=1;continue
        epkick=clock(es.get('kickoff_utc'))
        if not epkick or abs((epkick-kick).total_seconds())>600 or cutoff<epkick+dt.timedelta(minutes=95):
            rejections['FINAL_KICKOFF_NOT_CONFIRMED']+=1;continue
        score=es.get('score');research_score=r.get('score')
        if (not isinstance(score,list) or len(score)!=2 or score!=research_score
            or any(type(x)!=int or not 0<=x<=40 for x in score)):
            rejections['SCORE_MISMATCH']+=1;continue
        if ev['match_state']=='TWO_SOURCE_AGREEMENT':
            providers=ev.get('providers') or {}
            if not any((providers.get(k) or {}).get('state')=='FINAL_SCORE_MATCHED' and
                    (providers[k] or {}).get('score')==score for k in ('football_data','openfootball')):
                rejections['SECOND_PROVIDER_NOT_CONFIRMED']+=1;continue
            if any((providers.get(k) or {}).get('state')=='FINAL_SCORE_MATCHED' and
                   (providers[k] or {}).get('score')!=score for k in ('football_data','openfootball')):
                rejections['PROVIDER_FINAL_CONFLICT']+=1;continue
        calc=normalized_market_result(p['market'],p['selection'],score[0],score[1])
        if calc!=r.get('outcome'):
            rejections['UNSUPPORTED_OR_CONTRADICTORY_NATIVE_MARKET']+=1;continue
        net=round(float(stake*(odds-1) if calc=='WIN' else (-stake if calc=='LOSS' else 0.0)),6)
        if not isinstance(r.get('theoretical_pnl_usd'),(int,float)) or abs(net-r['theoretical_pnl_usd'])>0.0001:
            rejections['R10_PNL_DISAGREEMENT']+=1;continue
        # Detect conflicting previously-settled sibling score, not correlated predictions.
        other=c.execute('''SELECT COUNT(*) FROM v55_paper WHERE original_event_id=? AND origin=?
             AND status IN ('WIN','LOSS','PUSH') AND result_home IS NOT NULL AND result_away IS NOT NULL
             AND (result_home!=? OR result_away!=?)''',(p['original_event_id'],p['origin'],score[0],score[1])).fetchone()[0]
        if other:rejections['CONFLICT_WITH_ALREADY_SETTLED_EVENT']+=1;continue
        if p['origin']=='sf_forecasts':
            found=c.execute('SELECT home_score,away_score FROM sf_matches WHERE id=?',(p['original_event_id'],)).fetchall()
            if len(found)!=1:
                rejections['MISSING_OR_AMBIGUOUS_NATIVE_FIXTURE']+=1;continue
            hs,aw=found[0]
            if (hs is None)!=(aw is None) or (hs is not None and (hs,aw)!=tuple(score)):
                rejections['CONFLICT_WITH_NATIVE_STORED_SCORE']+=1;continue
        candidates.append({'forecast_id':fid,'original':p,'status':calc,'profit':net,
            'result_home':score[0],'result_away':score[1],'proof_grade':r['grade'],
            'espn_event_id':es['event_id'],'event_sha12':sha12(p['original_event_id'])})
    if len({x['forecast_id'] for x in candidates})!=len(candidates):raise RuntimeError('DUPLICATE_CANDIDATES')
    return candidates,dict(rejections)

def backup_database():
    free=shutil.disk_usage(str(DATA)).free;sz=DB.stat().st_size
    needed=max(16*1024**3,int(sz*2.4))
    if free<needed:raise RuntimeError('DISK_TOO_FULL_FOR_SAFE_SQLITE_BACKUP')
    BACKUP.mkdir(parents=True,exist_ok=True,mode=0o700)
    name='hope_v3_'+now().strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:10]
    dst=BACKUP/(name+'.sqlite3')
    fd=os.open(str(dst),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
    try:
        ro=sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True,timeout=60)
        wr=sqlite3.connect(str(dst),timeout=60)
        try:ro.backup(wr,pages=2048,sleep=.05)
        finally:wr.close();ro.close()
        c=sqlite3.connect('file:'+str(dst)+'?mode=ro',uri=True,timeout=60)
        try:
            if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise RuntimeError('ONLINE_BACKUP_SQLITE_QUICK_CHECK_FAILED')
        finally:c.close()
        if dst.stat().st_size<1024*1024:raise RuntimeError('ONLINE_BACKUP_TOO_SMALL')
        emit('ONLINE_BACKUP_PASS',{'path':str(dst),'bytes':dst.stat().st_size})
        return dst,name
    except Exception:
        if dst.exists():dst.rename(str(dst)+'.incomplete')
        raise

def apply(candidates):
    """Only after online backup; exact old row equality inside BEGIN IMMEDIATE."""
    if not candidates:return {'newly_closed':0,'backups_created':0,'state':'NO_ELIGIBLE_SIMULATED_RESULTS'}
    backup,tag=backup_database();manifest=BACKUP/(tag+'.manifest.json')
    timestamp=stamp()
    manifest_obj={'release':'HOPE_V3_PAPER_SIMULATION','state':'PREPARED',
                  'source':SOURCE_PREFIX,'backup':str(backup),'created_at':timestamp,
                  'candidates':candidates,'risk_label':'AS_RECORDED_ODDS_UNVERIFIED_NOT_REAL_EXECUTION',
                  'real_orders':0}
    write_atomic(manifest,manifest_obj)
    c=sqlite3.connect(str(DB),timeout=30,isolation_level=None)
    c.execute('PRAGMA busy_timeout=30000')
    try:
        c.execute('BEGIN IMMEDIATE')
        for x in candidates:
            old=c.execute('SELECT '+','.join(PAPER_COLS)+' FROM v55_paper WHERE forecast_id=?',
                          (x['forecast_id'],)).fetchall()
            if len(old)!=1 or dict(zip(PAPER_COLS,old[0]))!=x['original']:
                raise RuntimeError('CHANGED_SINCE_PREVIEW_ABORT_ATOMIC')
            if x['original']['status']!='PENDING':raise RuntimeError('ALREADY_SETTLED')
        for x in candidates:
            changed=c.execute('''UPDATE v55_paper SET status=?,result_home=?,result_away=?,result_seen=?,
                     settled_at=?,profit=?,source=? WHERE forecast_id=? AND status='PENDING' ''',
                     (x['status'],x['result_home'],x['result_away'],timestamp,timestamp,x['profit'],
                      SOURCE_PREFIX+'_'+x['proof_grade'],x['forecast_id']))
            if changed.rowcount!=1:raise RuntimeError('NON_ATOMIC_ROW_COUNT')
        c.execute('''INSERT INTO v55_settlement_runs(checked_at,checked,settled,outcomes_unavailable,report)
                VALUES(?,?,?,?,?)''',(timestamp,len(candidates),len(candidates),0,json.dumps({
                    'state':'HOPE_V3_AS_RECORDED_PAPER_SIMULATION','tag':tag,
                    'odds_evidence':'UNVERIFIED_BOOKMAKER_PRICE','real_orders':0,
                    'do_not_mix_with_VERIFIED_QUOTE_PNL':True},sort_keys=True)))
        c.execute('COMMIT')
    except Exception:
        if c.in_transaction:c.execute('ROLLBACK')
        raise
    finally:c.close()
    manifest_obj['state']='APPLIED'
    write_atomic(manifest,manifest_obj)
    c=sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True,timeout=30)
    try:
        for x in candidates:
            v=c.execute('SELECT status,profit,source,result_home,result_away FROM v55_paper WHERE forecast_id=?',
                        (x['forecast_id'],)).fetchone()
            if v is None or v!=(x['status'],x['profit'],SOURCE_PREFIX+'_'+x['proof_grade'],x['result_home'],x['result_away']):
                raise RuntimeError('POST_SETTLEMENT_VERIFICATION_FAILED:'+repr(v)+' expected '+repr((x['status'],x['profit'],SOURCE_PREFIX+'_'+x['proof_grade'],x['result_home'],x['result_away'])))
    finally:c.close()
    manifest_obj['state']='APPLIED_VERIFIED';write_atomic(manifest,manifest_obj)
    result={'newly_closed':len(candidates),'backups_created':1,'backup_path':str(backup),
            'manifest_path':str(manifest),'outcomes':dict(collections.Counter(x['status'] for x in candidates)),
            'net_simulated_usd':round(sum(x['profit'] for x in candidates),4),
            'grade':dict(collections.Counter(x['proof_grade'] for x in candidates)),
            'odds_proven_by_bookmaker':False,'state':'APPLIED_VERIFIED','real_orders':0}
    emit('SIMULATED_PAPER_SETTLEMENT_APPLIED',result)
    return result

def classify_all(c):
    paper=collections.Counter();port=collections.Counter();unlinked=0;due_unresolved=0
    all_paper={};pnl=collections.defaultdict(lambda:{'rows':0,'stake':0.0,'net':0.0})
    for fid,status,source,kick,stake,profit in c.execute('SELECT forecast_id,status,source,kickoff_utc,stake,profit FROM v55_paper'):
        all_paper[str(fid)]=(status,source,kick)
        if status in ('WIN','LOSS','PUSH'):
            pool='SIMULATED_UNVERIFIED_ODDS' if str(source or '').startswith(SOURCE_PREFIX) else 'PREEXISTING_SETTLEMENT'
            pnl[pool]['rows']+=1;pnl[pool]['stake']+=float(stake or 0);pnl[pool]['net']+=float(profit or 0)
        if status in ('WIN','LOSS','PUSH'):
            cat='FINISHED_AS_RECORDED_UNVERIFIED_ODDS' if str(source or '').startswith(SOURCE_PREFIX) else 'FINISHED_PREEXISTING'
        elif status=='PENDING':
            t=clock(kick)
            if t and t>now():cat='WAITING_EVENT'
            else:cat='STILL_NEEDS_RESULT_OR_EVIDENCE';due_unresolved+=1
        elif 'ARCHIV' in str(status).upper():cat='LEGACY_ARCHIVED_NOT_SETTLED'
        else:cat='OTHER_'+str(status)
        paper[cat]+=1
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='v56_portfolio'").fetchone():
        for (fid,) in c.execute('SELECT forecast_id FROM v56_portfolio'):
            if fid not in all_paper:unlinked+=1;port['UNLINKED']+=1
            else:
                st,src,_=all_paper[str(fid)]
                cat='FINISHED_AS_RECORDED_UNVERIFIED_ODDS' if str(src or '').startswith(SOURCE_PREFIX) else str(st)
                port[cat]+=1
    result_pnl={k:{'rows':x['rows'],'stake_usd':round(x['stake'],4),
                    'net_usd':round(x['net'],4),
                    'roi_pct':round(x['net']/x['stake']*100,4) if x['stake'] else None}
                for k,x in pnl.items()}
    return {'paper':dict(paper),'portfolio':dict(port),'portfolio_unlinked':unlinked,
            'still_due_pending':due_unresolved,'total_paper':sum(paper.values()),
            'total_portfolio':sum(port.values()),'pnl_separated_by_quote_evidence':result_pnl,
            'WARNING':'NATIVE_R10_ALL_PAPER_PNL_INCLUDES_V3_SIMULATIONS_AFTER_V3_APPLY; USE_SEPARATED_PNL'}

def reconcile_incomplete_manifests():
    """Recover from power loss between SQLite COMMIT and manifest fsync.

    Does not rewrite a single paper row. Fail closed on mixed or altered states.
    """
    if not BACKUP.is_dir():return
    for path in sorted(BACKUP.glob('hope_v3_*.manifest.json')):
        data=json.loads(path.read_text(encoding='utf-8'))
        if data.get('state') not in ('PREPARED','APPLIED'):continue
        if data.get('release')!='HOPE_V3_PAPER_SIMULATION' or data.get('source')!=SOURCE_PREFIX:
            raise RuntimeError('UNTRUSTED_PREPARED_MANIFEST')
        c=sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True,timeout=30)
        states=set()
        try:
            for x in data['candidates']:
                row=c.execute('SELECT '+','.join(PAPER_COLS)+' FROM v55_paper WHERE forecast_id=?',(x['forecast_id'],)).fetchall()
                if len(row)!=1:states.add('UNKNOWN');continue
                old=dict(zip(PAPER_COLS,row[0]))
                if old==x['original']:states.add('OLD');continue
                vals=(old['status'],old['result_home'],old['result_away'],old['result_seen'],
                    old['settled_at'],old['profit'],old['source'])
                want=(x['status'],x['result_home'],x['result_away'],data['created_at'],
                    data['created_at'],x['profit'],SOURCE_PREFIX+'_'+x['proof_grade'])
                if vals==want:states.add('NEW')
                else:states.add('UNKNOWN')
        finally:c.close()
        if states=={'OLD'}:data['state']='NOT_APPLIED'
        elif states=={'NEW'}:data['state']='APPLIED_VERIFIED'
        else:raise RuntimeError('INCOMPLETE_MANIFEST_REQUIRES_MANUAL_REVIEW:'+path.name)
        write_atomic(path,data)
        emit('MANIFEST_RECOVERED',{'name':path.name,'state':data['state']})

def rollback_data_latest():
    """Manual explicit selective rollback; no automatic data rollback on schedule rollback."""
    manifests=sorted(BACKUP.glob('hope_v3_*.manifest.json'),reverse=True)
    selected=None
    for path in manifests:
        data=json.loads(path.read_text())
        if data.get('state')=='APPLIED_VERIFIED':selected=(path,data);break
    if selected is None:emit('NO_APPLIED_MANIFEST',{});return
    p,data=selected;rows=data['candidates']
    c=sqlite3.connect(str(DB),timeout=30,isolation_level=None)
    try:
        c.execute('BEGIN IMMEDIATE')
        for x in rows:
            current=c.execute('SELECT '+','.join(OUTCOME_COLS)+' FROM v55_paper WHERE forecast_id=?',
                              (x['forecast_id'],)).fetchall()
            expected=(x['status'],x['result_home'],x['result_away'],data['created_at'],
                      data['created_at'],x['profit'],SOURCE_PREFIX+'_'+x['proof_grade'])
            if len(current)!=1 or current[0]!=expected:
                raise RuntimeError('ROLLBACK_ROW_CHANGED_REFUSE')
        for x in rows:
            old=x['original']
            vals=tuple(old[k] for k in OUTCOME_COLS)+(x['forecast_id'],)
            q='UPDATE v55_paper SET '+','.join(k+'=?' for k in OUTCOME_COLS)+' WHERE forecast_id=?'
            if c.execute(q,vals).rowcount!=1:raise RuntimeError('ROLLBACK_NON_ATOMIC_ROWCOUNT')
        c.execute('COMMIT')
    except Exception:
        if c.in_transaction:c.execute('ROLLBACK')
        raise
    finally:c.close()
    data['state']='ROLLED_BACK';write_atomic(p,data)
    emit('ROLLBACK_DATA',{'rows_restored':len(rows),'backup_retained':data['backup'],
                          'only_hope_v3_rows':True})

def run_cycle():
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    # V2 owns its own reconciliation lock; V3 a separate lock over the complete cycle.
    fd=os.open(str(STATE/'finality_v3.lock'),os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        v2=v2_module();v2.load_v1().runtime_identity()
        reconcile_incomplete_manifests()
        v2.run_once(True,True)
        snap=json.loads((STATE/'reconciliation_v2_last.json').read_text(encoding='utf-8'))
        report=json.loads(assert_r10_report(snap['r10_report']).read_text(encoding='utf-8'))
        v1=v2.load_v1()
        v1.runtime_identity()
        ro=v1.ro_connection()
        try:candidates,rejected=candidate_plan(report,ro)
        finally:ro.close()
        emit('PREVIEW',{'strict_native_r10':snap.get('r10_newly_settled'),
            'additional_as_recorded_candidates':len(candidates),'not_qualified':rejected,
            'forecast_event_count':len(set(x['event_sha12'] for x in candidates)),'real_orders':0})
        write_result=apply(candidates)
        ro=v1.ro_connection()
        try:statuses=classify_all(ro)
        finally:ro.close()
        answer={'at':stamp(),'mode':'PAPER_ONLY',
                'scheduler':'EVERY_3_HOURS','native_r10_strict_newly_settled':snap.get('r10_newly_settled'),
                'simulated_as_recorded':write_result,'status':statuses,
                'remaining_r10_reasons':report.get('reasons'),
                'quote_unverified_simulations_are_not_executable_price_proof':True,
                'real_orders':0,'ui_changed':False,'production_changed':False,
                'all_pending_remain_in_next_three_hour_audit':True}
        write_atomic(STATE/'finality_v3_last.json',answer)
        emit('FINALITY_CYCLE_PASS',answer)
        return answer
    except Exception as exc:
        failure={'at':stamp(),'error':type(exc).__name__+':'+str(exc)[:300],
                 'next_retry':'NEXT_3_HOUR','real_orders':0}
        write_atomic(STATE/'finality_v3_failure.json',failure)
        emit('FAILED',failure)
        raise
    finally:os.close(fd)

def main():
    action=sys.argv[1] if len(sys.argv)>1 else 'run'
    if action=='selftest':
        assert normalized_market_result('1X2','П1',2,1)=='WIN'
        assert normalized_market_result('ОБЕ ЗАБЬЮТ','Нет',0,2)=='WIN'
        assert normalized_market_result('ТОТАЛ 2.5','Больше 2.5',0,2)=='LOSS'
        emit('SELFTEST_PASS',{'real_orders':0})
    elif action=='run':run_cycle()
    elif action=='status':
        p=STATE/'finality_v3_last.json'
        if p.is_file():print(p.read_text())
        else:emit('NO_COMPLETED_CYCLE',{})
    elif action=='rollback-data':
        v2=v2_module();v2.load_v1().runtime_identity()
        STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(str(STATE/'finality_v3.lock'),os.O_CREAT|os.O_RDWR,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            reconcile_incomplete_manifests()
            rollback_data_latest()
        finally:os.close(fd)
    else:raise RuntimeError('UNSUPPORTED_ACTION')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        emit('ABORTED',{'error':type(exc).__name__+':'+str(exc)[:300]});sys.exit(1)
