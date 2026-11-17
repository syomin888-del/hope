#!/usr/bin/env python3
"""ORACLE read-only local dashboard. Shows source snapshots, not invented forecasts."""
import argparse
import json
import os
import sqlite3
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import research
import knowledge
import discovery
import telemetry
import storage

ROOT=Path(__file__).resolve().parent


def snapshot(db_path, model_path):
    models={}
    if model_path.is_file():
        try:
            data=json.loads(model_path.read_text(encoding='utf-8'))
            if isinstance(data,dict):
                models={str(k):{'kind':str(v.get('kind','')), 'approved':bool(v.get('approved', False))} for k,v in data.items() if isinstance(v,dict)}
        except (ValueError,OSError):
            pass
    signals=[]; pending=0
    if db_path.exists():
        try:
            with sqlite3.connect(f'file:{db_path}?mode=ro',uri=True,timeout=2) as cn:
                cn.row_factory=sqlite3.Row
                signals=[dict(x) for x in cn.execute('SELECT id, kind, issued_at, horizon_at, status, sha256, payload, outcome, resolved_at FROM signals ORDER BY issued_at DESC LIMIT 200')]
                pending=cn.execute('SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL').fetchone()[0]
        except sqlite3.Error:
            pass
    source_status=[]
    if db_path.exists():
        try:
            with sqlite3.connect(f'file:{db_path}?mode=ro',uri=True,timeout=2) as cn:
                has_tables={r[0] for r in cn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'raw_snapshots' in has_tables:
                    source_status.extend({'provider':r[0], 'records':r[1], 'last_seen':r[2], 'type':'snapshots'}
                        for r in cn.execute('SELECT provider,COUNT(*),MAX(observed_at) FROM raw_snapshots GROUP BY provider'))
                if 'candles' in has_tables:
                    source_status.extend({'provider':r[0], 'records':r[1], 'last_seen':r[2], 'type':'candles'}
                        for r in cn.execute('SELECT provider,COUNT(*),MAX(fetched_at) FROM candles GROUP BY provider'))
        except sqlite3.Error:
            pass
    source_jobs=[]
    if db_path.exists():
        try:
            with sqlite3.connect(f'file:{db_path}?mode=ro',uri=True,timeout=2) as cn:
                tables={r[0] for r in cn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'source_runs' in tables:
                    sql='SELECT job,run_at,state,new_rows,message FROM source_runs WHERE id IN (SELECT MAX(id) FROM source_runs GROUP BY job) ORDER BY job'
                    source_jobs=[{'job':r[0],'last_run':r[1],'state':r[2],'new_rows':r[3],'message':r[4]} for r in cn.execute(sql)]
        except sqlite3.Error:
            pass
    cleaned=[]
    for s in signals:
        try:
            p=json.loads(s.pop('payload'))
        except (ValueError,TypeError):
            p={}
        try:
            outcome=json.loads(s.pop('outcome') or 'null')
        except ValueError:
            outcome=None
        cleaned.append({**s, 'title':str(p.get('title','')), 'model_id':str(p.get('model_id','')), 'probability':p.get('probability'), 'probability_lower':p.get('probability_lower'), 'odds':p.get('odds'), 'selection':p.get('selection'), 'bookmaker':p.get('bookmaker'), 'coin_id':p.get('coin_id'), 'entry_usd':p.get('entry_usd'),'target_usd':p.get('target_usd'),'sources':p.get('sources',[]), 'risks':p.get('risks',[]), 'outcome':outcome})
    return {'status':'research_engine', 'running': all(x['heartbeat_fresh'] for x in telemetry.get(db_path).get('services',[])) and len(telemetry.get(db_path).get('services',[])) == 2, 'database_exists':db_path.exists(), 'signals':cleaned, 'pending_notifications':pending, 'models':models,'sources_connected':len({s['provider'] for s in source_status}), 'source_status':source_status,'source_jobs':source_jobs,'ai_connected':0, 'disclaimer':'Вычислительный движок обучается на реальных архивах. Исторические модели — только виртуальные прогнозы, без гарантий.'}


def handler_factory(db, models):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,directory=str(ROOT),**kwargs)
        def do_GET(self):
            route=urlsplit(self.path).path
            if route in ('/', '/index.html'):
                self.path='/index.html'
                return super().do_GET()
            if route=='/api/intelligence':
                try:
                    with sqlite3.connect(f'file:{db}?mode=ro',uri=True,timeout=4) as c:
                        data=research.status(c)
                        data['recent_forecasts']=[dict(zip(['id','symbol','horizon_hours','threshold','entry','target','probability','status','actual_hit','recorded_at'],r)) for r in c.execute('SELECT id,symbol,horizon_hours,threshold,entry,target,probability,status,actual_hit,recorded_at FROM paper_forecasts ORDER BY recorded_at DESC LIMIT 60')]
                        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        data['backfill']=[dict(zip(['symbol','status','count'],r)) for r in c.execute('SELECT symbol,status,COUNT(*) FROM history_downloads GROUP BY symbol,status')] if 'history_downloads' in tables else []
                        data['knowledge']=knowledge.status(c)
                        data['discovery']=discovery.status(c)
                        data['hypotheses_recent']=[{'key':r[0],'status':r[1],'tested_at':r[2]} for r in c.execute('SELECT key,status,run_at FROM hypothesis_experiments ORDER BY id DESC LIMIT 20')]
                except (sqlite3.Error,ValueError) as e:
                    data={'models':[], 'paper':[], 'recent_forecasts':[], 'backfill':[], 'error':'Research not initialized yet'}
                body=json.dumps(data,ensure_ascii=False).encode('utf8')
                self.send_response(200);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
            if route=='/api/telemetry':
                body=json.dumps(telemetry.get(db),ensure_ascii=False,default=str).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route=='/api/storage':
                body=json.dumps(storage.report(),ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body);return
            if route=='/api/state':
                body=json.dumps(snapshot(db,models),ensure_ascii=False,default=str).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
                return
            self.send_error(404,'Not found')
        def do_POST(self):
            self.send_error(405,'Read-only dashboard')
        def log_message(self,format,*args):
            pass
    return Handler


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--db',default=str(os.environ.get('ORACLE_DB',str(ROOT/'oracle.sqlite3'))))
    parser.add_argument('--models',default=str(ROOT/'approved_models.json'))
    args=parser.parse_args()
    server=ThreadingHTTPServer((args.host,args.port),handler_factory(Path(args.db).resolve(),Path(args.models).resolve()))
    print(f'ORACLE dashboard: http://localhost:{args.port}',flush=True)
    print('Только локально (127.0.0.1); Ctrl+C для остановки.',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__=='__main__':main()
