#!/usr/bin/env python3
"""Safely transfer an existing v0.7 research DB and private configuration.

This is a one-time offline operation. Does not alter the old installation.
Stop OLD processes before running, and don't start new services until complete.
"""
import argparse
import sqlite3
import shutil
from pathlib import Path

ROOT=Path(__file__).resolve().parent

def migrate(old):
    old=Path(old).expanduser().resolve()
    if old==ROOT:raise ValueError('Source must be separate from destination')
    if not old.is_dir():raise ValueError('Previous installation folder not found')
    source=old/'oracle.sqlite3'
    if not source.is_file():raise ValueError('Old research database not found')
    dest=ROOT/'oracle.sqlite3'
    if dest.exists():raise FileExistsError('Destination DB already exists; refusing overwrite')
    cn=sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True)
    try:
        tmp=ROOT/'oracle.migration.tmp'
        if tmp.exists():raise FileExistsError('Temporary file exists; inspect it first')
        try:
            out=sqlite3.connect(str(tmp))
            try:cn.backup(out)
            finally:out.close()
            test=sqlite3.connect(str(tmp))
            try:
                integrity=test.execute('PRAGMA integrity_check').fetchone()[0]
                if integrity!='ok':raise RuntimeError('Database integrity check failed')
            finally:test.close()
            tmp.rename(dest)
        finally:
            if tmp.exists():tmp.unlink()
    finally:cn.close()
    config=old/'.env'
    if config.is_file() and not (ROOT/'.env').exists():
        shutil.copy2(config,ROOT/'.env')
        (ROOT/'.env').chmod(0o600)
    # Copy artifacts only from user's known local project; models remain paper-only.
    arts=old/'model_artifacts'
    if arts.is_dir() and not (ROOT/'model_artifacts').exists():
        shutil.copytree(arts,ROOT/'model_artifacts')
    return {'database':str(dest),'status':'copied, source unchanged',
      'old_models':'copied for existing shadow-only analysis' if arts.is_dir() else 'none'}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--from',dest='old',required=True,help='Existing ORACLE_CORE_v07 directory')
    args=ap.parse_args()
    print(migrate(args.old))
if __name__=='__main__':main()
