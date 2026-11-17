#!/usr/bin/env python3
"""ORACLE pre-move event ledger v2.5. Timestamped *uncalibrated* hypotheses.

Does NOT predict guaranteed gains, place trades, or calculate fake confidence.
Detects a potentially relevant positive *public* event only when the token has
not already exhibited a strong price move; captures latest observed spot quote.
All outcome labels are computed strictly after the 24-hour observation horizon.
"""
from __future__ import annotations
import datetime as dt
import json
import math
import os
import re
import sqlite3
import time
import traceback
from pathlib import Path
import collect

UTC=dt.timezone.utc
HORIZON=dt.timedelta(hours=24)
LEVELS=(10,15,20,30,40)
INTERVAL=max(180,min(1800,int(os.getenv('ORACLE_PRESIGNAL_INTERVAL','300'))))
MIN_VOLUME=max(100_000,float(os.getenv('ORACLE_PRESIGNAL_MIN_VOLUME','1000000')))
MAX_DAILY_CHANGE=float(os.getenv('ORACLE_PRESIGNAL_MAX_PRIOR_24H','4.0'))
MAX_HOURLY_CHANGE=float(os.getenv('ORACLE_PRESIGNAL_MAX_PRIOR_1H','1.5'))
MAX_EVENT_AGE_MIN=40
MAX_ARTICLE_AGE_H=3
COIN_ALIASES={'BMT':('bubblemaps',),'DGB':('digibyte',)}
# Keyword evidence only. Never claims to infer an actual causal impact.
POSITIVE={'listing':('will list','new listing','listing announcement','spot listing', 'listed on', 'exchange listing'),
          'partnership':('strategic partnership','strategic alliance','partnership announced','signed partnership'),
          'launch':('mainnet launch','network upgrade','protocol upgrade','hard fork','product launch'),
          'institutional':('institutional investment','strategic investment','funding round','token acquisition'),
          'supply':('token burn','buyback','supply reduction'),
          'approval':('regulatory approval','etf approved','license approval')}
NEGATIVE=('hack','hacked','exploit','stolen','lawsuit','investigation','rug pull','delist',
          'token unlock','scam','fraud','arrest','alleged','accused','vulnerability')

def utcnow():return dt.datetime.now(UTC).isoformat(timespec='seconds')
def parse(ts):
    x=dt.datetime.fromisoformat(str(ts).replace('Z','+00:00'))
    if x.tzinfo is None:raise ValueError('Missing timezone')
    return x.astimezone(UTC)
def stamp(x):return x.astimezone(UTC).isoformat(timespec='seconds')
def table_names(c):return {x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS ps_hypotheses(
      id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, news_key TEXT NOT NULL,
      event_cluster TEXT NOT NULL, created_at TEXT NOT NULL, news_first_seen TEXT NOT NULL,
      published_at TEXT, headline TEXT NOT NULL, news_url TEXT NOT NULL, category TEXT NOT NULL,
      observed_quote_at TEXT NOT NULL, entry_price REAL NOT NULL, prior_24h REAL,
      prior_1h REAL NOT NULL, quote_volume_24h REAL NOT NULL,
      targets_json TEXT NOT NULL, evidence_json TEXT NOT NULL, horizon_end TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'PAPER_OPEN', forward_peak REAL, forward_close REAL,
      samples INTEGER NOT NULL DEFAULT 0, hour_bins INTEGER NOT NULL DEFAULT 0,
      hits_json TEXT, settled_at TEXT,
      UNIQUE(symbol,event_cluster));
    CREATE INDEX IF NOT EXISTS ps_recent ON ps_hypotheses(created_at DESC);
    CREATE INDEX IF NOT EXISTS ps_pending ON ps_hypotheses(state,horizon_end);
    CREATE TABLE IF NOT EXISTS ps_screened(
      id INTEGER PRIMARY KEY AUTOINCREMENT, screened_at TEXT NOT NULL, symbol TEXT NOT NULL,
      news_key TEXT NOT NULL, reason TEXT NOT NULL, headline TEXT NOT NULL,
      UNIQUE(symbol,news_key,reason));
    CREATE TABLE IF NOT EXISTS ps_event_triage(
      symbol TEXT NOT NULL,news_key TEXT NOT NULL,first_seen_at TEXT NOT NULL,
      reviewed_at TEXT NOT NULL,headline TEXT NOT NULL,url TEXT NOT NULL,
      event_kind TEXT NOT NULL,direction TEXT NOT NULL,link_type TEXT NOT NULL,
      decision_reason TEXT NOT NULL,eligible_pre_move INTEGER NOT NULL,
      review_mode TEXT NOT NULL,PRIMARY KEY(symbol,news_key));
    CREATE INDEX IF NOT EXISTS ps_triage_time ON ps_event_triage(reviewed_at DESC);
    CREATE TABLE IF NOT EXISTS ps_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, job TEXT NOT NULL,
      summary TEXT NOT NULL);
    ''');c.commit()

def classify(text):
    t=str(text).casefold()
    if any(re.search(r'(?<!\w)'+re.escape(k)+r'(?!\w)',t) for k in NEGATIVE):return 'BLOCKED_NEGATIVE'
    for name,terms in POSITIVE.items():
        if any(re.search(r'(?<!\w)'+re.escape(k)+r'(?!\w)',t) for k in terms):return name
    return 'NO_CATALYST'

# A triage is a watch/research classification, NOT evidence of future price direction.
# No sentiment number or probability is invented from a headline.
_PROJECTS={
 'BTC':('bitcoin',), 'ETH':('ethereum',), 'SOL':('solana',), 'ADA':('cardano',),
 'ARB':('arbitrum',), 'BMT':('bubblemaps',), 'DGB':('digibyte',),
 'MASK':('mask network',), 'AVAX':('avalanche',), 'SUI':('sui',),
 'XRP':('xrp','ripple'), 'DOGE':('dogecoin',), 'LINK':('chainlink',),
 'DOT':('polkadot',), 'AAVE':('aave',), 'UNI':('uniswap',)
}
_MARKET_RISKS=('fake liquidity','wash trad','fake volume','price manipulation',
               'rug pull','rugpull','pump and dump','exploit','hacked','fraud',
               'investigation','alleged','accused','stolen','scam', 'vulnerability')
_MACRO_RISKS=('rate-hike','rate hike','iran','oil above','stocks lower','war ',
              'sanctions','selloff','slides toward','slides towards','plunges')
_OPINION=('would you ', 'should you ', 'what if ', 'opinion:', 'how to ', '?')
_ADVOCACY=('pide que', 'urges ', 'calls on ', 'asks ', 'demands ', 'requests ',
           'proposes ', 'seeks ', 'petición', 'propuesta', 'призывает', 'предлагает')
_DIRECT_ACTION=('announces','announced','confirms','confirmed','launches',
                'launched','unveils','unveiled','releases','released','signed',
                'signs','completes','completed','burns','burned','upgrades',
                'запускает','объявляет','запустил','обновление сети',
                'anuncia','lanza','presenta','升级','推出','发布')
_INDIRECT_SIGNALS=(' on bitcoin', ' on ethereum', ' for ethereum',
                   ' for bitcoin', ' for arbitrum', ' на ethereum',
                   ' on solana', ' for solana', 'usdt on bitcoin',
                   ' bring bitcoin', ' bring ethereum', ' offering bitcoin',
                   ' ofrecer bitcoin','bitcoin and stock trading',
                   ' ahead of november','ahead of launch')


def triage_event(headline,symbol):
    t=str(headline or '').casefold()
    sym=str(symbol).upper().removesuffix('USDT')
    aliases=_PROJECTS.get(sym,())
    main_prefix=t.split(':',1)[0][:100]
    # Avoid assigning a positive outlook to a host chain because an unrelated
    # application uses that chain (Fhenix on Arbitrum ≠ ARB value capture).
    # When Bubblemaps REPORTS third-party wrongdoing, distinguish its
    # investigative product activity from allegations AGAINST the project.
    if sym=='BMT' and 'bubblemaps' in t and any(x in t for x in
        ('bubblemaps exposes','bubblemaps uncovers','bubblemaps reveals',
         'bubblemaps investigates','bubblemaps tracks')):
        return {'kind':'PROJECT_INVESTIGATIVE_ACTIVITY','direction':'UNKNOWN',
                'link':'PROJECT_NEWS_WITH_UNVERIFIED_TOKEN_EFFECT',
                'reason':'PRODUCT_ATTENTION_NOT_VERIFIED_TOKEN_DEMAND','qualified':False}
    if any(x in t for x in _MARKET_RISKS):
        return {'kind':'MARKET_INTEGRITY_RISK','direction':'RISK',
                'link':'MENTION_ONLY','reason':'MARKET_RISK_NOT_BULLISH','qualified':False}
    if any(x in t for x in _MACRO_RISKS):
        return {'kind':'MACRO_OR_GEOPOLITICAL_RISK','direction':'UNCERTAIN_OR_NEGATIVE',
                'link':'INDIRECT','reason':'MACRO_RISK_REQUIRES_CROSS_ASSET_ANALYSIS','qualified':False}
    if any(x in t for x in _OPINION):
        return {'kind':'EDITORIAL_OR_QUESTION','direction':'UNKNOWN',
                'link':'MENTION_ONLY','reason':'NOT_A_NEW_PUBLIC_FACT','qualified':False}
    if any(x in t for x in _ADVOCACY):
        return {'kind':'POLICY_PROPOSAL_OR_LOBBYING','direction':'UNKNOWN',
                'link':'INDIRECT','reason':'NOT_A_CONFIRMED_REGULATORY_DECISION','qualified':False}
    if any(x in t for x in _INDIRECT_SIGNALS) or ('robinhood' in t and sym in ('BTC','ETH')):
        return {'kind':'THIRD_PARTY_OR_INFRASTRUCTURE_EVENT','direction':'UNCERTAIN',
                'link':'INDIRECT','reason':'NO_DIRECT_TOKEN_VALUE_CAPTURE_PROVEN','qualified':False}
    # Named third-party products launched ON the named chain are not a direct
    # change in the blockchain's native token economics.
    if ('fhenix' in t and sym in ('ETH','ARB')) or ('utexo' in t and sym=='BTC'):
        return {'kind':'THIRD_PARTY_OR_INFRASTRUCTURE_EVENT','direction':'UNCERTAIN',
                'link':'INDIRECT','reason':'NO_DIRECT_TOKEN_VALUE_CAPTURE_PROVEN','qualified':False}
    cat=classify(headline)
    if cat=='BLOCKED_NEGATIVE':
        return {'kind':'POTENTIAL_NEGATIVE_EVENT','direction':'RISK',
                'link':'UNKNOWN','reason':'NEGATIVE_EVENT_NOT_BULLISH','qualified':False}
    if cat=='NO_CATALYST':
        return {'kind':'UNCLASSIFIED_NEWS_MENTION','direction':'UNKNOWN',
                'link':'MENTION_ONLY','reason':'NO_DIRECT_CATALYST_VERIFIED','qualified':False}
    # Directness is a conservative headlined-based heuristic, not a verified
    # economic causal relationship. Cannot rely on a bare short ticker.
    # The token must act as the subject, not merely be named elsewhere.
    # For instance, 'DigiByte announces ... with Cardano' is NOT a direct ADA
    # action. This is still a headline heuristic, never causal verification.
    direct=any(re.search(r'(?<!\w)'+re.escape(a)+r'(?!\w)(?:\s+\w+){0,4}\s+(?:'+
                         '|'.join(re.escape(v) for v in _DIRECT_ACTION)+r')(?!\w)',
                         main_prefix) for a in aliases)
    if cat=='listing' and aliases and any(a in t for a in aliases):
        direct = direct or bool(re.search(r'(?:will list|listing of|to list|spot listing|new listing)',t))
    if cat=='approval' and aliases and any(a in t for a in aliases):
        direct=direct or bool(re.search(r'(?:approves?|approved|approval for).{0,100}(?:'+ '|'.join(re.escape(a) for a in aliases)+r')',t))
    if not direct:
        return {'kind':'EVENT_WITH_UNVERIFIED_TOKEN_IMPACT','direction':'UNCERTAIN',
                'link':'INDIRECT_OR_UNKNOWN','reason':'ECONOMIC_LINK_UNVERIFIED','qualified':False}
    return {'kind':cat.upper(),'direction':'POTENTIALLY_POSITIVE',
            'link':'DIRECT_HEADLINE_MENTION','reason':'POTENTIAL_DIRECT_EVENT_UNVALIDATED','qualified':True}


def record_triage(c,at,sym,key,seen,title,url,ev,retrospective=False):
    with c:
        c.execute('''INSERT INTO ps_event_triage(
           symbol,news_key,first_seen_at,reviewed_at,headline,url,
           event_kind,direction,link_type,decision_reason,eligible_pre_move,review_mode)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(symbol,news_key) DO UPDATE SET
             event_kind=excluded.event_kind,direction=excluded.direction,
             link_type=excluded.link_type,decision_reason=excluded.decision_reason,
             eligible_pre_move=excluded.eligible_pre_move''',
           (sym,key,seen,at,str(title)[:500],str(url or '')[:1000],
            ev['kind'],ev['direction'],ev['link'],ev['reason'],int(ev['qualified']),
            'RETROSPECTIVE_REVIEW_ONLY' if retrospective else 'REALTIME_SCREENING'))


def review_prior_rejections(c,at=None):
    """Research old rejections, explicitly never create backdated hypotheses."""
    at=at or utcnow()
    if not {'ps_screened','oc_news'}.issubset(table_names(c)):return 0
    rows=c.execute('''SELECT s.symbol,s.news_key,n.first_seen_at,s.headline,n.url
      FROM ps_screened s LEFT JOIN oc_news n ON s.news_key=n.news_key
      WHERE NOT EXISTS(SELECT 1 FROM ps_event_triage r
        WHERE r.symbol=s.symbol AND r.news_key=s.news_key)
      ORDER BY s.id DESC LIMIT 1000''').fetchall()
    for sym,key,seen,headline,url in rows:
        record_triage(c,at,sym,key,seen or at,headline,url,
                      triage_event(headline,sym),True)
    return len(rows)

def candidate_articles(c,at):
    if not {'oi_mentions','oc_news','mw_symbols','mw_quotes'}.issubset(table_names(c)):return []
    # Use the time ORACLE FIRST SAW the article. Older articles may be evidence
    # but cannot become new pre-move predictions after deployment/backfill.
    minseen=stamp(parse(at)-dt.timedelta(minutes=MAX_EVENT_AGE_MIN))
    rows=c.execute('''SELECT n.news_key,n.first_seen_at,n.published_at,n.title,n.url,
       m.entity,n.domain,n.source,ci.cluster_id
       FROM oi_mentions m JOIN oc_news n ON m.news_key=n.news_key
       LEFT JOIN oi_cluster_items ci ON ci.news_key=n.news_key
       WHERE m.entity LIKE 'crypto:%' AND n.first_seen_at>=? AND n.first_seen_at<=?
       ORDER BY n.first_seen_at ASC LIMIT 500''',(minseen,at)).fetchall()
    out=[];keys=set()
    for k,seen,pub,title,url,entity,domain,src,cluster in rows:
        sym=entity.removeprefix('crypto:').upper()+'USDT'
        item=(sym,k,seen,pub,title,url,domain,src,cluster)
        if (sym,k) not in keys:out.append(item);keys.add((sym,k))
    # Narrow alias recovery for missed entities. Bare token abbreviations such
    # as 'BMT' can be ambiguous; require an unambiguous project name or cashtag.
    for symbol,aliases in COIN_ALIASES.items():
        for k,seen,pub,title,url,domain,src in c.execute('''SELECT news_key,first_seen_at,
            published_at,title,url,domain,source FROM oc_news
            WHERE first_seen_at>=? AND first_seen_at<=? ORDER BY first_seen_at LIMIT 500''',(minseen,at)):
            if (symbol+'USDT',k) in keys:continue
            if not (any(re.search(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',title,re.I) for alias in aliases)
                    or re.search(r'(?<!\w)\$'+re.escape(symbol)+r'(?!\w)',title,re.I)):
                continue
            cluster_row=c.execute('SELECT cluster_id FROM oi_cluster_items WHERE news_key=?',(k,)).fetchone()
            out.append((symbol+'USDT',k,seen,pub,title,url,domain,src,cluster_row[0] if cluster_row else None))
            keys.add((symbol+'USDT',k))
    return out

def nearest_one_hour(c,symbol,at):
    # Strictly prior 45–90 minute quote; if missing, abstain.
    a=parse(at)
    start=stamp(a-dt.timedelta(minutes=90));end=stamp(a-dt.timedelta(minutes=45))
    return c.execute('''SELECT observed_at,price FROM mw_quotes WHERE symbol=?
       AND observed_at>=? AND observed_at<=? AND price>0 ORDER BY observed_at DESC LIMIT 1''',
       (symbol,start,end)).fetchone()

def quote_asof(c,symbol,at):
    return c.execute('''SELECT observed_at,price,quote_volume_24h,change_24h_pct
       FROM mw_quotes WHERE symbol=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1''',
       (symbol,at)).fetchone()

def record_screen(c,sym,key,reason,head,at):
    with c:c.execute('INSERT OR IGNORE INTO ps_screened(screened_at,symbol,news_key,reason,headline) VALUES(?,?,?,?,?)',
                     (at,sym,key,reason,str(head)[:500]))

def screen(c,at=None):
    at=at or utcnow();t=parse(at);result={'candidate_articles':0,'hypotheses_created':0,'rejected':{}}
    if not {'oi_mentions','oc_news','mw_symbols','mw_quotes','oi_cluster_items'}.issubset(table_names(c)):
        return {**result,'error':'REQUIRED_NEWS_OR_QUOTE_TABLE_MISSING'}
    for sym,key,seen,pub,headline,url,domain,src,cluster in candidate_articles(c,at):
        result['candidate_articles']+=1
        reason=None
        eligible=c.execute('SELECT eligible FROM mw_symbols WHERE symbol=?',(sym,)).fetchone()
        if not eligible or not eligible[0]:reason='NOT_LISTED_CRYPTO_SPOT'
        ev=triage_event(headline,sym)
        record_triage(c,at,sym,key,seen,headline,url,ev)
        cat=classify(headline)
        if not reason and not ev['qualified']:reason=ev['reason']
        if not reason and (t-parse(seen)).total_seconds()>MAX_EVENT_AGE_MIN*60:reason='OLD_NEWS_FIRST_SEEN'
        try:
            if not reason and pub and parse(pub)>parse(seen):reason='PUBLICATION_TIME_IN_FUTURE'
            if not reason and pub and (parse(seen)-parse(pub)).total_seconds()>MAX_ARTICLE_AGE_H*3600:
                reason='ALREADY_OLD_PUBLICATION'
        except (ValueError,TypeError):reason='BAD_NEWS_TIMESTAMP'
        q=quote_asof(c,sym,at)
        if not reason and not q:reason='NO_MARKET_PRICE'
        if not reason:
            qat,price,vol,change=q
            if parse(qat)<parse(seen):reason='NO_QUOTE_AFTER_NEWS'
            elif (t-parse(qat)).total_seconds()>15*60:reason='STALE_MARKET_PRICE'
            elif not math.isfinite(float(price)) or float(price)<=0:reason='BAD_MARKET_PRICE'
            elif float(vol)<MIN_VOLUME:reason='LOW_LIQUIDITY'
            elif change is None or float(change)>MAX_DAILY_CHANGE:reason='ALREADY_MOVED_24H'
            elif float(change)<-25:reason='ABNORMAL_24H_DROP'
        prior=nearest_one_hour(c,sym,at) if not reason else None
        if not reason and not prior:reason='NO_PRIOR_HOUR_PRICE'
        prior_delta=None
        if not reason:
            prior_delta=100*(float(q[1])/float(prior[1])-1)
            if prior_delta>MAX_HOURLY_CHANGE:reason='ALREADY_MOVED_1H'
        if reason:
            record_screen(c,sym,key,reason,headline,at)
            result['rejected'][reason]=result['rejected'].get(reason,0)+1
            continue
        # Only one hypothesis per headline cluster and token; retries are safe.
        eventkey='cluster:'+str(cluster) if cluster is not None else 'article:'+key
        # All analyst evidence must exist as-of the created_at timestamp.
        evidence={'news_first_seen':seen,'source':src,'domain':domain,'url':url,
                  'headline':headline,'published_at':pub,'class_keyword':cat,
                  'observed_price_at':q[0],'change_24h_pct':q[3],
                  'prior_1h_pct':round(prior_delta,4),'quote_volume_24h':q[2],
                  'independent_sources_verified':False,'article_content_verified':False,
                  'calibrated_probability':None,'quality':'UNVALIDATED_EVENT_RULE',
                  'method':'public_headline_keyword_before_large_move'}
        end=stamp(t+HORIZON)
        with c:
            cur=c.execute('''INSERT OR IGNORE INTO ps_hypotheses(
              symbol,news_key,event_cluster,created_at,news_first_seen,published_at,
              headline,news_url,category,observed_quote_at,entry_price,prior_24h,
              prior_1h,quote_volume_24h,targets_json,evidence_json,horizon_end,state)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PAPER_OPEN')''',
              (sym,key,eventkey,at,seen,pub,headline,url,cat,q[0],float(q[1]),q[3],
               prior_delta,float(q[2]),json.dumps(LEVELS),json.dumps(evidence,ensure_ascii=False),end))
            result['hypotheses_created']+=int(cur.rowcount>0)
    with c:c.execute('INSERT INTO ps_runs(at,job,summary) VALUES(?,?,?)',
                     (at,'screen',json.dumps(result,ensure_ascii=False)))
    return result

def settle(c,at=None):
    at=at or utcnow();result={'complete':0,'incomplete':0,'checked':0}
    for id_,sym,created,end,entry in c.execute('''SELECT id,symbol,created_at,horizon_end,entry_price
       FROM ps_hypotheses WHERE state='PAPER_OPEN' AND horizon_end<=? ORDER BY id LIMIT 200''',(at,)).fetchall():
        data=c.execute('''SELECT observed_at,price FROM mw_quotes WHERE symbol=?
           AND observed_at>? AND observed_at<=? AND price>0 ORDER BY observed_at''',(sym,created,end)).fetchall()
        result['checked']+=1
        hours={int((parse(ts)-parse(created)).total_seconds()//3600) for ts,p in data}
        near_start=data and parse(data[0][0])<=parse(created)+dt.timedelta(hours=1)
        near_end=data and parse(data[-1][0])>=parse(end)-dt.timedelta(hours=1)
        enough=len(data)>=16 and len(hours)>=16 and near_start and near_end
        peak=max((float(x[1]) for x in data),default=None)
        last=float(data[-1][1]) if data else None
        hits={str(p):int(peak>=entry*(1+p/100)) for p in LEVELS} if enough else None
        state='PAPER_SETTLED' if enough else 'INCOMPLETE'
        with c:c.execute('''UPDATE ps_hypotheses SET state=?,forward_peak=?,forward_close=?,
          samples=?,hour_bins=?,hits_json=?,settled_at=? WHERE id=?''',
          (state,peak,last,len(data),len(hours),json.dumps(hits) if hits else None,at,id_))
        result['complete' if enough else 'incomplete']+=1
    with c:c.execute('INSERT INTO ps_runs(at,job,summary) VALUES(?,?,?)',(at,'settle',json.dumps(result)))
    return result

def report(c):
    setup(c)
    return {'total_hypotheses':c.execute('SELECT COUNT(*) FROM ps_hypotheses').fetchone()[0],
            'open':c.execute("SELECT COUNT(*) FROM ps_hypotheses WHERE state='PAPER_OPEN'").fetchone()[0],
            'settled':c.execute("SELECT COUNT(*) FROM ps_hypotheses WHERE state='PAPER_SETTLED'").fetchone()[0],
            'screened':c.execute('SELECT COUNT(*) FROM ps_screened').fetchone()[0],
            'top_rejection_reasons':c.execute('SELECT reason,COUNT(*) FROM ps_screened GROUP BY reason ORDER BY 2 DESC LIMIT 10').fetchall(),
            'latest':c.execute('SELECT symbol,created_at,entry_price,prior_24h,category,state,hits_json FROM ps_hypotheses ORDER BY id DESC LIMIT 12').fetchall(),
            'news_events_reviewed':c.execute('SELECT COUNT(*) FROM ps_event_triage').fetchone()[0],
            'event_categories':c.execute('SELECT event_kind,COUNT(*) FROM ps_event_triage GROUP BY event_kind ORDER BY 2 DESC LIMIT 12').fetchall(),
            'mode':'EVENT_FIRST_UNVALIDATED_PAPER_NOT_TRADE'}

def dashboard(db):
    """Read-only JSON, safe to call on dashboard request thread."""
    res={'module':'presignal','research_only':True,'calibrated_probability':None,
         'signal_policy':'PRE_MOVE_PUBLIC_EVENT_RESEARCH_NOT_A_BUY_CALL',
         'targets_pct':list(LEVELS),'alerts':[], 'reviews':[], 'screened':[], 'totals':{}}
    if not Path(db).exists():return res
    c=sqlite3.connect('file:'+Path(db).resolve().as_posix()+'?mode=ro',uri=True,timeout=4)
    try:
        if 'ps_hypotheses' not in table_names(c):return res
        labels=('symbol','created_at','headline','url','category','entry_price','prior_24h',
                'prior_1h','volume_24h','horizon_end','state','forward_peak','forward_close',
                'hits','evidence')
        rows=c.execute('''SELECT symbol,created_at,headline,news_url,category,entry_price,
           prior_24h,prior_1h,quote_volume_24h,horizon_end,state,forward_peak,
           forward_close,hits_json,evidence_json FROM ps_hypotheses
           ORDER BY created_at DESC,id DESC LIMIT 65''').fetchall()
        for r in rows:
            obj=dict(zip(labels,r))
            for key in ('hits','evidence'):
                try:obj[key]=json.loads(obj[key]) if obj[key] else None
                except (ValueError,TypeError):obj[key]=None
            res['alerts'].append(obj)
        res['totals']={'open':c.execute("SELECT COUNT(*) FROM ps_hypotheses WHERE state='PAPER_OPEN'").fetchone()[0],
                       'settled':c.execute("SELECT COUNT(*) FROM ps_hypotheses WHERE state='PAPER_SETTLED'").fetchone()[0],
                       'insufficient':c.execute("SELECT COUNT(*) FROM ps_hypotheses WHERE state='INCOMPLETE'").fetchone()[0]}
        res['screened']=[{'reason':x[0],'count':x[1]} for x in c.execute('SELECT reason,COUNT(*) FROM ps_screened GROUP BY reason ORDER BY 2 DESC LIMIT 10')]
        if 'ps_event_triage' in table_names(c):
            res['totals']['news_events_reviewed']=c.execute('SELECT COUNT(*) FROM ps_event_triage').fetchone()[0]
            labels=('symbol','first_seen_at','headline','url','event_kind','direction','link_type','decision_reason','eligible_pre_move','review_mode')
            res['reviews']=[dict(zip(labels,x)) for x in c.execute('''SELECT symbol,first_seen_at,headline,url,event_kind,direction,
                link_type,decision_reason,eligible_pre_move,review_mode
                FROM ps_event_triage ORDER BY reviewed_at DESC,news_key DESC LIMIT 40''')]
    finally:c.close()
    return res

def main():
    import argparse
    p=argparse.ArgumentParser();p.add_argument('action',choices=('run','once','status','settle'))
    args=p.parse_args();c=collect.db_open();setup(c);review_prior_rejections(c)
    if args.action=='status':out=report(c)
    elif args.action=='settle':out=settle(c)
    elif args.action=='once':out={'screen':screen(c),'settlement':settle(c)}
    else:
        print('ORACLE PreSignal 2.5 started — event FIRST, movement SECOND. PAPER only.',flush=True)
        while True:
            try:print(json.dumps({'screen':screen(c),'settle':settle(c)},ensure_ascii=False),flush=True)
            except Exception as ex:
                print(type(ex).__name__,str(ex)[:250],flush=True);traceback.print_exc()
                try:c.close()
                except Exception:pass
                c=collect.db_open();setup(c)
            time.sleep(INTERVAL)
        return
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
