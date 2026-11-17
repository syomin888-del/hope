#!/usr/bin/env python3
"""EVA-only, fail-closed pre-insert policy. No model, SQL trigger or DB schema changes.

Injected at run time only into verified historical oracle_v5_settlement.prepare.
Rejected candidates never reach SQLite; original V1.3 ABORT triggers remain authoritative.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re

AUDIT = Path('/logs/v177/rejected_paper.jsonl')
_INSERT = re.compile(r'^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+v55_paper\s*\(([^()]*)\)\s*VALUES\s*\(([^()]*)\)\s*;?\s*$', re.I|re.S)
DOUBLE = {'DOUBLE_CHANCE','DOUBLECHANCE','DC','ДВОЙНОЙ_ШАНС'}
SELECTION = {'1X','X2','12','1Х','Х2','1x','x2','1х','х2'}


def parse_statement(sql):
    match=_INSERT.fullmatch(sql)
    if not match:raise RuntimeError('V177_UNSUPPORTED_HISTORICAL_INSERT_SHAPE')
    columns=[x.strip().lower() for x in match.group(1).split(',')]
    exprs=[x.strip() for x in match.group(2).split(',')]
    if len(columns)!=len(exprs) or len(columns)!=len(set(columns)):
        raise RuntimeError('V177_INSERT_COLUMNS_INVALID')
    required={'forecast_id','origin','market','selection','probability','odds','stake',
              'quote_at','kickoff_utc','registered_at'}
    if not required.issubset(set(columns)):raise RuntimeError('V177_INSERT_REQUIRED_COLUMNS_MISSING')
    if any(e not in ('?',"'PENDING'",'"PENDING"') for e in exprs):
        raise RuntimeError('V177_INSERT_VALUES_UNSUPPORTED')
    if 'status' in columns and exprs[columns.index('status')].upper() not in ("'PENDING'",'"PENDING"'):
        raise RuntimeError('V177_CANNOT_OVERRIDE_PAPER_STATUS')
    return columns,exprs


def values_map(sql,params):
    cols,expr=parse_statement(sql)
    try:values=list(params)
    except TypeError:raise RuntimeError('V177_BINDINGS_NOT_SEQUENCE')
    if len(values)!=expr.count('?'):
        raise RuntimeError('V177_SQL_BIND_COUNT_MISMATCH')
    it=iter(values)
    return {col:(next(it) if e=='?' else 'PENDING') for col,e in zip(cols,expr)}


def utc(v):
    if not isinstance(v,str):return None
    try:
        out=dt.datetime.fromisoformat(v.replace('Z','+00:00'))
        return out.astimezone(dt.timezone.utc) if out.tzinfo is not None else None
    except (ValueError,OverflowError):return None


def reason(row,created_at,at=None):
    # Unknown or ambiguous inputs fail CLOSED, never interpreted optimistically.
    def number(x):
        try:
            y=float(x)
            return y if math.isfinite(y) else None
        except (ValueError,TypeError,OverflowError):return None
    odd=number(row.get('odds')); prob=number(row.get('probability'));stake=number(row.get('stake'))
    if odd is None or not 1.70<=odd<=3.50:return 'ODDS_OUTSIDE_1_70_3_50'
    if prob is None or not 0<prob<1:return 'PROBABILITY_INVALID'
    if stake is None or abs(stake-10)>1e-6:return 'STAKE_NOT_TEN'
    origin=row.get('origin');market=row.get('market');selection=row.get('selection')
    if not origin or origin=='EVA_SYNTHETIC':return 'ORIGIN_INVALID'
    if not isinstance(market,str) or not isinstance(selection,str):return 'MARKET_INVALID'
    m=market.replace(' ','_').upper()
    if m in DOUBLE or 'шанс' in market.lower() or selection.strip() in SELECTION:return 'DOUBLE_CHANCE'
    if not row.get('forecast_id'):return 'FORECAST_ID_MISSING'
    if row.get('status','PENDING')!='PENDING':return 'NOT_PENDING'
    q=utc(row.get('quote_at'));r=utc(row.get('registered_at'));k=utc(row.get('kickoff_utc'))
    created=utc(created_at)
    now=at or dt.datetime.now(dt.timezone.utc)
    if any(x is None for x in (q,r,k,created)):return 'INVALID_TIMESTAMPS'
    if not (q<=r<k and r>=created and r<=now and k>now):return 'NOT_PROSPECTIVE'
    return None


def record_rejection(row,why,audit=AUDIT):
    if audit.is_symlink() or audit.parent.is_symlink():raise RuntimeError('V177_AUDIT_PATH_UNSAFE')
    audit.parent.mkdir(parents=True,exist_ok=True)
    forecast=str(row.get('forecast_id') or '')
    entry={'at':dt.datetime.now(dt.timezone.utc).isoformat(),
           'reason':why,'forecast_sha256':hashlib.sha256(forecast.encode()).hexdigest(),
           'source':'V177_EVA_ONLY_PREINSERT','real_orders':0}
    fd=os.open(audit,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:
        f.write(json.dumps(entry,sort_keys=True)+'\n');f.flush();os.fsync(f.fileno())


def execute(conn,sql,params):
    row=values_map(sql,params)
    record=conn.execute("SELECT value FROM eva_control WHERE key='created_utc'").fetchone()
    if not record or not isinstance(record[0],str):
        raise RuntimeError('V177_EVA_CONTROL_MISSING_FAIL_CLOSED')
    why=reason(row,record[0])
    if why:
        record_rejection(row,why)
        # Keep the caller's INSERT cursor interface, with no paper writes.
        # Unlike RAISE(IGNORE), this skips BEFORE reaching the original ABORT trigger.
        return conn.execute('SELECT 1 WHERE 0')
    # Final enforcement ALWAYS happens in the 4 unmodified original SQLite triggers.
    return conn.execute(sql,params)
