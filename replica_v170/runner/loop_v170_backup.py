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
def cycle():
  log=[]
  for name,args in TASKS:
    try:
      p=subprocess.run(args,cwd='/app',text=True,capture_output=True,timeout=480,env=os.environ)
      log.append({'job':name,'returncode':p.returncode,'stdout':p.stdout[-1500:],'stderr':p.stderr[-1500:]})
    except Exception as exc: log.append({'job':name,'error':str(exc)[:300]})
  doc={'time_utc':datetime.now(timezone.utc).isoformat(),'jobs':log,'real_orders':0}
  print(json.dumps(doc,ensure_ascii=False),flush=True)
  return all(x.get('returncode')==0 for x in log)
while True:
  begun=time.monotonic()
  cycle()
  time.sleep(max(0,3600-(time.monotonic()-begun)))
