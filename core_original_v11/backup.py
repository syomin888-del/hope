#!/usr/bin/env python3
'Consistent compressed SQLite backups plus incremental archive mirroring.'
import datetime as dt
import gzip
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
from contextlib import closing

import collect
import storage

DIR=Path(os.environ.get('ORACLE_BACKUP_DIR',str(Path(__file__).resolve().parent/'backups')))
MIN_DISK_RESERVE=256*1024**2

def sha_file(file):
    h=hashlib.sha256()
    with open(file,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()

def mirror_archives():
    # Backup once per immutable compressed partition, not a full copy per day.
    conn=sqlite3.connect(str(collect.DB))
    try:
        if not storage._table(conn,'candle_archives'):return 0
        entries=conn.execute('SELECT file_path,file_sha256 FROM candle_archives').fetchall()
    finally:conn.close()
    copied=0
    for rel,expected in entries:
        source=storage._safe_path(rel)
        if not source.is_file() or sha_file(source)!=expected:
            raise RuntimeError('Cannot back up corrupted/missing archive: '+rel)
        dest=(DIR/'archive_mirror'/rel).resolve()
        if not dest.is_relative_to(DIR.resolve()):
            raise RuntimeError('Unsafe backup path')
        if dest.is_file() and sha_file(dest)==expected:continue
        dest.parent.mkdir(parents=True,exist_ok=True)
        if shutil.disk_usage(dest.parent).free<source.stat().st_size+MIN_DISK_RESERVE:
            raise RuntimeError('Not enough space to mirror archive')
        temp=dest.with_name(dest.name+'.tmp')
        try:
            shutil.copyfile(source,temp)
            if sha_file(temp)!=expected:raise RuntimeError('Backup archive checksum mismatch')
            os.replace(temp,dest)
            copied+=1
        finally:temp.unlink(missing_ok=True)
    return copied

def perform(keep=7):
    if keep<2 or keep>31:raise ValueError('Keep must be between 2 and 31')
    if not collect.DB.is_file():return None
    DIR.mkdir(parents=True,exist_ok=True)
    mirror_archives()
    db_size=collect.DB.stat().st_size
    if shutil.disk_usage(DIR).free<max(2*db_size,MIN_DISK_RESERVE):
        raise RuntimeError('Insufficient free space for verified DB snapshot')
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    output=DIR/f'oracle-{stamp}.sqlite3.gz'
    snap=DIR/f'snapshot-{stamp}.sqlite3'
    temp=DIR/f'backup-{stamp}.tmp'
    try:
        with closing(sqlite3.connect(str(collect.DB))) as src, closing(sqlite3.connect(str(snap))) as dest:
            src.backup(dest,pages=250,sleep=.02)
            if dest.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise RuntimeError('SQLite backup integrity check failed')
        # Compare snapshot checksum against last verified snapshot; skip identical DB.
        snap_sha=sha_file(snap)
        previous=sorted(DIR.glob('oracle-*.sqlite3.gz'),reverse=True)
        if previous:
            prev_hash=previous[0].with_suffix('.dbsha256')
            if prev_hash.is_file() and prev_hash.read_text().strip()==snap_sha:
                return previous[0]
        with snap.open('rb') as source, gzip.open(temp,'wb',compresslevel=6) as dest:
            shutil.copyfileobj(source,dest,1024*1024)
        # Verify gzip roundtrip bytes and checksum before making this backup visible.
        verify=hashlib.sha256()
        with gzip.open(temp,'rb') as restored:
            for part in iter(lambda:restored.read(1024*1024),b''):
                verify.update(part)
        if verify.hexdigest()!=snap_sha:raise RuntimeError('Backup compression verification failed')
        os.replace(temp,output)
        output.with_suffix('.dbsha256').write_text(snap_sha)
        # Cap redundant local copies without ever pruning the two newest.
        # Immutable Parquet mirror is outside this cap and is never discarded.
        max_gb=float(os.environ.get('ORACLE_BACKUP_MAX_GB','12'))
        if max_gb<1:raise ValueError('ORACLE_BACKUP_MAX_GB must be >=1')
        keep_files=sorted(DIR.glob('oracle-*.sqlite3.gz'),reverse=True)
        budget=int(max_gb*1024**3)
        used=sum(x.stat().st_size for x in keep_files)
        for index,old in enumerate(keep_files):
            if index<2:continue
            if index>=keep or used>budget:
                used-=old.stat().st_size
                old.unlink()
                old.with_suffix('.dbsha256').unlink(missing_ok=True)
        return output
    finally:
        snap.unlink(missing_ok=True)
        temp.unlink(missing_ok=True)

if __name__=='__main__':
    print(perform())
