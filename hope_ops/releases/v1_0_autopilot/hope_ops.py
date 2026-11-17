#!/usr/bin/env python3
"""HOPE EVA isolated operations; standard library, no production changes.

Not a replacement for HOPE Neural Cortex UI, native forecast engine or
its original quote ingestion pipeline. A fail-closed autopilot wrapper.
"""
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

UTC = dt.timezone.utc
HOST_ROOT = Path('/opt/ORACLE_EVA')
ROOT = HOST_ROOT / 'hope_ops'
STATE = ROOT / 'state'
LOGS = ROOT / 'logs'
DB = HOST_ROOT / 'replica_v170/state/oracle.sqlite3'
CONTAINER = 'oracle_eva_v170_legacy-historic-oracle-1'
NAME = '/' + CONTAINER
DATA_MOUNT = str(DB.parent)
UI_UNIT = 'oracle-hope-ui.service'
UI_ROOT = HOST_ROOT / 'hope_ui/v0_1'
SOURCE_SHA = 'a22936af760b26e0cbed3eacdea5f3a6ade0f757e4297caf6a881a3dca3ecf2f'
MIN_SPACE = 15 * 1024**3


def utc():
    return dt.datetime.now(UTC)


def iso():
    return utc().isoformat(timespec='seconds')


def emit(name, obj):
    print('HOPE_V1_' + name + ' ' + json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str), flush=True)


def atomic(path, obj):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(obj, out, ensure_ascii=False, sort_keys=True, indent=2, default=str)
            out.flush()
            os.fsync(out.fileno())
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():tmp.unlink()


def run(argv, timeout=20):
    return subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout, check=False)


def parse_clock(value):
    if not value:return None
    try:
        x=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if x.tzinfo is None:return None
        return x.astimezone(UTC)
    except (ValueError, OverflowError, TypeError):return None


def runtime_identity():
    p=run(['docker','inspect',CONTAINER],timeout=20)
    if p.returncode or not p.stdout.strip():
        raise RuntimeError('EVA_CONTAINER_UNAVAILABLE')
    items=json.loads(p.stdout)
    if len(items)!=1 or items[0].get('Name')!=NAME:
        raise RuntimeError('WRONG_CONTAINER_IDENTITY')
    info=items[0]
    if not info.get('State',{}).get('Running'):
        raise RuntimeError('EVA_CONTAINER_NOT_RUNNING')
    mounts=[x for x in info.get('Mounts',[]) if x.get('Destination')=='/data']
    if len(mounts)!=1 or mounts[0].get('Source')!=DATA_MOUNT:
        raise RuntimeError('WRONG_OR_ABSENT_EVA_VOLUME')
    if not DB.is_file():raise RuntimeError('EVA_DB_NOT_FOUND')
    # Defend against accidentally pointing this release at production.
    if '/ORACLE_EVA/' not in str(DB) or '/ORACLE_CORE' in str(DB):
        raise RuntimeError('PRODUCTION_PATH_NOT_ALLOWED')
    return {'name':info.get('Name'),'db_bytes':DB.stat().st_size,
            'mount_source':mounts[0].get('Source')}


def space_check(require_backup=False):
    free=shutil.disk_usage(str(HOST_ROOT)).free
    required=max(MIN_SPACE, int(DB.stat().st_size*2.1)) if require_backup else 512*1024**2
    if free<required:
        raise RuntimeError('DISK_SPACE_BELOW_SAFE_RESERVE free=%d required=%d'%(free,required))
    return {'free_bytes':free,'required_bytes':required}


def ro_connection():
    c=sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True,timeout=25)
    c.execute('PRAGMA query_only=ON')
    c.execute('PRAGMA busy_timeout=25000')
    return c


def ui_probe():
    result={'service':UI_UNIT,'checked_at':iso(),'no_ui_files_modified':True}
    svc=run(['systemctl','show',UI_UNIT,'-p','ActiveState','-p','ExecStart'],timeout=10)
    out=svc.stdout
    result['service_active']='ActiveState=active' in out
    result['unit_matches_expected']='hope_server.py' in out and str(UI_ROOT) in out
    for url, key in [('http://127.0.0.1:8877/health','health'),
                     ('http://127.0.0.1:8877/neural','neural'),
                     ('http://127.0.0.1:8877/api/neural','neural_api')]:
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'HOPE-AUTOPILOT/1.0'})
            with urllib.request.urlopen(req,timeout=7) as r:
                body=r.read(150000)
                result[key+'_http']=r.status
                if key=='health':
                    obj=json.loads(body)
                    result['ui_version']=str(obj.get('version','unknown'))
                    result['ui_state']=obj.get('state')
                elif key=='neural_api':
                    obj=json.loads(body)
                    result['neural_status']=obj.get('status')
                    result['neural_active_verified']=bool(obj.get('active')) and obj.get('status') not in ('NO_VERIFIED_ACTIVE_PROCESS',None)
                else:
                    low=body.lower()
                    result['neural_page_bytes_sampled']=len(body)
                    result['renderer_hints']={x: (x.encode() in low) for x in ('three','canvas','glb','webgl')}
        except (OSError,ValueError,TimeoutError) as ex:
            result[key+'_error']=type(ex).__name__+':'+str(ex)[:120]
    result['ui_http_healthy']=result.get('health_http')==200 and result.get('neural_http')==200
    result['ui_renders_3d_not_proven_by_http']=True
    return result


def maybe_recover_ui(u):
    # Restarts are restricted to the known isolated UI, NOT the EVA container.
    state_file=STATE/'ui_recovery.json'
    try:s=json.loads(state_file.read_text())
    except (OSError,ValueError):s={'failures':0,'restarts':[]}
    restarts=[x for x in s.get('restarts',[]) if (parse_clock(x) and (utc()-parse_clock(x)).total_seconds()<86400)]
    failures=0 if u.get('ui_http_healthy') else int(s.get('failures',0))+1
    recovered=False
    # No restart on a broken 3D page while the service is still running;
    # that needs a genuine UI source fix, not a fake skeleton fallback.
    if failures>=3 and not u.get('service_active') and u.get('unit_matches_expected') and len(restarts)<2:
        p=run(['systemctl','restart',UI_UNIT],timeout=25)
        restarts.append(iso())
        recovered=(p.returncode==0)
        failures=0
    atomic(state_file,{'failures':failures,'restarts':restarts,'last_attempt_ok':recovered})
    return {'restart_attempted':recovered,'failures':failures,'restarts_last_24h':len(restarts)}


def status_snapshot(recover=False):
    result={'at':iso(),'mode':'PAPER_ONLY','production_oracle_mutations':0,
            'real_orders':0,'monitor':'HOST_SIDE_ONLY',
            'unverified_predictions_not_promoted':True}
    try:
        identity=runtime_identity()
        result['eva']=identity
        try:
            c=ro_connection()
            try:
                result['paper_status']={str(a):int(b) for a,b in c.execute('SELECT status,COUNT(*) FROM v55_paper GROUP BY status')}
                result['last_paper_registration']=c.execute('SELECT MAX(registered_at) FROM v55_paper').fetchone()[0]
                result['last_settlement_run']=c.execute('SELECT MAX(checked_at) FROM v55_settlement_runs').fetchone()[0]
                source_counts=c.execute("""SELECT COALESCE(source,'(none)'),COUNT(*),
                  ROUND(COALESCE(SUM(profit),0),3) FROM v55_paper
                  WHERE status IN ('WIN','LOSS','PUSH') GROUP BY source ORDER BY COUNT(*) DESC""").fetchall()
                result['settlement_sources']=[{'source':str(src),'rows':int(n),'net_usd':net}
                    for src,n,net in source_counts]
                result['non_r10_settlements_present']=any(
                    x['rows'] and not x['source'].startswith('EVA_R10_TWO_INDEPENDENT_FINAL_EXACT_ORIGINAL_QUOTE')
                    for x in result['settlement_sources'])
            finally:c.close()
        except (sqlite3.Error,OSError) as e:
            result['database_error']=str(e)[:150]
    except (RuntimeError,OSError,ValueError,subprocess.TimeoutExpired) as e:
        result['eva_error']=str(e)[:180]
    result['ui']=ui_probe()
    if recover:
        result['ui_recovery']=maybe_recover_ui(result['ui'])
    try:result['disk_free_bytes']=shutil.disk_usage(str(HOST_ROOT)).free
    except OSError:result['disk_free_bytes']=None
    for name in ('settlement','capture'):
        try:result[name]=json.loads((STATE/(name+'_last.json')).read_text())
        except (OSError,ValueError):result[name]=None
    result['timers']={}
    for tag in ('settle','capture','health'):
        p=run(['systemctl','is-active','oracle-hope-ops-'+tag+'.timer'],timeout=8)
        result['timers'][tag]=p.stdout.strip() if p.returncode==0 else 'inactive'
    issues=[]
    if 'eva_error' in result:issues.append('EVA_NOT_RUNNING_OR_WRONG_MOUNT')
    if 'database_error' in result:issues.append('DB_READ_FAILED')
    if not result['ui'].get('ui_http_healthy'):issues.append('HOPE_UI_NOT_HEALTHY')
    if result['ui'].get('neural_status')=='NO_VERIFIED_ACTIVE_PROCESS':issues.append('NEURAL_ACTIVE_PROCESS_UNVERIFIED')
    if result['disk_free_bytes'] is not None and result['disk_free_bytes']<MIN_SPACE:issues.append('DISK_LOW')
    if not all(x=='active' for x in result['timers'].values()):issues.append('AUTOPILOT_TIMER_INACTIVE')
    if result['settlement'] and result['settlement'].get('result')=='FAILED':issues.append('LAST_SETTLEMENT_FAILED')
    for name,minutes in (('settlement',240),('capture',12)):
        if result[name]:
            t=parse_clock(result[name].get('at'))
            if t and (utc()-t).total_seconds()>minutes*60:issues.append(name.upper()+'_STALE')
    # Historical settlements by other engines are not automatically invalid;
    # they just have not been verified by the current R10 strict gate.
    result['other_engine_settlements_need_independent_evidence_audit']=result.get('non_r10_settlements_present',False)
    result['issues']=issues
    result['state']='OK' if not issues else 'DEGRADED'
    atomic(STATE/'status.json',result)
    emit('HEALTH',{'state':result['state'],'issues':issues,'ui_version':result['ui'].get('ui_version'),
        'ui_recovery':result.get('ui_recovery'),'paper_status':result.get('paper_status'),
        'real_orders':0})
    return result


def lock(name):
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    f=(STATE/(name+'.lock')).open('a')
    try:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RuntimeError('ALREADY_RUNNING_'+name.upper())
    return f


def evidence_capture():
    """Companion journal. Never claims the price was seen at quote_at.

    Captures quotes within 5min of REGISTERED time, source current quote as
    observed by this daemon, not an exchange/execution guarantee.
    """
    with contextlib.closing(lock('capture')):
        runtime_identity();space_check()
        STAT=STATE/'quote_receipts.sqlite3'
        jc=sqlite3.connect(str(STAT),timeout=30)
        try:
            jc.execute('PRAGMA journal_mode=WAL')
            jc.execute('''CREATE TABLE IF NOT EXISTS quote_receipts(
                forecast_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL,
                quote_at TEXT, registered_at TEXT, paper_odds REAL,
                current_price REAL, status TEXT NOT NULL,
                receipt_json TEXT NOT NULL, sha256 TEXT NOT NULL)''')
            seen=set(x[0] for x in jc.execute('SELECT forecast_id FROM quote_receipts'))
            c=ro_connection()
            try:
                required={'forecast_id','quote_key','quote_at','registered_at','odds','market','selection','status'}
                cols={r[1] for r in c.execute('PRAGMA table_info(v55_paper)')}
                if not required<=cols:raise RuntimeError('PAPER_CAPTURE_SCHEMA_UNVERIFIED')
                qcols={r[1] for r in c.execute('PRAGMA table_info(fb_full_current)')}
                if not {'quote_key','decimal_price'}<=qcols:raise RuntimeError('CURRENT_QUOTE_SCHEMA_UNVERIFIED')
                # Bound read: recent registered paper rows only; no full 5.7GB scan.
                floor=(utc()-dt.timedelta(minutes=7)).isoformat(timespec='seconds')
                q='SELECT forecast_id,quote_key,quote_at,registered_at,odds,market,selection,status FROM v55_paper WHERE registered_at>=? ORDER BY registered_at DESC LIMIT 1000'
                rows=c.execute(q,(floor,)).fetchall()
                count=collections_count={'saved':0,'late':0,'no_current':0,'price_mismatch':0,'within_5min':0}
                for fid,key,qat,reg,odds,market,selection,state in rows:
                    if fid in seen:continue
                    when=parse_clock(reg)
                    if not when or abs((utc()-when).total_seconds())>300:
                        count['late']+=1;continue
                    quoted=c.execute('SELECT decimal_price FROM fb_full_current WHERE quote_key=? LIMIT 2',(key,)).fetchall()
                    captured=utc().isoformat(timespec='microseconds')
                    cp=float(quoted[0][0]) if len(quoted)==1 and quoted[0][0] is not None else None
                    if cp is None:quality='NO_LIVE_QUOTE';count['no_current']+=1
                    elif abs(cp-float(odds))>1e-7:quality='PRICE_MISMATCH_AT_CAPTURE';count['price_mismatch']+=1
                    else:quality='POST_REGISTRATION_OBSERVATION_ONLY';count['within_5min']+=1
                    msg={'forecast_id':str(fid),'quote_key':str(key),'quote_at':qat,'registered_at':reg,
                         'market':market,'selection':selection,'paper_odds':odds,'observed_at':captured,
                         'current_price':cp,'proof_grade':quality,'claim_original_at_quote_at':False}
                    serialized=json.dumps(msg,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':'))
                    digest=hashlib.sha256(serialized.encode()).hexdigest()
                    jc.execute('INSERT OR IGNORE INTO quote_receipts VALUES(?,?,?,?,?,?,?,?,?)',
                               (str(fid),captured,qat,reg,odds,cp,quality,serialized,digest))
                    count['saved']+=1
                jc.commit()
            finally:c.close()
            counts={'saved_this_run':count['saved'],'late_skipped':count['late'],
                    'quote_absent':count['no_current'],'price_mismatch':count['price_mismatch'],
                    'matched_at_post_registration_capture':count['within_5min'],
                    'cumulative':jc.execute('SELECT count(*) FROM quote_receipts').fetchone()[0],
                    'at':iso(),'claim_original_at_quote_at':False,'db_original_writes':0}
            atomic(STATE/'capture_last.json',counts)
            emit('CAPTURE',counts)
            return counts
        finally:jc.close()


def settlement_cycle():
    with contextlib.closing(lock('settlement')):
        identity=runtime_identity();disk=space_check(True)
        script=Path(__file__).with_name('remote_runner.py')
        expected_path=Path(__file__).with_name('release.sha256.json')
        expected=json.loads(expected_path.read_text())['remote_runner.py']
        if hashlib.sha256(script.read_bytes()).hexdigest()!=expected:
            raise RuntimeError('SETTLEMENT_ENGINE_HASH_MISMATCH')
        # The container itself verifies /app/oracle_v5.py and settlement SHA.
        # No code or Docker Compose changes and no historical price interpolation.
        LOGS.mkdir(mode=0o700,parents=True,exist_ok=True)
        fn=LOGS/('settle_'+utc().strftime('%Y%m%dT%H%M%SZ')+'.log')
        argv=['docker','exec','-i','-e','EVA_R10_ACTION=apply','-e','PYTHONDONTWRITEBYTECODE=1',CONTAINER,'python','-']
        started=time.monotonic();ending='';summary=None;errors=[]
        with script.open('rb') as inp,fn.open('wb') as output:
            with subprocess.Popen(argv,stdin=inp,stdout=subprocess.PIPE,stderr=subprocess.STDOUT) as proc:
                for line in iter(proc.stdout.readline,b''):
                    if output.tell()<15*1024*1024:output.write(line)
                    out=line.decode('utf-8','replace').strip()
                    if out.startswith('EVA_R10_COMPLETED '):
                        try:summary=json.loads(out[len('EVA_R10_COMPLETED '):])
                        except ValueError:pass
                    if out.startswith('EVA_R10_ABORTED '):errors.append(out[:400])
                    if out.startswith('EVA_R10_APPLIED_VERIFIED '):ending='APPLIED_VERIFIED'
                    if out.startswith('EVA_R10_NO_CHANGES '):ending='NO_CHANGES'
                ret=proc.wait()
        state='PASS' if ret==0 and summary is not None else 'FAILED'
        record={'at':iso(),'result':state,'exit_code':ret,'duration_seconds':round(time.monotonic()-started,2),
                'ended':ending,'summary':summary,'errors':errors[:4], 'log':str(fn),
                'db_original_code_changed':False,'real_orders':0,'disk':disk,'eva':identity}
        atomic(STATE/'settlement_last.json',record)
        emit('SETTLEMENT',record)
        if state!='PASS':raise RuntimeError('SETTLEMENT_EXECUTION_FAILED '+str(ret))
        return record


def main():
    action=sys.argv[1] if len(sys.argv)>1 else 'health'
    if action not in ('health','capture','settle','status','selftest'):raise RuntimeError('UNKNOWN_ACTION')
    if action=='selftest':
        assert parse_clock('2026-11-16T12:00:00+00:00').hour==12
        assert parse_clock('broken') is None
        assert str(DB).startswith('/opt/ORACLE_EVA/')
        emit('SELFTEST_PASS',{'status':'PASS','real_orders':0});return
    if action=='settle':settlement_cycle();return
    if action=='capture':evidence_capture();return
    if action=='status':
        try:print((STATE/'status.json').read_text(encoding='utf-8'))
        except OSError:status_snapshot(False)
        return
    status_snapshot(True)


if __name__=='__main__':
    try:main()
    except Exception as ex:
        emit('FAIL_CLOSED',{'action':sys.argv[1:] or ['health'],'error_type':type(ex).__name__,
            'error':str(ex)[:250],'real_orders':0})
        sys.exit(1)
