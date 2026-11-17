import json, os, subprocess, time
from datetime import datetime,timezone
DB='/data/oracle.sqlite3'
TASKS=[
 ('archive_ingest',['python','-u','/app/sports_free.py','ingest']),
 ('original_sports_hypotheses',['python','-u','/app/sports_free.py','propose']),
 ('original_sports_finals',['python','-u','/app/sports_free.py','settle']),
 ('original_v4_jobs',['python','-u','-c','import json,sports_free,oracle_v4; c=sports_free.db(); print(json.dumps(oracle_v4.cycle(c),default=str)); c.close()']),
 ('fonbet_snapshot',['python','-u','/app/oracle_fonbet_full.py','once','--db',DB]),
 ('original_v7_cycle',['python','-u','/app/oracle_v5.py','once','--db',DB]),
]
BAD={'ERROR','FAILED','PARTIAL','BLOCKED','UNAVAILABLE','SAFETY_HALT','ACTION_REQUIRED'}
def has_internal_failure(obj):
  if isinstance(obj,dict):
    if any(str(v).upper() in BAD for k,v in obj.items() if k in ('state','status')):return True
    return any(has_internal_failure(v) for v in obj.values())
  if isinstance(obj,list):return any(has_internal_failure(x) for x in obj)
  return False
def cycle():
  log=[]
  for name,args in TASKS:
    try:
      p=subprocess.run(args,cwd='/app',text=True,capture_output=True,timeout=480,env=os.environ)
      try:
        doc=json.loads(p.stdout)
        valid=isinstance(doc,dict) and not has_internal_failure(doc)
      except (ValueError,TypeError):valid=False
      log.append({'job':name,'returncode':p.returncode,'internal_state_ok':valid,
                  'stdout_tail':p.stdout[-1000:],'stderr_tail':p.stderr[-700:]})
    except Exception as exc:log.append({'job':name,'error':str(exc)[:300]})
  passed=all(x.get('returncode')==0 and x.get('internal_state_ok') is True for x in log)
  doc={'time_utc':datetime.now(timezone.utc).isoformat(),
       'state':'PASS' if passed else 'BLOCKED','jobs':log,'real_orders':0}
  print(json.dumps(doc,ensure_ascii=False),flush=True)
  return passed
while True:
  begun=time.monotonic()
  cycle()
  time.sleep(max(0,3600-(time.monotonic()-begun)))
