#!/usr/bin/env python3
"""EVA R6: native, verified-only paper settlement; optional exact ESPN-final fallback.

No network access, no real orders, no changes to forecasts or bookmaker quotations.
"""
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import unicodedata
from urllib.parse import urlparse

UTC = dt.timezone.utc


def timestamp(v):
    if isinstance(v, dt.datetime):
        t = v
    else:
        t = dt.datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    if t.tzinfo is None:
        raise ValueError('naive UTC timestamp rejected')
    return t.astimezone(UTC)


def norm_team(s):
    """Only orthographic normalization. Never guess aliases/transliterations."""
    decomposed = unicodedata.normalize('NFKD', str(s))
    stripped = ''.join(ch for ch in decomposed if not unicodedata.combining(ch))
    return ' '.join(''.join(ch if ch.isalnum() else ' ' for ch in stripped.casefold()).split())


def verified_espn_final(c, original_id, kickoff, now):
    if not isinstance(original_id, str) or not original_id.startswith('v58|'):
        return None
    root = original_id.split('|', 1)[1]
    if not root:
        return None
    kk, at = timestamp(kickoff), timestamp(now)
    if at < kk + dt.timedelta(minutes=95):
        return None
    ev = c.execute('SELECT sport,home,away,kickoff_utc FROM v5_catalog WHERE root_id=?', (root,)).fetchone()
    if not ev or ev[0] != 'football' or ev[3] != kickoff:
        return None
    home, away = norm_team(ev[1]), norm_team(ev[2])
    if not home or not away or home == away:
        return None
    lo = (kk-dt.timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%S')
    hi = (kk+dt.timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%S')
    candidates=[]
    for date,h,a,hs,aw,source,url,seen,proof in c.execute('''
        SELECT date_utc,home,away,home_score,away_score,source,source_url,
               observed_at,final_provenance FROM v58_results
        WHERE sport='football' AND date_utc>=? AND date_utc<=?
          AND final_provenance='SOURCE_MARKED_FINAL' ''',(lo,hi)):
        if norm_team(h) != home or norm_team(a) != away:
            continue
        if source != 'ESPN_UNOFFICIAL_SCOREBOARD' or not isinstance(url,str):
            continue
        parsed = urlparse(url)
        if parsed.scheme!='https' or parsed.hostname!='site.api.espn.com' or not parsed.path.startswith('/apis/site/v2/sports/'):
            continue
        try:
            event_k = timestamp(date)
            observed = timestamp(seen)
            if abs((kk-event_k).total_seconds()) > 600:
                continue
            if not kk+dt.timedelta(minutes=95) <= observed <= at:
                continue
            home_score,away_score=int(hs),int(aw)
            if str(home_score)!=str(hs) or str(away_score)!=str(aw):
                continue
            if not (0 <= home_score <= 30 and 0 <= away_score <= 30):
                continue
        except (ValueError,TypeError,OverflowError):
            continue
        candidates.append((home_score,away_score,seen,'ESPN_UNOFFICIAL_SCOREBOARD_SOURCE_MARKED_FINAL'))
    # An identity must be UNIQUE. Do not choose a favorable result among duplicates.
    if len(candidates)!=1:
        return None
    return candidates[0]


def selftest():
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE v5_catalog (root_id TEXT,sport TEXT,home TEXT,away TEXT,kickoff_utc TEXT)')
    c.execute('CREATE TABLE v58_results (date_utc TEXT,home TEXT,away TEXT,home_score INTEGER,away_score INTEGER,source TEXT,source_url TEXT,observed_at TEXT,final_provenance TEXT,sport TEXT)')
    k='2026-11-10T16:00:00+00:00'; now='2026-11-11T02:00:00+00:00'
    c.execute('INSERT INTO v5_catalog VALUES (?,?,?,?,?)',('123','football','Café United','Hässle FC',k))
    row=(k,'Cafe United','Hassle FC',2,1,'ESPN_UNOFFICIAL_SCOREBOARD','https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard','2026-11-10T18:20:00+00:00','SOURCE_MARKED_FINAL','football')
    c.execute('INSERT INTO v58_results VALUES (?,?,?,?,?,?,?,?,?,?)',row)
    assert verified_espn_final(c,'v58|123',k,now)[:2] == (2,1)
    assert verified_espn_final(c,'v58|123',k,'2026-11-10T17:00:00+00:00') is None
    assert verified_espn_final(c,'v58|123','2026-11-10T17:00:00+00:00',now) is None
    c.execute('INSERT INTO v58_results VALUES (?,?,?,?,?,?,?,?,?,?)',row)
    assert verified_espn_final(c,'v58|123',k,now) is None
    c.close()
    print('EVA_SETTLEMENT_R6_SELFTEST PASS: verified final, timing, identity, duplicate rejection')


def run_once():
    sys.path.insert(0,'/app')
    import sports_free
    import oracle_v58_brain
    import oracle_v5_settlement
    original = oracle_v58_brain.resolve_final_for_portfolio
    counts={'espn_strict_fallback_used':0}
    def compatible_resolver(c, original_id, kick, now=None):
        prev=original(c,original_id,kick,now)
        if prev is not None:
            return prev
        at = now or dt.datetime.now(UTC).isoformat(timespec='seconds')
        candidate = verified_espn_final(c,original_id,kick,at)
        if candidate is not None:
            counts['espn_strict_fallback_used'] += 1
        return candidate
    oracle_v58_brain.resolve_final_for_portfolio = compatible_resolver
    c=sports_free.db()
    try:
        # The official module owns outcome math and all updates.
        r=oracle_v5_settlement.settle(c)
        print(json.dumps({'state':r.get('state','UNKNOWN'),'native_settlement':r,
                          'strict_espn_fallback':counts,'real_orders':0},ensure_ascii=False,sort_keys=True))
        return 0 if r.get('state')=='OK' and r.get('real_orders')==0 else 2
    finally:
        c.close()


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--selftest':
        selftest();return 0
    if len(sys.argv)==2 and sys.argv[1]=='once':
        try:return run_once()
        except Exception as exc:
            print(json.dumps({'state':'ERROR','error':type(exc).__name__+': '+str(exc)[:200],'real_orders':0}),file=sys.stderr)
            return 2
    print('Usage: python eva_settlement_verified_r6.py once|--selftest',file=sys.stderr)
    return 2

if __name__=='__main__':sys.exit(main())
