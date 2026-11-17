#!/usr/bin/env python3
"""ORACLE v0.5: read-only public data ingestion. No predictions, wagers or trading.

No third-party dependencies. Optional .env with free-source keys.
Raw source documents are stored with retrieval time and SHA-256 for reproducibility.
"""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('ORACLE_DB',str(ROOT/'oracle.sqlite3')))
UTC = dt.timezone.utc

def stamp():
    return dt.datetime.now(UTC).isoformat(timespec='seconds')

def local_keys():
    result = {}
    fn = ROOT / '.env'
    if fn.exists():
        for line in fn.read_text('utf-8').splitlines():
            line=line.strip()
            if not line or line.startswith('#') or '=' not in line: continue
            k,v=line.split('=',1)
            if k.strip() in ('ORACLE_ODDS_KEY','ORACLE_NBA_KEY','ORACLE_COINGECKO_KEY','ORACLE_FRED_KEY'):
                result[k.strip()] = v.strip().strip('"').strip("'")
    for k in ('ORACLE_ODDS_KEY','ORACLE_NBA_KEY','ORACLE_COINGECKO_KEY','ORACLE_FRED_KEY'):
        if os.environ.get(k): result[k] = os.environ[k]
    return result

def db_open(path=DB):
    conn=sqlite3.connect(str(path))
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS raw_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
      subject TEXT NOT NULL, observed_at TEXT NOT NULL,
      source_time TEXT, sha256 TEXT NOT NULL, payload TEXT NOT NULL,
      UNIQUE(provider,subject,observed_at,sha256))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS candles (
      provider TEXT NOT NULL, symbol TEXT NOT NULL,
      timeframe TEXT NOT NULL, open_ms INTEGER NOT NULL,
      open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
      close REAL NOT NULL, volume REAL NOT NULL, fetched_at TEXT NOT NULL,
      PRIMARY KEY(provider,symbol,timeframe,open_ms))''')
    conn.commit()
    return conn

def request_json(url, headers=None):
    req=urllib.request.Request(url, headers={'User-Agent':'ORACLE-Research-v0.5/1.0',**(headers or {})})
    try:
        with urllib.request.urlopen(req,timeout=25) as response:
            raw=response.read(8_000_000)
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'API returned HTTP {e.code}; check limits/key/region') from e
    except (urllib.error.URLError, ValueError) as e:
        raise RuntimeError(f'API unavailable or malformed response: {e}') from e

def snapshot(conn,provider,subject,data,source_time=None):
    txt=json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    sha=hashlib.sha256(txt.encode()).hexdigest()
    with conn:
        cur=conn.execute('''INSERT OR IGNORE INTO raw_snapshots
        (provider,subject,observed_at,source_time,sha256,payload) VALUES (?,?,?,?,?,?)''',
        (provider,subject,stamp(),source_time,sha,txt))
    return cur.rowcount

def nba(conn,keys,day=None):
    if not keys.get('ORACLE_NBA_KEY'):
        print('SKIP NBA: free key missing (ORACLE_NBA_KEY)'); return 0
    day=day or dt.datetime.now(UTC).date()
    total=0
    for offset in (0,1):
        d=(day+dt.timedelta(days=offset)).isoformat()
        params=urllib.parse.urlencode({'dates[]':d,'per_page':100})
        data=request_json('https://api.balldontlie.io/v1/games?'+params,
                          {'Authorization':keys['ORACLE_NBA_KEY']})
        games=data.get('data',[])
        if not isinstance(games,list): raise RuntimeError('NBA response lacks data list')
        for game in games:
            if not isinstance(game,dict) or 'id' not in game: continue
            total+=snapshot(conn,'nba_games',str(game['id']),game,game.get('datetime'))
        print(f'NBA {d}: {len(games)} matches, new versions: {total}')
    return total

def odds(conn,keys):
    key=keys.get('ORACLE_ODDS_KEY')
    if not key:
        print('SKIP ODDS: free key missing (ORACLE_ODDS_KEY)');return 0
    params=urllib.parse.urlencode({'apiKey':key,'regions':'us','markets':'h2h','oddsFormat':'decimal'})
    # Official provider: the-odds-api.com (with hyphens), NOT theoddsapi.com
    data=request_json('https://api.the-odds-api.com/v4/sports/basketball_nba/odds/?'+params)
    if not isinstance(data,list):raise RuntimeError('Odds API response is not an event list')
    total=0
    for event in data:
        if isinstance(event,dict) and event.get('id'):
            total+=snapshot(conn,'nba_odds',str(event['id']),event,event.get('commence_time'))
    print(f'ODDS: {len(data)} games, new versions: {total}')
    return total

def cg(conn,keys):
    key=keys.get('ORACLE_COINGECKO_KEY')
    if not key:
        print('SKIP COINGECKO: free Demo key missing');return 0
    params=urllib.parse.urlencode({'vs_currency':'usd','order':'market_cap_desc', 'per_page':100,'page':1,'sparkline':'false'})
    data=request_json('https://api.coingecko.com/api/v3/coins/markets?'+params,
                      {'x-cg-demo-api-key':key})
    if not isinstance(data,list):raise RuntimeError('CoinGecko response is not a list')
    total=0
    for coin in data:
        if isinstance(coin,dict) and coin.get('id'):
            total+=snapshot(conn,'coingecko_market',str(coin['id']),coin,coin.get('last_updated'))
    print(f'COINGECKO: {len(data)} coins, new versions: {total}')
    return total

def normalize_ms(value):
    n=int(value)
    if n>10**15: n=n//1000  # Binance archived data in microseconds since 2025
    if not (10**11<n<10**14):raise ValueError('Invalid ms timestamp')
    return n

def insert_candle(conn, symbol, timeframe, row, provider):
    ms=normalize_ms(row[0]);o,h,l,c,v=map(float,row[1:6])
    if not (0<l<=h and o>0 and c>0 and v>=0):raise ValueError('Invalid OHLCV')
    with conn:
        cur=conn.execute('''INSERT OR IGNORE INTO candles
        (provider,symbol,timeframe,open_ms,open,high,low,close,volume,fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)''',(provider,symbol,timeframe,ms,o,h,l,c,v,stamp()))
    return cur.rowcount

def crypto(conn,symbols=('BTCUSDT','ETHUSDT','SOLUSDT')):
    total=0
    for symbol in symbols:
        if not symbol.isalnum() or len(symbol)>24: raise ValueError('Invalid symbol')
        params=urllib.parse.urlencode({'symbol':symbol,'interval':'1h','limit':300})
        data=request_json('https://data-api.binance.vision/api/v3/klines?'+params)
        if not isinstance(data,list):raise RuntimeError('Invalid Binance candle response')
        cutoff=int(dt.datetime.now(UTC).timestamp()*1000)
        count=0
        for row in data:
            if not isinstance(row,list) or len(row)<7:continue
            if normalize_ms(row[6])>cutoff:continue  # never store incomplete bar
            count+=insert_candle(conn,symbol,'1h',row,'binance_public')
        total+=count
        print(f'BINANCE {symbol}: {len(data)} retrieved, {count} new CLOSED candles')
    return total

def history(conn,symbol,year):
    if not (2017<=year<=dt.datetime.now(UTC).year):raise ValueError('Year out of range')
    if not (symbol.isalnum() and 4<=len(symbol)<=24):raise ValueError('Invalid symbol')
    total=0
    for month in range(1,13):
        if (year,month)>=(dt.datetime.now(UTC).year,dt.datetime.now(UTC).month):break
        basename=f'{symbol}-1d-{year}-{month:02d}.zip'
        url=f'https://data.binance.vision/data/spot/monthly/klines/{symbol}/1d/{basename}'
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':'ORACLE-Research-v0.5/1.0'}),timeout=40) as r:
                packed=r.read(6_000_000)
            with zipfile.ZipFile(io.BytesIO(packed)) as z:
                files=[f for f in z.namelist() if f.endswith('.csv') and '/' not in f]
                if len(files)!=1: raise ValueError('Unexpected ZIP content')
                with z.open(files[0]) as raw:
                    rdr=csv.reader(io.TextIOWrapper(raw,encoding='utf-8-sig'))
                    count=0
                    for row in rdr:
                        try:count+=insert_candle(conn,symbol,'1d',row,'binance_archive')
                        except (ValueError,IndexError):
                            if row and row[0].lower() in ('open time','open_time'):continue
                            raise
            total+=count
            print(f'{basename}: {count} new daily candles')
        except urllib.error.HTTPError as e:
            if e.code==404:
                print(f'SKIP {basename}: not published');continue
            raise RuntimeError(f'Historical archive HTTP {e.code}') from e
    return total

def status(conn):
    print('ORACLE — SOURCES (no signal generation)')
    for row in conn.execute('''SELECT provider,COUNT(*),MAX(observed_at) FROM raw_snapshots GROUP BY provider ORDER BY provider'''):
        print(f'{row[0]}: {row[1]} raw versions, last observed: {row[2]}')
    for row in conn.execute('''SELECT provider,symbol,timeframe,COUNT(*),MIN(open_ms),MAX(open_ms) FROM candles GROUP BY provider,symbol,timeframe ORDER BY provider,symbol'''):
        first=dt.datetime.fromtimestamp(row[4]/1000,UTC).date()
        last=dt.datetime.fromtimestamp(row[5]/1000,UTC).date()
        print(f'{row[0]} {row[1]} {row[2]}: {row[3]} candles ({first} .. {last})')
    print('Forecast models: NOT TRAINED. Approved signals: NONE.')

def main():
    parser=argparse.ArgumentParser(description='ORACLE data acquisition (research only)')
    parser.add_argument('command',choices=['init','nba','odds','crypto','coingecko','all','history','status'])
    parser.add_argument('--symbol',default='BTCUSDT')
    parser.add_argument('--year',type=int,default=2024)
    parser.add_argument('--db',default=str(DB))
    args=parser.parse_args()
    conn=db_open(args.db);keys=local_keys()
    try:
        if args.command=='init':print('Created database:',args.db)
        elif args.command=='nba':nba(conn,keys)
        elif args.command=='odds':odds(conn,keys)
        elif args.command=='crypto':crypto(conn)
        elif args.command=='coingecko':cg(conn,keys)
        elif args.command=='history':history(conn,args.symbol.upper(),args.year)
        elif args.command=='all':
            for source in (nba,odds,cg):
                try:source(conn,keys)
                except RuntimeError as e:print('WARNING',str(e),file=sys.stderr)
            try:crypto(conn)
            except RuntimeError as e:print('WARNING',str(e),file=sys.stderr)
        status(conn)
    finally:conn.close()

if __name__=='__main__':main()
