#!/usr/bin/env python3
"""Copy an existing v0.9 (or schema-compatible) SQLite DB without changing source.

Stop original launch agents before migration. Never overwrite destination data.
"""
import argparse
from pathlib import Path
import shutil
import sqlite3

ROOT=Path(__file__).resolve().parent

def migrate(folder):
    old=Path(folder).expanduser().resolve()
    if old==ROOT:raise ValueError('Old and new directories must differ')
    if not old.is_dir():raise ValueError('Source directory missing')
    previous=old/'oracle.sqlite3'
    if not previous.is_file():raise FileNotFoundError('Old SQLite DB not found')
    current=ROOT/'oracle.sqlite3'
    if current.exists():raise FileExistsError('New DB already exists; will not overwrite')
    tmp=ROOT/'oracle.migration.tmp'
    if tmp.exists():raise FileExistsError('Temporary migration copy exists; inspect before retry')
    src=sqlite3.connect(f'file:{previous}?mode=ro',uri=True)
    try:
        dest=sqlite3.connect(tmp)
        try:src.backup(dest)
        finally:dest.close()
        chk=sqlite3.connect(tmp)
        try:
            integrity=chk.execute('PRAGMA integrity_check').fetchone()[0]
            tables={r[0] for r in chk.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if integrity!='ok' or not {'raw_snapshots','candles'}.issubset(tables):
                raise RuntimeError('Invalid or incompatible ORACLE database')
        finally:chk.close()
        tmp.replace(current)
    finally:
        src.close()
        if tmp.exists():tmp.unlink()
    env=old/'.env'
    if env.is_file() and not (ROOT/'.env').exists():
        shutil.copy2(env,ROOT/'.env');(ROOT/'.env').chmod(0o600)
    model=old/'model_artifacts'
    if model.is_dir() and not (ROOT/'model_artifacts').exists():
        shutil.copytree(model,ROOT/'model_artifacts')
    return f'Migration complete: {current}; original unchanged: {previous}'

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--from',dest='folder',required=True)
    print(migrate(p.parse_args().folder))
