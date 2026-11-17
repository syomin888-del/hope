"""ORACLE V4 verified Fonbet/OpenLiga fixture bridge.

Conservative cross-language event linking with no guessed bets or unsupported markets.
Preserves original numeric factor IDs; only independently documented 921/922/923
may be presented as football 1X2. Does not modify existing odds/forecast tables.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import re
import sqlite3
import unicodedata
from collections import Counter

UTC = dt.timezone.utc
KNOWN_RESULT_CODES = {'921': '1', '922': 'X', '923': '2'}

# Explicit, auditable naming references. Never rely on fuzzy spelling by itself.
KNOWN_ALIASES = {
 'SV Darmstadt 98': ('Дармштадт', 'Дармштадт 98'),
 'FC St. Pauli': ('Санкт-Паули', 'Санкт Паули', 'Сент-Паули'),
 'VfL Bochum': ('Бохум',),
 'VfL Wolfsburg': ('Вольфсбург',),
 'VfB Stuttgart': ('Штутгарт',),
 'Borussia Mönchengladbach': ('Боруссия Менхенгладбах', 'Боруссия Мёнхенгладбах', 'Боруссия М'),
 '1. FC Heidenheim 1846': ('Хайденхайм', 'Хайденхайм 1846'),
 'Energie Cottbus': ('Энерги Котбус', 'Энерги', 'Энерги Котбус 1966'),
 'Hertha BSC': ('Герта', 'Герта Берлин'),
 'DSC Arminia Bielefeld': ('Арминия Билефельд', 'Арминия'),
 '1. FC Magdeburg': ('Магдебург',),
 'Karlsruher SC': ('Карлсруэ',),
 'RB Leipzig': ('РБ Лейпциг', 'Лейпциг'),
 'SV 07 Elversberg': ('Эльферсберг', 'Эльверсберг'),
 'FC Augsburg': ('Аугсбург',),
 '1. FC Union Berlin': ('Унион Берлин', 'Унион'),
 '1. FSV Mainz 05': ('Майнц', 'Майнц 05'),
 'SV Werder Bremen': ('Вердер', 'Вердер Бремен'),
 '1. FC Köln': ('Кельн',),
 'FC Schalke 04': ('Шальке', 'Шальке 04'),
 'SC Paderborn 07': ('Падерборн', 'Падерборн 07'),
 'Hamburger SV': ('Гамбург', 'Гамбург СВ'),
 'Borussia Dortmund': ('Боруссия Дортмунд', 'Боруссия Д'),
 'Eintracht Frankfurt': ('Айнтрахт Франкфурт', 'Айнтрахт Ф'),
 '1. FC Nürnberg': ('Нюрнберг',),
 '1. FC Kaiserslautern': ('Кайзерслаутерн',),
 'Holstein Kiel': ('Хольштайн Киль', 'Хольштайн', 'Хольштейн Киль'),
 'Dynamo Dresden': ('Динамо Дрезден',),
 'Eintracht Braunschweig': ('Айнтрахт Брауншвейг',),
 'Hannover 96': ('Ганновер', 'Ганновер 96'),
 'VfL Osnabrück': ('Оснабрюк', 'Оснабрюкк'),
 'SpVgg Greuther Fürth': ('Гройтер Фюрт', 'Гройтер Фюрт 1903'),
 'TSG Hoffenheim': ('Хоффенхайм',),
 'Bayer 04 Leverkusen': ('Байер', 'Байер Леверкузен', 'Байер 04'),
 'SC Freiburg': ('Фрайбург',),
 'FC Bayern München': ('Бавария', 'Бавария Мюнхен'),
}

def norm(s):
    s = unicodedata.normalize('NFKD', str(s or '').casefold())
    return ''.join(ch for ch in s if ch.isalnum() and not unicodedata.combining(ch))

def accepted_names(s):
    return {norm(s)} | {norm(x) for x in KNOWN_ALIASES.get(str(s), ())}

def parse(t):
    value=dt.datetime.fromisoformat(str(t).replace('Z','+00:00'))
    if value.tzinfo is None:raise ValueError('Timestamp missing UTC offset')
    return value.astimezone(UTC)

def stamp(t=None):
    return (t or dt.datetime.now(UTC)).astimezone(UTC).isoformat(timespec='seconds')

def table_exists(c, table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() is not None

def setup(c):
    c.executescript('''
      CREATE TABLE IF NOT EXISTS fb_bridge_links (
        oracle_event_id TEXT PRIMARY KEY,
        fonbet_event_id TEXT NOT NULL UNIQUE,
        oracle_home TEXT NOT NULL,oracle_away TEXT NOT NULL,
        fonbet_home TEXT NOT NULL,fonbet_away TEXT NOT NULL,
        kickoff_utc TEXT NOT NULL,linked_at TEXT NOT NULL,
        rule TEXT NOT NULL,source_snapshot TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS fb_bridge_1x2 (
        oracle_event_id TEXT NOT NULL,
        fonbet_event_id TEXT NOT NULL,
        selection TEXT NOT NULL,
        decimal_price REAL NOT NULL,
        quote_key TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        root_event_id TEXT NOT NULL,
        PRIMARY KEY(oracle_event_id,selection,quote_key,captured_at)
      );
      CREATE TABLE IF NOT EXISTS fb_bridge_runs (
        id INTEGER PRIMARY KEY,at TEXT NOT NULL,state TEXT NOT NULL,
        matched_new INTEGER NOT NULL,events_considered INTEGER NOT NULL,
        quote_rows_new INTEGER NOT NULL,details TEXT NOT NULL
      );
    ''')
    c.commit()

def candidates(c,at=None,window_hours=72):
    """Pre-kickoff exact/curated-alias pairs only; never use same kickoff alone."""
    now=parse(at) if isinstance(at,str) else (at or dt.datetime.now(UTC))
    stop=now+dt.timedelta(hours=window_hours)
    games=c.execute('''SELECT event_id,kickoff_utc,home,away FROM v4_games
        WHERE sport='football' AND final=0 AND kickoff_utc>=? AND kickoff_utc<=?''',
        (stamp(now),stamp(stop))).fetchall()
    # Include already captured Fonbet events even if their last snapshot is older.
    fb=c.execute('''SELECT event_id,start_utc,home,away,last_seen FROM fb_full_events
      WHERE sport='football' AND level=1 AND root_event_id=event_id
      AND place='line' AND start_utc>=? AND start_utc<=? AND home<>'' AND away<>'' ''',
      (stamp(now-dt.timedelta(minutes=15)),stamp(stop+dt.timedelta(minutes=15)))).fetchall()
    candidates=[];orphans=[]
    for event_id,kick,home,away in games:
        hk,ak=accepted_names(home),accepted_names(away)
        matches=[]
        for fid,fkick,fh,fa,seen in fb:
            try:seconds=abs((parse(fkick)-parse(kick)).total_seconds())
            except ValueError:continue
            if seconds>600:continue
            if norm(fh) not in hk or norm(fa) not in ak:continue
            rule='EXACT' if norm(fh)==norm(home) and norm(fa)==norm(away) else 'EXPLICIT_TEAM_ALIASES'
            matches.append({'oracle_event_id':event_id,'fonbet_event_id':str(fid),
                'oracle_home':home,'oracle_away':away,'fonbet_home':fh,'fonbet_away':fa,
                'kickoff_utc':kick,'source_snapshot':seen,'rule':rule})
        if len(matches)==1:candidates+=matches
        else:orphans.append({'oracle_event_id':event_id,'home':home,'away':away,
                               'reason':'AMBIGUOUS' if matches else 'NO_CONFIRMED_MATCH','n':len(matches)})
    # Global uniqueness: a bookmaker event may never link to multiple ORACLE games.
    by_id=Counter(x['fonbet_event_id'] for x in candidates)
    safe=[x for x in candidates if by_id[x['fonbet_event_id']]==1]
    duplicates=[x for x in candidates if by_id[x['fonbet_event_id']]>1]
    return {'safe':safe,'unmatched':orphans,'ambiguous':duplicates,'games_seen':len(games),
            'fb_events_seen':len(fb)}

def run(c,at=None,window_hours=72):
    at=stamp(parse(at) if isinstance(at,str) else at)
    if not table_exists(c,'fb_full_events') or not table_exists(c,'v4_games'):
        return {'state':'MISSING_SOURCE_TABLES'}
    c.execute('PRAGMA busy_timeout=30000')
    setup(c)
    discovered=candidates(c,at,window_hours)
    inserted=0;prices=0;skipped=0
    with c:
        for match in discovered['safe']:
            existing=c.execute('SELECT fonbet_event_id FROM fb_bridge_links WHERE oracle_event_id=?',
                               (match['oracle_event_id'],)).fetchone()
            if existing and existing[0]!=match['fonbet_event_id']:
                skipped+=1;continue  # never rewrite a previously verified event link silently
            other=c.execute('SELECT oracle_event_id FROM fb_bridge_links WHERE fonbet_event_id=?',
                            (match['fonbet_event_id'],)).fetchone()
            if other and other[0]!=match['oracle_event_id']:
                skipped+=1;continue
            if not existing:
                values=(match['oracle_event_id'],match['fonbet_event_id'],match['oracle_home'],
                    match['oracle_away'],match['fonbet_home'],match['fonbet_away'],
                    match['kickoff_utc'],at,match['rule'],match['source_snapshot'])
                c.execute('INSERT INTO fb_bridge_links VALUES(?,?,?,?,?,?,?,?,?,?)',values)
                inserted+=1
            # Restrict to root-level full-time 1X2; bookies' child period events are excluded.
            for qkey,code,price,seen,active,point in c.execute('''
                SELECT quote_key,factor_id,decimal_price,last_snapshot,active,point
                FROM fb_full_current WHERE event_id=? AND root_event_id=?
                AND factor_id IN ('921','922','923') AND active=1''',
                (match['fonbet_event_id'],match['fonbet_event_id'])):
                if not (1.01<float(price)<=1001):continue
                if parse(seen)>=parse(match['kickoff_utc']):continue
                try:param=json.loads(point or '{}')
                except (ValueError,TypeError):continue
                if param.get('p') is not None or param.get('pt') is not None:continue
                cursor=c.execute('''INSERT OR IGNORE INTO fb_bridge_1x2 VALUES(?,?,?,?,?,?,?)''',
                    (match['oracle_event_id'],match['fonbet_event_id'],KNOWN_RESULT_CODES[code],
                     price,qkey,seen,match['fonbet_event_id']))
                prices+=int(cursor.rowcount>0)
        c.execute('''INSERT INTO fb_bridge_runs(at,state,matched_new,events_considered,quote_rows_new,details)
            VALUES(?,?,?,?,?,?)''',
            (at,'OK',inserted,discovered['games_seen'],prices,json.dumps({'ambiguous':len(discovered['ambiguous']),
             'missing':len(discovered['unmatched']),'conflicts':skipped},ensure_ascii=False)))
    return {'state':'OK','games_in_horizon':discovered['games_seen'],
            'fonbet_root_events_in_horizon':discovered['fb_events_seen'],
            'uniquely_confirmed_now':len(discovered['safe']),
            'new_event_links':inserted,'new_pre_kickoff_1x2_prices':prices,
            'missing':len(discovered['unmatched']),'ambiguous':len(discovered['ambiguous']),
            'conflicts':skipped,'paper_bets_created':0,
            'note':'Only explicit name equivalence + <=10min kickoff + uniqueness; NO fake bookmaker timestamps'}

def report(c,at=None):
    if not table_exists(c,'fb_full_events') or not table_exists(c,'v4_games'):
        return {'state':'MISSING_SOURCE_TABLES'}
    data=candidates(c,at)
    # Non-authoritative hints: same kickoff ONLY, never treated as confirmed links.
    missing_hints=[]
    for game in data['unmatched'][:12]:
        kickoff=c.execute('SELECT kickoff_utc FROM v4_games WHERE event_id=?',(game['oracle_event_id'],)).fetchone()
        hints=[]
        if kickoff:
            for eid,home,away,t in c.execute("SELECT event_id,home,away,start_utc FROM fb_full_events WHERE sport='football' AND level=1 AND root_event_id=event_id AND place='line' AND home<>'' AND away<>''"):
                try:distance=abs((parse(t)-parse(kickoff[0])).total_seconds())
                except ValueError:continue
                if distance<=600:hints.append({'event_id':eid,'home':home,'away':away,'seconds_apart':int(distance)})
        missing_hints.append({'oracle_home':game['home'],'oracle_away':game['away'],
                              'fonbet_same_kickoff_unverified':hints[:6]})
    out={'state':'DIAGNOSTIC','games_in_horizon':data['games_seen'],
         'fonbet_root_events_in_horizon':data['fb_events_seen'],
         'proposed_links':data['safe'][:40],'unmatched_games':data['unmatched'][:40],
         'unverified_time_only_hints':missing_hints,
         'ambiguous':data['ambiguous'][:20],
         'persisted_links':c.execute('SELECT COUNT(*) FROM fb_bridge_links').fetchone()[0]
                            if table_exists(c,'fb_bridge_links') else 0,
         'linked_1x2_captures':c.execute('SELECT COUNT(*) FROM fb_bridge_1x2').fetchone()[0]
                            if table_exists(c,'fb_bridge_1x2') else 0,
         'top_unmapped_factor_ids':[
           {'factor_id':code,'active_quotes':n} for code,n in c.execute('''
            SELECT factor_id, COUNT(*) AS n FROM fb_full_current
            WHERE active=1 AND factor_id NOT IN ('921','922','923')
            GROUP BY factor_id ORDER BY n DESC LIMIT 15''')
         ] if table_exists(c,'fb_full_current') else []}
    return out

def main():
    p=argparse.ArgumentParser(description='ORACLE Fonbet odds-forecast conservative bridge')
    p.add_argument('action',choices=('diagnose','once','status'))
    p.add_argument('--db',default='/app/state/oracle.sqlite3')
    p.add_argument('--hours',type=int,default=72)
    args=p.parse_args()
    if not 1<=args.hours<=336:raise SystemExit('hours must be 1..336')
    readonly=args.action!='once'
    uri='file:'+args.db+'?mode=ro' if readonly else args.db
    with sqlite3.connect(uri,uri=readonly,timeout=35) as c:
        value=report(c) if readonly else run(c,window_hours=args.hours)
    print(json.dumps(value,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
