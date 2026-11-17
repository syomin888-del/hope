"""Rights-aware research discovery: fetch *metadata* from public publisher RSS only.

Does not download ebooks, watch YouTube videos, train models on unauthorized text,
claim truth, or execute code from retrieved articles. Future model promotion requires
out-of-time backtests and paper validation.
"""
from __future__ import annotations
import datetime as dt, hashlib, json, re, sqlite3, urllib.request, urllib.parse, xml.etree.ElementTree as ET
from urllib.parse import urlsplit
UTC=dt.timezone.utc
# Owners' category RSS metadata; additional feeds require vetted code change.
FEEDS={
 'arxiv_qfin_tr':'https://rss.arxiv.org/rss/q-fin.TR',
 'arxiv_qfin_st':'https://rss.arxiv.org/rss/q-fin.ST',
 'arxiv_qfin_cp':'https://rss.arxiv.org/rss/q-fin.CP',
 'arxiv_qfin_pm':'https://rss.arxiv.org/rss/q-fin.PM',
}

def stamp(at=None):return (at or dt.datetime.now(UTC)).astimezone(UTC).isoformat(timespec='seconds')
def setup(c):
 c.executescript('''
 CREATE TABLE IF NOT EXISTS v4_discoveries(id TEXT PRIMARY KEY,source TEXT NOT NULL,
  title TEXT NOT NULL,url TEXT NOT NULL,abstract TEXT,first_seen TEXT NOT NULL,
  published_at TEXT,content_access TEXT NOT NULL,study_status TEXT NOT NULL,
  evidence_grade TEXT NOT NULL,claims_status TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS v4_discovery_checks(id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,checked_at TEXT NOT NULL,status TEXT NOT NULL,items INTEGER NOT NULL,
  error TEXT);
 CREATE TABLE IF NOT EXISTS v4_claim_tests(id TEXT PRIMARY KEY,item_id TEXT NOT NULL,claim TEXT NOT NULL,
  refutation_condition TEXT NOT NULL,evidence_url TEXT NOT NULL,observed_at TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'UNTESTED');
 ''');c.commit()

def clean(text):
 text=re.sub('<[^>]{0,200}>',' ',text or '')
 return re.sub(r'\s+',' ',text).strip()[:1500]

def parse_rss(blob,source,at=None):
 if len(blob)>1_000_000:raise ValueError('Oversized feed')
 root=ET.fromstring(blob)
 items=[]
 for item in root.findall('.//item')[:150]:
  name=clean(item.findtext('title') or '')
  url=(item.findtext('link') or '').strip()
  # Treat all RSS content as untrusted; only official arxiv links accepted.
  u=urlsplit(url)
  if u.hostname not in ('arxiv.org','export.arxiv.org') or u.scheme not in ('http','https') or not name:continue
  if u.scheme=='http':url='https://'+u.netloc+u.path+('?' + u.query if u.query else '')
  abstract=clean(item.findtext('description') or '')
  ident=hashlib.sha256(url.encode()).hexdigest()[:24]
  items.append((ident,source,name,url,abstract,stamp(at),item.findtext('pubDate'),
    'METADATA_AND_ABSTRACT_ONLY','NOT_FULL_TEXT_READ','NOT_INDEPENDENTLY_VERIFIED','UNTESTED'))
 return items

def download(url):
 if url not in FEEDS.values():raise ValueError('Feed not in vetted list')
 req=urllib.request.Request(url,headers={'User-Agent':'ORACLE-V4-Research/1.0','Accept':'application/rss+xml,application/xml'})
 with urllib.request.urlopen(req,timeout=16) as response:
  b=response.read(1_000_001)
 return b

def ingest(c,at=None,loader=download):
 setup(c);now=stamp(at);inserted=0;errors=0
 for source,url in FEEDS.items():
  try:
   data=loader(url)
   rows=parse_rss(data,source,at)
   with c:
    for row in rows:
     cur=c.execute('INSERT OR IGNORE INTO v4_discoveries VALUES(?,?,?,?,?,?,?,?,?,?,?)',row)
     inserted+=int(cur.rowcount>0)
    c.execute('INSERT INTO v4_discovery_checks(source,checked_at,status,items,error) VALUES(?,?,?,?,?)',
       (source,now,'OK',len(rows),None))
  except Exception as e:
   errors+=1
   with c:c.execute('INSERT INTO v4_discovery_checks(source,checked_at,status,items,error) VALUES(?,?,?,?,?)',
     (source,now,'ERROR',0,type(e).__name__))
 return {'new_discoveries':inserted,'feed_errors':errors,'feeds_checked':len(FEEDS),
   'note':'Abstracts are candidate evidence, not studied full texts or verified truths.'}

def status(c):
 return {'discovered_items':c.execute('SELECT COUNT(*) FROM v4_discoveries').fetchone()[0],
  'fully_read_books':0, # measured separately in kb_notes, not presumed
  'unverified_claims':c.execute("SELECT COUNT(*) FROM v4_discoveries WHERE evidence_grade!='INDEPENDENTLY_VERIFIED'").fetchone()[0],
  'recent':[{'title':r[0],'url':r[1],'source':r[2],'status':r[3]} for r in c.execute(
   'SELECT title,url,source,study_status FROM v4_discoveries ORDER BY first_seen DESC LIMIT 12')]}

# Optional discovery feeds: channel *metadata*, never claimed as watched videos.
def youtube_sources(raw):
 import re
 ids=[s.strip() for s in raw.split(',') if s.strip()]
 return {f'youtube_{i}':f'https://www.youtube.com/feeds/videos.xml?channel_id={i}' for i in ids[:8]
         if re.fullmatch(r'UC[A-Za-z0-9_-]{22}',i)}

def parse_youtube_atom(blob,source,at=None):
 if len(blob)>1_000_000:raise ValueError('Oversized feed')
 root=ET.fromstring(blob)
 ns={'a':'http://www.w3.org/2005/Atom','yt':'http://www.youtube.com/xml/schemas/2015'}
 items=[]
 for item in root.findall('a:entry',ns)[:30]:
  vid=item.findtext('yt:videoId',default='',namespaces=ns)
  title=clean(item.findtext('a:title',default='',namespaces=ns))
  if not re.fullmatch(r'[A-Za-z0-9_-]{11}',vid or '') or not title:continue
  url='https://www.youtube.com/watch?v='+vid
  ident=hashlib.sha256(url.encode()).hexdigest()[:24]
  items.append((ident,source,title,url,None,stamp(at),
      item.findtext('a:published',default=None,namespaces=ns),
      'PUBLIC_METADATA_ONLY','VIDEO_NOT_WATCHED','NOT_INDEPENDENTLY_VERIFIED','UNTESTED'))
 return items

def ingest_video_feeds(c,raw,at=None,loader=None):
 setup(c);feeds=youtube_sources(raw)
 if not feeds:return {'state':'NOT_CONFIGURED','channels':0}
 if loader is None:
  def loader(url):
   if url not in feeds.values():raise ValueError('Not in vetted channels')
   req=urllib.request.Request(url,headers={'User-Agent':'ORACLE-V4-Research/1.0'})
   with urllib.request.urlopen(req,timeout=15) as r:return r.read(1_000_001)
 new=0;errors=0
 for channel,url in feeds.items():
  try:
   entries=parse_youtube_atom(loader(url),channel,at)
   # Remove most unrelated lectures; this remains metadata discovery, not video comprehension.
   focus=re.compile(r'\b(finance|financ\w*|economic\w*|probability|machine learning|game theory|statistics|risk|trading|investment|bank\w*|market\w*|optimization|causal|regression|stochastic)\b',re.I)
   entries=[row for row in entries if focus.search(row[2])]
   with c:
    for row in entries:
     new+=int(c.execute('INSERT OR IGNORE INTO v4_discoveries VALUES(?,?,?,?,?,?,?,?,?,?,?)',row).rowcount>0)
  except Exception:errors+=1
 return {'state':'OK' if not errors else 'PARTIAL','channels':len(feeds),'new_video_metadata':new,'errors':errors}

def discover_book_metadata(c,api_key,at=None,loader=None):
 """Public Google Books catalog search; key is optional, not authentication."""
 from urllib.parse import urlencode
 setup(c)
 count=0;errors=0
 for topic in ('subject:Business & Economics quantitative trading','sports betting analytics'):
  params={'q':topic,'orderBy':'newest','maxResults':12}
  if api_key:params['key']=api_key
  url='https://www.googleapis.com/books/v1/volumes?'+urlencode(params)
  try:
   if loader:payload=loader(url)
   else:
    with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'ORACLE-V4-Research/1.0'}),timeout=15) as r:
     payload=json.loads(r.read(700_000))
   with c:
    for item in payload.get('items',[])[:12]:
     info=item.get('volumeInfo') or {};title=clean(info.get('title') or '')
     identifier=str(item.get('id') or '')
     if not identifier or not title:continue
     ref='https://books.google.com/books?id='+urllib.parse.quote(identifier,safe='')
     key=hashlib.sha256(ref.encode()).hexdigest()[:24]
     row=(key,'google_books',title,ref,clean(info.get('description') or ''),stamp(at),
          info.get('publishedDate'), 'PUBLIC_CATALOG_METADATA_ONLY','NOT_FULL_TEXT_READ',
          'NOT_INDEPENDENTLY_VERIFIED','UNTESTED')
     count+=int(c.execute('INSERT OR IGNORE INTO v4_discoveries VALUES(?,?,?,?,?,?,?,?,?,?,?)',row).rowcount>0)
  except Exception:errors+=1
 return {'state':'OK' if not errors else 'PARTIAL' if count else 'ERROR','books_discovered':count,'errors':errors,'registration_required':False,'study_status':'METADATA_ONLY'}
