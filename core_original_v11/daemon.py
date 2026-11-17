#!/usr/bin/env python3
"""ORACLE independent read-only perpetual scheduler. Internet is required. No trade execution.

Use macOS LaunchAgent or Linux systemd for restart-on-crash, not a terminal tab.
"""
import argparse
import datetime as dt
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time

import collect
import sources
import telemetry

ROOT=Path(__file__).resolve().parent

def log_init():
    d=ROOT/'logs';d.mkdir(exist_ok=True)
    lg=logging.getLogger('oracle');lg.setLevel(logging.INFO)
    if not lg.handlers:
        f=RotatingFileHandler(d/'oracle.log',maxBytes=3_000_000,backupCount=3,encoding='utf-8')
        f.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        lg.addHandler(f)
        s=logging.StreamHandler();s.setFormatter(logging.Formatter('%(asctime)s %(message)s'));lg.addHandler(s)
    return lg

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('command',choices=['once','run','status','list'])
    ap.add_argument('--only', help='Optional comma-separated job names')
    ap.add_argument('--sleep',type=int,default=60, help='Seconds between due-time checks (30..3600)')
    args=ap.parse_args()
    if args.command=='list':
        for n,(secs,_) in sources.SCHEDULE.items():print(f'{n:23s} every {secs//60:5d} minutes'+(' [API key]' if n in sources.OPTIONAL else ' [public]'))
        return
    conn=collect.db_open()
    sources.create_tables(conn)
    keys=collect.local_keys()
    names=set(args.only.split(',')) if args.only else None
    if names and not names.issubset(sources.SCHEDULE):raise SystemExit('Unknown job name in --only')
    if args.command=='status':
        collect.status(conn)
        for row in conn.execute('''SELECT job,run_at,state,new_rows,message FROM source_runs WHERE id IN
            (SELECT MAX(id) FROM source_runs GROUP BY job) ORDER BY job'''):
            print(f'{row[0]}: {row[2]} @ {row[1]} ({row[3]} new) {row[4]}')
        return
    lg=log_init()
    if args.command=='once':
        # Explicit one-off run bypasses persistent next_due, but still records health.
        result={}
        for name in (names or sources.SCHEDULE.keys()):
            state,count=sources.run_one(conn,keys,name)
            result[name]={'status':state,'new_rows':count}
        print(json.dumps(result,ensure_ascii=False,indent=2));return
    if args.sleep<30 or args.sleep>3600:raise SystemExit('Invalid --sleep')
    lg.info('ORACLE scheduler starting: read-only, %d registered sources',len(sources.SCHEDULE))
    telemetry.heartbeat(conn,'sources')
    while True:
        try:
            telemetry.heartbeat(conn,'sources')
            res=sources.run_due(conn,keys,selected=names)
            telemetry.heartbeat(conn,'sources')
            if res:lg.info(json.dumps(res,ensure_ascii=False))
        except Exception:
            lg.exception('Scheduler loop error')
            try:conn.close()
            except Exception:pass
            conn=collect.db_open();sources.create_tables(conn)
        time.sleep(args.sleep)

if __name__=='__main__':main()
