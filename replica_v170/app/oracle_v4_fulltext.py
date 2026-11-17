"""Read open-licensed books from canonical author/publisher repositories.

This is a *document ingestion engine*, not a claim that an LLM has understood
or verified a book. No piracy, no automated scraping Gutenberg's human site.
Stored chapters and candidate excerpts retain the source URI and a digest.
"""
from __future__ import annotations
import datetime as dt,hashlib,json,re,sqlite3,urllib.parse,urllib.request
from pathlib import PurePosixPath

ROOT='https://raw.githubusercontent.com'
SOURCES={
  'mastering_bitcoin': {'repo':'bitcoinbook/bitcoinbook','branch':'develop','index':'BOOK.md',
      'title':'Mastering Bitcoin','license_reference':'https://github.com/bitcoinbook/bitcoinbook/blob/develop/LICENSE'},
  'mastering_ethereum': {'repo':'ethereumbook/ethereumbook','branch':'develop','index':'src/SUMMARY.md',
      'title':'Mastering Ethereum (open repository)','license_reference':'https://github.com/ethereumbook/ethereumbook/blob/develop/README.md'}
}
UA='ORACLE-OpenKnowledge/4.2 (open license, bounded personal research)'
MAX=1_100_000
TERMS=re.compile(r'\b(?:risk|trading|liquidit\w*|fee|volatil\w*|mempool|market|security|incentive|oracle|staking|consensus|arbitrage|pool|price|derivativ\w*|margin|valuation)\b',re.I)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):raise ValueError('Redirect blocked')

def stamp(at=None):return (at or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).isoformat(timespec='seconds')

def sha(text):return hashlib.sha256(text.encode('utf-8')).hexdigest()

def canonical_url(book,path):
    if book not in SOURCES:raise ValueError('Unvetted source')
    if path.startswith('/') or '\\' in path or '?' in path or '#' in path:raise ValueError('Invalid relative path')
    parts=PurePosixPath(path).parts
    if not parts or any(p in ('..','.','') for p in parts):raise ValueError('Directory traversal')
    if not re.fullmatch(r'[A-Za-z0-9._/\-]+',path):raise ValueError('Unsafe path')
    cfg=SOURCES[book]
    return f'{ROOT}/{cfg["repo"]}/{cfg["branch"]}/{path}'

def download(url):
    from urllib.parse import urlsplit
    s=urlsplit(url)
    if s.scheme!='https' or s.hostname!='raw.githubusercontent.com' or s.username or s.port or s.fragment or s.query:raise ValueError('Untrusted host')
    ok=any(url.startswith(f'{ROOT}/{x["repo"]}/{x["branch"]}/') for x in SOURCES.values())
    if not ok:raise ValueError('Unapproved repository')
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'text/plain'})
    with urllib.request.build_opener(NoRedirect).open(req,timeout=15) as resp:
        blob=resp.read(MAX+1)
        if len(blob)>MAX:raise ValueError('Content too large')
        return blob.decode('utf-8')

def path_relative_to_index(book,child):
    p=urllib.parse.unquote(child.split('#',1)[0])
    if p.startswith(('https:','http:','/')) or '\\' in p:raise ValueError('External path')
    base=PurePosixPath(SOURCES[book]['index']).parent
    if str(base)=='.':root=p
    else:root=str(base/p)
    # No ../ escaping, including URL-encoded traversal.
    canonical_url(book,root)
    return root

def chapter_links(book,index):
    paths=[];seen=set()
    for label,link in re.findall(r'\[([^\]]{2,150})\]\(([^)]+)\)',index):
        try:p=path_relative_to_index(book,link)
        except ValueError:continue
        if p in seen or not p.lower().endswith(('.md','.adoc','.asciidoc')):continue
        if p==SOURCES[book]['index'] or re.search(r'(?i)license|readme|contribut|summary',p):continue
        seen.add(p);paths.append((label.strip(),p))
    return paths[:60]

def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS v4_fulltext_sources(book_id TEXT PRIMARY KEY,title TEXT NOT NULL,
        license_reference TEXT NOT NULL,chapter_count_expected INTEGER NOT NULL DEFAULT 0,
        checked_at TEXT,status TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v4_fulltext_chapters(book_id TEXT NOT NULL,path TEXT NOT NULL,
        title TEXT NOT NULL,source_url TEXT NOT NULL,first_read_at TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,full_text TEXT NOT NULL,word_count INTEGER NOT NULL,
        study_status TEXT NOT NULL,PRIMARY KEY(book_id,path));
    CREATE TABLE IF NOT EXISTS v4_fulltext_claim_candidates(id TEXT PRIMARY KEY,book_id TEXT NOT NULL,
        chapter_path TEXT NOT NULL,quote TEXT NOT NULL,source_url TEXT NOT NULL,
        discovered_at TEXT NOT NULL,status TEXT NOT NULL);
    ''');c.commit()

def candidate_passages(text):
    paras=re.split(r'\n\s*\n',text)
    return [p.strip().replace('\n',' ')[:600] for p in paras
            if len(p)>=85 and TERMS.search(p) and not p.startswith(('```','//'))][:6]

def ingest(c,at=None,loader=download,max_new=4):
    setup(c);now=stamp(at);added=0;errors=[];index_ok=0
    for book,cfg in SOURCES.items():
        try:
            toc=loader(canonical_url(book,cfg['index']))
            if len(toc)>MAX or not isinstance(toc,str):raise ValueError('Bad index')
            paths=chapter_links(book,toc)
            if not paths:raise ValueError('No chapters in index')
            index_ok+=1
            with c:c.execute('''INSERT INTO v4_fulltext_sources(book_id,title,license_reference,chapter_count_expected,checked_at,status)
                VALUES(?,?,?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET chapter_count_expected=excluded.chapter_count_expected,checked_at=excluded.checked_at,status=excluded.status''',
                (book,cfg['title'],cfg['license_reference'],len(paths),now,'INDEX_OK'))
            for title,path in paths:
                if added>=max_new:break
                if c.execute('SELECT 1 FROM v4_fulltext_chapters WHERE book_id=? AND path=?',(book,path)).fetchone():continue
                try:
                    url=canonical_url(book,path)
                    text=loader(url)
                    if not isinstance(text,str) or len(text)<400 or len(text)>MAX or '<html' in text[:200].lower():raise ValueError('Not a chapter text')
                    h=sha(text)
                    with c:
                        c.execute('INSERT INTO v4_fulltext_chapters VALUES(?,?,?,?,?,?,?,?,?)',
                                  (book,path,title,url,now,h,text,len(text.split()),'TEXT_ACQUIRED_NOT_FACT_CHECKED'))
                        for p in candidate_passages(text):
                            key=sha(book+'|'+path+'|'+p)[:32]
                            c.execute('INSERT OR IGNORE INTO v4_fulltext_claim_candidates VALUES(?,?,?,?,?,?,?)',
                                      (key,book,path,p,url,now,'UNTESTED_EXCERPT'))
                    added+=1
                except Exception as exc:
                    errors.append(f'{book}:{path}:{type(exc).__name__}')
                    # Prevent repeatedly hammering a failing chapter this cycle.
                    break
        except Exception as exc:errors.append(f'{book}:index:{type(exc).__name__}')
    return {'state':'OK' if index_ok and not errors else 'PARTIAL' if index_ok else 'ERROR',
            'indexes_ok':index_ok,'chapters_new':added,'errors':errors[:8],
            'note':'Full chapter text obtained from source, not independently tested; no AI conclusion inferred.'}

def summary(c):
    try:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'v4_fulltext_chapters' not in tables:return {'state':'NOT_INITIALIZED','chapters_read':0}
        sources=c.execute('''SELECT s.book_id,s.title,s.chapter_count_expected,COUNT(ch.path)
                              FROM v4_fulltext_sources s LEFT JOIN v4_fulltext_chapters ch ON ch.book_id=s.book_id
                              GROUP BY s.book_id ORDER BY s.book_id''').fetchall()
        return {'books':[{'id':id,'title':title,'expected_chapters':expected,'chapters_obtained':have,
                         'all_source_chapters_obtained':bool(expected and have>=expected)} for id,title,expected,have in sources],
                'chapters_obtained':c.execute('SELECT COUNT(*) FROM v4_fulltext_chapters').fetchone()[0],
                'untested_claim_candidates':c.execute('SELECT COUNT(*) FROM v4_fulltext_claim_candidates').fetchone()[0],
                'semantically_verified_books':0,
                'warning':'Reading and storing chapters does not prove comprehension, truth or profitable signals.'}
    except sqlite3.Error:return {'state':'ERROR','chapters_read':0}
