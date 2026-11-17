#!/usr/bin/env python3
'Copy a local ORACLE v1.0 database safely; never overwrite existing v1.1 history.'
import argparse
import datetime as dt
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT=Path(__file__).resolve().parent

def migrate(old):
    old=Path(old).expanduser().resolve()
    if old==ROOT:raise RuntimeError('Source and destination cannot be the same')
    source=old/'oracle.sqlite3'
    target=ROOT/'oracle.sqlite3'
    if not source.is_file():raise RuntimeError('Source database not found: '+str(source))
    if target.exists():raise RuntimeError('Target database already exists: refusing to overwrite')
    with sqlite3.connect(str(source)) as c:
        # Avoid freezing a moving target. Stop the old processes before copying.
        present={x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'telemetry_heartbeats' in present:
            for name,timestamp in c.execute('SELECT service,last_seen_utc FROM telemetry_heartbeats'):
                try:
                    age=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(timestamp)).total_seconds()
                    if -30<=age<=300:raise RuntimeError('Previous '+name+' process still active; stop old ORACLE before migration')
                except ValueError:pass
        tmp=ROOT/'oracle.sqlite3.migrating'
        try:
            with sqlite3.connect(str(tmp)) as dest:
                c.backup(dest,pages=250,sleep=.01)
                if dest.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                    raise RuntimeError('Migrated DB integrity check failed')
            tmp.replace(target)
        finally:tmp.unlink(missing_ok=True)
    # Preserve the user's secrets and model artifacts; NEVER copy transient logs.
    for filename in ('.env','approved_models.json'):
        source_file=old/filename
        dest_file=ROOT/filename
        if source_file.is_file():
            if filename=='approved_models.json' and dest_file.exists():
                shutil.copy2(dest_file,ROOT/'approved_models.default.json')
            shutil.copy2(source_file,dest_file)
            if filename=='.env':dest_file.chmod(0o600)
    for subdir in ('model_artifacts','archives'):
        original=old/subdir
        dest=ROOT/subdir
        if original.is_dir():shutil.copytree(original,dest,dirs_exist_ok=True)
    print('Database verified and copied to:',target)
    print('Original ORACLE folder and backups preserved:',old)
    print('Run: python3 storage.py report')

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--from',dest='source',required=True)
    a=p.parse_args()
    try:migrate(a.source)
    except Exception as e:
        print('Migration refused:',e,file=sys.stderr);raise SystemExit(1)
