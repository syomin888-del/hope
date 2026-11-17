#!/usr/bin/env python3
"""Rate-conscious monthly OHLCV historical backfill. No trading credentials.

All data are primary-source Binance public monthly archives. Never scans future
months, never repeats a successful archive; store official missing month as a
retryable soft gap rather than permanently silently dropping it.
"""
import argparse
import csv
import datetime as dt
import io
import json
import sqlite3
import urllib.error
import urllib.request
import zipfile

import collect

SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT')
FIRST_MONTH={'BTCUSDT':(2017,8),'ETHUSDT':(2017,8),'SOLUSDT':(2020,8)}
NOW=dt.timezone.utc

def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS history_downloads(
      symbol TEXT NOT NULL, year INTEGER NOT NULL, month INTEGER NOT NULL,
      status TEXT NOT NULL, rows INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
      error TEXT NOT NULL DEFAULT '', PRIMARY KEY(symbol,year,month))''')
    c.commit()

def months_for(symbol):
    y,m=FIRST_MONTH[symbol]
    today=dt.datetime.now(NOW)
    while (y,m)<(today.year,today.month):
        yield y,m
        m+=1
        if m>12:y+=1;m=1

def one(c,symbol,year,month):
    basename=f'{symbol}-1h-{year}-{month:02d}.zip'
    url=f'https://data.binance.vision/data/spot/monthly/klines/{symbol}/1h/{basename}'
    req=urllib.request.Request(url,headers={'User-Agent':'ORACLE-Research/0.7'})
    try:
        with urllib.request.urlopen(req,timeout=45) as response:
            content=response.read(12_000_001)
        if len(content)>12_000_000:raise ValueError('Compressed archive is unexpectedly large')
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            names=[a for a in z.namelist() if a.endswith('.csv') and '/' not in a]
            if len(names)!=1 or z.getinfo(names[0]).file_size>80_000_000:
                raise ValueError('Unexpected archive structure or size')
            with z.open(names[0]) as raw:
                reader=csv.reader(io.TextIOWrapper(raw,encoding='utf-8-sig'))
                rows=[]
                for r in reader:
                    if not r or r[0].lower() in ('open_time','open time'):continue
                    if len(r)<6:raise ValueError('Corrupt OHLCV archive')
                    ms=collect.normalize_ms(r[0])
                    if dt.datetime.fromtimestamp(ms/1000,NOW).strftime('%Y-%m')!=f'{year}-{month:02d}':
                        raise ValueError('Out-of-month candle in archive')
                    vals=list(map(float,r[1:6]))
                    if not(0<vals[2]<=vals[1] and vals[0]>0 and vals[3]>0 and vals[4]>=0):
                        raise ValueError('OHLCV check failed')
                    rows.append((symbol,'1h',ms,*vals,collect.stamp()))
        # Insert batch atomically: never half-import a corrupt file.
        with c:
            c.executemany('''INSERT OR IGNORE INTO candles
            (provider,symbol,timeframe,open_ms,open,high,low,close,volume,fetched_at)
            VALUES('binance_archive',?,?,?,?,?,?,?,?,?)''',rows)
        return len(rows),'ok'
    except urllib.error.HTTPError as e:
        if e.code==404:return 0,'missing'
        raise

def run(c,limit=4):
    if not 1<=limit<=24:raise ValueError('Limit must be 1..24')
    setup(c)
    result=[]
    # Interleave symbols so BTC does not monopolize collection.
    future={s:list(months_for(s)) for s in SYMBOLS}
    for y,m in sorted(set(sum(future.values(),[])),reverse=True):
        for symbol in SYMBOLS:
            if (y,m) not in future[symbol]:continue
            old=c.execute('SELECT status,updated_at FROM history_downloads WHERE symbol=? AND year=? AND month=?',(symbol,y,m)).fetchone()
            if old and old[0]=='ok':continue
            if old and old[0] in ('missing','error'):
                previous=dt.datetime.fromisoformat(old[1])
                backoff_hours=14*24 if old[0]=='missing' else 24
                if (dt.datetime.now(NOW)-previous).total_seconds()<backoff_hours*3600:continue
            try:
                rows,status=one(c,symbol,y,m)
                err=''
            except Exception as exc:
                rows,status,err=0,'error',type(exc).__name__+': '+str(exc)[:120]
            with c:
                c.execute('''INSERT INTO history_downloads(symbol,year,month,status,rows,updated_at,error)
                 VALUES(?,?,?,?,?,?,?) ON CONFLICT(symbol,year,month) DO UPDATE SET
                 status=excluded.status,rows=excluded.rows,updated_at=excluded.updated_at,error=excluded.error''',
                  (symbol,y,m,status,rows,collect.stamp(),err))
            result.append({'symbol':symbol,'month':f'{y}-{m:02d}','status':status,'rows':rows,'error':err})
            if len(result)>=limit:return result
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--limit',type=int,default=4)
    a=p.parse_args()
    conn=collect.db_open()
    try:print(json.dumps(run(conn,a.limit),ensure_ascii=False,indent=2))
    finally:conn.close()
