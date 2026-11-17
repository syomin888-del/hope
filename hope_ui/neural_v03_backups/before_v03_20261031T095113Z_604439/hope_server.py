#!/usr/bin/env python3
"""HOPE Control Center v0.1: localhost-only, read-only observer of ORACLE EVA.
No imports from the historic app, no user SQL, and no mutating HTTP methods.
"""
import argparse
import datetime as dt
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

VERSION = '0.2.1'
# Strict Tailscale Serve hostname allowlist. Does not replace authentication/ACL.
TAILSCALE_FQDN = 'platinum-intel-de-1-v-2-7176961-520446-main.tail454fc6.ts.net'
TAILSCALE_HTTPS_PORT = 8443
HERE = Path(__file__).resolve().parent
DEFAULT_BASE = Path('/opt/ORACLE_EVA/replica_v170')
MIMES = {'/':('index.html','text/html; charset=utf-8'), '/assets/app.js':('app.js','text/javascript; charset=utf-8'), '/assets/style.css':('style.css','text/css; charset=utf-8')}
MAX_BODY = 900000

class ReadOnlyData:
    def __init__(self, base):
        self.base=Path(base)
        self.db=self.base/'state/oracle.sqlite3'
        self.logs=self.base/'logs/v175'

    def connect(self):
        # SQLite URI ro is necessary but insufficient alone; also query_only and service sandbox.
        url='file:'+quote(str(self.db.resolve()),safe='/')+'?mode=ro'
        c=sqlite3.connect(url,uri=True,timeout=2)
        c.row_factory=sqlite3.Row
        c.execute('PRAGMA query_only=ON')
        c.execute('PRAGMA trusted_schema=OFF')
        c.execute('PRAGMA busy_timeout=2000')
        return c

    @staticmethod
    def rows(c, query, params=()):
        return [dict(r) for r in c.execute(query, params)]

    @staticmethod
    def safe_json(path, max_bytes=128000):
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size>max_bytes:
                return None
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError,UnicodeError):
            return None

    def latest_cycles(self, limit=8):
        root=self.logs/'core'
        result=[]
        for p in sorted(root.glob('run_*/events.jsonl'), reverse=True)[:max(12,limit*3)]:
            if p.is_symlink():continue
            try:
                if p.stat().st_size>1048576:continue
                for line in p.read_text(encoding='utf-8').splitlines():
                    e=json.loads(line)
                    if e.get('kind')=='RUN_FINISHED':
                        result.append({k:e.get(k) for k in ('at','state','completed_tasks','expected_tasks','real_orders')})
            except (OSError,ValueError,UnicodeError):continue
        return sorted(result,key=lambda x:x.get('at') or '',reverse=True)[:limit]

    def latest_v4(self,limit=16):
        out=[]
        for p in sorted((self.logs/'v4').glob('*/result.json'),reverse=True)[:max(limit*3,48)]:
            d=self.safe_json(p)
            if isinstance(d,dict):out.append({k:d.get(k) for k in ('at','job','state','exit_code','real_orders')})
        return sorted(out,key=lambda d:d.get('at') or '',reverse=True)[:limit]

    def overview(self):
        c=self.connect()
        try:
            paper=dict((str(x[0]),int(x[1])) for x in c.execute('SELECT status,COUNT(*) FROM v55_paper GROUP BY status'))
            portfolio=c.execute('''SELECT COUNT(*) FROM v56_portfolio p JOIN v55_paper v ON p.forecast_id=v.forecast_id
                            WHERE v.status!='EVA_LEGACY_ARCHIVED' ''').fetchone()[0]
            quarantine=c.execute('SELECT COUNT(*) FROM eva_v178_portfolio_quarantine').fetchone()[0]
            triggers=c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='v55_paper'").fetchone()[0]
            guard=c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='eva_v178_portfolio_forward_only'").fetchone()[0]
            settlement=self.rows(c,'SELECT checked_at,checked,settled,outcomes_unavailable FROM v55_settlement_runs ORDER BY id DESC LIMIT 1')
            recent=self.rows(c,'SELECT checked_at,provider,sport,state,events_seen,quotes_saved FROM odds_hub_runs ORDER BY id DESC LIMIT 1')
        finally:c.close()
        return {'product':'HOPE','version':VERSION,'at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
                'paper':paper,'portfolio_active_total':portfolio,'quarantine':quarantine,
                'paper_triggers':triggers,'portfolio_guard':bool(guard),
                'last_settlement':settlement[0] if settlement else None,
                'last_odds_direction':recent[0] if recent else None,
                'cycles':self.latest_cycles(6), 'v4_jobs':self.latest_v4(10),
                'scheduler':self.safe_json(self.logs/'last_hourly_status.json'),
                'cursor':self.safe_json(self.logs/'v4_position.json'),
                'odds_progress':self.safe_json(self.logs/'odds_hub_progress_v1710.json'),
                'mode':'PAPER ONLY','production_access':False}

    def paper(self, limit=30):
        c=self.connect()
        try:
            return self.rows(c,'''SELECT forecast_id,root_id,origin,market,selection,probability,
                      odds,stake,quote_at,kickoff_utc,registered_at,status,profit
                      FROM v55_paper WHERE status!='EVA_LEGACY_ARCHIVED'
                      ORDER BY registered_at DESC LIMIT ?''',(limit,))
        finally:c.close()

    def portfolio(self,limit=30):
        c=self.connect()
        try:
            return self.rows(c,'''SELECT pf.root_id,pf.forecast_id,pf.canonical_key,pf.selected_at,
                     pf.independent_models,pf.consensus_ev_pct, p.market,p.selection,p.odds,
                     p.probability,p.kickoff_utc,p.status,p.profit
                     FROM v56_portfolio pf JOIN v55_paper p ON pf.forecast_id=p.forecast_id
                     WHERE p.status!='EVA_LEGACY_ARCHIVED'
                     ORDER BY pf.selected_at DESC LIMIT ?''',(limit,))
        finally:c.close()

    def sources(self):
        c=self.connect()
        try:
            last=self.rows(c,'''SELECT checked_at,provider,sport,state,events_seen,quotes_saved,matches_linked
                             FROM odds_hub_runs ORDER BY id DESC LIMIT 40''')
            t=self.rows(c,'''SELECT checked_at,provider,sport,state,quotes_saved
                             FROM odds_hub_runs WHERE checked_at=(SELECT MAX(checked_at) FROM odds_hub_runs)
                             ORDER BY id''')
            return {'latest_directions':last,'latest_sweep':t,'progress':self.safe_json(self.logs/'odds_hub_progress_v1710.json'),
                    'v4_jobs':self.latest_v4(25)}
        finally:c.close()

    def settlements(self):
        c=self.connect()
        try:
            r=self.rows(c,'''SELECT checked_at,checked,settled,outcomes_unavailable
                             FROM v55_settlement_runs ORDER BY id DESC LIMIT 12''')
            ps=self.rows(c,'''SELECT status,COUNT(*) as total FROM v55_paper
                              WHERE status!='EVA_LEGACY_ARCHIVED' GROUP BY status''')
            perf=self.rows(c,'''SELECT COUNT(*) AS n, COALESCE(SUM(profit),0) AS profit,
                               COALESCE(SUM(stake),0) AS stake
                               FROM v55_paper WHERE status IN ('WIN','LOSS','PUSH')''')[0]
            pp=self.rows(c,'''SELECT COUNT(*) AS n, COALESCE(SUM(p.profit),0) AS profit,
                              COALESCE(SUM(p.stake),0) AS stake
                              FROM v56_portfolio x JOIN v55_paper p ON x.forecast_id=p.forecast_id
                              WHERE p.status IN ('WIN','LOSS','PUSH')''')[0]
            final=[]
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='v58_results' AND type='table'").fetchone():
                final=self.rows(c,'''SELECT league,date_utc,home,away,home_score,away_score,source,observed_at
                                      FROM v58_results ORDER BY observed_at DESC LIMIT 12''')
        finally:c.close()
        return {'runs':r,'paper_statuses':ps, 'paper_settled':perf,'portfolio_settled':pp,'final_scores':final,
                'note':'ROI только по подтверждённым WIN/LOSS/PUSH; PENDING и исторический архив исключены.'}

class Handler(BaseHTTPRequestHandler):
    server_version='HopeUI/0.1'
    sys_version=''
    def log_message(self, fmt, *args):
        logging.info('HTTP %s %s',self.client_address[0],fmt%args)
    def send_common(self):
        for k,v in {'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
          'Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
          'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'"}.items():self.send_header(k,v)
    def output(self,code,kind,data,head=False):
        if isinstance(data,str):data=data.encode('utf-8')
        if len(data)>MAX_BODY:
            code=503;kind='application/json';data=b'{"error":"RESPONSE_TOO_LARGE"}'
        self.send_response(code);self.send_common();self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(data)));self.end_headers()
        if not head:self.wfile.write(data)
    def do_GET(self):self.handle_get(False)
    def do_HEAD(self):self.handle_get(True)
    def handle_get(self,head):
        host=self.headers.get('Host','')
        expected={'127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port),
                  '127.0.0.1','localhost'}
        expected.add(TAILSCALE_FQDN)
        expected.add(f'{TAILSCALE_FQDN}:{TAILSCALE_HTTPS_PORT}')
        if host not in expected:
            return self.output(421,'text/plain','INVALID_HOST',head)
        parsed=urlparse(self.path)
        path=parsed.path
        if path in MIMES:
            f,mime=MIMES[path]
            return self.output(200,mime,(HERE/f).read_bytes(),head)
        if path=='/health':
            return self.json(200,{'state':'OK','app':'HOPE','version':VERSION},head)
        if path.startswith('/api/'):
            try:
                api={'/api/overview':self.server.data.overview,
                     '/api/paper':self.server.data.paper,
                     '/api/portfolio':self.server.data.portfolio,
                     '/api/sources':self.server.data.sources,
                     '/api/settlements':self.server.data.settlements}
                if path not in api:return self.json(404,{'error':'NOT_FOUND'},head)
                if path in ('/api/paper','/api/portfolio'):
                    q=parse_qs(parsed.query)
                    n=int(q.get('limit',['30'])[0]);n=max(1,min(n,50))
                    result=api[path](n)
                else:result=api[path]()
                return self.json(200,result,head)
            except (sqlite3.Error,OSError,ValueError,KeyError) as exc:
                logging.warning('Data source unavailable: %s',type(exc).__name__)
                return self.json(503,{'error':'HOPE_DATA_UNAVAILABLE','kind':type(exc).__name__},head)
        return self.output(404,'text/plain; charset=utf-8','Not found',head)
    def json(self,code,data,head=False):return self.output(code,'application/json; charset=utf-8',json.dumps(data,ensure_ascii=False,default=str).encode('utf-8'),head)
    def do_POST(self):self.output(405,'text/plain','READ_ONLY')
    def do_PUT(self):self.output(405,'text/plain','READ_ONLY')
    def do_PATCH(self):self.output(405,'text/plain','READ_ONLY')
    def do_DELETE(self):self.output(405,'text/plain','READ_ONLY')

class Server(ThreadingHTTPServer):
    daemon_threads=True
    request_queue_size=16
    def __init__(self,bind,port,data):
        super().__init__((bind,port),Handler)
        self.data=data

def main():
    a=argparse.ArgumentParser()
    a.add_argument('--base',default=str(DEFAULT_BASE))
    a.add_argument('--port',type=int,default=8877)
    a.add_argument('--bind',default='127.0.0.1')
    args=a.parse_args()
    if args.bind!='127.0.0.1':raise SystemExit('HOPE_REFUSES_NON_LOOPBACK_BIND')
    if not (1024<=args.port<=65535):raise SystemExit('INVALID_PORT')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    data=ReadOnlyData(args.base)
    with data.connect() as c:
        c.execute('SELECT COUNT(*) FROM v55_paper').fetchone()
    with Server(args.bind,args.port,data) as server:
        logging.info('HOPE_CONTROL_CENTER_READY http://127.0.0.1:%d (RO)',args.port)
        server.serve_forever(poll_interval=.5)
if __name__=='__main__':main()
