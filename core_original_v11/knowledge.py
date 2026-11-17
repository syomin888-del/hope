#!/usr/bin/env python3
"""ORACLE knowledge memory: source-linked facts and point-in-time queries.

An observation is NOT an independently confirmed fact. `observed_at` is the
only time at which ORACLE can claim to have known an ingested value. Publisher
'source_time' describes source content and never backdates the observation.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import sqlite3

import collect

SCHEMA = '''
 CREATE TABLE IF NOT EXISTS knowledge_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  raw_snapshot_id INTEGER NOT NULL, provider TEXT NOT NULL,
  subject TEXT NOT NULL, field TEXT NOT NULL, value_json TEXT NOT NULL,
  observed_at TEXT NOT NULL, source_time TEXT, content_sha256 TEXT NOT NULL,
  FOREIGN KEY(raw_snapshot_id) REFERENCES raw_snapshots(id),
  UNIQUE(raw_snapshot_id,field));
 CREATE INDEX IF NOT EXISTS knowledge_lookup ON knowledge_facts(provider,subject,field,observed_at,id);
 CREATE TABLE IF NOT EXISTS knowledge_cursor (name TEXT PRIMARY KEY,last_id INTEGER NOT NULL DEFAULT 0);
 CREATE TABLE IF NOT EXISTS knowledge_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL,
  snapshots INTEGER NOT NULL, facts INTEGER NOT NULL,
  rejected INTEGER NOT NULL, note TEXT NOT NULL);
'''

# A curated whitelist: do not recursively promote arbitrary untrusted JSON into "knowledge".
FIELDS = {
 'coingecko_market': ('current_price','market_cap','total_volume','price_change_percentage_24h','last_updated'),
 'nba_games': ('status','home_team_score','visitor_team_score','datetime','postseason'),
 'bitcoin_mempool': ('count','vsize','total_fee'),
 'crypto_fear_greed': ('value','value_classification','timestamp'),
 'binance_funding': ('fundingRate','fundingTime','symbol'),
 'binance_open_interest': ('openInterest','symbol','time'),
}

def setup(c):
    c.executescript(SCHEMA)
    c.commit()

def trusted_field(provider,field,value):
    if provider.startswith('official_news_'):
        if field not in ('title','link','publication_text'):return False
        if field=='link':return isinstance(value,str) and value.startswith('https://') and len(value)<2048
        return isinstance(value,str) and len(value)<2048
    if field not in FIELDS.get(provider,()):return False
    if isinstance(value,(dict,list)) or value is None:return False
    if isinstance(value,(int,float)) and (not math.isfinite(value) or isinstance(value,bool)):return False
    return isinstance(value,(str,int,float,bool)) and len(str(value))<=1024

def _valid_utc(s):
    try:
        t=dt.datetime.fromisoformat(s.replace('Z','+00:00'))
        return t.tzinfo is not None and t.utcoffset() is not None
    except (ValueError,AttributeError):return False

def ingest(c, limit=4000):
    """Idempotent bounded ingestion; no external network or AI required."""
    setup(c)
    cursor=c.execute("SELECT last_id FROM knowledge_cursor WHERE name='raw_snapshots'").fetchone()
    after=cursor[0] if cursor else 0
    rows=c.execute('''SELECT id,provider,subject,observed_at,source_time,sha256,payload
      FROM raw_snapshots WHERE id>? ORDER BY id LIMIT ?''',(after,limit)).fetchall()
    accepted=rejected=0
    for row in rows:
        sid,provider,subject,seen,published,sha,payload=row
        if not _valid_utc(seen):rejected+=1;continue
        try:
            obj=json.loads(payload)
            if not isinstance(obj,dict):rejected+=1;continue
            if hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()!=sha:
                rejected+=1;continue
            entries=[(k,v) for k,v in obj.items() if trusted_field(provider,k,v)]
            if not entries:rejected+=1;continue
            with c:
                for key,val in entries:
                    cur=c.execute('''INSERT OR IGNORE INTO knowledge_facts
                      (raw_snapshot_id,provider,subject,field,value_json,observed_at,source_time,content_sha256)
                      VALUES(?,?,?,?,?,?,?,?)''',(sid,provider,subject,key,json.dumps(val,ensure_ascii=False),seen,published,sha))
                    accepted+=cur.rowcount
        except (ValueError,TypeError,sqlite3.Error):rejected+=1
    with c:
        if rows:
            c.execute("INSERT INTO knowledge_cursor(name,last_id) VALUES('raw_snapshots',?) ON CONFLICT(name) DO UPDATE SET last_id=MAX(last_id,excluded.last_id)",(rows[-1][0],))
        c.execute('INSERT INTO knowledge_runs(run_at,snapshots,facts,rejected,note) VALUES(?,?,?,?,?)',
            (collect.stamp(),len(rows),accepted,rejected,'Unverified observations; observed_at is the earliest usable knowledge time.'))
    return {'snapshots_scanned':len(rows),'facts_added':accepted,'rejected':rejected}

def as_known(c, cutoff, provider=None, subject=None, limit=100):
    """Last observed version of each field *as of* cutoff. Never look into future rows."""
    if not _valid_utc(cutoff):raise ValueError('cutoff must include UTC offset')
    cutoff=dt.datetime.fromisoformat(cutoff.replace('Z','+00:00')).astimezone(dt.timezone.utc).isoformat(timespec='seconds')
    setup(c)
    where='observed_at<=?';args=[cutoff]
    if provider is not None:where+=' AND provider=?';args.append(provider)
    if subject is not None:where+=' AND subject=?';args.append(subject)
    rows=c.execute(f'''SELECT provider,subject,field,value_json,observed_at,source_time,content_sha256
       FROM knowledge_facts WHERE {where} ORDER BY observed_at DESC,id DESC''',args).fetchall()
    result=[];seen=set()
    for p,s,f,v,t,source_time,sha in rows:
        key=(p,s,f)
        if key in seen:continue
        seen.add(key)
        result.append({'provider':p,'subject':s,'field':f,'value':json.loads(v),'known_since':t,
          'source_time':source_time,'snapshot_sha':sha})
        if len(result)>=limit:break
    return result

def status(c):
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'knowledge_facts' not in tables:
        return {'facts':0,'providers':0,'last_ingestion':None,'initialized':False}
    return {'facts':c.execute('SELECT COUNT(*) FROM knowledge_facts').fetchone()[0],
      'providers':c.execute('SELECT COUNT(DISTINCT provider) FROM knowledge_facts').fetchone()[0],
      'last_ingestion':next((r[0] for r in c.execute('SELECT run_at FROM knowledge_runs ORDER BY id DESC LIMIT 1')),None)}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('command',choices=('ingest','status','time-machine'))
    ap.add_argument('--at');ap.add_argument('--provider');ap.add_argument('--subject')
    a=ap.parse_args();c=collect.db_open()
    try:
        if a.command=='ingest':out=ingest(c)
        elif a.command=='status':out=status(c)
        else:
            if not a.at:ap.error('--at requires ISO timestamp with UTC offset')
            out=as_known(c,a.at,a.provider,a.subject)
        print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
    finally:c.close()
if __name__=='__main__':main()
