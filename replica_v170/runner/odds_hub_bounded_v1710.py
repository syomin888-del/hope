#!/usr/bin/env python3
"""EVA V1.7.10: bounded fair per-sport orchestration of unchanged historical poll.

Each sport is passed to the original poll() in its original parsing/DB-commit
path. One private progress cursor survives timeouts, allowing fair coverage.
No historical /app module, odds mapping, credentials or production resources
are modified. The source's due() check remains in original oracle_v4.cycle().
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

CURSOR=Path('/logs/v175/odds_hub_progress_v1710.json')
SOFT_SECONDS=73
VALID_PROVIDERS=('oddslink','moviodds')


def plan(default_sports,providers=VALID_PROVIDERS,sports=None):
    result=[]
    for provider in providers:
        if provider not in VALID_PROVIDERS:continue
        options=default_sports(provider) if sports is None else sports
        for sport in options:
            if not isinstance(sport,str) or not sport.replace('_','').isalnum():
                raise RuntimeError('V1710_UNSAFE_SPORT_NAME')
            result.append((provider,sport))
    if not result or len(result)!=len(set(result)) or len(result)>40:
        raise RuntimeError('V1710_INVALID_PROVIDER_PLAN')
    return result


def plan_hash(items):
    return hashlib.sha256(json.dumps(items,separators=(',',':')).encode()).hexdigest()


def cursor_read(path,items):
    if path.is_symlink():raise RuntimeError('V1710_CURSOR_SYMLINK')
    if not path.exists():return 0
    j=json.loads(path.read_text())
    if j.get('plan_sha256')!=plan_hash(items):
        raise RuntimeError('V1710_SPORT_PLAN_DRIFT_REFUSE_CURSOR_RESET')
    pos=j.get('next_index')
    if type(pos) is not int or not 0<=pos<len(items):
        raise RuntimeError('V1710_CORRUPT_CURSOR_POSITION')
    return pos


def cursor_write(path,items,position):
    if path.is_symlink():raise RuntimeError('V1710_CURSOR_SYMLINK')
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise RuntimeError('V1710_CURSOR_DIR_MISSING_OR_SYMLINK')
    j={'version':'V1710','plan_sha256':plan_hash(items),
       'next_index':position,'next':list(items[position]),
       'last_checkpoint_utc':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.v1710_cursor_')
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(j,f,sort_keys=True)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def poll(connection,original_poll,default_sports,now=None,loader=None,
         providers=VALID_PROVIDERS,sports=None,*,
         progress=CURSOR,soft_seconds=SOFT_SECONDS,clock=time.monotonic):
    """No network beyond unchanged original_poll; partial outputs never claim OK."""
    items=plan(default_sports,providers=providers,sports=sports)
    start_index=cursor_read(progress,items)
    start=clock()
    rows=[]
    totals={'total_events_seen':0,'new_prices':0,'odds_matched_existing_football':0}
    completed=0
    for n in range(len(items)):
        # Preserve at least ~12s for one socket wait + parsing and checkpoint.
        if n>0 and clock()-start >= soft_seconds-14:break
        index=(start_index+n)%len(items)
        provider,sport=items[index]
        t0=clock()
        # Original function performs existing provider cooldown, parsing, writes,
        # and source provenance tracking. A process kill leaves the cursor on
        # this very same sport for safe retry next V4 timeslot.
        r=original_poll(connection,now=now,providers=(provider,),sports=(sport,))
        if not isinstance(r,dict) or not isinstance(r.get('runs'),list):
            raise RuntimeError('V1710_ORIGINAL_POLL_RETURN_DRIFT')
        sub=r['runs']
        if any(x.get('provider')!=provider or x.get('sport') not in (sport,'*') for x in sub):
            raise RuntimeError('V1710_POLL_SOURCE_SCOPE_DRIFT')
        rows.extend(sub)
        for key in totals:
            totals[key]+=int(r.get(key,0))
        completed+=1
        cursor_write(progress,items,(index+1)%len(items))
        state=','.join(str(x.get('state')) for x in sub) or 'NO_ROWS'
        print('V1710_ODDS_HUB_STEP '+json.dumps({'provider':provider,'sport':sport,
            'elapsed_s':round(clock()-t0,3),'state':state,
            'quotes_saved':int(r.get('new_prices',0)),
            'completed':completed,'total':len(items)},sort_keys=True),
              file=sys.stderr,flush=True)
    remainder=len(items)-completed
    healthy=all(x.get('state') in ('OK','EMPTY') for x in rows)
    outcome='OK' if not remainder and healthy and len(rows)==len(items) else 'PARTIAL'
    summary={'state':outcome,'runs':rows,**totals,
             'directions_completed':completed,'directions_planned':len(items),
             'directions_remaining':remainder,'resumes_from':list(items[(start_index+completed)%len(items)]),
             'soft_budget_seconds':soft_seconds,
             'source_code_changed':False,'real_orders':0}
    print('V1710_ODDS_HUB_SUMMARY '+json.dumps({k:v for k,v in summary.items() if k!='runs'},
              sort_keys=True),file=sys.stderr,flush=True)
    return summary
