#!/usr/bin/env python3
"""Runs exactly ONE ORIGINAL V4 task via its own due/cycle functions.

No historical oracle_*.py source edits. The source's own due() interval and
bookmaker/sports algorithms remain unchanged; only orchestration is filtered.
"""
import ast
import json
import os
import sqlite3
import sys
from pathlib import Path

DUPLICATES={'fonbet_first_party','v5_full_universe'}  # The identical native commands run in the core lane.

def names_from_source(path):
    tree=ast.parse(Path(path).read_bytes(),filename=str(path))
    fn=next((x for x in tree.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name=='cycle'),None)
    if fn is None:raise RuntimeError('ORIGINAL_V4_CYCLE_MISSING')
    for node in ast.walk(fn):
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='tasks' for t in node.targets):
            if not isinstance(node.value,(ast.List,ast.Tuple)):raise RuntimeError('V4_TASK_LIST_DRIFT')
            names=[]
            for elt in node.value.elts:
                if not isinstance(elt,ast.Tuple) or not elt.elts or not isinstance(elt.elts[0],ast.Constant) or not isinstance(elt.elts[0].value,str):
                    raise RuntimeError('UNSUPPORTED_V4_TASK_DECLARATION')
                names.append(elt.elts[0].value)
            if len(names)<10 or len(names)!=len(set(names)) or any(not x.replace('_','').isalnum() for x in names):
                raise RuntimeError('INVALID_V4_TASK_INVENTORY')
            return names
    raise RuntimeError('V4_TASKS_DECLARATION_NOT_FOUND')

def run(target):
    tasks=names_from_source('/app/oracle_v4.py')
    if target not in tasks:raise RuntimeError('V4_TARGET_NOT_IN_ORIGINAL_TASKS:'+target)
    if target in DUPLICATES:return {'state':'SKIPPED_DUPLICATE_CORE','task':target}
    # python /runner/v4_slot_v175.py does not include /app on sys.path.
    if not Path('/app/oracle_v4.py').is_file():raise RuntimeError('ORIGINAL_V4_SOURCE_MISSING')
    sys.path.insert(0,'/app')
    import oracle_v4
    if target == 'multi_sport_odds_hub':
        # Patch only this process in memory; original oracle_odds_hub.py is untouched.
        import odds_hub_bounded_v1710 as v1710
        historical_poll=oracle_v4.oracle_odds_hub.poll
        original_defaults=oracle_v4.oracle_odds_hub.default_sports
        oracle_v4.oracle_odds_hub.poll=(
            lambda connection,now=None,**kwargs: v1710.poll(
                connection,historical_poll,original_defaults,now=now,**kwargs))
    original_due=oracle_v4.sports.due
    calls=[]
    def single_due(connection,job,interval,at=None):
        if job != 'v4_'+target:return False
        calls.append(job)
        return original_due(connection,job,interval,at=at)
    oracle_v4.sports.due=single_due
    db_path=os.environ.get('ORACLE_DB','/data/oracle.sqlite3')
    if db_path!='/data/oracle.sqlite3':raise RuntimeError('PRIVATE_EVA_DB_REQUIRED')
    with sqlite3.connect(db_path,timeout=45) as con:
        con.execute('PRAGMA busy_timeout=45000')
        result=oracle_v4.cycle(con)
    if len(calls)!=1:raise RuntimeError('V4_DUE_ENTRY_NOT_REACHED:'+target)
    if not isinstance(result,dict):raise RuntimeError('V4_RESULT_NOT_MAPPING')
    sub=result.get(target)
    if sub is None:return {'state':'NOT_DUE','task':target}
    bad=False
    if isinstance(sub,dict):
        state=str(sub.get('state',sub.get('status','OK'))).upper()
        bad=state in ('ERROR','FAILED','PARTIAL','UNAVAILABLE','BLOCKED') or 'error' in sub
    else:state='OK'
    return {'state':'DEGRADED' if bad else 'OK','task':target,'original_result':sub}

if __name__=='__main__':
    try:
        if len(sys.argv)!=2:raise ValueError('Specify one original V4 task')
        result=run(sys.argv[1]);print(json.dumps(result,ensure_ascii=False,default=str),flush=True)
        sys.exit(2 if result['state']=='DEGRADED' else 0)
    except Exception as exc:
        print(json.dumps({'state':'ERROR','task':sys.argv[1:] ,'error':type(exc).__name__+':'+str(exc)[:300]}),flush=True)
        sys.exit(2)
