#!/usr/bin/env python3
"""EVA-only bounded historical first cycle with persistent per-job evidence.

This runner changes observability of an ALREADY RECONSTRUCTED scheduler,
never historical forecasting or Fonbet source modules.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

DATA='/data/oracle.sqlite3'
ACTIONS=[
 ('archive_ingest',['python','-u','/app/sports_free.py','ingest']),
 ('original_sports_hypotheses',['python','-u','/app/sports_free.py','propose']),
 ('original_sports_finals',['python','-u','/app/sports_free.py','settle']),
 ('original_v4_jobs',['python','-u','-c','import json,sports_free,oracle_v4; c=sports_free.db(); print(json.dumps(oracle_v4.cycle(c),default=str)); c.close()']),
 ('fonbet_snapshot',['python','-u','/app/oracle_fonbet_full.py','once','--db',DATA]),
 ('original_v7_cycle',['python','-u','/app/oracle_v5.py','once','--db',DATA]),
]
INVALID={'ERROR','FAILED','PARTIAL','BLOCKED','UNAVAILABLE','SAFETY_HALT','ACTION_REQUIRED',
         'DB_BUSY','RATE_LIMITED','COOLDOWN','IMPORT_ALREADY_RUNNING'}
MAX_JOB_OUTPUT=16*1024*1024

def now():return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')

def errors(obj):
    if isinstance(obj,dict):
        if any(str(v).upper() in INVALID for k,v in obj.items() if k in ('state','status')):
            return True
        return any(errors(v) for v in obj.values())
    if isinstance(obj,list):return any(errors(v) for v in obj)
    return False

def summarize(path, exit_code, timed_out=False, oversized=False):
    size=path.stat().st_size if path.exists() else 0
    with path.open('rb') as f:
        if size > 2048:f.seek(size-2048)
        tail=f.read().decode('utf-8','replace')
    state='UNKNOWN';bad=True
    if not timed_out and not oversized and exit_code==0 and size<=MAX_JOB_OUTPUT:
        try:
            content=path.read_text('utf-8')
            data=json.loads(content)
            if isinstance(data,dict):
                state=str(data.get('state',data.get('status','OK')))
                bad=errors(data)
            else:state='JSON_NOT_OBJECT'
        except (ValueError,UnicodeError):state='INVALID_OR_MISSING_JSON'
    elif timed_out:state='TIMEOUT'
    elif oversized:state='OUTPUT_LIMIT'
    elif exit_code!=0:state='EXIT_NONZERO'
    return {'state':state,'internal_failure':bad or exit_code!=0,'stdout_tail':tail[-1200:],
            'stdout_bytes':size}


def append_log(file, row):
    payload=json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n'
    with file.open('a',encoding='utf-8') as f:
        f.write(payload);f.flush();os.fsync(f.fileno())

def publish(label, data):
    print('V174 '+label+' '+json.dumps(data,ensure_ascii=False,sort_keys=True),flush=True)

def terminate_process(p):
    if p.poll() is not None:return
    try:os.killpg(p.pid,signal.SIGTERM)
    except ProcessLookupError:return
    try:p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:os.killpg(p.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        p.wait(timeout=3)

def run_actions(actions, journal_root, max_total=380, per_job=180, max_output=MAX_JOB_OUTPUT, *, job_timeouts=None):
    journal_root=Path(journal_root)
    if journal_root.is_symlink() or (journal_root.exists() and not journal_root.is_dir()):
        raise RuntimeError('INVALID_PROBE_LOG_DIR')
    journal_root.mkdir(parents=True,exist_ok=True)
    run_id=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:10]
    folder=journal_root/('run_'+run_id)
    folder.mkdir(mode=0o700)
    journal=folder/'events.jsonl'
    started=time.monotonic()
    results=[]
    publish('RUN_STARTED',{'id':run_id,'journal':str(journal),'jobs':len(actions),'max_total_seconds':max_total})
    append_log(journal,{'kind':'RUN_STARTED','id':run_id,'at':now(),'jobs':len(actions)})
    for index,(name,command) in enumerate(actions,1):
        remaining=max_total-(time.monotonic()-started)
        if remaining<5:
            row={'kind':'TASK_FINISHED','job':name,'state':'TOTAL_TIME_BUDGET_EXHAUSTED','ok':False,'at':now()}
            append_log(journal,row);publish('TASK_FINISHED',row);results.append(row)
            break
        task_limit = job_timeouts.get(name, per_job) if job_timeouts else per_job
        limit=min(task_limit,max(1,int(remaining-4)))
        outfile=folder/('%02d_%s.stdout' %(index,name))
        errfile=folder/('%02d_%s.stderr' %(index,name))
        start={'kind':'TASK_STARTED','job':name,'index':index,'at':now(),'timeout_seconds':limit}
        append_log(journal,start);publish('TASK_STARTED',start)
        p=None;timed_out=False;oversized=False;code=None;error=None
        begin=time.monotonic()
        try:
            with outfile.open('xb') as out,errfile.open('xb') as err:
                p=subprocess.Popen(command,cwd='/app' if Path('/app').exists() else None,
                                   stdout=out,stderr=err,start_new_session=True,
                                   env=os.environ.copy())
                while p.poll() is None:
                    elapsed=time.monotonic()-begin
                    if outfile.stat().st_size+errfile.stat().st_size>max_output:
                        oversized=True;terminate_process(p);break
                    if elapsed>=limit:
                        timed_out=True;terminate_process(p);break
                    time.sleep(0.25)
                code=p.wait(timeout=3)
        except (OSError,subprocess.SubprocessError) as e:
            error=type(e).__name__+': '+str(e)[:250]
            if p is not None:terminate_process(p)
        s=summarize(outfile,code,timed_out,oversized) if outfile.exists() else {
            'state':'SPAWN_ERROR','internal_failure':True,'stdout_tail':'','stdout_bytes':0}
        stderr=''
        if errfile.exists():
            with errfile.open('rb') as f:
                size=errfile.stat().st_size
                if size>1000:f.seek(size-1000)
                stderr=f.read().decode('utf-8','replace')[-1000:]
        good=not s['internal_failure'] and code==0 and not error
        row={'kind':'TASK_FINISHED','job':name,'index':index,'at':now(),
             'duration_seconds':round(time.monotonic()-begin,2),'exit_code':code,
             'ok':good,'state':s['state'],'error':error,'stdout_tail':s['stdout_tail'],
             'stderr_tail':stderr,'stdout_bytes':s['stdout_bytes']}
        append_log(journal,row);publish('TASK_FINISHED',row);results.append(row)
        if not good:break  # Fail early; do not run remaining jobs against a partial state.
    passed=len(results)==len(actions) and all(x.get('ok') is True for x in results)
    final={'kind':'RUN_FINISHED','state':'PASS' if passed else 'BLOCKED',
           'id':run_id,'at':now(),'completed_tasks':len(results),'expected_tasks':len(actions),
           'journal':str(journal),'real_orders':0}
    append_log(journal,final);publish('RUN_FINISHED',final)
    return final

def main():
    root=Path('/logs/v174')
    result=run_actions(ACTIONS,root)
    return 0 if result['state']=='PASS' else 2

if __name__=='__main__':sys.exit(main())
