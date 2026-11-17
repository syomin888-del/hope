#!/usr/bin/env python3
"""Daily operational audit. Never equate technical health with predictive edge."""
import argparse
import datetime as dt
import json
import shutil
import sqlite3
import collect
import research
import sources
import backfill

def inspect():
    c=research.open_db();sources.create_tables(c);backfill.setup(c)
    free=shutil.disk_usage(collect.DB.parent).free
    problems=[]
    integrity=c.execute('PRAGMA quick_check').fetchone()[0]
    if integrity!='ok':problems.append('SQLite integrity check failed')
    if free<2*1024**3:problems.append('Disk below 2GB free; archive loading may fail')
    r=[]
    now=dt.datetime.now(dt.timezone.utc)
    for name,(delay,_) in sources.SCHEDULE.items():
        row=c.execute('SELECT run_at,state FROM source_runs WHERE job=? ORDER BY id DESC LIMIT 1',(name,)).fetchone()
        if not row:
            r.append({'source':name,'state':'NOT_STARTED'});continue
        tm=dt.datetime.fromisoformat(row[0]);age=(now-tm).total_seconds()
        condition='STALE' if age>max(delay*3,86400) else row[1].upper()
        r.append({'source':name,'state':condition,'age_hours':round(age/3600,1)})
        if condition in ('ERROR','STALE'):problems.append('Source issue: '+name)
    papers=c.execute('SELECT COUNT(*),SUM(CASE WHEN status="settled" THEN 1 ELSE 0 END) FROM paper_forecasts').fetchone()
    backfills=c.execute('SELECT COUNT(*) FROM history_downloads WHERE status="ok"').fetchone()[0]
    c.close()
    return {'generated_at':now.isoformat(),'database_integrity':integrity,'free_gb':round(free/1024**3,2),
            'problems':problems,'sources':r,'archives_loaded':backfills,
            'paper_predictions':papers[0],'paper_outcomes_settled':papers[1] or 0,
            'interpretation':'An operating system is not a profitable or statistically validated forecasting system.'}
if __name__=='__main__':print(json.dumps(inspect(),indent=2,ensure_ascii=False))
