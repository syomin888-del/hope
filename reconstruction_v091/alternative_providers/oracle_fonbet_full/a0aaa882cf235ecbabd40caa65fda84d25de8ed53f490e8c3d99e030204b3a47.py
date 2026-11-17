"""ORACLE Fonbet complete observed odds snapshots, V4 MA adapter.

First-party /ma/events/list?version=0 complete responses. Does not evade authentication,
security controls or rate limiting. Uses IPv4 first because the user's VPS demonstrated
that IPv4 works. Quotes retain opaque factor code and full factor metadata. Research only.
"""
from __future__ import annotations
import argparse
import datetime as dt
import gzip
import hashlib
import http.client
import io
import json
import math
import os
import socket
import sqlite3
import ssl
import sys
import time
import urllib.parse
from collections import Counter

UTC=dt.timezone.utc
DEFAULT_HOST='line-lb61-w.bk6bba-resources.com'
MAX_WIRE=70_000_000
MAX_BODY=100_000_000
PATH='/ma/events/list?lang=ru&version=0&scopeMarket=1600'

def stamp(t=None):
    d=t or dt.datetime.now(UTC)
    return d.astimezone(UTC).isoformat(timespec='seconds')

def canonical_host(host=None):
    value=host or os.getenv('ORACLE_FONBET_HOST') or DEFAULT_HOST
    if value not in (DEFAULT_HOST,):
        raise ValueError('Only the endpoint verified from this VPS is enabled')
    return value

def canonical_url():return 'https://'+canonical_host()+PATH

class IPv4HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        last=None
        addrs=socket.getaddrinfo(self.host,self.port,socket.AF_INET,socket.SOCK_STREAM)
        for family,kind,proto,_,addr in addrs[:4]:
            s=socket.socket(family,kind,proto)
            try:
                s.settimeout(self.timeout)
                s.connect(addr)
                self.sock=self._context.wrap_socket(s,server_hostname=self.host)
                return
            except (OSError,ssl.SSLError) as e:
                last=e;s.close()
        raise OSError('All IPv4 endpoints failed: '+type(last).__name__)

def decode_json_body(raw,encoding=''):
    if len(raw)>MAX_WIRE:raise ValueError('Compressed HTTP body exceeds 70 MB')
    if raw.startswith(b'\x1f\x8b'):
        pieces=[];size=0
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as src:
            while True:
                part=src.read(min(65536,MAX_BODY+1-size))
                if not part:break
                size+=len(part)
                if size>MAX_BODY:raise ValueError('Inflated body exceeds 100 MB')
                pieces.append(part)
        raw=b''.join(pieces)
    elif encoding.lower() in ('gzip','x-gzip'):
        raise ValueError('Expected gzip magic bytes')
    if len(raw)>MAX_BODY:raise ValueError('JSON body exceeds 100 MB')
    return json.loads(raw)

def fetch_json(fetcher=None):
    """One bounded TLS request, no redirects, identity verification, no auth."""
    if fetcher is not None:return fetcher(canonical_url())
    conn=IPv4HTTPSConnection(canonical_host(),timeout=12,context=ssl.create_default_context())
    try:
        conn.request('GET',PATH,headers={'Accept':'application/json','Accept-Encoding':'gzip',
                     'User-Agent':'ORACLE-Research/4.6 (public line monitor; no wagers)',
                     'Connection':'close'})
        r=conn.getresponse()
        if r.status in (429,503):
            raise RateLimited(r.status,r.getheader('Retry-After'))
        if r.status!=200:raise ValueError('HTTP '+str(r.status))
        if r.getheader('Content-Encoding','').lower() not in ('','gzip','x-gzip','identity'):
            raise ValueError('Unsupported encoding')
        body=r.read(MAX_WIRE+1)
        return decode_json_body(body,r.getheader('Content-Encoding',''))
    finally:conn.close()

class RateLimited(Exception):
    def __init__(self,status,retry_after=None):
        self.status=status;self.retry_after=retry_after
        super().__init__('HTTP '+str(status))

def snapshot_data(doc):
    d=doc.get('data',doc) if isinstance(doc,dict) else None
    if not isinstance(d,dict) or not isinstance(d.get('events'),list) or not isinstance(d.get('customFactors'),list) or not isinstance(d.get('sports'),list):
        raise ValueError('Expected Fonbet MA full snapshot with events, sports, customFactors')
    if not isinstance(d.get('packetVersion'),int) or d['packetVersion']<=0:
        raise ValueError('Invalid packetVersion')
    if d.get('fromVersion') not in (None,0):
        raise ValueError('Unexpected delta response: import requires full snapshot from version=0')
    return d

def summarize_payload(doc):
    d=snapshot_data(doc)
    nf=sum(len(row.get('factors') or []) for row in d['customFactors'] if isinstance(row,dict))
    sports=sorted({str(s.get('alias') or s.get('name')) for s in d['sports'] if isinstance(s,dict) and s.get('kind')=='sport' and (s.get('alias') or s.get('name'))})
    return {'state':'SCHEMA_OK','packet_version':d['packetVersion'],'events':len(d['events']),
            'sports_or_competitions':len(d['sports']),'factor_groups':len(d['customFactors']),
            'factor_entries':nf,'sport_labels':sports[:50],
            'warning':'Counts include nested events and opaque factor IDs, not verified available bets'}

def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS fb_full_events(
       event_id TEXT PRIMARY KEY,root_event_id TEXT,sport TEXT,league TEXT,home TEXT,away TEXT,
       start_utc TEXT,place TEXT,level INTEGER,source_json TEXT,first_seen TEXT,last_seen TEXT);
    CREATE TABLE IF NOT EXISTS fb_full_current(
       quote_key TEXT PRIMARY KEY,event_id TEXT,root_event_id TEXT,factor_id TEXT,point TEXT,
       decimal_price REAL,source_json TEXT,first_seen TEXT,last_seen TEXT,active INTEGER NOT NULL,
       last_snapshot TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS fb_full_current_event ON fb_full_current(root_event_id,event_id,active);
    CREATE TABLE IF NOT EXISTS fb_full_history(
       id INTEGER PRIMARY KEY,quote_key TEXT NOT NULL,event_id TEXT,decimal_price REAL,
       active INTEGER NOT NULL,changed_at TEXT NOT NULL,raw_sha TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fb_full_runs(id INTEGER PRIMARY KEY,checked_at TEXT NOT NULL,
       source TEXT NOT NULL,state TEXT NOT NULL,events_count INTEGER NOT NULL,factors_count INTEGER NOT NULL,
       changed_count INTEGER NOT NULL,detail TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fb_full_cooldown(provider TEXT PRIMARY KEY,until_utc TEXT,reason TEXT);
    CREATE TABLE IF NOT EXISTS fb_full_packets(packet_version TEXT PRIMARY KEY,imported_at TEXT NOT NULL,
       events_count INTEGER NOT NULL,observations INTEGER NOT NULL,source_host TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fb_full_sports(id TEXT PRIMARY KEY,raw_json TEXT NOT NULL,last_seen TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fb_full_live_context(id TEXT PRIMARY KEY,event_misc_json TEXT,live_info_json TEXT,last_seen TEXT NOT NULL);
    ''')
    c.commit()

def iso_start(value):
    try:return stamp(dt.datetime.fromtimestamp(float(value),UTC))
    except (ValueError,TypeError,OverflowError):return ''

def unpack_sports(d):
    ref={str(x.get('id')):x for x in d['sports'] if isinstance(x,dict) and x.get('id') is not None}
    def labels(sid):
        node=ref.get(str(sid));league=str(node.get('name') or '') if node else ''
        seen=set()
        while node and str(node.get('id')) not in seen:
            seen.add(str(node.get('id')))
            if node.get('kind')=='sport':return str(node.get('alias') or node.get('name') or ''),league
            node=ref.get(str(node.get('parentId')))
        return '',league
    return labels

def make_stage_rows(d,ids,root_for):
    """Emit every offered *valid decimal* factor; distinguish p and pt parameterization."""
    for group in d['customFactors']:
        if not isinstance(group,dict):continue
        eid=str(group.get('e',''))
        if eid not in ids:continue
        for f in group.get('factors') or []:
            if not isinstance(f,dict) or f.get('f') is None:continue
            try:
                price=float(f.get('v'))
                if not math.isfinite(price) or not (1.01<=price<=1001):continue
            except (TypeError,ValueError,OverflowError):continue
            # All non-price identity dimensions, preserving p, pt and any provider extension.
            identity={k:v for k,v in f.items() if k not in ('v', 'blocked', 'active', 'updateTime')}
            id_json=json.dumps([eid,identity],sort_keys=True,separators=(',',':'),ensure_ascii=False)
            qk=hashlib.sha256(id_json.encode()).hexdigest()
            point=json.dumps({'p':f.get('p'),'pt':f.get('pt')},ensure_ascii=False,separators=(',',':'))
            raw=json.dumps(f,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            yield (qk,eid,root_for(eid),str(f['f']),point,price,raw,hashlib.sha256(raw.encode()).hexdigest())

def ingest(c,document,observed_at=None):
    d=snapshot_data(document)
    counts=summarize_payload(document)
    if not counts['events'] or not counts['factor_entries']:
        raise ValueError('Empty snapshot rejected')
    observed_at=observed_at or stamp()
    ids={str(e['id']):e for e in d['events'] if isinstance(e,dict) and e.get('id') is not None}
    if not ids:raise ValueError('No events with valid IDs')
    existing=c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fb_full_packets'").fetchone()
    if not existing:setup(c)
    packet=str(d['packetVersion'])
    last=c.execute('SELECT packet_version FROM fb_full_packets ORDER BY CAST(packet_version AS INTEGER) DESC LIMIT 1').fetchone()
    if last and int(packet)<int(last[0]):
        return {'state':'STALE_PACKET','packet_version':packet,'latest_packet_version':last[0],
                'new_or_changed_quotes':0,'note':'Refused to regress live data to an older snapshot'}
    if c.execute('SELECT 1 FROM fb_full_packets WHERE packet_version=?',(packet,)).fetchone():
        with c:c.execute('''INSERT INTO fb_full_runs(checked_at,source,state,events_count,factors_count,changed_count,detail)
          VALUES(?,?,?,?,?,?,?)''',(observed_at,'fonbet_ma','UNCHANGED_PACKET',len(ids),counts['factor_entries'],0,'Provider packet unchanged'))
        return {'state':'UNCHANGED_PACKET','packet_version':packet,'events':len(ids),'new_or_changed_quotes':0}
    def root_for(eid):
        cur=str(eid);seen=set()
        while cur in ids and cur not in seen:
            seen.add(cur);e=ids[cur];parent=e.get('parentId')
            if not parent or str(parent)==cur:return cur
            cur=str(parent)
        return str(eid)
    labels=unpack_sports(d)
    # TEMP stage prevents per-factor Python SELECT (250k rows) and supports atomic diffs.
    c.execute('''CREATE TEMP TABLE IF NOT EXISTS fb_stage(
       quote_key TEXT PRIMARY KEY,event_id TEXT,root_event_id TEXT,factor_id TEXT,point TEXT,
       decimal_price REAL,source_json TEXT,raw_sha TEXT)''')
    c.execute('DELETE FROM fb_stage')
    buffer=[];raw_total=0
    try:
        for item in make_stage_rows(d,ids,root_for):
            raw_total+=1;buffer.append(item)
            if len(buffer)>=2000:
                c.executemany('INSERT OR REPLACE INTO fb_stage VALUES(?,?,?,?,?,?,?,?)',buffer);buffer=[]
        if buffer:c.executemany('INSERT OR REPLACE INTO fb_stage VALUES(?,?,?,?,?,?,?,?)',buffer)
        usable=c.execute('SELECT COUNT(*) FROM fb_stage').fetchone()[0]
        if usable<1:raise ValueError('No valid numeric decimal prices in snapshot')
        previous=c.execute('SELECT COUNT(*) FROM fb_full_current WHERE active=1').fetchone()[0]
        if previous>5000 and usable<previous*.25:
            raise ValueError('Unexpected >75% drop in full odds snapshot; preserving database')
        # Prevent an accidental update from a severely malformed but nonempty document.
        # Require root-level sports/event evidence for a live-sized feed.
        with c:
            for sport in d['sports']:
                if isinstance(sport,dict) and sport.get('id') is not None:
                    c.execute('INSERT INTO fb_full_sports VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET raw_json=excluded.raw_json,last_seen=excluded.last_seen',
                       (str(sport['id']),json.dumps(sport,ensure_ascii=False,separators=(',',':')),observed_at))
            misc={str(v.get('id')):v for v in d.get('eventMiscs',[]) if isinstance(v,dict) and v.get('id') is not None}
            live={str(v.get('id')):v for v in d.get('liveEventInfos',[]) if isinstance(v,dict) and v.get('id') is not None}
            for ctx_id in misc.keys()|live.keys():
                c.execute('INSERT INTO fb_full_live_context VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET event_misc_json=excluded.event_misc_json,live_info_json=excluded.live_info_json,last_seen=excluded.last_seen',
                   (ctx_id,json.dumps(misc[ctx_id],ensure_ascii=False) if ctx_id in misc else None,
                    json.dumps(live[ctx_id],ensure_ascii=False) if ctx_id in live else None,observed_at))
            for e in ids.values():
                eid=str(e['id']);rootid=root_for(eid);root=ids.get(rootid,e)
                sp,lg=labels(e.get('sportId'))
                c.execute('''INSERT INTO fb_full_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(event_id) DO UPDATE SET root_event_id=excluded.root_event_id,
                   sport=excluded.sport,league=excluded.league,home=excluded.home,away=excluded.away,
                   start_utc=excluded.start_utc,place=excluded.place,level=excluded.level,
                   source_json=excluded.source_json,last_seen=excluded.last_seen''',
                   (eid,rootid,sp,lg,str(e.get('team1') or root.get('team1') or ''),
                    str(e.get('team2') or root.get('team2') or ''),
                    iso_start(e.get('startTime') or root.get('startTime')),
                    str(e.get('place') or root.get('place') or ''),int(e.get('level') or 1),
                    json.dumps(e,ensure_ascii=False,separators=(',',':')),observed_at,observed_at))
            # A missing price at the next full snapshot means no longer observed, never claim it is still live.
            removed=c.execute('''SELECT COUNT(*) FROM fb_full_current a WHERE a.active=1
               AND NOT EXISTS(SELECT 1 FROM fb_stage s WHERE s.quote_key=a.quote_key)''').fetchone()[0]
            changes=c.execute('''SELECT COUNT(*) FROM fb_stage s LEFT JOIN fb_full_current a
               ON a.quote_key=s.quote_key WHERE a.quote_key IS NULL OR a.active=0
               OR a.decimal_price!=s.decimal_price OR a.source_json!=s.source_json''').fetchone()[0]
            c.execute('''INSERT INTO fb_full_history(quote_key,event_id,decimal_price,active,changed_at,raw_sha)
               SELECT s.quote_key,s.event_id,s.decimal_price,1,?,s.raw_sha FROM fb_stage s
               LEFT JOIN fb_full_current a ON a.quote_key=s.quote_key
               WHERE a.quote_key IS NULL OR a.active=0 OR a.decimal_price!=s.decimal_price OR a.source_json!=s.source_json''',(observed_at,))
            c.execute('''INSERT INTO fb_full_history(quote_key,event_id,decimal_price,active,changed_at,raw_sha)
               SELECT a.quote_key,a.event_id,a.decimal_price,0,?,'' FROM fb_full_current a
               WHERE a.active=1 AND NOT EXISTS(SELECT 1 FROM fb_stage s WHERE s.quote_key=a.quote_key)''',(observed_at,))
            c.execute('''INSERT INTO fb_full_current
                  (quote_key,event_id,root_event_id,factor_id,point,decimal_price,source_json,first_seen,last_seen,active,last_snapshot)
                SELECT s.quote_key,s.event_id,s.root_event_id,s.factor_id,s.point,s.decimal_price,
                  s.source_json,?,?,?,? FROM fb_stage s WHERE 1
                ON CONFLICT(quote_key) DO UPDATE SET event_id=excluded.event_id,
                  root_event_id=excluded.root_event_id,factor_id=excluded.factor_id,point=excluded.point,
                  decimal_price=excluded.decimal_price,source_json=excluded.source_json,
                  last_seen=excluded.last_seen,active=1,last_snapshot=excluded.last_snapshot''',
                (observed_at,observed_at,1,observed_at))
            c.execute('''UPDATE fb_full_current SET active=0,last_snapshot=? WHERE active=1
                AND NOT EXISTS(SELECT 1 FROM fb_stage s WHERE s.quote_key=fb_full_current.quote_key)''',(observed_at,))
            c.execute('''INSERT INTO fb_full_packets VALUES(?,?,?,?,?)''',(packet,observed_at,len(ids),usable,canonical_host()))
            c.execute('''INSERT INTO fb_full_runs(checked_at,source,state,events_count,factors_count,changed_count,detail)
                VALUES(?,?,?,?,?,?,?)''',(observed_at,'fonbet_ma','OK',len(ids),usable,changes+removed,
                       'full version=0 packet '+packet+'; missing active quotes deactivated'))
        return {'state':'OK','packet_version':packet,'events':len(ids),'factor_observations':usable,
                'raw_valid_entries':raw_total,'new_or_changed_quotes':changes,'removed_quotes':removed,
                'unknown_market_ids_preserved':c.execute('SELECT COUNT(DISTINCT factor_id) FROM fb_full_current').fetchone()[0],
                'note':'Every numeric factor stored with source metadata; not all factor codes are decoded'}
    finally:
        try:c.execute('DROP TABLE IF EXISTS temp.fb_stage')
        except sqlite3.Error:pass

def status(c):
    setup(c)
    last=c.execute('SELECT checked_at,state,events_count,factors_count,changed_count,detail FROM fb_full_runs ORDER BY id DESC LIMIT 1').fetchone()
    row=c.execute('SELECT imported_at,events_count,observations,packet_version FROM fb_full_packets ORDER BY imported_at DESC LIMIT 1').fetchone()
    if not row:return {'state':'NOT_CONNECTED','events':0,'factor_quotes_current':0,'factor_codes':0,'historical_price_changes':0}
    now=dt.datetime.now(UTC);fresh_time=(last[0] if last and last[1] in ('OK','UNCHANGED_PACKET') else row[0]);age=(now-dt.datetime.fromisoformat(fresh_time)).total_seconds()
    # Only active quotes, not old historical prices or closed markets.
    active=c.execute('SELECT COUNT(*) FROM fb_full_current WHERE active=1').fetchone()[0]
    n_codes=c.execute('SELECT COUNT(DISTINCT factor_id) FROM fb_full_current WHERE active=1').fetchone()[0]
    n_changes=c.execute('SELECT COUNT(*) FROM fb_full_history').fetchone()[0]
    sports=c.execute('''SELECT e.sport,COUNT(DISTINCT e.root_event_id),COUNT(*)
      FROM fb_full_current q JOIN fb_full_events e ON q.event_id=e.event_id
      WHERE q.active=1 GROUP BY e.sport ORDER BY COUNT(*) DESC LIMIT 16''').fetchall()
    return {'state':'OBSERVED' if 0<=age<=3600 else 'STALE',
            'events':row[1],'factor_quotes_current':active,'factor_quotes_seen_all_time':c.execute('SELECT COUNT(*) FROM fb_full_current').fetchone()[0],
            'factor_codes':n_codes,'historical_price_changes':n_changes,'packet_version':row[3],
            'last_snapshot_utc':row[0],
            'last_run':dict(zip(('at','state','events','factors','changed','detail'),last)) if last else None,
            'sports':[{'sport':sport or 'unknown','events':ev,'factor_quotes':n} for sport,ev,n in sports],
            'warning':'Raw factor IDs and parameters are not always decoded to human-readable betting markets'}

def cooldown(c,now):
    r=c.execute('SELECT until_utc FROM fb_full_cooldown WHERE provider=?',('fonbet',)).fetchone()
    if not r:return 0
    try:return max(0,int((dt.datetime.fromisoformat(r[0])-now).total_seconds()))
    except (TypeError,ValueError):return 0

def once(c,loader=None,now=None):
    setup(c);now=now or dt.datetime.now(UTC)
    hold=cooldown(c,now)
    if hold:return {'state':'COOLDOWN','seconds_remaining':hold}
    try:
        return ingest(c,fetch_json(loader),stamp(now))
    except RateLimited as e:
        try:seconds=int(e.retry_after)
        except (TypeError,ValueError):seconds=900
        seconds=min(14400,max(60,seconds))
        with c:c.execute('''INSERT INTO fb_full_cooldown VALUES(?,?,?)
            ON CONFLICT(provider) DO UPDATE SET until_utc=excluded.until_utc,reason=excluded.reason''',
            ('fonbet',stamp(now+dt.timedelta(seconds=seconds)),str(e)))
        out={'state':'RATE_LIMITED','seconds_remaining':seconds}
    except Exception as e:
        out={'state':'UNAVAILABLE','error':type(e).__name__+': '+str(e)[:180],
             'existing_data_preserved':True}
    with c:c.execute('''INSERT INTO fb_full_runs(checked_at,source,state,events_count,factors_count,changed_count,detail)
       VALUES(?,?,?,?,?,?,?)''',(stamp(now),'fonbet_ma',out['state'],0,0,0,out.get('error','retry later')))
    return out

def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=('probe','probe-file','ingest-stdin','once','status'))
    p.add_argument('--db',default=os.getenv('ORACLE_DB','/app/state/oracle.sqlite3'))
    p.add_argument('--file',help='Host-local snapshot file used by probe-file')
    args=p.parse_args()
    if args.command in ('probe','probe-file'):
        try:
            if args.command=='probe-file':
                if not args.file:raise ValueError('--file is required')
                with open(args.file,'rb') as handle:raw=handle.read(MAX_WIRE+1)
                result=summarize_payload(decode_json_body(raw))
            else:result=summarize_payload(fetch_json())
        except Exception as e:result={'state':'UNAVAILABLE','error':type(e).__name__+': '+str(e)[:150]}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if result['state']!='SCHEMA_OK':raise SystemExit(2)
        return
    c=sqlite3.connect(args.db,timeout=120)
    try:
        if args.command=='ingest-stdin':
            raw=sys.stdin.buffer.read(MAX_WIRE+1)
            result=ingest(c,decode_json_body(raw))
        elif args.command=='once':result=once(c)
        else:result=status(c)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:c.close()

if __name__=='__main__':main()
