#!/usr/bin/env python3
"""Read-only US equity ingestion research branch.

SEC: CIK mapping and public 8-K/10-K/10-Q filing *metadata* from submissions.
Alpha Vantage: optional historical unadjusted daily OHLCV; never claims live quotes,
returns, or adjusted series. Sources require permissions appropriate to user.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request

SEC_BASE='https://data.sec.gov/submissions/CIK{cik:010d}.json'
MAP_URL='https://www.sec.gov/files/company_tickers.json'
AV_URL='https://www.alphavantage.co/query'

def stamp():return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')

def sha(s):return hashlib.sha256(s.encode('utf-8')).hexdigest()

def conn_db(path):
 c=sqlite3.connect(path,timeout=15);c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA busy_timeout=15000')
 c.executescript('''CREATE TABLE IF NOT EXISTS eq_issuers(ticker TEXT PRIMARY KEY,cik INTEGER NOT NULL,
  name TEXT NOT NULL, verified_at TEXT NOT NULL,source TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS eq_filings(cik INTEGER NOT NULL,accession TEXT NOT NULL,form TEXT NOT NULL,
  filing_date TEXT NOT NULL,report_date TEXT,document TEXT NOT NULL,url TEXT NOT NULL,
  observed_at TEXT NOT NULL,PRIMARY KEY(cik,accession));
 CREATE TABLE IF NOT EXISTS eq_prices(symbol TEXT NOT NULL,session_date TEXT NOT NULL,open REAL NOT NULL,
  high REAL NOT NULL,low REAL NOT NULL,close REAL NOT NULL,volume REAL NOT NULL,
  fetched_at TEXT NOT NULL,provider TEXT NOT NULL,adjustment TEXT NOT NULL,
  PRIMARY KEY(symbol,session_date,provider));
 CREATE TABLE IF NOT EXISTS eq_ingest_status(source TEXT NOT NULL,subject TEXT NOT NULL,
  checked_at TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL,
  PRIMARY KEY(source,subject));''')
 c.commit();return c

def get_json(url,user_agent):
 if not user_agent or 'example.com' in user_agent or '@' not in user_agent:
  raise ValueError('Provide identifying SEC_USER_AGENT with contact email')
 req=urllib.request.Request(url,headers={'User-Agent':user_agent,'Accept':'application/json'})
 with urllib.request.urlopen(req,timeout=25) as response:
  if response.status!=200:raise RuntimeError('Bad HTTP response')
  body=response.read(12_000_000)
 return json.loads(body)

def sec_tickers(c,agent,allowed:list[str]):
 """Do not use name-only matching; ticker->CIK official mapping."""
 allowed={a.upper() for a in allowed}
 items=get_json(MAP_URL,agent)
 results=[]
 for x in items.values():
  sym=str(x.get('ticker','')).upper()
  if sym not in allowed:continue
  cik=int(x['cik_str'])
  c.execute('INSERT INTO eq_issuers VALUES(?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET cik=excluded.cik,name=excluded.name,verified_at=excluded.verified_at,source=excluded.source',(sym,cik,x['title'],stamp(),MAP_URL))
  results.append((sym,cik))
 c.commit();return results

def sec_filings(c,agent,ticker,form_types=('8-K','10-K','10-Q')):
 found=c.execute('SELECT cik FROM eq_issuers WHERE ticker=?',(ticker.upper(),)).fetchone()
 if not found:raise ValueError('Ticker not mapped to official CIK')
 cik=found[0];obj=get_json(SEC_BASE.format(cik=cik),agent)
 recent=obj.get('filings',{}).get('recent',{})
 count=0
 for i,form in enumerate(recent.get('form',[])):
  if form not in form_types:continue
  accession=recent['accessionNumber'][i]
  filing=recent['filingDate'][i]
  report=(recent.get('reportDate') or ['']*len(recent['form']))[i]
  doc=recent['primaryDocument'][i]
  url=f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace("-","")}/{urllib.parse.quote(doc,safe="")}'
  c.execute('''INSERT OR IGNORE INTO eq_filings(cik,accession,form,filing_date,report_date,document,url,observed_at)
    VALUES(?,?,?,?,?,?,?,?)''',(cik,accession,form,filing,report,doc,url,stamp()))
  count+=1
 c.execute('INSERT INTO eq_ingest_status VALUES(?,?,?,?,?) ON CONFLICT(source,subject) DO UPDATE SET checked_at=excluded.checked_at,status=excluded.status,detail=excluded.detail',('SEC_EDGAR',ticker.upper(),stamp(),'OK',str(count)+' filing metadata records seen'))
 c.commit();return count

def import_av_payload(c,ticker,payload,at=None):
 at=at or stamp()
 series=payload.get('Time Series (Daily)')
 if not isinstance(series,dict):raise ValueError('No time series; plan, quota or provider error')
 n=0
 for day,row in series.items():
  o,h,l,z,v=(float(row[x]) for x in ('1. open','2. high','3. low','4. close','5. volume'))
  if min(o,h,l,z)<=0 or v<0 or not (l<=o<=h and l<=z<=h):continue
  c.execute('''INSERT OR IGNORE INTO eq_prices VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (ticker.upper(),day,o,h,l,z,v,at,'ALPHAVANTAGE','UNADJUSTED'))
  n+=1
 c.commit();return n

def av_prices(c,agent,ticker,key):
 if not key:raise ValueError('ORACLE_STOCKS_AV_KEY absent; no quotes available')
 params=urllib.parse.urlencode({'function':'TIME_SERIES_DAILY','symbol':ticker.upper(),
                                  'outputsize':'compact','apikey':key})
 payload=get_json(AV_URL+'?'+params,agent)
 return import_av_payload(c,ticker,payload)

def main():
 p=argparse.ArgumentParser();p.add_argument('command',choices=['init','sec','prices','status']);p.add_argument('--db',default='/app/state/oracle.sqlite3');p.add_argument('--tickers',default='AAPL,MSFT,NVDA,TSLA');a=p.parse_args()
 c=conn_db(a.db);names=[s.strip().upper() for s in a.tickers.split(',') if s.strip()]
 if len(names)>25:raise SystemExit('Max 25 tickers per batch to avoid provider overload')
 if a.command=='sec':
  agent=os.getenv('SEC_USER_AGENT','');matched=sec_tickers(c,agent,names)
  for i,(symbol,cik) in enumerate(matched):
   if i:time.sleep(.3)
   print('SEC',symbol,sec_filings(c,agent,symbol))
 if a.command=='prices':
  key=os.getenv('ORACLE_STOCKS_AV_KEY','')
  for i,s in enumerate(names):
   if i:time.sleep(12)  # conservative rate limit; does not bypass daily quota
   try:print('AV',s,av_prices(c,os.getenv('SEC_USER_AGENT','ORACLE-Research your-contact@your-domain.com'),s,key))
   except Exception as exc:print('ERROR',s,type(exc).__name__,str(exc)[:100])
 print(json.dumps({'issuers':c.execute('SELECT COUNT(*) FROM eq_issuers').fetchone()[0],
  'filings':c.execute('SELECT COUNT(*) FROM eq_filings').fetchone()[0],
  'unadjusted_daily_bars':c.execute('SELECT COUNT(*) FROM eq_prices').fetchone()[0],
  'status':'RESEARCH_NOT_VALIDATED'}))
if __name__=='__main__':main()
