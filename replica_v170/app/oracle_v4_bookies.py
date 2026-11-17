"""Optional *authorized* public bookmaker feed adapter (paper research only).

No undocumented endpoint guessing, credential bypass or simulated quotes. The
adapter consumes bookmaker-published machine-readable JSON only where robots
allows and the endpoint is publicly reachable. Russian bookmakers are preferred
but are NOT claimed as actually providing suitable endpoints by default.
"""
from __future__ import annotations
import datetime as dt
import json, math, re, sqlite3, urllib.parse, urllib.request, urllib.robotparser
from urllib.parse import urlsplit
from html.parser import HTMLParser

BOOKS = {
    'ligastavok': ('www.ligastavok.ru', 'ligastavok.ru'),
    'fonbet': ('fonbet.ru','www.fonbet.ru'),
    '1xbet': ('1xbet.com','www.1xbet.com'),
}
AGENT='ORACLE-CitablePublicOdds/4.2 (research, respectful crawler)'
MAX_BYTES=750_000

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Cross-endpoint redirects are not allowed')

def utc(s):
    d=dt.datetime.fromisoformat(str(s).replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('Time must include a timezone')
    return d.astimezone(dt.timezone.utc)

def stamp(t=None):
    return (t or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).isoformat(timespec='seconds')

def validate_url(book,url):
    s=urlsplit(url)
    if book not in BOOKS or s.scheme!='https' or s.hostname not in BOOKS[book] or s.port or s.username or s.password or s.fragment:
        raise ValueError('Only exact HTTPS bookmaker origins are allowed')
    if re.search(r'(?i)(login|signin|sign-in|auth|registration|account|private|betslip)',s.path):
        raise ValueError('Authenticated pages are excluded')
    return url

def fetch_robots(book,url,load_bytes=None):
    validate_url(book,url)
    host=urlsplit(url).hostname
    robots='https://'+host+'/robots.txt'
    if load_bytes is None:
        def load_bytes(u):
            with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(u,headers={'User-Agent':AGENT}),timeout=12) as resp:
                blob=resp.read(150_001)
                if len(blob)>150_000:raise ValueError('robots too large')
                return blob
    try:
        data=load_bytes(robots)
        if isinstance(data,bytes):data=data.decode('utf-8','replace')
        rp=urllib.robotparser.RobotFileParser()
        rp.parse(str(data).splitlines())
        return bool(rp.can_fetch(AGENT,url)), 'ROBOTS_ALLOWED' if rp.can_fetch(AGENT,url) else 'ROBOTS_DISALLOWED'
    except Exception:
        return False,'ROBOTS_NOT_VERIFIED'

def fetch_feed(book,url,load_bytes=None,load_robots=None):
    validate_url(book,url)
    allowed,reason=fetch_robots(book,url,load_robots)
    if not allowed:raise PermissionError(reason)
    if load_bytes is None:
        def load_bytes(u):
            with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(u,headers={'User-Agent':AGENT,'Accept':'application/json'}),timeout=15) as r:
                blob=r.read(MAX_BYTES+1)
                if len(blob)>MAX_BYTES:raise ValueError('oversized feed')
                return blob
    raw=load_bytes(url)
    if len(raw)>MAX_BYTES:raise ValueError('oversized feed')
    return json.loads(raw)

def normalize_team(s):
    return re.sub(r'[^\w]+',' ',str(s).casefold()).strip()

def find_event(c,row):
    kickoff=utc(row['kickoff_utc'])
    a,b=normalize_team(row['home']),normalize_team(row['away'])
    if not a or not b or a==b:raise ValueError('Ambiguous team labels')
    choices=c.execute('SELECT event_id,home,away,kickoff_utc FROM v4_games WHERE final=0 AND kickoff_utc>? AND kickoff_utc<?',
        (stamp(kickoff-dt.timedelta(minutes=20)),stamp(kickoff+dt.timedelta(minutes=20)))).fetchall()
    valid=[id for id,h,away,k in choices if normalize_team(h)==a and normalize_team(away)==b and abs((utc(k)-kickoff).total_seconds())<=900]
    return valid[0] if len(valid)==1 else None

def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS v4_bookie_sources(
        bookmaker TEXT PRIMARY KEY, checked_at TEXT NOT NULL,
        state TEXT NOT NULL, detail TEXT NOT NULL, imported_quotes INTEGER NOT NULL)''')
    c.commit()

def save_state(c,book,state,detail,quotes=0,now=None):
    with c:c.execute('INSERT INTO v4_bookie_sources VALUES(?,?,?,?,?) ON CONFLICT(bookmaker) DO UPDATE SET checked_at=excluded.checked_at,state=excluded.state,detail=excluded.detail,imported_quotes=excluded.imported_quotes',
        (book,stamp(now),state,str(detail)[:240],int(quotes)))

def validate_quote(row,asof):
    if not isinstance(row,dict):raise ValueError('Expected object')
    if row.get('market')!='1X2' or row.get('selection') not in ('1','X','2'):raise ValueError('Only 1X2 match-time market accepted')
    # Make sure quote originates before match and is recent on retrieval.
    captured=utc(row['captured_at']); last=utc(row['book_last_update']);kick=utc(row['kickoff_utc'])
    if not last<=captured<kick:raise ValueError('Market update/quote after kickoff')
    if abs((asof-captured).total_seconds())>600 or (captured-last).total_seconds()>900:raise ValueError('Stale snapshot')
    price=row['price']
    if isinstance(price,bool) or not isinstance(price,(int,float)) or not math.isfinite(price) or not 1.01<=price<=250:raise ValueError('Invalid bookmaker odds')
    if not re.fullmatch(r'[a-zA-Z0-9._:-]{4,150}',str(row.get('origin_id') or '')):raise ValueError('Real provider event id required')
    return captured,last,kick

def parse_rows(c,book,body,now=None):
    """Ingest only official public publisher's structured feed; no derived/fair odds."""
    now=now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(body,dict) or body.get('bookmaker')!=book or not isinstance(body.get('events'),list):raise ValueError('Invalid publisher feed identity/schema')
    imported=0;rejected=0
    for row in body['events'][:100]:
        try:
            captured,last,kick=validate_quote(row,now)
            event=find_event(c,row)
            if not event:rejected+=1;continue
            with c:
                cur=c.execute('''INSERT OR IGNORE INTO v4_book_odds
                    (event_id,bookmaker,selection,price,captured_at,book_last_update,source,origin_id)
                    VALUES(?,?,?,?,?,?,?,?)''',
                    (event,book,row['selection'],float(row['price']),stamp(captured),stamp(last),'VERIFIED_PUBLIC_BOOKMAKER_FEED',row['origin_id']))
            imported+=int(cur.rowcount>0)
        except (ValueError,KeyError,TypeError,sqlite3.Error):rejected+=1
    return imported,rejected

def poll(c,config='',at=None,reader=fetch_feed):
    """Default: NO KEYS, NO PRETEND PROVIDERS. Configure vetted feed URLs only if available.

    Environment ORACLE_PUBLIC_BOOKMAKER_FEEDS is JSON {"fonbet":"https://..."}.
    This does not manufacture undocumented data channels.
    """
    setup(c)
    try:feeds=json.loads(config or '{}')
    except (ValueError,TypeError):feeds={}
    if not isinstance(feeds,dict):feeds={}
    new=0;states=[]
    for book in BOOKS:
        url=feeds.get(book)
        if not url:
            states.append({'bookmaker':book,'state':'NO_VERIFIED_PUBLIC_FEED'})
            save_state(c,book,'NO_VERIFIED_PUBLIC_FEED','No permitted stable public odds feed verified; no quotes invented',0,at)
            continue
        try:
            validate_url(book,url)
            body=reader(book,url)
            added,rejected=parse_rows(c,book,body,at)
            state='OK' if added else 'EMPTY_OR_UNMATCHED'
            new+=added
            states.append({'bookmaker':book,'state':state,'new_quotes':added,'rejected':rejected})
            save_state(c,book,state,f'imported={added} rejected={rejected}',added,at)
        except Exception as e:
            states.append({'bookmaker':book,'state':'UNAVAILABLE','reason':type(e).__name__})
            save_state(c,book,'UNAVAILABLE',type(e).__name__,0,at)
    return {'state':'OK' if new else 'NO_VERIFIED_ODDS','new_quotes':new,'bookmakers':states,
            'bookmaker_roi_available':new>0,'note':'No bookmaker odds are inferred from prediction markets.'}

def summary(c):
    try:rows=c.execute('SELECT bookmaker,checked_at,state,detail,imported_quotes FROM v4_bookie_sources ORDER BY bookmaker').fetchall()
    except sqlite3.Error:rows=[]
    return {'russian_bookmaker_candidates':list(BOOKS),
        'sources':[{'bookmaker':b,'checked_at':d,'state':s,'detail':detail,'last_import':n} for b,d,s,detail,n in rows],
        'proven_public_bookmaker_quotes':c.execute("SELECT COUNT(*) FROM v4_book_odds WHERE source='VERIFIED_PUBLIC_BOOKMAKER_FEED'").fetchone()[0] if 'v4_book_odds' in {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")} else 0}
