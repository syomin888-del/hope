#!/usr/bin/env python3
"""ORACLE 7.0, SHA-pinned upgrade directly from installed V5.7.
Additive research-only components; transactional source-file rollback, online SQLite backup.
"""
from __future__ import annotations
import argparse,contextlib,datetime as dt,hashlib,json,os,py_compile,shutil,sqlite3,subprocess,sys,tempfile
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=Path('/opt/ORACLE_CORE_v11')
DB=Path('/var/lib/docker/volumes/oracle_core_v10_oracle-data/_data/oracle.sqlite3')
SERVICES=('sportslab','dashboard')

def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def cmd(args,root=ROOT,timeout=1200):
    r=subprocess.run(args,cwd=root,text=True,capture_output=True,timeout=timeout)
    if r.returncode:raise RuntimeError(' '.join(args)+'\n'+(r.stderr or r.stdout)[-1400:])
    return r.stdout.strip()

def check(root=ROOT,db=DB,docker=True):
    if not (root/'compose.yaml').is_file():raise RuntimeError('COMPOSE_NOT_FOUND')
    if not db.is_file():raise RuntimeError('DB_NOT_FOUND')
    m=json.loads((HERE/'manifest.json').read_text())
    for name,v in m['files'].items():
        src=HERE/'files'/name;dest=root/name
        if not src.is_file() or digest(src)!=v['sha256']:raise RuntimeError('RELEASE_HASH_MISMATCH '+name)
        if src.suffix=='.py':py_compile.compile(str(src),doraise=True)
        allowed=v.get('base_sha256')
        if allowed is None:
            if dest.exists():raise RuntimeError('NEW_FILE_ALREADY_EXISTS '+name)
        elif not dest.is_file() or digest(dest) not in allowed:
            raise RuntimeError('SOURCE_HASH_MISMATCH '+name+' '+(digest(dest) if dest.is_file() else 'MISSING'))
    with contextlib.closing(sqlite3.connect('file:'+db.as_posix()+'?mode=ro',uri=True,timeout=60)) as c:
        if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('DB_CORRUPT')
        for t in ('v5_catalog','v5_forecasts','v55_paper','v56_portfolio','fb_full_packets','mw_quotes'):
            if not c.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(t,)).fetchone():
                raise RuntimeError('DB_TABLE_MISSING '+t)
    if docker:
        names=set(cmd(['docker','compose','config','--services'],root).split())
        if not set(SERVICES).issubset(names):raise RuntimeError('COMPOSE_SERVICE_MISSING')
        for service in SERVICES:
            if not cmd(['docker','compose','ps','-q',service],root):raise RuntimeError('SERVICE_NOT_RUNNING '+service)
    return 'PREFLIGHT OK: installed V5.7 with all expected source fingerprints'

def backup_db(db,dest):
    if shutil.disk_usage(dest.parent).free<db.stat().st_size*2+200_000_000:raise RuntimeError('BACKUP_SPACE_INSUFFICIENT')
    with contextlib.closing(sqlite3.connect('file:'+db.as_posix()+'?mode=ro',uri=True,timeout=90)) as c,contextlib.closing(sqlite3.connect(dest,timeout=90)) as dst:
        c.backup(dst,pages=512,sleep=.08)
        if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('BACKUP_CORRUPT')
    dest.chmod(0o600)

def atomic(src,dst):
    tmp=None
    try:
        with tempfile.NamedTemporaryFile(dir=dst.parent,delete=False,prefix='.'+dst.name+'.v7_') as f:
            tmp=Path(f.name);f.write(src.read_bytes());f.flush();os.fsync(f.fileno())
        tmp.chmod(0o644);os.replace(tmp,dst)
    finally:
        if tmp is not None:tmp.unlink(missing_ok=True)

def health(root=ROOT):
    cmd(['docker','compose','up','-d','--build','--no-deps',*SERVICES],root,1200)
    code='import oracle_v5,oracle_v58_brain,oracle_v7,oracle_v7_core,oracle_v7_crypto,oracle_console,oracle_scorecard;print("V7_IMPORT_OK")'
    for svc in SERVICES:
        if 'V7_IMPORT_OK' not in cmd(['docker','compose','exec','-T',svc,'python','-c',code],root,140):
            raise RuntimeError('IMPORT_CHECK_FAILED '+svc)
    # Additive DB schema AFTER import validation; old tables untouched.
    if '"version": "7.0"' not in cmd(['docker','compose','exec','-T','sportslab','python','oracle_v7.py','init'],root,180):
        raise RuntimeError('V7_INIT_FAILED')

def apply(root=ROOT,db=DB,docker=True):
    print(check(root,db,docker),flush=True)
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=root/'backups'/('ORACLE_V7_0_'+stamp)
    backup.mkdir(mode=0o700,parents=True,exist_ok=False);backup.chmod(0o700)
    m=json.loads((HERE/'manifest.json').read_text());old={}
    for name,v in m['files'].items():
        if v['base_sha256'] is not None:
            shutil.copy2(root/name,backup/name);old[name]=backup/name
    print('Backing up SQLite online ...',flush=True)
    backup_db(db,backup/'oracle-before.sqlite3')
    changed=[]
    try:
        for name in m['files']:
            atomic(HERE/'files'/name,root/name);changed.append(name)
        if docker:health(root)
        print('INSTALL OK: ORACLE V7.0 GLOBAL RESEARCH (paper only); backup:',backup,flush=True)
    except BaseException:
        print('INSTALL ERROR: restoring source files; SQLite NOT overwritten',file=sys.stderr,flush=True)
        for name in reversed(changed):
            try:
                if name in old:atomic(old[name],root/name)
                else:(root/name).unlink(missing_ok=True)
            except Exception as exc:print('ROLLBACK_FAILED',name,type(exc).__name__,file=sys.stderr)
        if docker:
            try:cmd(['docker','compose','up','-d','--build','--no-deps',*SERVICES],root,1200)
            except Exception as exc:print('ROLLBACK_RESTART_ERROR',type(exc).__name__,file=sys.stderr)
        print('Online backup:',backup/'oracle-before.sqlite3',file=sys.stderr)
        raise

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('check','apply'));a=p.parse_args()
    try:
        if a.action=='check':print(check())
        else:apply()
    except Exception as e:print('INSTALL ERROR:',type(e).__name__,str(e),file=sys.stderr);raise SystemExit(2)
if __name__=='__main__':main()
