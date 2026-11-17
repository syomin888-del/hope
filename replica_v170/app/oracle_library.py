#!/usr/bin/env python3
"""Traceable library registry: catalogue is not equivalent to reading books.

No copyrighted text is acquired or stored by this tool. Verified study notes must
carry provenance (specific lawful source, chapter/page, reviewer and timestamp).
"""
from __future__ import annotations
import csv,datetime as dt,sqlite3

def setup(c):
 c.executescript('''CREATE TABLE IF NOT EXISTS kb_books(id TEXT PRIMARY KEY,domain TEXT NOT NULL,
   title TEXT NOT NULL,author TEXT NOT NULL,access_status TEXT NOT NULL,
   read_status TEXT NOT NULL,added_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS kb_principles(id INTEGER PRIMARY KEY AUTOINCREMENT,
   book_id TEXT NOT NULL,concept TEXT NOT NULL,chapter_or_pages TEXT NOT NULL,
   source_url_or_receipt TEXT NOT NULL,researcher TEXT NOT NULL,reviewed_at TEXT NOT NULL,
   test_id TEXT,test_status TEXT NOT NULL DEFAULT 'UNTESTED',
   FOREIGN KEY(book_id) REFERENCES kb_books(id));''')
 c.commit()

def ingest_catalog(c,path):
 count=0
 with open(path,newline='',encoding='utf-8') as f:
  for row in csv.DictReader(f):
   c.execute('''INSERT OR IGNORE INTO kb_books VALUES(?,?,?,?,?,?,?)''',(
    row['id'],row['domain'],row['title'],row['author'],row['access_status'],
    row['read_status'],dt.datetime.now(dt.timezone.utc).isoformat()))
   count+=1
 c.commit();return count

def add_verified_note(c,book_id,concept,chapter_or_pages,source_url_or_receipt,researcher):
 if not all([concept,chapter_or_pages,source_url_or_receipt,researcher]):
  raise ValueError('No ungrounded book notes; precise provenance is mandatory')
 if not c.execute('SELECT 1 FROM kb_books WHERE id=?',(book_id,)).fetchone():raise ValueError('Unknown book')
 c.execute('''INSERT INTO kb_principles(book_id,concept,chapter_or_pages,source_url_or_receipt,
 researcher,reviewed_at) VALUES(?,?,?,?,?,?)''',(
 book_id,concept,chapter_or_pages,source_url_or_receipt,researcher,dt.datetime.now(dt.timezone.utc).isoformat()))
 c.commit()
