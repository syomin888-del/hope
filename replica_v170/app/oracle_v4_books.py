"""Book content ingestion only for text provided with explicit rights.

Avoid copy-pasting arbitrary copyrighted titles into model training. Index private,
licensed excerpts locally only, with source + page, no sharing of full text.
"""
from __future__ import annotations
import hashlib,sqlite3,datetime as dt

def setup(c):
 c.executescript('''CREATE TABLE IF NOT EXISTS v4_study_fragments(
   id TEXT PRIMARY KEY,book_id TEXT NOT NULL,section TEXT NOT NULL,
   rights_basis TEXT NOT NULL,source TEXT NOT NULL,summary TEXT NOT NULL,
   recorded_at TEXT NOT NULL,corroboration TEXT NOT NULL DEFAULT 'UNVERIFIED');''');c.commit()

def record(c,book_id,section,rights_basis,source,summary,at=None):
 if rights_basis not in ('PUBLIC_DOMAIN','OPEN_LICENSE','USER_AUTHORIZED_TEXT','ORIGINAL_NOTES'):
  raise ValueError('Unverified content rights')
 if not (book_id and section and source and summary and len(summary)<=6000):
  raise ValueError('Full provenance or bounded original notes required')
 if not c.execute('SELECT 1 FROM kb_books WHERE id=?',(book_id,)).fetchone():
  raise ValueError('Book not in registry')
 ident=hashlib.sha256((book_id+'|'+section+'|'+source+'|'+summary).encode()).hexdigest()[:24]
 c.execute('INSERT OR IGNORE INTO v4_study_fragments(id,book_id,section,rights_basis,source,summary,recorded_at) VALUES(?,?,?,?,?,?,?)',
    (ident,book_id,section,rights_basis,source,summary,(at or dt.datetime.now(dt.timezone.utc)).isoformat()))
 c.commit();return ident
