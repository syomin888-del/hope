#!/usr/bin/env python3
"""Independent, read-only ORACLE sources. No betting/trading endpoints.

Each adapter saves data and provenance; none generates an investment recommendation.
Never trust source publication timestamps as proof that content was publicly accessible
at that time: true historical validation needs point-in-time archives.
"""
import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import collect

UA = 'ORACLE-Research/0.6 (+read-only data archiving)'

def retrieve_xml(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':UA, 'Accept':'application/atom+xml,application/rss+xml,application/xml'}), timeout=22) as r:
        payload=r.read(1_500_000)
        if len(payload)>=1_500_000: raise RuntimeError('RSS response too large')
    return ET.fromstring(payload)

def smart_snapshot(conn, provider, subject, payload, source_time=None):
    """Keep only changed document versions (but never overwrite the old one)."""
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    sha=hashlib.sha256(raw.encode('utf-8')).hexdigest()
    row=conn.execute('SELECT sha256 FROM raw_snapshots WHERE provider=? AND subject=? ORDER BY id DESC LIMIT 1',(provider,subject)).fetchone()
    if row is not None and row[0]==sha: return 0
    return collect.snapshot(conn,provider,subject,payload,source_time)

def coinbase(conn):
    """Official public product ticker is a trade snapshot, not a closed candle."""
    total=0
    for market in ('BTC-USD','ETH-USD','SOL-USD'):
        url=f'https://api.coinbase.com/api/v3/brokerage/market/products/{market}/ticker?limit=20'
        data=collect.request_json(url)
        if not isinstance(data,dict):raise RuntimeError('Coinbase malformed ticker')
        # Request time is the observation time; never pretend this is exchange close.
        total+=collect.snapshot(conn,'coinbase_ticker',market,data)
    return total

def kraken(conn):
    """Kraken OHLC public candles. Last bar may be incomplete and is skipped."""
    total=0
    for market,symbol in (('XBTUSD','BTCUSD'),('ETHUSD','ETHUSD'),('SOLUSD','SOLUSD')):
        url='https://api.kraken.com/0/public/OHLC?'+urllib.parse.urlencode({'pair':market,'interval':60})
        data=collect.request_json(url)
        if not isinstance(data,dict) or data.get('error'):raise RuntimeError('Kraken error: '+str(data.get('error'))[:130])
        result=data.get('result') or {}
        names=[k for k,v in result.items() if k!='last' and isinstance(v,list)]
        if not names:raise RuntimeError('No Kraken OHLC rows')
        # Kraken OHLC rows: time in seconds, o,h,l,c,vwap,volume,count
        now=dt.datetime.now(dt.timezone.utc).timestamp()
        for row in result[names[0]]:
            if len(row)<7 or float(row[0])+3600>now:continue
            k=[int(float(row[0])*1000),row[1],row[2],row[3],row[4],row[6]]
            total+=collect.insert_candle(conn,symbol,'1h',k,'kraken_public')
    return total

def defillama(conn):
    """Independent DeFi market context: TVL per chain and stablecoin supply."""
    a=collect.request_json('https://api.llama.fi/v2/chains')
    b=collect.request_json('https://stablecoins.llama.fi/stablecoins?includePrices=true')
    if not isinstance(a,list) or not isinstance(b,dict):raise RuntimeError('DefiLlama schema changed')
    x=smart_snapshot(conn,'defillama_chains','all_chains',a)
    y=smart_snapshot(conn,'defillama_stablecoins','all_stablecoins',b)
    return x+y

def fear_greed(conn):
    data=collect.request_json('https://api.alternative.me/fng/?limit=2&format=json')
    records=data.get('data',[]) if isinstance(data,dict) else []
    if not records:raise RuntimeError('Fear & Greed empty or malformed')
    return sum(smart_snapshot(conn,'crypto_fear_greed',str(r['timestamp']),r,str(r['timestamp'])) for r in records)

def bitcoin_mempool(conn):
    """On-chain Bitcoin mempool context, not a price oracle."""
    data=collect.request_json('https://mempool.space/api/mempool')
    if not isinstance(data,dict) or 'count' not in data: raise RuntimeError('mempool.space malformed payload')
    return collect.snapshot(conn,'bitcoin_mempool','mempool',data)

def binance_orderbook(conn):
    """Order books give depth/slippage context, NOT profit guarantees."""
    total=0
    for pair in ('BTCUSDT','ETHUSDT','SOLUSDT'):
        url='https://data-api.binance.vision/api/v3/depth?'+urllib.parse.urlencode({'symbol':pair,'limit':100})
        data=collect.request_json(url)
        if not isinstance(data,dict) or not {'bids','asks'}.issubset(data):raise RuntimeError('Binance order book malformed')
        total+=collect.snapshot(conn,'binance_depth',pair,data)
    return total

def rss_news(conn):
    """Only named primary-source official feeds; store item links for later verification."""
    feeds=(
      ('bitcoin_core_releases','https://github.com/bitcoin/bitcoin/releases.atom'),
      ('ethereum_client_releases','https://github.com/ethereum/go-ethereum/releases.atom'),
      ('fed_press','https://www.federalreserve.gov/feeds/press_all.xml'),
    )
    total=0
    successes=0
    for name,url in feeds:
        try:
            tree=retrieve_xml(url)
            # RSS 2.0 and Atom, including namespace-aware entries.
            entries=tree.findall('./channel/item')
            atom=False
            if not entries:
                entries=tree.findall('{http://www.w3.org/2005/Atom}entry');atom=True
            if not entries:raise RuntimeError('Unknown RSS/Atom format')
            successes+=1
            for entry in entries[:25]:
                def value(tag):
                    x=entry.find(('{http://www.w3.org/2005/Atom}' if atom else '')+tag)
                    return (x.text or '').strip() if x is not None else ''
                title=value('title')
                timestamp=value('updated') or value('published') if atom else value('pubDate')
                if atom:
                    link=''
                    for l in entry.findall('{http://www.w3.org/2005/Atom}link'):
                        if l.get('rel','alternate')=='alternate':link=l.get('href','');break
                    identity=value('id') or link
                else:
                    link=value('link');identity=value('guid') or link
                if not identity or not title or not link.startswith('https://'):continue
                payload={'id':identity,'title':title,'link':link,'publication_text':timestamp,'feed':url}
                total+=smart_snapshot(conn,'official_news_'+name,identity,payload,timestamp or None)
        except Exception as exc:
            # One broken feed must not prevent the others, but failures must surface.
            log_run(conn,'rss_'+name,'error',0,str(exc)[:240])
    if not successes:raise RuntimeError('All official news feeds unavailable; see rss_* errors in source_runs')
    return total

def fred_macro(conn,keys):
    """FRED series carry vintage data issues: forecasts must use available-at-time snapshots."""
    key=keys.get('ORACLE_FRED_KEY')
    if not key:return 0
    total=0
    for series in ('CPIAUCSL','UNRATE','FEDFUNDS','DGS10','DGS2'):
        params=urllib.parse.urlencode({'series_id':series,'api_key':key,'file_type':'json','sort_order':'desc','limit':60})
        data=collect.request_json('https://api.stlouisfed.org/fred/series/observations?'+params)
        if not isinstance(data,dict) or 'observations' not in data:raise RuntimeError('FRED schema invalid')
        total+=smart_snapshot(conn,'fred_macro',series,data)
    return total

def multisport_odds(conn,keys):
    """Budgeted daily odds snapshots for independent sports.

    Only one region and one market per sport; three sports -> ~90 calls/month.
    Strong sport models will require richer point-in-time archives later.
    """
    key=keys.get('ORACLE_ODDS_KEY')
    if not key:return 0
    total=0
    for sport in ('soccer_epl','icehockey_nhl','americanfootball_nfl'):
        params=urllib.parse.urlencode({'apiKey':key,'regions':'us','markets':'h2h','oddsFormat':'decimal'})
        url='https://api.the-odds-api.com/v4/sports/'+sport+'/odds/?'+params
        events=collect.request_json(url)
        if not isinstance(events,list):raise RuntimeError('Sports odds malformed for '+sport)
        for e in events:
            if isinstance(e,dict) and e.get('id'):
                total+=smart_snapshot(conn,'sports_odds_'+sport,str(e['id']),e,e.get('commence_time'))
    return total

def sports_scores(conn,keys):
    """The Odds API settlement snapshots, separate from NBA provider result."""
    key=keys.get('ORACLE_ODDS_KEY')
    if not key:return 0
    url='https://api.the-odds-api.com/v4/sports/basketball_nba/scores/?'+urllib.parse.urlencode({'apiKey':key,'daysFrom':3})
    data=collect.request_json(url)
    if not isinstance(data,list):raise RuntimeError('The Odds API scores schema invalid')
    return sum(smart_snapshot(conn,'nba_scores','odds-'+str(x['id']),x,x.get('commence_time')) for x in data if isinstance(x,dict) and x.get('id'))

def create_tables(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS source_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL,
        run_at TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('ok','skip','error')),
        new_rows INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS source_schedule (
        job TEXT PRIMARY KEY, last_attempt TEXT, next_attempt TEXT NOT NULL,
        consecutive_errors INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_runs_job ON source_runs(job, id DESC);
    ''')
    conn.commit()

def log_run(conn,job,state,n=0,message=''):
    # Secrets never enter this log. API errors are redacted to plain messages.
    with conn:
        conn.execute('INSERT INTO source_runs(job,run_at,state,new_rows,message) VALUES(?,?,?,?,?)',
          (job,collect.stamp(),state,int(n),str(message)[:240]))

# Scheduling is deliberately sparse for sources with keys, to stay within free tiers.
# intervals are in seconds; most "high volume" data comes from bulk historical archives.
SCHEDULE={
  'binance_candles':(900,lambda c,k:collect.crypto(c)),
  'binance_depth':(1800,lambda c,k:binance_orderbook(c)),
  'coinbase_ticker':(1800,lambda c,k:coinbase(c)),
  'kraken_candles':(3600,lambda c,k:kraken(c)),
  'bitcoin_mempool':(3600,lambda c,k:bitcoin_mempool(c)),
  'fear_greed':(21600,lambda c,k:fear_greed(c)),
  'defillama':(43200,lambda c,k:defillama(c)),
  'official_news':(43200,lambda c,k:rss_news(c)),
  'nba_games':(21600,lambda c,k:collect.nba(c,k)),
  'nba_odds':(86400,lambda c,k:collect.odds(c,k)),
  'sports_multi_odds':(86400,lambda c,k:multisport_odds(c,k)),
  'nba_scores':(86400,lambda c,k:sports_scores(c,k)),
  'coingecko':(43200,lambda c,k:collect.cg(c,k)),
  'fred_macro':(86400,lambda c,k:fred_macro(c,k)),
}
OPTIONAL={'nba_games':'ORACLE_NBA_KEY','nba_odds':'ORACLE_ODDS_KEY','sports_multi_odds':'ORACLE_ODDS_KEY','nba_scores':'ORACLE_ODDS_KEY','coingecko':'ORACLE_COINGECKO_KEY','fred_macro':'ORACLE_FRED_KEY'}

def run_one(conn,keys,name,clock=None):
    if name not in SCHEDULE:raise ValueError('Unknown source '+name)
    now=clock or dt.datetime.now(dt.timezone.utc)
    if name in OPTIONAL and not keys.get(OPTIONAL[name]):
        log_run(conn,name,'skip',0,'API key absent: '+OPTIONAL[name])
        return 'skip',0
    try:
        n=SCHEDULE[name][1](conn,keys)
        log_run(conn,name,'ok',n,'')
        return 'ok',n
    except Exception as ex:
        log_run(conn,name,'error',0,type(ex).__name__+': '+str(ex)[:170])
        return 'error',0

def run_due(conn,keys,now=None,selected=None):
    """Persist due times in UTC to prevent repeated expensive calls after restarts."""
    now=now or dt.datetime.now(dt.timezone.utc)
    summary={}
    jobs={k:v for k,v in SCHEDULE.items() if not selected or k in selected}
    for name,(every,fn) in jobs.items():
        row=conn.execute('SELECT next_attempt,consecutive_errors FROM source_schedule WHERE job=?',(name,)).fetchone()
        if row and dt.datetime.fromisoformat(row[0])>now:continue
        state,n=run_one(conn,keys,name,now)
        streak=((row[1] if row else 0)+1) if state=='error' else 0
        delay=min(86400,every*(2**min(streak,5))) if streak else every
        upcoming=(now+dt.timedelta(seconds=delay)).isoformat(timespec='seconds')
        with conn:
            conn.execute('INSERT INTO source_schedule(job,last_attempt,next_attempt,consecutive_errors) VALUES(?,?,?,?) '
                         'ON CONFLICT(job) DO UPDATE SET last_attempt=excluded.last_attempt,next_attempt=excluded.next_attempt,consecutive_errors=excluded.consecutive_errors',
                         (name,now.isoformat(timespec='seconds'),upcoming,streak))
        summary[name]={'status':state,'new_records':n,'next_attempt':upcoming}
    return summary

def binance_derivatives(conn):
    """Public futures mark/funding/open-interest context; independent of Binance spot.

    Historical availability is limited. Never backfill current funding into past ML features.
    May be unavailable in some jurisdictions; errors are tracked, not worked around.
    """
    count=0
    for symbol in ('BTCUSDT','ETHUSDT','SOLUSDT'):
        for endpoint,key in (('/fapi/v1/premiumIndex','funding'),('/fapi/v1/openInterest','open_interest')):
            url='https://fapi.binance.com'+endpoint+'?'+urllib.parse.urlencode({'symbol':symbol})
            payload=collect.request_json(url)
            if not isinstance(payload,dict) or payload.get('symbol')!=symbol:
                raise ValueError('Unexpected derivatives API format')
            count+=smart_snapshot(conn,'binance_derivatives_'+key,symbol,payload)
    return count

# Version 0.7 source adapters: all jobs are independently isolated and rate-limited.
SCHEDULE['binance_derivatives']=(21600,lambda c,k:binance_derivatives(c))
