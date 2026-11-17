"""ORACLE V4 direct Fonbet public-line reader, research only.

All *observable* event factor entries are archived by (event, sub-event, factor id,
point) without guessing semantics. The public route was reported by third-party
open-source tools; ability to access it MUST be independently proven on the VPS.
Never logs in, breaks challenges, rotates IPs, or places bets.
"""
from __future__ import annotations
import argparse
import gzip
import io
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import time
from collections import Counter

DEFAULT_BASE='https://line01w.bk6bba-resources.com'
MAX_BYTES=70_000_000
UTC=dt.timezone.utc

def stamp(d=None):
    d=d or dt.datetime.now(UTC)
    return d.astimezone(UTC).isoformat(timespec='seconds')

def canonical_url(base=None):
    base=(base or os.environ.get('ORACLE_FONBET_PUBLIC_BASE') or DEFAULT_BASE).rstrip('/')
    p=urllib.parse.urlsplit(base)
    if p.scheme!='https' or not p.hostname or not p.hostname.endswith('.bk6bba-resources.com') or p.username or p.password or p.query or p.fragment or p.path not in ('','/'):
        raise ValueError('Only published HTTPS Fonbet resource hosts are allowed')
    return base+'/events/listBase?scopeMarket=1600&lang=ru'

class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        a=urllib.parse.urlsplit(req.full_url);b=urllib.parse.urlsplit(newurl)
        if b.scheme!='https' or b.hostname != a.hostname:
            raise ValueError('Unexpected redirect outside original resource host')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def decode_json_body(content, content_encoding=''):
    """Decode bounded JSON, accepting broken servers that GZIP despite identity request.

    Read limit is applied both to compressed input by fetch_json and to the
    uncompressed output here. gzip.GzipFile also verifies gzip CRC/trailer.
    """
    enc=(content_encoding or '').strip().lower()
    if enc not in ('', 'identity', 'gzip', 'x-gzip'):
        raise ValueError('Unsupported HTTP Content-Encoding: '+enc[:32])
    has_gzip_magic=content.startswith(b'\x1f\x8b')
    if enc in ('gzip','x-gzip') or has_gzip_magic:
        if not has_gzip_magic:
            raise ValueError('Content-Encoding says gzip but body has no gzip magic')
        pieces=[]; expanded=0
        with gzip.GzipFile(fileobj=io.BytesIO(content),mode='rb') as reader:
            while True:
                chunk=reader.read(min(65536,MAX_BYTES+1-expanded))
                if not chunk:break
                expanded+=len(chunk)
                if expanded>MAX_BYTES:
                    raise ValueError('Uncompressed JSON exceeds safety limit')
                pieces.append(chunk)
        content=b''.join(pieces)
    if len(content)>MAX_BYTES:
        raise ValueError('JSON exceeds safety limit')
    return json.loads(content)


def fetch_json(url=None,fetcher=None):
    url=url or canonical_url()
    if url != canonical_url(url.split('/events/')[0]):
        raise ValueError('Unexpected URL')
    if fetcher: return fetcher(url)
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; ORACLE-Research/4; no betting)', 'Accept':'application/json','Accept-Encoding':'gzip, identity'})
    opener=urllib.request.build_opener(SameHostRedirect)
    with opener.open(req,timeout=20) as r:
        if r.status!=200:raise ValueError(f'Unexpected HTTP status: {r.status}')
        if 'json' not in r.headers.get('Content-Type','').lower():raise ValueError('Unexpected content type')
        encoding=r.headers.get('Content-Encoding','')
        content=r.read(MAX_BYTES+1)
    if len(content)>MAX_BYTES:raise ValueError('Source payload exceeds 70 MB safety cap')
    return decode_json_body(content,encoding)


def snapshot_data(doc):
    d=doc.get('data',doc) if isinstance(doc,dict) else None
    if not isinstance(d,dict) or not isinstance(d.get('events'),list) or not isinstance(d.get('customFactors'),list):
        raise ValueError('No full Fonbet snapshot: events[] and customFactors[] required')
    if d.get('packetVersion') is None and not isinstance(d.get('sports'),list):
        raise ValueError('Expected sports[] or packetVersion')
    return d


def summarize_payload(doc):
    d=snapshot_data(doc)
    factors=[f for row in d['customFactors'] if isinstance(row,dict) for f in (row.get('factors') or []) if isinstance(f,dict)]
    sport_names=sorted({str(x.get('alias')) for x in d.get('sports',[]) if isinstance(x,dict) and x.get('kind')=='sport' and x.get('alias')})
    return {'state':'SCHEMA_OK','events':len(d['events']),'factor_groups':len(d['customFactors']),
            'factor_entries':len(factors),'sport_labels':sport_names[:50],
            'packet_version':d.get('packetVersion'),
            'warning':'Raw factor IDs are not yet fully decoded into readable market names'}


def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS fb_full_events(
       event_id TEXT PRIMARY KEY, root_event_id TEXT, sport TEXT, league TEXT, home TEXT,away TEXT,
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
    ''')
    c.commit()


def iso_start(value):
    if value is None:return ''
    try:return stamp(dt.datetime.fromtimestamp(float(value),UTC))
    except (ValueError,TypeError,OverflowError):return ''


def unpack_sports(d):
    seg={str(x.get('id')):x for x in (d.get('sports') or []) if isinstance(x,dict) and x.get('id') is not None}
    def labels(sid):
        node=seg.get(str(sid));league=node.get('name','') if node else ''
        seen=set()
        while isinstance(node,dict) and str(node.get('id')) not in seen:
            seen.add(str(node.get('id')))
            if node.get('kind')=='sport':return str(node.get('alias') or node.get('name') or ''),str(league)
            node=seg.get(str(node.get('parentId')))
        return '',str(league)
    return labels


def ingest(c,document,observed_at=None):
    d=snapshot_data(document);observed_at=observed_at or stamp()
    counts=summarize_payload(document)
    if not counts['events'] or not counts['factor_entries']:
        raise ValueError('Empty events or factor entries: not replacing known quotes')
    labels=unpack_sports(d)
    ids={str(x.get('id')):x for x in d['events'] if isinstance(x,dict) and x.get('id') is not None}
    def root_for(event_id):
        cur=str(event_id);seen=set()
        while cur in ids and cur not in seen:
            seen.add(cur);node=ids[cur];parent=node.get('parentId')
            if not parent or str(parent)==cur:return cur
            cur=str(parent)
        return str(event_id)
    # Full-snapshot API: `events/listBase` returns the observed active feed. Cannot assume delta.
    factor_ids=set();changes=0;valid=0
    with c:
        for eid,ev in ids.items():
            sport,league=labels(ev.get('sportId'))
            root_id=root_for(eid)
            root=ids.get(root_id,ev)
            home=str(ev.get('team1') or root.get('team1') or '')
            away=str(ev.get('team2') or root.get('team2') or '')
            meta=json.dumps(ev,ensure_ascii=False,separators=(',',':'))
            c.execute('''INSERT INTO fb_full_events(event_id,root_event_id,sport,league,home,away,start_utc,place,level,source_json,first_seen,last_seen)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET
              root_event_id=excluded.root_event_id,sport=excluded.sport,league=excluded.league,
              home=excluded.home,away=excluded.away,start_utc=excluded.start_utc,place=excluded.place,
              level=excluded.level,source_json=excluded.source_json,last_seen=excluded.last_seen''',
              (eid,root_id,sport,league,home,away,iso_start(ev.get('startTime') or root.get('startTime')),
               str(ev.get('place') or root.get('place') or ''),int(ev.get('level') or 1),meta,observed_at,observed_at))
        for row in d['customFactors']:
            if not isinstance(row,dict):continue
            eid=str(row.get('e',''))
            if eid not in ids:continue
            for f in (row.get('factors') or []):
                if not isinstance(f,dict) or f.get('f') is None:continue
                try:
                    price=float(f.get('v'))
                    if not math.isfinite(price) or not (1.01<=price<=1001):continue
                except (TypeError,ValueError):continue
                fid=str(f['f']);pt=str(f.get('pt') if f.get('pt') is not None else '')
                # Do not assume factors are human-readable; preserve all raw fields.
                qk=hashlib.sha256(json.dumps([eid,fid,pt],ensure_ascii=False).encode()).hexdigest()
                factor_ids.add(qk);valid+=1
                raw=json.dumps(f,ensure_ascii=False,separators=(',',':'),sort_keys=True)
                prev=c.execute('SELECT decimal_price,source_json,active FROM fb_full_current WHERE quote_key=?',(qk,)).fetchone()
                if prev is None:
                    c.execute('''INSERT INTO fb_full_current VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                              (qk,eid,root_for(eid),fid,pt,price,raw,observed_at,observed_at,1,observed_at))
                else:
                    c.execute('''UPDATE fb_full_current SET decimal_price=?,source_json=?,active=1,last_snapshot=? WHERE quote_key=?''',
                              (price,raw,observed_at,qk))
                if prev is None or prev[0]!=price or prev[2]!=1:
                    h=hashlib.sha256(raw.encode()).hexdigest()
                    c.execute('''INSERT INTO fb_full_history(quote_key,event_id,decimal_price,active,changed_at,raw_sha)
                                 VALUES(?,?,?,?,?,?)''',(qk,eid,price,1,observed_at,h))
                    changes+=1
        # Only deactivate missing quotes when the snapshot is explicitly complete.
        # Partial or delta payload must not invalidate previously observed lines.
        if d.get('isDelta') is False or d.get('delta') is False or (d.get('packetVersion') is None and len(ids)>0):
            # Conservative: if source lacks explicit completeness signal, record new values, keep unknown former active values
            pass
        c.execute('INSERT INTO fb_full_runs(checked_at,source,state,events_count,factors_count,changed_count,detail) VALUES(?,?,?,?,?,?,?)',
                  (observed_at,'fonbet_first_party','OK',len(ids),valid,changes,f'All numeric factors stored; {len(factor_ids)} unique observations'))
    return {'state':'OK','events':len(ids),'factor_observations':valid,'new_or_changed_quotes':changes,
            'market_ids':len({r[0] for r in c.execute('SELECT DISTINCT factor_id FROM fb_full_current')}),
            'note':'Raw factor codes preserved; not all are semantically mapped; feed completeness unverified'}


def status(c):
    setup(c)
    ev=c.execute('SELECT COUNT(*) FROM fb_full_events').fetchone()[0]
    all_seen,fac=c.execute('SELECT COUNT(*),COUNT(DISTINCT factor_id) FROM fb_full_current').fetchone()
    history=c.execute('SELECT COUNT(*) FROM fb_full_history').fetchone()[0]
    last=c.execute('SELECT checked_at,state,events_count,factors_count,changed_count,detail FROM fb_full_runs ORDER BY id DESC LIMIT 1').fetchone()
    good=c.execute("SELECT checked_at FROM fb_full_runs WHERE state='OK' ORDER BY id DESC LIMIT 1").fetchone()
    recent_at=good[0] if good else None
    # Treat only the most recently observed snapshot as present in the line.
    cur=c.execute('SELECT COUNT(*) FROM fb_full_current WHERE last_snapshot=?',(recent_at,)).fetchone()[0] if recent_at else 0
    bysport=c.execute('''SELECT COALESCE(e.sport,''),COUNT(DISTINCT e.root_event_id),COUNT(*)
         FROM fb_full_current q JOIN fb_full_events e ON e.event_id=q.event_id WHERE q.last_snapshot=? GROUP BY e.sport ORDER BY COUNT(*) DESC LIMIT 15''',(recent_at,)).fetchall()
    age=(dt.datetime.now(UTC)-dt.datetime.fromisoformat(recent_at)).total_seconds() if recent_at else None
    fresh=age is not None and 0<=age<=3600
    return {'state':('OBSERVED' if fresh else 'STALE') if cur else 'NOT_CONNECTED','events':ev,'factor_quotes_current':cur,'factor_quotes_seen_all_time':all_seen,
            'factor_codes':fac,'historical_price_changes':history,
            'last_run':dict(zip(('at','state','events','factors','changed','detail'),last)) if last else None,
            'sports':[{'sport':sp,'events':n,'factor_quotes':q} for sp,n,q in bysport],
            'warning':'Full raw factors are not equivalent to decoded/verified available bets'}


def cooldown(c,at):
    row=c.execute('SELECT until_utc FROM fb_full_cooldown WHERE provider=?',('fonbet',)).fetchone()
    if row and row[0] and dt.datetime.fromisoformat(row[0])>at:
        return int((dt.datetime.fromisoformat(row[0])-at).total_seconds())
    return 0


def once(c,loader=None,now=None):
    setup(c);now=now or dt.datetime.now(UTC)
    remaining=cooldown(c,now)
    if remaining:return {'state':'COOLDOWN','seconds_remaining':remaining}
    url=canonical_url()
    try:
        data=fetch_json(url,loader)
        return ingest(c,data,stamp(now))
    except urllib.error.HTTPError as exc:
        err=f'HTTP {exc.code}'
        if exc.code in (429,503):
            try:secs=int(exc.headers.get('Retry-After','1800'))
            except (ValueError,AttributeError,TypeError):secs=1800
            secs=max(60,min(secs,14400))
            with c:c.execute('INSERT INTO fb_full_cooldown VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET until_utc=excluded.until_utc,reason=excluded.reason',
                             ('fonbet',stamp(now+dt.timedelta(seconds=secs)),err))
        with c:c.execute('INSERT INTO fb_full_runs(checked_at,source,state,events_count,factors_count,changed_count,detail) VALUES(?,?,?,?,?,?,?)',
                         (stamp(now),'fonbet_first_party','HTTP_ERROR',0,0,0,err))
        return {'state':'HTTP_ERROR','detail':err,'existing_prices_preserved':True}
    except Exception as exc:
        info=type(exc).__name__+': '+str(exc)[:160]
        with c:c.execute('INSERT INTO fb_full_runs(checked_at,source,state,events_count,factors_count,changed_count,detail) VALUES(?,?,?,?,?,?,?)',
                         (stamp(now),'fonbet_first_party','UNAVAILABLE',0,0,0,info))
        return {'state':'UNAVAILABLE','detail':info,
                'existing_prices_preserved':True}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=('probe','once','status'))
    p.add_argument('--db',default=os.getenv('ORACLE_DB','/app/state/oracle.sqlite3'))
    args=p.parse_args()
    if args.command=='probe':
        try:
            out=summarize_payload(fetch_json())
        except Exception as exc:out={'state':'UNAVAILABLE','detail':type(exc).__name__+': '+str(exc)[:200],
                                    'url':canonical_url()}
    else:
        c=sqlite3.connect(args.db,timeout=30)
        out=once(c) if args.command=='once' else status(c)
        c.close()
    print(json.dumps(out,ensure_ascii=False,indent=2))
    if args.command=='probe' and out['state']!='SCHEMA_OK':raise SystemExit(2)

if __name__=='__main__':main()
