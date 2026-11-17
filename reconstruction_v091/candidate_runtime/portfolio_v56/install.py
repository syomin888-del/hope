#!/usr/bin/env python3
"""V5.6 standalone, hash-pinned additive upgrade of installed V5.5 (no betting)."""
from __future__ import annotations
import argparse,datetime as dt,hashlib,json,os,py_compile,shutil,sqlite3,subprocess,sys,tempfile
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=Path('/opt/ORACLE_CORE_v11')
DB=Path('/var/lib/docker/volumes/oracle_core_v10_oracle-data/_data/oracle.sqlite3')
REPLACE=('oracle_v5.py','oracle_v5_settlement.py','oracle_console.py','oracle_v5.html')
ADD=('oracle_v56_portfolio.py',)
FILES=REPLACE+ADD
SERVICES=('sportslab','dashboard')
def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def command(argv,project=ROOT,timeout=1200):
    p=subprocess.run(argv,cwd=project,text=True,capture_output=True,timeout=timeout)
    if p.returncode:raise RuntimeError(' '.join(argv)+'\n'+(p.stderr or p.stdout)[-2200:])
    return p.stdout.strip()
def check(root=ROOT,db=DB,docker=True):
    m=json.loads((HERE/'manifest.json').read_text())
    for f in FILES:
        if digest(HERE/'files'/f)!=m['files'][f]['sha256']:raise RuntimeError('RELEASE_HASH_MISMATCH '+f)
        if f.endswith('.py'):py_compile.compile(str(HERE/'files'/f),doraise=True)
    if not (root/'compose.yaml').is_file():raise RuntimeError('ORACLE compose.yaml missing')
    if not db.is_file():raise RuntimeError('ORACLE database missing')
    for f in REPLACE:
        if not (root/f).is_file():raise RuntimeError('MISSING_BASE '+f)
        if digest(root/f)!=m['files'][f]['base_sha256']:raise RuntimeError('UNRECOGNIZED_INSTALLED_V55 '+f)
    for f in ADD:
        if (root/f).exists() and digest(root/f)!=m['files'][f]['sha256']:
            raise RuntimeError('CONFLICTING_NEW_MODULE '+f)
    with sqlite3.connect('file:'+db.as_posix()+'?mode=ro',uri=True,timeout=45) as c:
        if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('SQLITE_QUICK_CHECK_FAILED')
        for t in ('v55_paper','v5_forecasts','v5_catalog','sf_matches','v4_games'):
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone():
                raise RuntimeError('REQUIRED_TABLE_MISSING '+t)
    if docker:
        available=set(command(['docker','compose','config','--services'],root).split())
        if not set(SERVICES).issubset(available):raise RuntimeError('DOCKER_SERVICES_MISSING')
        if not command(['docker','compose','ps','-q','sportslab'],root):raise RuntimeError('SPORTSLAB_NOT_RUNNING')
    return 'PREFLIGHT OK: installed V5.5 sources and SQLite are compatible with V5.6'
def backup_db(src,dest):
    free=shutil.disk_usage(dest.parent).free
    if free<int(src.stat().st_size*1.5)+160_000_000:raise RuntimeError('DISK_SPACE_FOR_BACKUP_INSUFFICIENT')
    source=sqlite3.connect('file:'+src.as_posix()+'?mode=ro',uri=True,timeout=90)
    target=sqlite3.connect(dest,timeout=90)
    try:
        source.backup(target,pages=512,sleep=0.075)
        if target.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('BACKUP_CHECK_FAILED')
    finally:source.close();target.close()
    dest.chmod(0o600)
def atomic_copy(src,dest):
    with tempfile.NamedTemporaryFile(dir=dest.parent,prefix='.'+dest.name+'.v56-',delete=False) as tmp:
        path=Path(tmp.name);tmp.write_bytes(src.read_bytes());tmp.flush();os.fsync(tmp.fileno())
    try:os.chmod(path,0o644);os.replace(path,dest)
    finally:
        if path.exists():path.unlink()
def health(root):
    command(['docker','compose','up','-d','--build','--no-deps',*SERVICES],root,1200)
    v=command(['docker','compose','exec','-T','sportslab','python','-c',
        'import oracle_v5,oracle_v5_settlement,oracle_v56_portfolio;'
        'assert callable(oracle_v56_portfolio.sync);print("V56_IMPORT_OK")'],root,120)
    if 'V56_IMPORT_OK' not in v:raise RuntimeError('SPORTSLAB_V56_IMPORT_FAILED')
    v=command(['docker','compose','exec','-T','dashboard','python','-c',
        'import oracle_v5,oracle_v56_portfolio,oracle_console;print("V56_DASHBOARD_OK")'],root,120)
    if 'V56_DASHBOARD_OK' not in v:raise RuntimeError('DASHBOARD_V56_IMPORT_FAILED')
    for service in SERVICES:
        ids=command(['docker','compose','ps','-q',service],root).splitlines()
        if not ids or command(['docker','inspect','-f','{{.State.Running}}',ids[0]],root)!='true':
            raise RuntimeError('SERVICE_NOT_RUNNING '+service)
def apply(root=ROOT,db=DB):
    print(check(root,db),flush=True)
    dest=root/'backups'/('ORACLE_V5_6_'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    dest.mkdir(mode=0o700,parents=True,exist_ok=False)
    before={f:(root/f).exists() for f in FILES}
    for f in FILES:
        if before[f]:shutil.copy2(root/f,dest/f)
    print('Backing up SQLite online ...',flush=True)
    backup_db(db,dest/'oracle-before.sqlite3')
    replaced=[]
    try:
        for f in FILES:
            atomic_copy(HERE/'files'/f,root/f);replaced.append(f)
        health(root)
        result=command(['docker','compose','exec','-T','sportslab','python','-c',
            'import sqlite3,oracle_v56_portfolio;'
            'c=sqlite3.connect("/app/state/oracle.sqlite3",timeout=45);'
            'oracle_v56_portfolio.setup(c);c.close();print("V56_DB_READY")'],root,120)
        if 'V56_DB_READY' not in result:raise RuntimeError('V56_SCHEMA_FAILED')
        print('INSTALL OK: V5.6 portfolio is ready; backup:',dest,flush=True)
    except Exception:
        print('INSTALL ERROR: restoring original V5.5 source files',file=sys.stderr,flush=True)
        for f in reversed(replaced):
            if before[f]:atomic_copy(dest/f,root/f)
            else:(root/f).unlink(missing_ok=True)
        try:command(['docker','compose','up','-d','--build','--no-deps',*SERVICES],root,1200)
        except Exception as e:print('ROLLBACK_RESTART_ERROR',str(e)[:500],file=sys.stderr)
        print('SQLite not overwritten; verified backup',dest/'oracle-before.sqlite3',file=sys.stderr)
        raise

def main():
    a=argparse.ArgumentParser();a.add_argument('action',choices=('check','apply'))
    opt=a.parse_args()
    try:print(check() if opt.action=='check' else apply())
    except Exception as e:print('INSTALL ERROR:',type(e).__name__,str(e),file=sys.stderr);sys.exit(2)
if __name__=='__main__':main()
