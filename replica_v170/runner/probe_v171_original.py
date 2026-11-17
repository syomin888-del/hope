import json,os,subprocess,sys
from datetime import datetime,timezone
DB='/data/oracle.sqlite3'
ACTIONS=[
 ('archive_ingest',['python','-u','/app/sports_free.py','ingest']),
 ('original_sports_hypotheses',['python','-u','/app/sports_free.py','propose']),
 ('original_sports_finals',['python','-u','/app/sports_free.py','settle']),
 ('original_v4_jobs',['python','-u','-c','import json,sports_free,oracle_v4; c=sports_free.db(); print(json.dumps(oracle_v4.cycle(c),default=str)); c.close()']),
 ('fonbet_snapshot',['python','-u','/app/oracle_fonbet_full.py','once','--db',DB]),
 ('original_v7_cycle',['python','-u','/app/oracle_v5.py','once','--db',DB]),
]
results=[]
for name,action in ACTIONS:
    try:
        p=subprocess.run(action,cwd='/app',capture_output=True,text=True,timeout=180)
        try:
            doc=json.loads(p.stdout)
            if not isinstance(doc,dict):raise ValueError('NOT_DICTIONARY')
            state=str(doc.get('state','OK'))
            def errors(x):
                if isinstance(x,dict):
                    for k,v in x.items():
                        if k in ('state','status') and str(v).upper() in (
                          'ERROR','FAILED','PARTIAL','BLOCKED','UNAVAILABLE','SAFETY_HALT','ACTION_REQUIRED'):
                            return True
                        if errors(v):return True
                if isinstance(x,list):return any(errors(v) for v in x)
                return False
            bad=errors(doc)
        except Exception:
            state='INVALID_OR_MISSING_JSON';bad=True
        results.append({'job':name, 'exit_code':p.returncode,
                        'state':state,'internal_failure':bad,
                        'stdout_tail':p.stdout[-1200:], 'stderr_tail':p.stderr[-700:]})
    except Exception as e:
        results.append({'job':name,'error':type(e).__name__+':'+str(e)[:250]})
ok=all(r.get('exit_code')==0 and not r.get('internal_failure') for r in results)
print(json.dumps({'state':'PASS' if ok else 'BLOCKED','at':datetime.now(timezone.utc).isoformat(),
                  'tasks':results,'real_orders':0},ensure_ascii=False,indent=2))
sys.exit(0 if ok else 2)
