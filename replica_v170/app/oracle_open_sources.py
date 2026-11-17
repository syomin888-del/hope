"""ORACLE public-data federation: no account, zero fabricated quotes, paper only.

Read-only public endpoints. Every observation has provenance and first-observed timestamp.
No bookmaker claims from prediction market quotes; annual macro != real-time signal.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

UTC = dt.timezone.utc
UA = 'ORACLE-V4-OpenResearch/1.0 (respectful public data polling)'
MAX_BYTES = 2_000_000
APPROVED_HOSTS = frozenset({
    'api.worldbank.org', 'api.crossref.org', 'api.openalex.org',
    'publicreporting.cftc.gov', 'api.gleif.org', 'data-api.binance.vision',
    'gamma-api.polymarket.com'
})
PROVIDERS = {
    'gleif': {'branch':'causal','kind':'VERIFIED_LEGAL_ENTITY_RELATIONS','registration':'NO','frequency_hours':168,
       'documentation':'https://www.gleif.org/en/lei-data/gleif-api/'},
    'cftc': {'branch':'wall_street','kind':'WEEKLY_POSITIONING_NOT_LIVE','registration':'NO','frequency_hours':24,
       'documentation':'https://publicreporting.cftc.gov/'},
    'worldbank': {'branch':'macro','kind':'ANNUAL_MACRO','registration':'NO','frequency_hours':24,
       'documentation':'https://datahelpdesk.worldbank.org/knowledgebase/articles/889392'},
    'crossref': {'branch':'research','kind':'BIBLIOGRAPHIC_METADATA','registration':'NO','frequency_hours':24,
       'documentation':'https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/'},
    'openalex': {'branch':'research','kind':'SCHOLARLY_METADATA','registration':'NO','frequency_hours':24,
       'documentation':'https://help.openalex.org/api/authentication/'},
    'binance_spot': {'branch':'crypto','kind':'SPOT_MARKET_REFERENCE','registration':'NO','frequency_hours':1,
       'documentation':'https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md'},
    'polymarket_gamma': {'branch':'prediction_markets','kind':'NOT_BOOKMAKER','registration':'NO','frequency_hours':1,
       'documentation':'https://docs.polymarket.com/'},
}

def stamp(at=None):
    return (at or dt.datetime.now(UTC)).astimezone(UTC).isoformat(timespec='seconds')

def digest(s):return hashlib.sha256(str(s).encode()).hexdigest()[:24]

def parse_time(s):
    d=dt.datetime.fromisoformat(str(s).replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('Time zone required')
    return d.astimezone(UTC)

def safe_url(url):
    s=urlsplit(url)
    if s.scheme!='https' or s.hostname not in APPROVED_HOSTS or s.username or s.password or s.fragment or s.port:
        raise ValueError('Provider URL not approved')
    return url

def download(url):
    safe_url(url)
    req=urllib.request.Request(url, headers={'User-Agent':UA,'Accept':'application/json'})
    # Disable cross-domain redirects; URL prefix checking alone is insufficient.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError('Unexpected provider redirect')
    with urllib.request.build_opener(NoRedirect).open(req,timeout=16) as res:
        b=res.read(MAX_BYTES+1)
        if len(b)>MAX_BYTES:raise ValueError('Response too large')
        return json.loads(b)

def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS v4_public_source_state(
      provider TEXT PRIMARY KEY, checked_at TEXT NOT NULL, status TEXT NOT NULL,
      new_records INTEGER NOT NULL, detail TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v4_public_macro(
      provider TEXT NOT NULL,series TEXT NOT NULL,region TEXT NOT NULL,period TEXT NOT NULL,
      value REAL NOT NULL, first_seen_at TEXT NOT NULL, source_url TEXT NOT NULL,
      PRIMARY KEY(provider,series,region,period,first_seen_at));
    CREATE TABLE IF NOT EXISTS v4_public_crypto(
      provider TEXT NOT NULL,symbol TEXT NOT NULL, observed_at TEXT NOT NULL,
      last_price REAL NOT NULL,quote_volume REAL, change_24h_pct REAL,
      source_url TEXT NOT NULL, PRIMARY KEY(provider,symbol,observed_at));
    CREATE TABLE IF NOT EXISTS v4_public_market_refs(
      provider TEXT NOT NULL,event_id TEXT NOT NULL,market_id TEXT NOT NULL,
      market_question TEXT NOT NULL, outcome TEXT NOT NULL,observed_at TEXT NOT NULL,
      share_price REAL NOT NULL, liquidity_usd REAL,source_url TEXT NOT NULL,
      PRIMARY KEY(provider,market_id,outcome,observed_at));
    CREATE TABLE IF NOT EXISTS v4_public_cot(
      source TEXT NOT NULL,contract TEXT NOT NULL,report_date TEXT NOT NULL,
      first_seen_at TEXT NOT NULL,payload TEXT NOT NULL,source_url TEXT NOT NULL,
      PRIMARY KEY(source,contract,report_date,first_seen_at));
    CREATE TABLE IF NOT EXISTS v4_public_lei_entities(
      lei TEXT NOT NULL,legal_name TEXT NOT NULL,country TEXT,observed_at TEXT NOT NULL,
      source_url TEXT NOT NULL, PRIMARY KEY(lei,observed_at));
    CREATE TABLE IF NOT EXISTS v4_public_lei_relations(
      child_lei TEXT NOT NULL,parent_lei TEXT NOT NULL,relation TEXT NOT NULL,
      observed_at TEXT NOT NULL,source_url TEXT NOT NULL,
      PRIMARY KEY(child_lei,parent_lei,relation,observed_at));
    CREATE TABLE IF NOT EXISTS v4_public_research(
      id TEXT PRIMARY KEY, source TEXT NOT NULL,title TEXT NOT NULL,doi TEXT NOT NULL,
      published TEXT, link TEXT NOT NULL,first_seen_at TEXT NOT NULL,
      study_status TEXT NOT NULL DEFAULT 'METADATA_ONLY',verified INTEGER NOT NULL DEFAULT 0);
    ''')
    c.commit()

def source_state(c,provider,status,count,detail,at=None):
    if status not in ('OK','PARTIAL','EMPTY','ERROR','THROTTLED'):raise ValueError('Invalid state')
    with c:c.execute('''INSERT INTO v4_public_source_state(provider,checked_at,status,new_records,detail)
     VALUES(?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET checked_at=excluded.checked_at,
     status=excluded.status,new_records=excluded.new_records,detail=excluded.detail''',
     (provider,stamp(at),status,int(count),str(detail)[:200]))

def ingest_worldbank(c,at=None,loader=download):
    """Annual observations only; NEVER infer their historical release date from year."""
    setup(c);seen=stamp(at);total=0;fail=[]
    # Separate calls enforce source-specific provenance and bounded provider load.
    for region in ('USA','CHN','IDN'):
        for indicator in ('NY.GDP.MKTP.CD','FP.CPI.TOTL.ZG'):
            url=f'https://api.worldbank.org/v2/country/{region}/indicator/{indicator}?format=json&mrnev=3&per_page=10'
            try:
                body=loader(url)
                if not isinstance(body,list) or len(body)<2 or not isinstance(body[1],list):raise ValueError('World Bank shape')
                with c:
                    for row in body[1][:3]:
                        val=row.get('value');year=str(row.get('date') or '')
                        if not re.fullmatch(r'\d{4}',year) or isinstance(val,bool) or not isinstance(val,(int,float)) or not math.isfinite(val):continue
                        # first_seen_at part of PK tracks vintages; on daily polling avoid redundant identical copies.
                        previous=c.execute('SELECT value FROM v4_public_macro WHERE provider=? AND region=? AND series=? AND period=? ORDER BY first_seen_at DESC LIMIT 1',
                          ('worldbank',region,indicator,year)).fetchone()
                        if previous and previous[0]==float(val):continue
                        c.execute('INSERT INTO v4_public_macro VALUES(?,?,?,?,?,?,?)',('worldbank',indicator,region,year,float(val),seen,url));total+=1
            except Exception as ex:fail.append(f'{region}/{indicator}:{type(ex).__name__}')
    state='PARTIAL' if fail and total else 'ERROR' if fail else 'OK' if total or c.execute('SELECT COUNT(*) FROM v4_public_macro').fetchone()[0] else 'EMPTY'
    source_state(c,'worldbank',state,total,'Annual macro; failures: '+','.join(fail) if fail else 'Annual releases, not real-time ticks',at)
    return {'provider':'worldbank','state':state,'new_vintage_rows':total,'failures':len(fail)}

def ingest_binance(c,at=None,loader=download):
    setup(c);seen=stamp(at);inserted=0;errors=[]
    for symbol in ('BTCUSDT','ETHUSDT'):
        url='https://data-api.binance.vision/api/v3/ticker/24hr?symbol='+symbol
        try:
            row=loader(url)
            if str(row.get('symbol'))!=symbol:raise ValueError('Symbol mismatch')
            price=float(row['lastPrice']);vol=float(row['quoteVolume']);change=float(row['priceChangePercent'])
            if not (0<price<1e10 and vol>=0 and all(math.isfinite(x) for x in (price,vol,change))):raise ValueError('Bad price')
            with c:
                c.execute('INSERT OR IGNORE INTO v4_public_crypto VALUES(?,?,?,?,?,?,?)',('binance_spot',symbol,seen,price,vol,change,url));inserted+=1
        except Exception as ex:errors.append(symbol+':'+type(ex).__name__)
    state='PARTIAL' if errors and inserted else 'ERROR' if errors else 'OK'
    source_state(c,'binance_spot',state,inserted,'Spot research only; '+','.join(errors) if errors else 'Spot reference, not signal',at)
    return {'provider':'binance_spot','state':state,'new_spot_snapshots':inserted,'failures':len(errors)}

def _array(value):
    if isinstance(value,list):return value
    if isinstance(value,str):
        try:return json.loads(value)
        except (ValueError,TypeError):return []
    return []

def ingest_polymarket(c,at=None,loader=download):
    """Prediction exchange snapshots. Never map these to bookmaker odds or sports ROI."""
    setup(c);seen=stamp(at);url='https://gamma-api.polymarket.com/events?active=true&closed=false&limit=30';count=0
    try:
        events=loader(url)
        if not isinstance(events,list):raise ValueError('Expected event array')
        with c:
            for e in events[:30]:
                eid=str(e.get('id') or '')
                if not eid:continue
                for market in e.get('markets',[])[:12]:
                    if not isinstance(market,dict) or market.get('closed') is True:continue
                    mid=str(market.get('id') or '');question=str(market.get('question') or '')[:400]
                    if not mid or not question:continue
                    options=_array(market.get('outcomes'));prices=_array(market.get('outcomePrices'))
                    if len(options)!=len(prices) or len(options)>12:continue
                    try:liq=float(market.get('liquidityNum') or market.get('liquidity') or 0)
                    except (ValueError,TypeError):liq=None
                    for outcome,price_raw in zip(options,prices):
                        try:price=float(price_raw)
                        except (ValueError,TypeError):continue
                        if not (0<price<1 and math.isfinite(price)):continue
                        with_url='https://polymarket.com/event/'+urllib.parse.quote(str(e.get('slug') or ''),safe='-')
                        c.execute('INSERT OR IGNORE INTO v4_public_market_refs VALUES(?,?,?,?,?,?,?,?,?)',
                            ('polymarket_gamma',eid,mid,question,str(outcome)[:100],seen,price,liq,with_url))
                        count+=1
        state='OK'
    except Exception as ex:
        state='ERROR';source_state(c,'polymarket_gamma',state,0,type(ex).__name__,at)
        return {'provider':'polymarket_gamma','state':state,'new_outcome_quotes':0,'error':type(ex).__name__}
    source_state(c,'polymarket_gamma',state,count,'Prediction market shares, NOT bookmaker odds',at)
    return {'provider':'polymarket_gamma','state':state,'new_outcome_quotes':count,
      'warning':'Not sportsbook bookmaker data, not usable as sportsbook ROI without independent mapping.'}

KEYWORDS=('arbitrage','pricing','market','volatility','option','sports','betting','prediction',
          'forecast','portfolio','asset','finance','liquidity','risk','probability',
          'equity','stock','derivative','cryptocurrency','crypto','trading','exchange')

def relevant(title):
    lower=str(title).lower()
    return any(re.search(r'\b'+re.escape(w)+r'\b',lower) for w in KEYWORDS)

def ingest_crossref(c,at=None,loader=download):
    setup(c);seen=stamp(at);count=0;fail=[]
    for q in ('financial market forecasting','sports betting odds probability'):
        params=urllib.parse.urlencode({'query.bibliographic':q,'rows':15,'sort':'published','order':'desc'})
        url='https://api.crossref.org/works?'+params
        try:
            body=loader(url)
            rows=body['message']['items']
            with c:
                for row in rows[:15]:
                    titles=row.get('title') or []
                    title=str(titles[0] if titles else '').strip()[:300];doi=str(row.get('DOI') or '')
                    if not title or not relevant(title) or not doi or len(doi)>250:continue
                    ref='https://doi.org/'+urllib.parse.quote(doi,safe='/._-')
                    pub=row.get('published') or {};parts=pub.get('date-parts') or []
                    day='-'.join(str(n) for n in parts[0]) if parts else None
                    cur=c.execute('INSERT OR IGNORE INTO v4_public_research(id,source,title,doi,published,link,first_seen_at) VALUES(?,?,?,?,?,?,?)',
                        (digest(doi.lower()),'crossref',title,doi,day,ref,seen))
                    count+=int(cur.rowcount>0)
        except Exception as ex:fail.append(type(ex).__name__)
    state='PARTIAL' if fail and count else 'ERROR' if fail else 'OK'
    source_state(c,'crossref',state,count,'Metadata discovery only',at)
    return {'provider':'crossref','state':state,'new_papers':count,'failures':len(fail)}

def ingest_openalex(c,at=None,loader=download):
    """Low-volume keyless scholarly metadata; no abstract treated as verified full text."""
    setup(c);seen=stamp(at);count=0;failed=0
    for topic in ('statistical arbitrage','sports betting probability'):
        params=urllib.parse.urlencode({'search':topic,'per-page':12,'sort':'publication_date:desc',
          'select':'id,display_name,doi,publication_date'})
        url='https://api.openalex.org/works?'+params
        try:
            response=loader(url)
            if not isinstance(response,dict) or not isinstance(response.get('results'),list):raise ValueError('Missing results')
            with c:
                for row in response['results'][:12]:
                    title=str(row.get('display_name') or '').strip()[:300]
                    doi=str(row.get('doi') or '').replace('https://doi.org/','').strip()
                    if not title or not doi or not relevant(title):continue
                    ref='https://doi.org/'+urllib.parse.quote(doi,safe='/._-')
                    cur=c.execute('INSERT OR IGNORE INTO v4_public_research(id,source,title,doi,published,link,first_seen_at) VALUES(?,?,?,?,?,?,?)',
                         (digest(doi.lower()),'openalex',title,doi,str(row.get('publication_date') or ''),ref,seen))
                    count+=int(cur.rowcount>0)
        except Exception:failed+=1
    state='PARTIAL' if failed and count else 'ERROR' if failed else 'OK'
    source_state(c,'openalex',state,count,'Metadata only; low-volume anonymous API',at)
    return {'provider':'openalex','state':state,'new_papers':count,'failures':failed}

def ingest_cftc(c,at=None,loader=download):
    """CFTC public weekly report: as-of Tuesday; first observed is recorded separately."""
    setup(c);seen=stamp(at)
    params=urllib.parse.urlencode({'$limit':60,'$order':'report_date_as_yyyy_mm_dd DESC'})
    url='https://publicreporting.cftc.gov/resource/gpe5-46if.json?'+params
    try:
        payload=loader(url)
        if not isinstance(payload,list):raise ValueError('Expected COT rows')
        inserted=0
        with c:
            for row in payload[:60]:
                day=str(row.get('report_date_as_yyyy_mm_dd') or '')[:10]
                contract=str(row.get('market_and_exchange_names') or row.get('contract_market_name') or '').strip()[:250]
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day) or not contract:continue
                # Weekly data may be revised; preserve newly seen vintages only if changed.
                packed=json.dumps(row,sort_keys=True,ensure_ascii=False)[:6000]
                last=c.execute('SELECT payload FROM v4_public_cot WHERE source=? AND contract=? AND report_date=? ORDER BY first_seen_at DESC LIMIT 1',('cftc',contract,day)).fetchone()
                if last and last[0]==packed:continue
                c.execute('INSERT INTO v4_public_cot VALUES(?,?,?,?,?,?)',('cftc',contract,day,seen,packed,url));inserted+=1
        state='OK' if payload else 'EMPTY';source_state(c,'cftc',state,inserted,'Weekly positioning; Tuesday reference vs Friday publication',at)
        return {'provider':'cftc','state':state,'new_position_rows':inserted}
    except Exception as ex:
        source_state(c,'cftc','ERROR',0,type(ex).__name__,at)
        return {'provider':'cftc','state':'ERROR','new_position_rows':0,'error':type(ex).__name__}

def ingest_gleif(c,at=None,loader=download):
    """Only exact unique US entity match can qualify for an official parent LEI lookup.
    Fuzzy candidate matches or market-correlation patterns cannot become ownership edges.
    """
    setup(c);seen=stamp(at);matched=0;linked=0;errors=0
    names=('Apple Inc.','Microsoft Corporation','NVIDIA Corporation')
    for name in names:
        params=urllib.parse.urlencode({'filter[entity.legalName]':name,'page[size]':10})
        url='https://api.gleif.org/api/v1/lei-records?'+params
        try:
            raw=loader(url)
            rows=raw.get('data') or []
            if not isinstance(rows,list):raise ValueError('Missing GLEIF entity array')
            good=[]
            for rec in rows:
                a=rec.get('attributes') or {};en=a.get('entity') or {};reg=a.get('registration') or {}
                legal=en.get('legalName') or {};actual=str(legal.get('name') or '').strip().casefold()
                country=str((en.get('legalAddress') or {}).get('country') or '').upper()
                if actual==name.casefold() and country=='US' and en.get('status')=='ACTIVE' and reg.get('status')=='ISSUED':
                    lei=str(rec.get('id') or '')
                    if re.fullmatch(r'[A-Z0-9]{20}',lei):good.append(lei)
            if len(set(good))!=1:continue
            lei=good[0]
            with c:c.execute('INSERT OR IGNORE INTO v4_public_lei_entities VALUES(?,?,?,?,?)',(lei,name,'US',seen,url))
            matched+=1
            relurl='https://api.gleif.org/api/v1/lei-records/'+lei+'/direct-parent'
            try:
                parent=loader(relurl)
                parent_lei=str((parent.get('data') or {}).get('id') or '')
                if re.fullmatch(r'[A-Z0-9]{20}',parent_lei) and parent_lei!=lei:
                    with c:c.execute('INSERT OR IGNORE INTO v4_public_lei_relations VALUES(?,?,?,?,?)',(lei,parent_lei,'DIRECT_PARENT',seen,relurl));linked+=1
            except Exception:
                # No parent can be legitimately reported, or temporarily unavailable.
                pass
        except Exception:errors+=1
    state='PARTIAL' if errors and matched else 'ERROR' if errors else 'OK' if matched else 'EMPTY'
    source_state(c,'gleif',state,matched,f'Exact entity matches {matched}, observed parent links {linked}',at)
    return {'provider':'gleif','state':state,'verified_identity_matches':matched,'parent_links':linked,'failures':errors}

def summary(c):
    def count(name):
        try:return c.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
        except sqlite3.Error:return 0
    try:
        rows=c.execute('SELECT provider,checked_at,status,new_records,detail FROM v4_public_source_state ORDER BY provider').fetchall()
    except sqlite3.Error:rows=[]
    status={r[0]:{'checked_at':r[1],'state':r[2],'new_records_last_poll':r[3],'detail':r[4]} for r in rows}
    return {'providers':[{'name':name,'purpose':data['kind'],'branch':data['branch'],
       'registration':data['registration'],'state':status.get(name,{}).get('state','NOT_CHECKED'),
       'last_check':status.get(name,{}).get('checked_at')} for name,data in PROVIDERS.items()],
       'annual_macro_vintages':count('v4_public_macro'),
       'weekly_cftc_vintages':count('v4_public_cot'),
       'verified_lei_entities':count('v4_public_lei_entities'),
       'gleif_direct_parent_relations':count('v4_public_lei_relations'),
       'crypto_spot_reference_snapshots':count('v4_public_crypto'),
       'prediction_market_reference_quotes':count('v4_public_market_refs'),
       'new_research_metadata':count('v4_public_research'),
       'bookmaker_quotes_from_public_sources':0,
       'warning':'Prediction market prices are not sportsbook bookmaker odds; annual data is not intraday.'}
