#!/usr/bin/env python3
"""Long-running research brain. It coordinates; never trades or places bets.

All three activities persist checkpoint timestamps so restarting cannot cause
unbounded historical requests or model retraining every minute.
"""
import argparse
import datetime as dt
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sqlite3
import time
import traceback

import backfill
import backup
import shutil
import collect
import research
import market
import knowledge
import discovery
import telemetry
import oracle
import storage

ROOT=Path(__file__).resolve().parent
UTC=dt.timezone.utc
JOBS={
    'backfill': 3600,
    'train':86400,
    'settle':1800,
    'forecast':3600,
    'markets':21600,
    'backup':86400,
    'storage':6*3600,
    'knowledge':3600,
    'discover':86400,
}

def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS brain_schedule(
     job TEXT PRIMARY KEY, last_run TEXT, next_run TEXT NOT NULL,
     failures INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'never')''')
    c.commit()

def jobs(c,now=None,force=False):
    setup(c)
    now=now or dt.datetime.now(UTC)
    outcome={}
    fn={'backfill':lambda:(backfill.run(c,limit=6) if shutil.disk_usage(collect.DB.parent).free>8*1024**3 else [{'status':'skipped','reason':'Low disk space'}]),
        'train':lambda:research.train_all(c),
        'settle':lambda:research.settle(c),
        'forecast':lambda:research.generate_shadow(c),
        'markets':lambda:market.refresh(c),
        'backup':lambda:str(backup.perform(keep=7)),
        'storage':lambda:storage.run(c),
        'knowledge':lambda:knowledge.ingest(c,limit=4000),
        'discover':lambda:discovery.run(c)}
    for name,period in JOBS.items():
        row=c.execute('SELECT next_run,failures FROM brain_schedule WHERE job=?',(name,)).fetchone()
        if row and not force and dt.datetime.fromisoformat(row[0])>now:continue
        try:
            result=fn[name]()
            state='ok';error='';failures=0
            if name=='backfill' and any(x['status']=='error' for x in result):
                state='partial';error='Some archive requests failed'
        except Exception as exc:
            state='error';error=type(exc).__name__+': '+str(exc)[:180]
            failures=(row[1]+1 if row else 1)
        wait=period if state!='error' else min(24*3600,period*2**min(5,failures))
        next_dt=now+dt.timedelta(seconds=wait)
        with c:
            c.execute('''INSERT INTO brain_schedule(job,last_run,next_run,failures,status)
              VALUES(?,?,?,?,?) ON CONFLICT(job) DO UPDATE SET
              last_run=excluded.last_run,next_run=excluded.next_run,
              failures=excluded.failures,status=excluded.status''',
              (name,now.isoformat(),next_dt.isoformat(),failures,state))
        outcome[name]={'status':state,'message':error,'next_run':next_dt.isoformat()}
        research.log(c,name,state,outcome[name])
    return outcome

def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['once','run','status'])
    p.add_argument('--sleep',type=int,default=60)
    a=p.parse_args()
    if not 30<=a.sleep<=3600:raise SystemExit('Sleep outside 30..3600 seconds')
    c=research.open_db();setup(c)
    # Ensure the alert/audit journal exists even before the first approved signal.
    oracle.db_open(str(collect.DB)).close()
    if a.command=='status':
        print(json.dumps({'schedule':[dict(zip(['job','last_run','next_run','failures','status'],r))
          for r in c.execute('SELECT * FROM brain_schedule')], 'research':research.status(c)},ensure_ascii=False,indent=2));return
    if a.command=='once':
        print(json.dumps(jobs(c,force=True),ensure_ascii=False,indent=2));return
    logger=logging.getLogger('brain');logger.setLevel(logging.INFO)
    (ROOT/'logs').mkdir(exist_ok=True)
    h=RotatingFileHandler(ROOT/'logs'/'brain.log',maxBytes=3_000_000,backupCount=4)
    logger.addHandler(h)
    logger.info('ORACLE brain started; PAPER RESEARCH ONLY')
    telemetry.heartbeat(c,'research')
    while True:
        try:
            telemetry.heartbeat(c,'research')
            result=jobs(c)
            telemetry.heartbeat(c,'research')
            if result:logger.info(json.dumps(result,ensure_ascii=False))
        except Exception:
            logger.exception('Research scheduling exception')
            try:c.close()
            except Exception:pass
            c=research.open_db()
        time.sleep(a.sleep)
if __name__=='__main__':main()
