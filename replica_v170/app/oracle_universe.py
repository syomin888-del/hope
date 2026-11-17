#!/usr/bin/env python3
"""ORACLE 2.6: market-wide *paper research*, NOT live advice or a proven AI model.
Reads the entire eligible exchange universe, public news first-seen timestamps,
and closed-hour technical snapshots. Any unverified hypothesis is explicitly labelled.
"""
from __future__ import annotations
import argparse, collections, datetime as dt, json, math, os, re, sqlite3, time, urllib.request
from pathlib import Path
import collect

UTC=dt.timezone.utc
PCTS=(10,15,20,30,40)
INTERVAL=max(180,min(1800,int(os.getenv('ORACLE_UNIVERSE_INTERVAL','300'))))
MIN_VOL=max(100_000,float(os.getenv('ORACLE_UNIVERSE_MIN_VOLUME','1000000')))
COOLDOWN_H=max(1,min(24,int(os.getenv('ORACLE_UNIVERSE_COOLDOWN_H','12'))))
MAX_PRIOR24=4.0
NAME_URL='https://api.coingecko.com/api/v3/coins/list?include_platform=false'
# These are verified project name strings, NOT a complete universe. Optional
# /v1/coins adds a much larger catalogue, never ambiguous ticker guesses.
SEED={'BTC':'Bitcoin','ETH':'Ethereum','SOL':'Solana','DGB':'DigiByte','BMT':'Bubblemaps',
'ADA':'Cardano','AVAX':'Avalanche','DOT':'Polkadot','ATOM':'Cosmos','ARB':'Arbitrum',
'LINK':'Chainlink','SUI':'Sui','APT':'Aptos','NEAR':'NEAR Protocol','UNI':'Uniswap',
'AAVE':'Aave','XRP':'XRP','XLM':'Stellar','BNB':'BNB','DOGE':'Dogecoin',
'LTC':'Litecoin','BCH':'Bitcoin Cash','INJ':'Injective','TIA':'Celestia',
'TAO':'Bittensor','FIL':'Filecoin','ZEC':'Zcash','OP':'Optimism',
'POL':'Polygon','ALGO':'Algorand','ETC':'Ethereum Classic','AR':'Arweave',
'WIF':'dogwifhat','PEPE':'Pepe','BONK':'Bonk','SHIB':'Shiba Inu',
'FET':'Artificial Superintelligence Alliance','SEI':'Sei','HBAR':'Hedera',
'ICP':'Internet Computer','VET':'VeChain','TRX':'TRON','LDO':'Lido DAO',
'RUNE':'THORChain','PENDLE':'Pendle','ONDO':'Ondo','MKR':'Maker',
'MASK':'Mask Network','SAND':'The Sandbox','MANA':'Decentraland',
'ENJ':'Enjin Coin','GRT':'The Graph','IMX':'Immutable','CRV':'Curve DAO Token',
'COMP':'Compound','SNX':'Synthetix','KSM':'Kusama','CAKE':'PancakeSwap',
'FTM':'Fantom','THETA':'Theta Network','IOTA':'IOTA'}
DIRECT_RE=re.compile(r'(?i)\b(?:announces?|confirms?|launches?|launched|releases?|released|unveils?|unveiled|lists?|listed|listing|approves?|approved|partnership|integrates?|integrated|completes?|completed|burns?|burned|upgrade|upgrading|mainnet|buyback|запускает|объявляет|запустил|выпустил|листинг|обновление|партнерство|партнёрство|anuncia|lanza|aprobado)\b')
RISK_RE=re.compile(r'(?i)\b(?:hack|hacked|exploit|fraud|scam|rug.pull|fake.liquidity|wash.trading|lawsuit|delist|arrest|investigation|accused|alleged)\b')
DIRECT_ACTION_RE=re.compile(r'(?i)\b(?:announces?|confirms?|launches?|launched|releases?|released|unveils?|unveiled|lists?|listed|burns?|burned|upgrades?|signs?|signed|запускает|объявляет|запустил|anuncia|lanza)\b')
COMMON={'one','new','near','gas','spell','sun','high','low','time','best','ai','up','go','cat','dog','hot','fun','move','people','market','world','stock','crypto','the','data','bank','fund','usd'}

def now():return dt.datetime.now(UTC).isoformat(timespec='seconds')
def when(x):
    a=dt.datetime.fromisoformat(str(x).replace('Z','+00:00'))
    if a.tzinfo is None:raise ValueError('No timezone')
    return a.astimezone(UTC)
def stamp(a):return a.astimezone(UTC).isoformat(timespec='seconds')
def age(s,at):return (when(at)-when(s)).total_seconds()
def names(c):return {x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS ub_state(key TEXT PRIMARY KEY,val TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ub_names(symbol TEXT PRIMARY KEY,name TEXT NOT NULL,source TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ub_dossiers(
      symbol TEXT PRIMARY KEY,analyzed_at TEXT NOT NULL,quote_at TEXT NOT NULL,
      price REAL NOT NULL,quote_volume REAL NOT NULL,change_24h REAL,
      observed_prior_1h REAL,news_count INTEGER NOT NULL,
      patterns_json TEXT NOT NULL,technical_at TEXT,tech_priority INTEGER,
      research_score INTEGER NOT NULL,classification TEXT NOT NULL,
      explanation_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ub_paper(
      id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT NOT NULL,created_at TEXT NOT NULL,
      entry_price REAL NOT NULL,quote_at TEXT NOT NULL,prior_24h REAL,
      prior_1h REAL,quote_volume REAL NOT NULL,scenario TEXT NOT NULL,
      reasons_json TEXT NOT NULL,source_news_json TEXT NOT NULL,
      horizon_end TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'PAPER_OPEN',
      max_future_price REAL,close_future_price REAL,hour_bins INTEGER,
      hits_json TEXT,settled_at TEXT);
    CREATE INDEX IF NOT EXISTS ub_paper_due ON ub_paper(state,horizon_end);
    CREATE INDEX IF NOT EXISTS ub_paper_coin_time ON ub_paper(symbol,created_at DESC);
    CREATE TABLE IF NOT EXISTS ub_cycles(
      at TEXT PRIMARY KEY,coins_scanned INTEGER,tradable_quotes INTEGER,
      with_tech INTEGER,with_direct_news INTEGER,hypotheses_created INTEGER,
      reasons_json TEXT NOT NULL);
    ''');c.commit()

def state(c,k,default=''):
    x=c.execute('SELECT val FROM ub_state WHERE key=?',(k,)).fetchone()
    return x[0] if x else default

def set_state(c,k,v):
    with c:c.execute('INSERT INTO ub_state(key,val) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET val=excluded.val',(k,str(v)))

def update_names(c,at=None,loader=None):
    """Best-effort 1 request / 24h; never give ambiguous ticker a foreign name."""
    at=at or now()
    with c:
        for sym,name in SEED.items():
            c.execute('INSERT OR IGNORE INTO ub_names VALUES(?,?,?,?)',(sym,name,'CURATED',at))
    if state(c,'names_last_attempt') and age(state(c,'names_last_attempt'),at)<(24*3600 if state(c,'names_last_success') else 2*3600):return {'status':'cached'}
    set_state(c,'names_last_attempt',at)
    try:
        if loader is None:
            req=urllib.request.Request(NAME_URL,headers={'User-Agent':'ORACLE-PersonalResearch/2.6'})
            with urllib.request.urlopen(req,timeout=18) as r:blob=r.read(8_000_001)
            if len(blob)>8_000_000:raise ValueError('Coin catalogue too large')
            raw=json.loads(blob)
        else: raw=loader()
        if not isinstance(raw,list) or not 100<=len(raw)<=40000:raise ValueError('Incomplete catalogue')
        listed={x[0] for x in c.execute('SELECT base FROM mw_symbols WHERE eligible=1')}
        candidates=collections.defaultdict(set)
        for item in raw:
            if not isinstance(item,dict) or item.get('is_active') is False:continue
            sym=str(item.get('symbol','')).upper();name=str(item.get('name','')).strip()
            if sym in listed and 4<=len(name)<=70 and name.casefold() not in COMMON:
                candidates[sym].add(name)
        n=0
        with c:
            for sym,vals in candidates.items():
                if len(vals)!=1 or sym in SEED:continue
                name=next(iter(vals))
                c.execute('INSERT INTO ub_names VALUES(?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name,source=excluded.source,updated_at=excluded.updated_at',(sym,name,'COINGECKO_UNVERIFIED_TICKER_NAME',at));n+=1
        set_state(c,'names_last_success',at)
        return {'status':'ok','new_names':n,'ambiguous_skipped':sum(len(v)>1 for v in candidates.values())}
    except Exception as e:
        set_state(c,'names_last_error',type(e).__name__+': '+str(e)[:120]);return {'status':'error','reason':str(e)[:100]}

def token_mentioned(title,base,full_name):
    # Accept explicit cashtag or full project name; DO NOT match bare small ticker.
    if re.search(r'(?<![\w])\$'+re.escape(base)+r'(?![\w])',title,re.I):return True
    if not full_name or len(full_name.strip())<4 or full_name.casefold() in COMMON:return False
    return bool(re.search(r'(?<!\w)'+re.escape(full_name)+r'(?!\w)',title,re.I))

def event_context(c,at,symbol,project_name,window_h=3,news_rows=None):
    if 'oc_news' not in names(c):return []
    earliest=stamp(when(at)-dt.timedelta(hours=window_h))
    rows=(news_rows if news_rows is not None else c.execute('''SELECT n.news_key,n.first_seen_at,n.published_at,n.title,n.url,n.source
     FROM oc_news n WHERE n.first_seen_at>=? AND n.first_seen_at<=?
     ORDER BY n.first_seen_at DESC LIMIT 700''',(earliest,at)).fetchall())
    out=[];base=symbol.removesuffix('USDT')
    for key,seen,pub,title,url,src in rows:
        if not token_mentioned(title,base,project_name):continue
        # Do not infer forward value from unverified identity or a vague news mention.
        # The named project must act as a subject, or be the named object
        # of an actual exchange listing. 'Fhenix launches on Ethereum' !=
        # 'Ethereum launches Fhenix'; this distinction matters.
        project_pattern=(r'(?<!\w)'+re.escape(project_name)+r'(?!\w)'
                         if project_name else r'(?<!\w)\$'+re.escape(base)+r'(?!\w)')
        subject=bool(re.search(project_pattern+r'(?:\s+\w+){0,3}\s+'+DIRECT_ACTION_RE.pattern.replace('(?i)',''),title,re.I))
        listing=bool(re.search(r'(?i)\b(?:binance|coinbase|kraken|okx|bybit)\b.{0,70}\b(?:lists?|listed|listing)\s+'+project_pattern,title,re.I))
        direct=bool(subject or listing)
        risk=bool(RISK_RE.search(title)) or (base=='BMT' and bool(re.search(r'(?i)bubblemaps.{0,45}(?:investigat|uncovers?|exposes?|reveals?)',title)))
        if pub:
            try:
                if when(pub)>when(seen) or age(pub,seen)>3*3600:continue
            except (ValueError,TypeError):continue
        out.append({'key':key,'first_seen':seen,'published':pub,'title':title[:300],
                    'url':url,'source':src,'direct_language':direct,'risk':risk,
                    'identity_certified':base in SEED,
                    'identity_source':'CURATED' if base in SEED else 'CATALOGUE_UNVERIFIED'})
        if len(out)>=5:break
    return out

def latest_quotes(c,at):
    if not {'mw_quotes','mw_symbols'}.issubset(names(c)):return []
    earliest=stamp(when(at)-dt.timedelta(minutes=20))
    return c.execute('''SELECT s.symbol,s.base,q.observed_at,q.price,q.quote_volume_24h,q.change_24h_pct
        FROM mw_symbols s JOIN mw_quotes q ON q.symbol=s.symbol
        WHERE s.eligible=1 AND q.observed_at=(SELECT MAX(z.observed_at)
          FROM mw_quotes z WHERE z.symbol=s.symbol AND z.observed_at<=?)
          AND q.observed_at>=? ORDER BY s.symbol''',(at,earliest)).fetchall()

def technical(c,sym,at):
    if 'oc_technical' not in names(c):return None
    x=c.execute('''SELECT evaluated_at,patterns,features,research_priority
         FROM oc_technical WHERE symbol=? AND evaluated_at<=? ORDER BY evaluated_at DESC LIMIT 1''',(sym,at)).fetchone()
    if not x or age(x[0],at)>90*60:return None
    try:return {'at':x[0],'patterns':json.loads(x[1]),'features':json.loads(x[2]),'priority':int(x[3])}
    except (ValueError,TypeError):return None

def prior_hour(c,sym,at):
    earliest=stamp(when(at)-dt.timedelta(minutes=90));latest=stamp(when(at)-dt.timedelta(minutes=45))
    x=c.execute('''SELECT price FROM mw_quotes WHERE symbol=? AND observed_at BETWEEN ? AND ?
        ORDER BY observed_at DESC LIMIT 1''',(sym,earliest,latest)).fetchone()
    return x[0] if x and x[0]>0 else None

def qualifies(quote,tech,events,prior):
    _,_,qat,price,vol,ch=quote
    reasons=[];classification='OBSERVE_ONLY';score=0
    if vol<MIN_VOL or price<=0 or not math.isfinite(price):return classification,score,['LIQUIDITY_OR_PRICE_UNFIT']
    if ch is None or ch>MAX_PRIOR24 or ch< -20:return classification,score,['ALREADY_MOVED_OR_NO_24H_CHANGE']
    if prior is None:return classification,score,['NO_PRIOR_HOUR_QUOTE']
    hour=100*(price/prior-1)
    if hour>1.5:return classification,score,['ALREADY_MOVED_LAST_HOUR']
    if hour< -5:return classification,score,['ABNORMAL_HOURLY_DROP']
    has_event=any(e['direct_language'] and not e['risk'] and 0<=age(e['first_seen'],qat)<=40*60 for e in events)
    if has_event:score+=3;reasons.append('DIRECT_EVENT_LEAD_HEADLINE_NOT_VERIFIED')
    if tech:
        feat=tech['features'];patterns=tech['patterns'];
        # Triggers intentionally conservative; all are evidence observations,
        # never calibrated probability estimates.
        if 'ema_uptrend_alignment' in patterns:score+=1;reasons.append('EMA_TREND_ALIGNMENT')
        if (feat.get('volume_6h_vs_prev24h') or 0)>=1.4:score+=2;reasons.append('VOLUME_RELATIVE_TO_PRIOR_HOURS')
        if 40<=float(feat.get('rsi14') or 0)<=67:score+=1;reasons.append('RSI_NOT_EXTENDED')
        if (feat.get('relative_to_btc_24h_pct') or 0)>=1:score+=1;reasons.append('RELATIVE_STRENGTH_VS_BTC')
        if (feat.get('macd_hist_pct') or 0)>0:score+=1;reasons.append('POSITIVE_MACD_HISTOGRAM')
    if has_event:classification='PRE_MOVE_NEWS_RESEARCH'
    elif score>=5 and tech:classification='PRE_MOVE_TECH_RESEARCH'
    return classification,score,reasons

def scan(c,at=None):
    at=at or now();setup(c)
    # Snapshot at actual cycle time; no future knowledge.
    rows=latest_quotes(c,at)
    names_map={base:name for base,name in c.execute('SELECT symbol,name FROM ub_names')}
    scanned=0;eligible=0;with_tech=0;with_news=0;created=0;reason_counts=collections.Counter()
    earliest=stamp(when(at)-dt.timedelta(hours=3))
    news_rows=(c.execute('''SELECT news_key,first_seen_at,published_at,title,url,source FROM oc_news
       WHERE first_seen_at>=? AND first_seen_at<=? ORDER BY first_seen_at DESC LIMIT 700''',
       (earliest,at)).fetchall() if 'oc_news' in names(c) else [])
    for row in rows:
        sym,base,qat,price,vol,ch=row;scanned+=1
        if vol<MIN_VOL:reason_counts['BELOW_VOLUME_FLOOR']+=1;continue
        eligible+=1
        tech=technical(c,sym,at);with_tech+=int(tech is not None)
        news=event_context(c,at,sym,names_map.get(base),news_rows=news_rows);with_news+=int(bool(news))
        prior=prior_hour(c,sym,at)
        classification,score,reasons=qualifies(row,tech,news,prior)
        if classification=='OBSERVE_ONLY':reason_counts.update(reasons or ['NO_QUALIFYING_COMBINATION'])
        evidence={'quote_time':qat,'news':news[:3],'technical':tech,'prior_hour_price':prior,
                  'rules':reasons,'not_a_validated_model':True,'estimated_probability':None}
        with c:c.execute('''INSERT INTO ub_dossiers(symbol,analyzed_at,quote_at,price,quote_volume,
             change_24h,observed_prior_1h,news_count,patterns_json,technical_at,tech_priority,
             research_score,classification,explanation_json)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET
             analyzed_at=excluded.analyzed_at,quote_at=excluded.quote_at,price=excluded.price,
             quote_volume=excluded.quote_volume,change_24h=excluded.change_24h,
             observed_prior_1h=excluded.observed_prior_1h,news_count=excluded.news_count,
             patterns_json=excluded.patterns_json,technical_at=excluded.technical_at,
             tech_priority=excluded.tech_priority,research_score=excluded.research_score,
             classification=excluded.classification,explanation_json=excluded.explanation_json''',
             (sym,at,qat,price,vol,ch,100*(price/prior-1) if prior else None,len(news),
              json.dumps(tech['patterns'] if tech else []),tech['at'] if tech else None,
              tech['priority'] if tech else None,score,classification,json.dumps(evidence,ensure_ascii=False)))
        if classification=='OBSERVE_ONLY':continue
        last=c.execute('SELECT created_at FROM ub_paper WHERE symbol=? ORDER BY created_at DESC LIMIT 1',(sym,)).fetchone()
        if last and age(last[0],at)<COOLDOWN_H*3600:reason_counts['PAPER_COOLDOWN']+=1;continue
        end=stamp(when(at)+dt.timedelta(hours=24))
        # Only save research hypotheses from information available AS OF now.
        with c:
            c.execute('''INSERT INTO ub_paper(symbol,created_at,entry_price,quote_at,prior_24h,
                prior_1h,quote_volume,scenario,reasons_json,source_news_json,horizon_end)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (sym,at,price,qat,ch,100*(price/prior-1),vol,classification,
                 json.dumps(reasons),json.dumps(news[:3],ensure_ascii=False),end))
        created+=1
    with c:c.execute('INSERT OR REPLACE INTO ub_cycles VALUES(?,?,?,?,?,?,?)',
        (at,scanned,eligible,with_tech,with_news,created,json.dumps(dict(reason_counts))))
    return {'exchange_pairs_scanned':scanned,'liquid_pairs':eligible,'technical_coverage':with_tech,
            'direct_news_mentions_seen':with_news,'new_paper_scenarios':created,
            'not_approved_predictions':True,'exclusions':dict(reason_counts)}

def settle(c,at=None):
    at=at or now();out={'settled':0,'incomplete':0}
    for pid,sym,created,end,entry in c.execute('''SELECT id,symbol,created_at,horizon_end,entry_price
     FROM ub_paper WHERE state='PAPER_OPEN' AND horizon_end<=? LIMIT 500''',(at,)).fetchall():
        quotes=c.execute('SELECT observed_at,price FROM mw_quotes WHERE symbol=? AND observed_at>? AND observed_at<=? AND price>0 ORDER BY observed_at',(sym,created,end)).fetchall()
        bins={int(age(created,t)//3600) for t,p in quotes}
        enough=(len(quotes)>=16 and len(bins)>=16 and age(created,quotes[0][0])<=3600
                and age(quotes[-1][0],end)<=3600) if quotes else False
        peak=max((float(p) for t,p in quotes),default=None)
        hits={str(pct):int(peak>=entry*(1+pct/100)) for pct in PCTS} if enough else None
        with c:c.execute('''UPDATE ub_paper SET state=?,max_future_price=?,close_future_price=?,
           hour_bins=?,hits_json=?,settled_at=? WHERE id=?''',
           ('PAPER_SETTLED' if enough else 'INCOMPLETE',peak,quotes[-1][1] if quotes else None,
            len(bins),json.dumps(hits) if hits else None,at,pid))
        out['settled' if enough else 'incomplete']+=1
    return out

def status(c):
    setup(c)
    def num(sql):return c.execute(sql).fetchone()[0]
    cyc=c.execute('SELECT at,coins_scanned,tradable_quotes,with_tech,with_direct_news,hypotheses_created,reasons_json FROM ub_cycles ORDER BY at DESC LIMIT 1').fetchone()
    return {'eligible_spot_pairs':num('SELECT COUNT(*) FROM mw_symbols WHERE eligible=1') if 'mw_symbols' in names(c) else 0,
      'dossiers_analyzed':num('SELECT COUNT(*) FROM ub_dossiers'),
      'open_scenarios':num("SELECT COUNT(*) FROM ub_paper WHERE state='PAPER_OPEN'"),
      'settled_scenarios':num("SELECT COUNT(*) FROM ub_paper WHERE state='PAPER_SETTLED'"),
      'incomplete_scenarios':num("SELECT COUNT(*) FROM ub_paper WHERE state='INCOMPLETE'"),
      'name_catalogue':num('SELECT COUNT(*) FROM ub_names'),
      'name_catalogue_last_success':state(c,'names_last_success') or None,
      'name_catalogue_last_error':state(c,'names_last_error') or None,
      'latest_cycle':{'at':cyc[0],'pairs_scanned':cyc[1],'liquid_pairs':cyc[2],'with_tech':cyc[3],
         'news_mentions':cyc[4],'new_scenarios':cyc[5],'exclusions':json.loads(cyc[6])} if cyc else None,
      'mode':'MARKET_WIDE_RESEARCH_ONLY_NOT_APPROVED_BUY_ADVICE'}

def dashboard(db):
    res={'module':'universe','mode':'PAPER_RESEARCH_NOT_TRADING','dossiers':[],'scenarios':[],'stats':{}}
    if not Path(db).exists():return res
    c=sqlite3.connect('file:'+Path(db).resolve().as_posix()+'?mode=ro',uri=True)
    try:
        if 'ub_dossiers' not in names(c):return res
        labels=('symbol','at','price','volume','change_24h','prior_1h','score','classification','tech_priority','news_count','explanation')
        for r in c.execute('''SELECT symbol,analyzed_at,price,quote_volume,change_24h,observed_prior_1h,
          research_score,classification,tech_priority,news_count,explanation_json FROM ub_dossiers
          ORDER BY CASE WHEN classification='OBSERVE_ONLY' THEN 1 ELSE 0 END,
          research_score DESC,quote_volume DESC LIMIT 600'''):
            x=dict(zip(labels,r));x['explanation']=json.loads(x['explanation']);res['dossiers'].append(x)
        keys=('symbol','at','entry','scenario','reasons','news','state','peak','hits','end')
        for r in c.execute('''SELECT symbol,created_at,entry_price,scenario,reasons_json,source_news_json,
          state,max_future_price,hits_json,horizon_end FROM ub_paper ORDER BY id DESC LIMIT 60'''):
            x=dict(zip(keys,r))
            for k in ('reasons','news','hits'):x[k]=json.loads(x[k]) if x[k] else None
            res['scenarios'].append(x)
        res['stats']={'total_pairs':c.execute('SELECT COUNT(*) FROM ub_dossiers').fetchone()[0],
                      'open':c.execute("SELECT COUNT(*) FROM ub_paper WHERE state='PAPER_OPEN'").fetchone()[0],
                      'settled':c.execute("SELECT COUNT(*) FROM ub_paper WHERE state='PAPER_SETTLED'").fetchone()[0],
                      'calibrated_predictions':0}
    finally:c.close()
    return res

def run(c):
    setup(c)
    while True:
        t0=time.monotonic()
        try:
            catalogue=update_names(c)
            result=scan(c)
            result['catalogue']=catalogue;result['settlement']=settle(c)
            print(json.dumps(result,ensure_ascii=False),flush=True)
        except Exception as exc:print('UNIVERSE ERROR',type(exc).__name__,str(exc)[:300],flush=True)
        time.sleep(max(1,INTERVAL-(time.monotonic()-t0)))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=('run','once','status','catalogue','settle'))
    args=ap.parse_args();c=collect.db_open();setup(c)
    if args.action=='run':run(c)
    else:
        x={'once':lambda:scan(c),'status':lambda:status(c),'catalogue':lambda:update_names(c),'settle':lambda:settle(c)}[args.action]()
        print(json.dumps(x,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
