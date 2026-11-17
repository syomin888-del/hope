#!/usr/bin/env python3
"""Independent EVA hourly loop: ORIGINAL CORE FIRST; bounded original V4 slots.

Runs all work sequentially (single SQLite writer lane). Each originally declared
V4 task still has its original due() check. No real bookmaker orders.
"""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import time
from v4_slot_v175 import names_from_source, DUPLICATES

DB='/data/oracle.sqlite3'
LOGS=Path('/logs/v175')
CORE=('/runner/probe.py',)
V4_TIME_BUDGET=480
V4_PER_TASK_TIMEOUT=35
ODDS_HUB_TASK_TIMEOUT=85  # EVA-only; hard limit for one bounded, resumable multi-provider sweep
V4_PER_CYCLE_LIMIT=12
HOUR=3600

def now():return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')

def atomic_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp_name=tempfile.mkstemp(dir=path.parent,prefix='.eva175_json_')
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(obj,f,ensure_ascii=False,sort_keys=True)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp_name,path)
    finally:
        if os.path.exists(tmp_name):os.unlink(tmp_name)

def read_position():
    p=LOGS/'v4_position.json'
    if not p.exists():return 0
    try:
        obj=json.loads(p.read_text());return max(0,int(obj.get('position',0)))
    except (ValueError,TypeError):raise RuntimeError('V4_CURSOR_CORRUPTED_REFUSE_REPLAY')

def core_due_wait():
    """Avoid a redundant second Fonbet full scan immediately after activation's probe."""
    root=LOGS/'core'
    recent=sorted(root.glob('run_*/events.jsonl')) if root.exists() else []
    if not recent:return 0
    for line in reversed(recent[-1].read_text().splitlines()):
        try:x=json.loads(line)
        except ValueError:continue
        if x.get('kind')=='RUN_STARTED':
            start=dt.datetime.fromisoformat(x['at'])
            age=(dt.datetime.now(dt.timezone.utc)-start).total_seconds()
            return max(0,HOUR-age) if age>=0 else 0
    return 0

def run_v4_slots():
    available=[n for n in names_from_source('/app/oracle_v4.py') if n not in DUPLICATES]
    if not available:raise RuntimeError('NO_V4_RESEARCH_TASKS')
    begin=time.monotonic()
    pos=read_position()%len(available)
    processed=0
    while processed < min(V4_PER_CYCLE_LIMIT,len(available)) and time.monotonic()-begin<V4_TIME_BUDGET:
        name=available[pos]
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        root=LOGS/'v4'/stamp
        root.mkdir(parents=True,exist_ok=False)
        args=['python','-u','/runner/v4_slot_v175.py',name]
        elapsed=time.monotonic()-begin
        max_task_seconds=(ODDS_HUB_TASK_TIMEOUT if name=='multi_sport_odds_hub'
                          else V4_PER_TASK_TIMEOUT)
        limit=max(1,min(max_task_seconds,int(V4_TIME_BUDGET-elapsed)))
        with (root/'stdout.txt').open('xb') as stdout,(root/'stderr.txt').open('xb') as stderr:
            try:
                p=subprocess.run(args,cwd='/app',stdout=stdout,stderr=stderr,timeout=limit,check=False)
                code=p.returncode;reason='EXIT_'+str(code)
            except subprocess.TimeoutExpired:
                code=124;reason='TIMEOUT_'+str(limit)+'S'
        entry={'at':now(),'job':name,'state':reason,'exit_code':code,'real_orders':0}
        atomic_json(root/'result.json',entry)
        print('V175_V4_SLOT '+json.dumps(entry,sort_keys=True),flush=True)
        pos=(pos+1)%len(available)
        atomic_json(LOGS/'v4_position.json',{'position':pos,'last_task':name,'at':now()})
        processed+=1
    return processed

def loop():
    LOGS.mkdir(parents=True,exist_ok=True)
    initial_wait=core_due_wait()
    if initial_wait:
        print('V175_ACTIVATION_GATE_COOLDOWN '+str(round(initial_wait))+'s',flush=True)
        time.sleep(initial_wait)
    while True:
        begin=time.monotonic()
        code=None
        try:
            p=subprocess.run(['python','-u',*CORE],cwd='/app',timeout=1420,check=False)
            code=p.returncode
        except subprocess.TimeoutExpired:
            code=124
        if code==0:
            try:run_v4_slots()
            except Exception as exc:
                print('V175_V4_SCHEDULER_DEGRADED '+type(exc).__name__+': '+str(exc)[:200],flush=True)
        else:
            print('V175_CORE_BLOCKED_NO_V4_START; exit_code='+str(code),flush=True)
        final={'at':now(),'core_exit_code':code,'state':'CORE_PASS' if code==0 else 'CORE_BLOCKED',
               'v4_scheduled':code==0,'real_orders':0}
        print('V175_HOURLY_STATUS '+json.dumps(final,sort_keys=True),flush=True)
        atomic_json(LOGS/'last_hourly_status.json',final)
        pause=max(0,HOUR-(time.monotonic()-begin))
        time.sleep(pause)

if __name__=='__main__':loop()
