"""Descriptive bookmaker market consensus, NOT a forecast or betting advice.

Normalize two- and three-way outcome markets and remove vig per bookmaker.
Never turn bookmaker implied price into self-certified betting probability.
"""
import json
import math
import statistics
import collect

def normalize(odds):
    if not isinstance(odds,dict) or len(odds) not in (2,3):return None
    try:
        prices={str(k):float(v) for k,v in odds.items()}
        if any(not math.isfinite(v) or v<=1.01 or v>100 for v in prices.values()):return None
    except (TypeError,ValueError):return None
    implied={k:1/v for k,v in prices.items()};total=sum(implied.values())
    if not .9<total<1.4:return None
    return {k:v/total for k,v in implied.items()}

def extract(event):
    if not isinstance(event,dict):return None
    offers=[]
    for bookmaker in event.get('bookmakers',[]):
        for market in bookmaker.get('markets',[]):
            if market.get('key')!='h2h':continue
            odds={o.get('name'):o.get('price') for o in market.get('outcomes',[]) if o.get('name')}
            probs=normalize(odds)
            if probs:offers.append((bookmaker.get('key','unknown'),probs))
    if len(offers)<2:return None
    names=set(offers[0][1]);complete=[(bm,ps) for bm,ps in offers if set(ps)==names]
    if len(complete)<2:return None
    return {'event_id':event.get('id'),'sport':event.get('sport_key'),
        'commence_time':event.get('commence_time'), 'outcomes':{k:round(statistics.median(x[1][k] for x in complete),5) for k in names},
        'bookmaker_count':len(complete),'bookmakers':[x[0] for x in complete],
        'meaning':'Market consensus ONLY; not independent probability or betting advantage.'}

def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS market_consensus(
      event_id TEXT PRIMARY KEY,sport TEXT,updated_at TEXT NOT NULL,
      payload TEXT NOT NULL)''');c.commit()

def refresh(c):
    setup(c);updated=0
    for provider in ('nba_odds','sports_odds_soccer_epl','sports_odds_icehockey_nhl','sports_odds_americanfootball_nfl'):
        rows=c.execute('''SELECT r.payload FROM raw_snapshots r INNER JOIN
          (SELECT subject, MAX(id) AS iid FROM raw_snapshots WHERE provider=? GROUP BY subject) x
          ON r.id=x.iid''',(provider,)).fetchall()
        for (encoded,) in rows:
            try: d=extract(json.loads(encoded))
            except (ValueError,TypeError):continue
            if not d or not d['event_id']:continue
            with c:
                c.execute('''INSERT INTO market_consensus(event_id,sport,updated_at,payload)
                  VALUES(?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET
                  updated_at=excluded.updated_at,payload=excluded.payload''',
                  (d['event_id'],d['sport'],collect.stamp(),json.dumps(d,ensure_ascii=False)))
            updated+=1
    return updated
