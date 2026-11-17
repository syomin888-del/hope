#!/usr/bin/env python3
"""Container-only EVA V1.3: initialise blank independent SQLite with V5.5 schema + policy.
Requires --network none and only an EVA app mount and EVA writable state directory.
Never imports a production database and never registers a real or synthetic signal.
"""
import datetime as dt
import importlib
import json
import os
import sqlite3
import sys
from pathlib import Path

DB=Path('/eva/state/eva_forward.sqlite3')
APP=Path('/eva/app')
SQL='''
CREATE TRIGGER eva_forward_insert BEFORE INSERT ON v55_paper BEGIN
 SELECT CASE WHEN NEW.status IS NOT 'PENDING' THEN RAISE(ABORT,'EVA_PENDING_ONLY') END;
 SELECT CASE WHEN NEW.odds IS NULL OR NEW.odds<1.70 OR NEW.odds>3.50
             THEN RAISE(ABORT,'EVA_ODDS_RANGE_1_70_TO_3_50') END;
 SELECT CASE WHEN NEW.probability IS NULL OR NEW.probability<=0 OR NEW.probability>=1
             THEN RAISE(ABORT,'EVA_PROBABILITY_INVALID') END;
 SELECT CASE WHEN NEW.stake IS NULL OR abs(NEW.stake-10.0)>0.000001
             THEN RAISE(ABORT,'EVA_PAPER_STAKE_NOT_TEN') END;
 SELECT CASE WHEN NEW.market IS NULL OR NEW.selection IS NULL
        OR upper(replace(NEW.market,' ','_')) IN ('DOUBLE_CHANCE','DOUBLECHANCE','DC','ДВОЙНОЙ_ШАНС')
        OR instr(NEW.market,'шанс')>0 OR instr(NEW.market,'ШАНС')>0 OR instr(NEW.market,'Шанс')>0
        OR trim(NEW.selection) IN ('1X','X2','12','1Х','Х2','1x','x2','1х','х2')
             THEN RAISE(ABORT,'EVA_DOUBLE_CHANCE_FORBIDDEN') END;
 SELECT CASE WHEN NEW.origin IS NULL OR NEW.origin='EVA_SYNTHETIC'
             THEN RAISE(ABORT,'EVA_SYNTHETIC_ORIGIN_FORBIDDEN') END;
 SELECT CASE WHEN julianday(NEW.quote_at) IS NULL OR julianday(NEW.registered_at) IS NULL
        OR julianday(NEW.kickoff_utc) IS NULL
        OR julianday(NEW.quote_at)>julianday(NEW.registered_at)
        OR julianday(NEW.registered_at)>=julianday(NEW.kickoff_utc)
        OR julianday(NEW.registered_at)>julianday('now')
        OR julianday(NEW.kickoff_utc)<=julianday('now')
        OR julianday(NEW.registered_at)<julianday((SELECT value FROM eva_control WHERE key='created_utc'))
             THEN RAISE(ABORT,'EVA_ONLY_NEW_PROSPECTIVE') END;
END;
CREATE TRIGGER eva_forward_no_delete BEFORE DELETE ON v55_paper BEGIN
 SELECT RAISE(ABORT,'EVA_LEDGER_IMMUTABLE_NO_DELETE');
END;
CREATE TRIGGER eva_forward_immutable_fields BEFORE UPDATE ON v55_paper WHEN
 NEW.forecast_id IS NOT OLD.forecast_id OR NEW.root_id IS NOT OLD.root_id OR
 NEW.original_event_id IS NOT OLD.original_event_id OR NEW.origin IS NOT OLD.origin OR
 NEW.market IS NOT OLD.market OR NEW.selection IS NOT OLD.selection OR
 NEW.probability IS NOT OLD.probability OR NEW.odds IS NOT OLD.odds OR
 NEW.stake IS NOT OLD.stake OR NEW.quote_key IS NOT OLD.quote_key OR
 NEW.quote_at IS NOT OLD.quote_at OR NEW.kickoff_utc IS NOT OLD.kickoff_utc OR
 NEW.registered_at IS NOT OLD.registered_at OR NEW.expected_ev_pct IS NOT OLD.expected_ev_pct
BEGIN SELECT RAISE(ABORT,'EVA_IMMUTABLE_PAPER_FIELDS'); END;
CREATE TRIGGER eva_forward_valid_settlement BEFORE UPDATE OF status ON v55_paper WHEN
 OLD.status IS NOT 'PENDING' OR NEW.status NOT IN ('WIN','LOSS','PUSH')
BEGIN SELECT RAISE(ABORT,'EVA_INVALID_SETTLEMENT_TRANSITION'); END;
'''

def create(db:Path=DB,app:Path=APP,now:str|None=None):
    assert db.parent.is_dir() and not db.exists() and not db.is_symlink()
    if not app.is_dir() or app.is_symlink():raise RuntimeError('MISSING_ISOLATED_APP')
    os.environ.clear()
    os.environ.update({'HOME':'/tmp','TMPDIR':'/tmp','PYTHONDONTWRITEBYTECODE':'1','ORACLE_DB':str(db)})
    sys.path.insert(0,str(app))
    v5=importlib.import_module('oracle_v5')
    v55=importlib.import_module('oracle_v5_settlement')
    timestamp=now or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    c=sqlite3.connect(str(db),timeout=3)
    try:
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA synchronous=FULL')
        c.execute('PRAGMA journal_mode=DELETE')
        v5.setup(c)
        v55.setup(c)
        c.execute('CREATE TABLE eva_control (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        c.executemany('INSERT INTO eva_control(key,value) VALUES(?,?)',[
            ('created_utc',timestamp),('mode','PAPER_ONLY_NO_FEED'),
            ('policy','odds_1.70_to_3.50_no_double_chance'),
            ('historical_input_import','FORBIDDEN'),('real_bets','FORBIDDEN'),
            ('source_data_feed','NOT_CONFIGURED'),('generator_schedule','NOT_ENABLED')])
        c.executescript(SQL)
        tables={a[0] for a in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        triggers={a[0] for a in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        expected={'eva_forward_insert','eva_forward_no_delete','eva_forward_immutable_fields','eva_forward_valid_settlement'}
        if not {'v5_forecasts','v5_catalog','v55_paper','eva_control'}.issubset(tables) or not expected.issubset(triggers):
            raise RuntimeError('REQUIRED_SCHEMA_MISSING')
        if c.execute('SELECT COUNT(*) FROM v55_paper').fetchone()[0]!=0:raise RuntimeError('NON_EMPTY_PAPER')
        if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('SQLITE_QUICK_CHECK_FAILED')
        schemas={name:c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(name,)).fetchone()[0] for name in expected}
        c.commit()
        return {'kind':'EVA_V130_DB_INIT','state':'PASS','empty':True,
                'tables':sorted(tables),'triggers':sorted(expected),
                'trigger_sql':schemas,'created_utc':timestamp,
                'historical_db_opened':False,'synthetic_records_inserted':0,
                'network_access':False,'real_orders':0}
    finally:c.close()

def main():
    try:
        res=create()
        print('EVA_V130_JSON:'+json.dumps(res,sort_keys=True,ensure_ascii=False),flush=True)
        return 0
    except BaseException as e:
        print('EVA_V130_JSON:'+json.dumps({'kind':'EVA_V130_DB_INIT','state':'BLOCKED',
            'error':type(e).__name__+':'+str(e)[:350]},ensure_ascii=False),flush=True)
        return 3

if __name__=='__main__':raise SystemExit(main())
