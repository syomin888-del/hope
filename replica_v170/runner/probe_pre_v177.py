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
 ('fonbet_snapshot',['python','-u','/app/oracle_fonbet_full.py','once','--db',DB]),
 ('original_v7_cycle',['python','-u','/app/oracle_v5.py','once','--db',DB]),
]

def main():
    print('V175_CORE_FIRST: ORIGINAL_FONBET_BEFORE_LONG_V4; ORIGINAL_V7_NOT_REPLACED',flush=True)
    answer=run_actions(CORE_ACTIONS,Path('/logs/v175/core'),max_total=380,per_job=180)
    return 0 if answer['state']=='PASS' else 2

if __name__=='__main__':sys.exit(main())
