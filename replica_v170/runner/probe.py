#!/usr/bin/env python3
"""Genuine historical collectors, CORE FIRST. V4 research has separate bounded slots.

No new forecast models, no bookmaker bets, no writes outside EVA's private mounts.
A Fonbet or V7 internal error blocks activation. All actual results are durably logged.
"""
import sys
from pathlib import Path
from v174_durable import run_actions

DB='/data/oracle.sqlite3'
CORE_ACTIONS=[
 ('archive_ingest',['python','-u','/app/sports_free.py','ingest']),
 ('original_sports_hypotheses',['python','-u','/app/sports_free.py','propose']),
 ('original_sports_finals',['python','-u','/app/sports_free.py','settle']),
 ('fonbet_snapshot',['python','-u','/runner/eva_fonbet_sqlite_temp_r1.py','once','--db',DB]),
 ('original_v7_cycle',['python','-u','/runner/eva_v178_launcher.py']),
 ('verified_paper_settlement',['python','-u','/runner/eva_settlement_verified_r6.py','once']),
]

def main():
    print('V178_CORE_FIRST: ORIGINAL_FONBET; ORIGINAL_V7; PAPER/PF_PORTFOLIO_GATES',flush=True)
    answer=run_actions(CORE_ACTIONS,Path('/logs/v175/core'),max_total=1400,per_job=180,job_timeouts={'fonbet_snapshot':1200})
    return 0 if answer['state']=='PASS' else 2

if __name__=='__main__':sys.exit(main())
