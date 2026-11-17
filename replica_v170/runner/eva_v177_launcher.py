#!/usr/bin/env python3
"""In-memory adaptation of ONE verified original paper INSERT, never modify /app files.

Predictions, bookmaker imports and source SHA bytes are unchanged. The only
adaptation is a pre-SQL validation for the V1.3 private-EVA paper policy.
"""
from __future__ import annotations
import ast
import hashlib
from pathlib import Path
import runpy
import sys
import types

EXPECTED_SETTLEMENT_SHA='a22936af760b26e0cbed3eacdea5f3a6ade0f757e4297caf6a881a3dca3ecf2f'
APP=Path('/app')
RUNNER=Path('/runner')
DB='/data/oracle.sqlite3'

class Adapter(ast.NodeTransformer):
    def __init__(self):self.count=0;self.statement=None
    def visit_Call(self,node):
        self.generic_visit(node)
        if (isinstance(node.func,ast.Attribute) and node.func.attr=='execute' and
            isinstance(node.func.value,ast.Name) and node.func.value.id=='c' and
            len(node.args)>=1 and isinstance(node.args[0],ast.Constant) and
            isinstance(node.args[0].value,str) and
            node.args[0].value.lstrip().upper().startswith('INSERT OR IGNORE INTO V55_PAPER')):
            if len(node.args)!=2 or node.keywords:
                raise RuntimeError('V177_UNEXPECTED_INSERT_CALL_SIGNATURE')
            import eva_paper_filter_v177 as gate
            gate.parse_statement(node.args[0].value)
            self.count+=1;self.statement=node.args[0].value
            node.func=ast.Attribute(value=ast.Name(id='eva_paper_gate',ctx=ast.Load()),
                                   attr='execute',ctx=ast.Load())
            node.args.insert(0,ast.Name(id='c',ctx=ast.Load()))
        return node

def transform(source):
    tree=ast.parse(source,filename='oracle_v5_settlement.py')
    tr=Adapter()
    result=tr.visit(tree)
    if tr.count!=1:raise RuntimeError('V177_NOT_EXACTLY_ONE_ORIGINAL_PAPER_INSERT:'+str(tr.count))
    # Restrict the adapted call to prepare() only.
    matches=0
    for node in tree.body:
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name=='prepare':
            matches=sum(1 for n in ast.walk(node) if isinstance(n,ast.Call) and
                        isinstance(n.func,ast.Attribute) and
                        isinstance(n.func.value,ast.Name) and
                        n.func.value.id=='eva_paper_gate' and n.func.attr=='execute')
    if matches!=1:raise RuntimeError('V177_INSERT_NOT_IN_PREPARE')
    ast.fix_missing_locations(result)
    return compile(result,'/app/oracle_v5_settlement.py','exec')

def install_in_memory():
    if not (APP/'oracle_v5.py').is_file() or not (APP/'oracle_v5_settlement.py').is_file():
        raise RuntimeError('V177_HISTORICAL_APP_MISSING')
    contents=(APP/'oracle_v5_settlement.py').read_bytes()
    if hashlib.sha256(contents).hexdigest()!=EXPECTED_SETTLEMENT_SHA:
        raise RuntimeError('V177_ORIGINAL_SETTLEMENT_SHA_DRIFT')
    if str(APP) not in sys.path:sys.path.insert(0,str(APP))
    if str(RUNNER) not in sys.path:sys.path.insert(0,str(RUNNER))
    import eva_paper_filter_v177 as gate
    code=transform(contents.decode('utf-8'))
    module=types.ModuleType('oracle_v5_settlement')
    module.__file__=str(APP/'oracle_v5_settlement.py')
    module.__package__=''
    module.eva_paper_gate=gate
    sys.modules['oracle_v5_settlement']=module
    try:exec(code,module.__dict__)
    except BaseException:
        del sys.modules['oracle_v5_settlement']
        raise

def main():
    install_in_memory()
    sys.argv=[str(APP/'oracle_v5.py'),'once','--db',DB]
    runpy.run_path(str(APP/'oracle_v5.py'),run_name='__main__')

if __name__=='__main__':main()
