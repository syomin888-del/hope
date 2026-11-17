#!/usr/bin/env python3
"""Verified original V5.6 source query tightened in RAM; never edit /app files.

Old historical portfolio code allowed rows already marked EVA_LEGACY_ARCHIVED.
For EVA forward selections require PENDING and registered no earlier than EVA genesis.
No artificial probabilities, historical reruns, or retrospective betting.
"""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path
import re
import sys
import types

SOURCE_SHA='0b27b7e272db7f2506ae011d1b733f0188dfdffea28e50d6506e5d4b3524567c'
APP=Path('/app')
OLD_ANCHOR=re.compile(r'\bWHERE\s+p\.kickoff_utc\s*>\s*\?\s+AND\s+p\.registered_at\s*<=\s*\?',re.I)
REPLACEMENT="WHERE p.status='PENDING' AND julianday(p.registered_at)>=julianday((SELECT value FROM eva_control WHERE key='created_utc')) AND p.kickoff_utc>? AND p.registered_at<=?"

class PortfolioTransformer(ast.NodeTransformer):
    def __init__(self):self.count=0;self.in_sync=False
    def visit_FunctionDef(self,node):
        previous=self.in_sync
        self.in_sync=node.name=='sync'
        self.generic_visit(node)
        self.in_sync=previous
        return node
    def visit_Constant(self,node):
        if not self.in_sync or not isinstance(node.value,str):return node
        sql=node.value
        if ('FROM v55_paper p' not in sql or 'JOIN v5_forecasts f' not in sql
                or 'p.kickoff_utc' not in sql):return node
        if 'p.status' in sql or 'eva_control' in sql:
            raise RuntimeError('V178_ORIGINAL_PORTFOLIO_QUERY_DRIFT')
        if len(re.findall(r"p\.kickoff_utc\s*>\s*\?\s+AND\s+p\.registered_at\s*<=\s*\?",sql,re.I))!=1:
            raise RuntimeError('V178_PORTFOLIO_ANCHOR_DUPLICATED_OR_MISSING')
        new,n=OLD_ANCHOR.subn(lambda m:REPLACEMENT,sql)
        if n!=1:raise RuntimeError('V178_PORTFOLIO_ANCHOR_MISSING_OR_DUPLICATE')
        self.count+=1
        return ast.copy_location(ast.Constant(value=new),node)

def compile_portfolio(source):
    tree=ast.parse(source,filename='/app/oracle_v56_portfolio.py')
    matches=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='sync']
    if len(matches)!=1:raise RuntimeError('V178_ONE_ORIGINAL_SYNC_REQUIRED')
    tr=PortfolioTransformer();tr.visit(tree)
    if tr.count!=1:raise RuntimeError('V178_NOT_EXACTLY_ONE_PORTFOLIO_QUERY:'+str(tr.count))
    ast.fix_missing_locations(tree)
    return compile(tree,'/app/oracle_v56_portfolio.py','exec')

def install_in_memory():
    path=APP/'oracle_v56_portfolio.py'
    blob=path.read_bytes()
    if hashlib.sha256(blob).hexdigest()!=SOURCE_SHA:
        raise RuntimeError('V178_HISTORICAL_V56_SHA_DRIFT')
    if str(APP) not in sys.path:sys.path.insert(0,str(APP))
    code=compile_portfolio(blob.decode('utf-8'))
    mod=types.ModuleType('oracle_v56_portfolio')
    mod.__file__=str(path)
    mod.__package__=''
    sys.modules['oracle_v56_portfolio']=mod
    try:exec(code,mod.__dict__)
    except BaseException:
        sys.modules.pop('oracle_v56_portfolio',None)
        raise
    print('V178_PORTFOLIO_SOURCE_GATE_ACTIVE',json.dumps({'original_sha256':SOURCE_SHA,'eligibility':'PENDING_CREATED_AFTER_EVA','original_file_changed':False}),flush=True)
