#!/usr/bin/env python3
"""ORACLE V7 administrative research CLI.  No API keys are printed or real orders placed."""
import argparse,json,os,sqlite3
import oracle_v7_core as core
import oracle_v7_crypto as crypto
DB=os.environ.get('ORACLE_DB','/app/state/oracle.sqlite3')

def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=('init','status','doctor','crypto-audit','crypto-backtest','integrations','probe','portfolio'))
 p.add_argument('--db',default=DB);p.add_argument('--symbols',type=int,default=45);p.add_argument('--hours',type=int,default=2500)
 a=p.parse_args();readonly=a.action not in ('init','probe')
 with sqlite3.connect('file:'+a.db+'?mode=ro' if readonly else a.db,uri=readonly,timeout=45) as c:
  if a.action=='init':core.init(c);v={'state':'OK','version':'7.0','real_orders':0}
  elif a.action in ('status','doctor'):v=core.dashboard(c)
  elif a.action=='crypto-audit':v=crypto.last_failure(c)
  elif a.action=='crypto-backtest':v=crypto.run_hypothesis_test(c,min(250,max(5,a.symbols)),min(12000,max(300,a.hours)))
  elif a.action=='integrations':v=core.dashboard(c)['integrations']
  elif a.action=='probe':v=core.probe(c)
  else:v=core.portfolio(c)
 print(json.dumps(v,ensure_ascii=False,indent=2,default=str))
if __name__=='__main__':main()
