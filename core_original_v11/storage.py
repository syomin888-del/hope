#!/usr/bin/env python3
'ORACLE safe storage compression and archival.'
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid
import zlib

import collect

ROOT = Path(__file__).resolve().parent
ARCHIVE = Path(os.environ.get('ORACLE_ARCHIVE_DIR', str(ROOT / 'archives'))).resolve()
MAGIC = b'ORCLZ1\x00'
UTC = dt.timezone.utc

def setup(c):
    c.executescript('''
      CREATE INDEX IF NOT EXISTS raw_versions_fast ON raw_snapshots(provider,subject,id DESC);
      CREATE TABLE IF NOT EXISTS candle_archives(
        provider TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
        year_month TEXT NOT NULL, file_path TEXT NOT NULL,
        row_count INTEGER NOT NULL, rows_sha256 TEXT NOT NULL,
        file_sha256 TEXT NOT NULL, archived_at TEXT NOT NULL,
        PRIMARY KEY(provider,symbol,timeframe,year_month));
      CREATE TABLE IF NOT EXISTS storage_skips(snapshot_id INTEGER PRIMARY KEY,reason TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS storage_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,run_at TEXT NOT NULL,
        status TEXT NOT NULL,compressed INTEGER NOT NULL,
        archived INTEGER NOT NULL,freed_estimate INTEGER NOT NULL,
        details TEXT NOT NULL);
    ''')
    c.commit()

def unpack(payload, expected_sha=None):
    if isinstance(payload,bytes) and payload.startswith(MAGIC):
        text=zlib.decompress(payload[len(MAGIC):]).decode('utf8')
    elif isinstance(payload,bytes):
        text=payload.decode('utf8')
    else:
        text=payload
    if expected_sha and hashlib.sha256(text.encode('utf8')).hexdigest()!=expected_sha:
        raise ValueError('Source integrity failure')
    return text

def compress_sources(c, limit=500, age_days=30):
    # Do not touch newly observed snapshots, latest source versions, or unprocessed knowledge.
    if limit<1 or limit>10000 or age_days<7:
        raise ValueError('Unsafe limits')
    if not _table(c,'knowledge_cursor'):
        return {'status':'not_ready','rows':0,'bytes_saved':0}
    cur=c.execute("SELECT last_id FROM knowledge_cursor WHERE name='raw_snapshots'").fetchone()
    if cur is None:
        return {'status':'not_ready','rows':0,'bytes_saved':0}
    cutoff=(dt.datetime.now(UTC)-dt.timedelta(days=age_days)).isoformat()
    rows=c.execute('''SELECT r.id,r.payload,r.sha256 FROM raw_snapshots r
      WHERE r.id<=? AND r.observed_at<? AND typeof(r.payload)='text'
      AND LENGTH(r.payload)>=256
      AND EXISTS (SELECT 1 FROM raw_snapshots later
                  WHERE later.provider=r.provider AND later.subject=r.subject
                  AND later.id>r.id)
      AND NOT EXISTS(SELECT 1 FROM storage_skips s WHERE s.snapshot_id=r.id)
      ORDER BY r.id LIMIT ?''',(cur[0],cutoff,limit)).fetchall()
    done=saved=0
    for id_,payload,sha in rows:
        raw=payload.encode('utf8')
        if hashlib.sha256(raw).hexdigest()!=sha:
            with c:c.execute('INSERT OR IGNORE INTO storage_skips VALUES(?,?)',(id_,'checksum_mismatch'))
            continue
        packed=MAGIC+zlib.compress(raw,6)
        if len(packed)>=len(raw)*.92:
            with c:c.execute('INSERT OR IGNORE INTO storage_skips VALUES(?,?)',(id_,'not_compressible'))
            continue
        if unpack(packed,sha)!=payload:raise RuntimeError('Compression roundtrip failed')
        with c:
            c.execute("UPDATE raw_snapshots SET payload=? WHERE id=? AND typeof(payload)='text'",
                (sqlite3.Binary(packed),id_))
        saved+=len(raw)-len(packed);done+=1
    return {'status':'ok','rows':done,'bytes_saved':saved}

def _table(c,name):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())

def _bounds(month):
    start=dt.datetime.strptime(month,'%Y-%m').replace(tzinfo=UTC)
    stop=(start.replace(year=start.year+1,month=1) if start.month==12
          else start.replace(month=start.month+1))
    return int(start.timestamp()*1000),int(stop.timestamp()*1000)

def _monthly_rows(c,symbol,month):
    lo,hi=_bounds(month)
    return c.execute('''SELECT open_ms,open,high,low,close,volume,provider FROM candles
      WHERE provider='binance_archive' AND symbol=? AND timeframe='1h'
      AND open_ms>=? AND open_ms<? ORDER BY open_ms''',(symbol,lo,hi)).fetchall()

def _digest(rows):
    digest=hashlib.sha256()
    for r in rows:
        digest.update(json.dumps([int(r[0])]+[float(v) for v in r[1:6]]+[r[6]],
              separators=(',',':'),allow_nan=False).encode('utf8')+b'\n')
    return digest.hexdigest()

def _safe_path(rel):
    target=(ARCHIVE/rel).resolve()
    if not target.is_relative_to(ARCHIVE):raise ValueError('Escaping archive root')
    return target

def parquet_rows(paths):
    if not paths:return []
    try:import duckdb
    except ImportError as e:raise RuntimeError('Install duckdb to read archived candle partitions') from e
    safe=[]
    for name in paths:
        path=_safe_path(name)
        if not path.is_file():raise RuntimeError('Missing candle archive: '+str(path))
        safe.append(str(path))
    with duckdb.connect(':memory:') as db:
        return db.execute('''SELECT open_ms,open,high,low,close,volume,provider
               FROM read_parquet(?) ORDER BY open_ms''',[safe]).fetchall()

def archived_candles(c,symbol):
    if not _table(c,'candle_archives'):return []
    entries=c.execute('''SELECT file_path,file_sha256,row_count,rows_sha256
      FROM candle_archives WHERE symbol=? AND provider='binance_archive' AND timeframe='1h'
      ORDER BY year_month''',(symbol,)).fetchall()
    if not entries:return []
    result=[]
    for relative,expected_file,n,expected_rows in entries:
        path=_safe_path(relative)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected_file:
            raise RuntimeError('Historical archive checksum failed: '+relative)
        rows=parquet_rows([relative])
        if len(rows)!=n or _digest(rows)!=expected_rows:
            raise RuntimeError('Historical data verification failed: '+relative)
        result.extend(rows)
    return result

def archive_month(c, min_age_days=45):
    # Only a completed, successfully ingested *monthly Binance archive* can be moved.
    # Never touch live exchange data, incomplete months or unsettled forecasts.
    if min_age_days<30:raise ValueError('Unacceptable archival age')
    try:import duckdb
    except ImportError:return {'status':'duckdb_unavailable','rows':0}
    setup(c)
    if not _table(c,'history_downloads'):
        return {'status':'no_history_index','rows':0}
    cutoff=(dt.datetime.now(UTC)-dt.timedelta(days=min_age_days)).strftime('%Y-%m')
    candidates=c.execute('''SELECT symbol,strftime('%Y-%m',open_ms/1000,'unixepoch') m,COUNT(*)
       FROM candles WHERE provider='binance_archive' AND timeframe='1h'
       GROUP BY symbol,m HAVING m<? AND COUNT(*)>=24 ORDER BY m,symbol LIMIT 25''',
       (cutoff,)).fetchall()
    for symbol,month,amount in candidates:
        if symbol not in ('BTCUSDT','ETHUSDT','SOLUSDT'):continue
        status=c.execute('''SELECT status,rows FROM history_downloads
            WHERE symbol=? AND year=? AND month=?''',
            (symbol,int(month[:4]),int(month[5:]))).fetchone()
        if not status or status[0]!='ok':continue
        if c.execute('''SELECT 1 FROM candle_archives WHERE
                 provider='binance_archive' AND symbol=? AND timeframe='1h' AND year_month=?''',
                 (symbol,month)).fetchone():continue
        rows=_monthly_rows(c,symbol,month)
        if len(rows)!=amount or len(rows)!=status[1]:continue
        before=_digest(rows)
        rel=f'candles/binance_archive/{symbol}/{month}.parquet'
        target=_safe_path(rel)
        target.parent.mkdir(parents=True,exist_ok=True)
        if shutil.disk_usage(target.parent).free<512*1024**2:
            return {'status':'disk_low','rows':0}
        tmp=target.with_name(target.stem+'-'+uuid.uuid4().hex+'.tmp.parquet')
        try:
            with duckdb.connect(':memory:') as d:
                d.execute('''CREATE TABLE x(open_ms BIGINT,open DOUBLE,high DOUBLE,
                          low DOUBLE,close DOUBLE,volume DOUBLE,provider VARCHAR)''')
                d.executemany('INSERT INTO x VALUES (?,?,?,?,?,?,?)', rows)
                d.execute("COPY x TO '{}' (FORMAT PARQUET,COMPRESSION ZSTD)".format(str(tmp).replace("'","''")))
                recovered=d.execute('''SELECT open_ms,open,high,low,close,volume,provider
                                    FROM read_parquet(?) ORDER BY open_ms''',[str(tmp)]).fetchall()
            if len(recovered)!=len(rows) or _digest(recovered)!=before:
                raise RuntimeError('Parquet archival verification failed')
            file_sha=hashlib.sha256(tmp.read_bytes()).hexdigest()
            c.execute('BEGIN IMMEDIATE')
            try:
                if _digest(_monthly_rows(c,symbol,month))!=before:
                    raise RuntimeError('Candle source changed during archive')
                os.replace(tmp,target)
                c.execute('''INSERT INTO candle_archives(
                    provider,symbol,timeframe,year_month,file_path,row_count,
                    rows_sha256,file_sha256,archived_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                    ('binance_archive',symbol,'1h',month,rel,len(rows),before,file_sha,
                     dt.datetime.now(UTC).isoformat()))
                lo,hi=_bounds(month)
                deleted=c.execute('''DELETE FROM candles WHERE
                    provider='binance_archive' AND symbol=? AND timeframe='1h'
                    AND open_ms>=? AND open_ms<?''',(symbol,lo,hi)).rowcount
                if deleted!=len(rows):raise RuntimeError('Unexpected deleted-row count')
                c.commit()
                return {'status':'ok','rows':deleted,'symbol':symbol,'month':month,
                        'file_bytes':target.stat().st_size}
            except Exception:
                c.rollback()
                raise
        finally:
            tmp.unlink(missing_ok=True)
    return {'status':'nothing_due','rows':0}

def clean_rotated_logs(days=21):
    if days<7:raise ValueError('Unsafe log retention')
    directory=ROOT/'logs'
    if not directory.is_dir():return 0
    count=0
    for path in directory.iterdir():
        if (path.is_file() and '.log.' in path.name
             and path.stat().st_mtime<time.time()-days*86400):
            path.unlink();count+=1
    return count

def report(c=None):
    path=collect.DB
    def used(p):
        if p.is_file():return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob('*') if f.is_file()) if p.is_dir() else 0
    disk=shutil.disk_usage(path.parent)
    owns=c is None
    if owns and path.exists():c=sqlite3.connect(str(path))
    compressed=partitions=0
    if c is not None:
        if _table(c,'raw_snapshots'):
            compressed=c.execute("SELECT COUNT(*) FROM raw_snapshots WHERE typeof(payload)='blob'").fetchone()[0]
        if _table(c,'candle_archives'):
            partitions=c.execute("SELECT COUNT(*) FROM candle_archives").fetchone()[0]
    if owns and c is not None:c.close()
    return {'db_bytes':used(path),'db_wal_bytes':used(Path(str(path)+'-wal')),
        'archives_bytes':used(ARCHIVE),
        'backups_bytes':used(Path(os.environ.get('ORACLE_BACKUP_DIR',str(ROOT/'backups')))),
        'logs_bytes':used(ROOT/'logs'),
        'free_bytes':disk.free,'total_bytes':disk.total,
        'compressed_snapshot_versions':compressed,'parquet_partitions':partitions}

def run(c,limit=500):
    setup(c)
    if shutil.disk_usage(collect.DB.parent).free<2*1024**3:
        outcome={'status':'disk_low','compressed_rows':0,'archived_rows':0,'estimated_saved':0}
    else:
        a=compress_sources(c,limit=limit)
        b=archive_month(c)
        clean_rotated_logs()
        c.execute('PRAGMA optimize')
        outcome={'status':'ok','compressed_rows':a['rows'],'archived_rows':b['rows'],
                 'estimated_saved':a['bytes_saved'],'archive_status':b['status']}
    with c:
        c.execute('''INSERT INTO storage_runs(run_at,status,compressed,archived,freed_estimate,details)
                     VALUES(?,?,?,?,?,?)''',
                  (dt.datetime.now(UTC).isoformat(),outcome['status'],
                   outcome['compressed_rows'],outcome['archived_rows'],
                   outcome['estimated_saved'],json.dumps(outcome,ensure_ascii=False)))
    return outcome

def vacuum_when_stopped(c):
    # This operation is intentionally MANUAL: VACUUM may need as much disk as the DB
    # and must never lock a live continuous-research service automatically.
    if _table(c,'telemetry_heartbeats'):
        for service,last_seen in c.execute('SELECT service,last_seen_utc FROM telemetry_heartbeats'):
            try:
                age=(dt.datetime.now(UTC)-dt.datetime.fromisoformat(last_seen)).total_seconds()
                if -30<=age<=300:
                    raise RuntimeError('Stop the active '+service+' service before VACUUM')
            except ValueError:pass
    before=collect.DB.stat().st_size
    if shutil.disk_usage(collect.DB.parent).free<max(before*2,1024**3):
        raise RuntimeError('Not enough free disk space for safe SQLite VACUUM')
    c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    c.execute('VACUUM')
    after=collect.DB.stat().st_size
    return {'db_before':before,'db_after':after,'bytes_reclaimed':max(0,before-after)}

def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['report','once','verify','vacuum'])
    a=p.parse_args()
    c=collect.db_open()
    setup(c)
    if a.command=='report':out=report(c)
    elif a.command=='once':out=run(c)
    elif a.command=='vacuum':out=vacuum_when_stopped(c)
    else:
        failed=[];checked=0
        for rel,sha in c.execute('SELECT file_path,file_sha256 FROM candle_archives'):
            file=_safe_path(rel);checked+=1
            if not file.exists() or hashlib.sha256(file.read_bytes()).hexdigest()!=sha:
                failed.append(rel)
        out={'checked':checked,'failed':failed}
    print(json.dumps(out,ensure_ascii=False,indent=2))
    if a.command=='verify' and out['failed']:raise SystemExit(2)

if __name__=='__main__':main()
