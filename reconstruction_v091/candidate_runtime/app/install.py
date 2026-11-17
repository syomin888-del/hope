#!/usr/bin/env python3
"""ORACLE V5.5 major cumulative release over verified ORACLE V5.3.
Hash-pinned; consistent online backup; staged atomic source replacement; rollback.
Never replaces active SQLite or creates live betting execution endpoints.
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, os, py_compile, shutil, sqlite3, subprocess, sys, tempfile
from pathlib import Path
from contextlib import closing
HERE=Path(__file__).resolve().parent
ROOT=Path('/opt/ORACLE_CORE_v11')
DATABASE=Path('/var/lib/docker/volumes/oracle_core_v10_oracle-data/_data/oracle.sqlite3')
NEEDED=('oracle_v5.py','oracle_v4.py','oracle_console.py','oracle_v5.html')
NEW=('oracle_v5_settlement.py',)
FILES=NEEDED+NEW
SERVICES=('sportslab','dashboard')

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def command(cmd,project=ROOT,timeout=1100):
    result=subprocess.run(cmd,cwd=project,text=True,capture_output=True,timeout=timeout)
    if result.returncode:
        raise RuntimeError(' '.join(cmd)+'\n'+(result.stderr or result.stdout)[-2200:])
    return result.stdout.strip()

def check(project=ROOT,db=DATABASE,docker=True):
    m=json.loads((HERE/'manifest.json').read_text())
    for f in FILES:
        if sha(HERE/'files'/f)!=m['files'][f]['sha256']:
            raise RuntimeError('CORRUPT_RELEASE_FILE: '+f)
        if f.endswith('.py'):
            py_compile.compile(str(HERE/'files'/f),doraise=True)
    if not (project/'compose.yaml').is_file():raise RuntimeError('ORACLE compose.yaml not found')
    if not db.is_file():raise RuntimeError('Main ORACLE SQLite missing: '+str(db))
    for f in NEEDED:
        dest=project/f
        if not dest.is_file():raise RuntimeError('Missing deployed dependency '+f)
        if sha(dest) not in m['files'][f]['supported_base_sha256']:
            raise RuntimeError('UNRECOGNIZED_INSTALLED_SOURCE: '+f+'; upgrade cancelled, existing source unchanged')
    for f in NEW:
        dest=project/f
        if dest.exists() and sha(dest)!=m['files'][f]['sha256']:
            raise RuntimeError('New V5 file already exists with different content: '+f)
    for dep in ('oracle_fonbet_full.py','oracle_fonbet_bridge.py','oracle_v4_sports.py'):
        if not (project/dep).is_file():raise RuntimeError('Required deployed module missing: '+dep)
    with closing(sqlite3.connect('file:'+db.as_posix()+'?mode=ro',uri=True,timeout=30)) as c:
        qc=c.execute('PRAGMA quick_check').fetchone()[0]
        if qc!='ok':raise RuntimeError('Active ORACLE database failed quick_check: '+str(qc))
        for table in ('sf_forecasts','sf_matches','v4_games','v4_sport_preds','fb_full_current','fb_full_events','fb_full_history','fb_full_packets','fb_bridge_links','v5_forecasts'):
            if not c.execute('SELECT 1 FROM sqlite_master WHERE name=? AND type=\'table\'',(table,)).fetchone():
                raise RuntimeError('Required existing table missing: '+table)
    if docker:
        services=set(command(['docker','compose','config','--services'],project).split())
        if not set(SERVICES).issubset(services):raise RuntimeError('Required Docker services absent')
        if not command(['docker','compose','ps','-q','sportslab'],project):
            raise RuntimeError('SportsLab container not running')
    return 'PREFLIGHT OK: ORACLE V5.3 -> V5.5, source hashes, SQLite, Docker checked'

def backup_db(src,dst):
    # Online SQLite Backup API, NOT a raw copy of a WAL-managed DB.
    free=shutil.disk_usage(dst.parent).free
    needed=int(src.stat().st_size*1.4)+170_000_000
    if free<needed:raise RuntimeError('Insufficient free space for a consistent backup')
    src_db=sqlite3.connect('file:'+src.as_posix()+'?mode=ro',uri=True,timeout=60)
    backup=sqlite3.connect(str(dst),timeout=60)
    try:
        src_db.backup(backup,pages=512,sleep=.075)
        if backup.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise RuntimeError('SQLite backup quick_check failed')
    finally:backup.close();src_db.close()
    dst.chmod(0o600)

def atomic_replace(dst,src):
    with tempfile.NamedTemporaryFile(dir=dst.parent,prefix='.'+dst.name+'.v5.',delete=False) as fp:
        tmp=Path(fp.name);fp.write(src.read_bytes());fp.flush();os.fsync(fp.fileno())
    try:
        os.chmod(tmp,0o644)
        os.replace(tmp,dst)
    finally:
        if tmp.exists():tmp.unlink()

def check_service_health(project=ROOT):
    command(['docker','compose','up','-d','--build','--no-deps',*SERVICES],project)
    result=command(['docker','compose','exec','-T','sportslab','python','-c',
        'import oracle_v5,oracle_v4,oracle_v5_settlement,oracle_console;'
        'assert callable(oracle_v5.run) and callable(oracle_v5_settlement.settle);'
        'assert callable(oracle_v5_settlement.backfill);'
        'print("ORACLE_V55_IMPORT_OK")'],project,timeout=180)
    if 'ORACLE_V55_IMPORT_OK' not in result:raise RuntimeError('V5 Python import failed')
    # Dashboard must also import the new read-only paper endpoint module.
    dashboard=command(['docker','compose','exec','-T','dashboard','python','-c',
        'import oracle_v5,oracle_v5_settlement;print("V55_DASHBOARD_IMPORT_OK")'],project,timeout=90)
    if 'V55_DASHBOARD_IMPORT_OK' not in dashboard:raise RuntimeError('Dashboard import failed')
    for name in SERVICES:
        cid=command(['docker','compose','ps','-q',name],project).splitlines()
        if not cid:raise RuntimeError('Service '+name+' missing after compose restart')
        running=command(['docker','inspect','-f','{{.State.Running}}',cid[0]],project)
        if running!='true':raise RuntimeError('Service '+name+' stopped')

def apply(project=ROOT,db=DATABASE):
    print(check(project,db),flush=True)
    where=project/'backups'/('ORACLE_V5_5_'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    where.mkdir(parents=True,exist_ok=False)
    os.chmod(where,0o700)
    prev={f:(project/f).is_file() for f in FILES}
    for f in FILES:
        if prev[f]:shutil.copy2(project/f,where/f)
    print('SOURCE BACKUP:',where,flush=True)
    print('Creating CONSISTENT online SQLite backup...',flush=True)
    backup_db(db,where/'oracle-before.sqlite3')
    print('SQLITE BACKUP:',where/'oracle-before.sqlite3',flush=True)
    changed=[]
    try:
        for f in FILES:
            atomic_replace(project/f,HERE/'files'/f);changed.append(f)
        check_service_health(project)
        # Create empty additive V5 schema; existing data is never replaced/deleted.
        init=command(['docker','compose','exec','-T','sportslab','python','oracle_v5.py','init'],project,timeout=130)
        if '"state": "OK"' not in init:raise RuntimeError('V5 schema init failed: '+init[:500])
        ledger=command(['docker','compose','exec','-T','sportslab','python','-c',
            'import sqlite3,oracle_v5_settlement as s; c=sqlite3.connect("/app/state/oracle.sqlite3");s.setup(c);c.close();print("V55_LEDGER_OK")'],project,timeout=120)
        if 'V55_LEDGER_OK' not in ledger:raise RuntimeError('V55 ledger init failed')
        print('INSTALL OK: ORACLE V5.5; backups at:',where,flush=True)
        print('Check: docker compose exec -T sportslab python oracle_v5.py once',flush=True)
        print('Then: docker compose exec -T sportslab python oracle_v5.py settle',flush=True)
        print('Then: docker compose exec -T sportslab python oracle_v5.py doctor',flush=True)
        print('Panel: http://127.0.0.1:8765/v5 (via existing SSH tunnel)',flush=True)
    except Exception:
        print('INSTALL FAILED — restoring prior Python/HTML files',file=sys.stderr,flush=True)
        for f in changed:
            if prev[f]:atomic_replace(project/f,where/f)
            else:
                try:(project/f).unlink()
                except FileNotFoundError:pass
        try:command(['docker','compose','up','-d','--build','--no-deps',*SERVICES],project)
        except Exception as ex:print('ROLLBACK RESTART FAILED:',type(ex).__name__,str(ex)[:1500],file=sys.stderr)
        print('SQLite never overwritten; consistent backup:',where/'oracle-before.sqlite3',file=sys.stderr)
        raise

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('check','apply'))
    args=p.parse_args()
    try:
        if args.action=='check':print(check())
        else:apply()
    except Exception as e:
        print('INSTALL ERROR:',type(e).__name__,str(e),file=sys.stderr)
        raise SystemExit(2)
if __name__=='__main__':main()
