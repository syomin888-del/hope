#!/usr/bin/env python3
"""Honest, auditable ORACLE operations telemetry.

Counts are records actually written to the local ORACLE SQLite database.
No predictions, provider requests, tokens or profit numbers are inferred.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent
DEFAULT_DB=Path(os.environ.get('ORACLE_DB',str(ROOT/'oracle.sqlite3')))
UTC=dt.timezone.utc
TZ=ZoneInfo('Asia/Makassar')


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS telemetry_heartbeats(
        service TEXT PRIMARY KEY, last_seen_utc TEXT NOT NULL,
        state TEXT NOT NULL, details TEXT NOT NULL DEFAULT '')''')
    c.commit()


def heartbeat(c, service, state='running', details='', now=None):
    if service not in ('sources','research'):raise ValueError('Unknown service')
    setup(c)
    now=now or dt.datetime.now(UTC)
    with c:
        c.execute('''INSERT INTO telemetry_heartbeats(service,last_seen_utc,state,details)
         VALUES(?,?,?,?) ON CONFLICT(service) DO UPDATE SET
         last_seen_utc=excluded.last_seen_utc, state=excluded.state, details=excluded.details''',
         (service,now.astimezone(UTC).isoformat(timespec='seconds'),str(state)[:35],str(details)[:160]))


def tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def count(c,tbl,field,start,end,existing):
    if tbl not in existing:return None
    # names are fixed in source; never accept user-provided identifiers
    return c.execute(f'SELECT COUNT(*) FROM {tbl} WHERE {field}>=? AND {field}<?',(start,end)).fetchone()[0]


def snapshot(c, day=None, zone='Asia/Makassar', now=None):
    tz=ZoneInfo(zone)
    now=now or dt.datetime.now(UTC)
    now=now.astimezone(UTC)
    local_day=dt.date.fromisoformat(day) if day else now.astimezone(tz).date()
    start=dt.datetime.combine(local_day,dt.time(0),tzinfo=tz).astimezone(UTC).isoformat(timespec='seconds')
    end=dt.datetime.combine(local_day+dt.timedelta(days=1),dt.time(0),tzinfo=tz).astimezone(UTC).isoformat(timespec='seconds')
    existing=tables(c)
    records={
      'raw_source_versions':count(c,'raw_snapshots','observed_at',start,end,existing),
      'candles_ingested':count(c,'candles','fetched_at',start,end,existing),
      'knowledge_facts_saved':count(c,'knowledge_facts','observed_at',start,end,existing),
      'experiments_performed':count(c,'hypothesis_experiments','run_at',start,end,existing),
      'research_job_runs':count(c,'research_jobs','run_at',start,end,existing),
      'paper_forecasts_created':count(c,'paper_forecasts','recorded_at',start,end,existing),
      'paper_forecasts_settled':count(c,'paper_forecasts','settled_at',start,end,existing),
      'signals_issued':count(c,'signals','issued_at',start,end,existing),
      'signals_resolved':count(c,'signals','resolved_at',start,end,existing),
    }
    # Different snapshots of the same source item are versions, not distinct news/events.
    unique_subjects=None
    if 'raw_snapshots' in existing:
        unique_subjects=c.execute('''SELECT COUNT(*) FROM (
            SELECT provider,subject FROM raw_snapshots WHERE observed_at>=? AND observed_at<?
            GROUP BY provider,subject)''',(start,end)).fetchone()[0]
    records['unique_provider_subject_pairs']=unique_subjects
    runs=None
    if 'source_runs' in existing:
        runs={r[0]:int(r[1]) for r in c.execute('''SELECT state,COUNT(*) FROM source_runs
         WHERE run_at>=? AND run_at<? GROUP BY state''',(start,end))}
        for k in ('ok','error','skip'):runs.setdefault(k,0)
    jobs=[]
    if 'source_runs' in existing:
        jobs=[dict(job=r[0],last_run=r[1],state=r[2],new_rows=r[3]) for r in c.execute('''SELECT job,run_at,state,new_rows FROM source_runs
         WHERE id IN (SELECT MAX(id) FROM source_runs GROUP BY job) ORDER BY job''')]
    services=[]
    if 'telemetry_heartbeats' in existing:
        for service,seen,state,details in c.execute('SELECT service,last_seen_utc,state,details FROM telemetry_heartbeats ORDER BY service'):
            try:
                age=(now-dt.datetime.fromisoformat(seen).astimezone(UTC)).total_seconds()
                alive=bool(-90<=age<=300 and state=='running')
            except ValueError:alive=False;age=None
            services.append(dict(service=service,last_seen_utc=seen,reported_state=state,
              heartbeat_fresh=alive,age_seconds=round(age) if age is not None else None))
    total_models=None; certified_models=0
    if 'research_models' in existing:
        states={str(r[0]):int(r[1]) for r in c.execute('SELECT state,COUNT(*) FROM research_models GROUP BY state')}
        total_models=sum(states.values())
        # HISTORICAL_PASS_SHADOW is NOT live-certified; don't count it as approved.
    open_signals=None
    if 'signals' in existing:
        open_signals=c.execute("SELECT COUNT(*) FROM signals WHERE status='open'").fetchone()[0]
    warnings=[]
    if not services:warnings.append('Нет подтверждённых heartbeat от работающих процессов')
    elif any(not s['heartbeat_fresh'] for s in services):warnings.append('Один или несколько процессов не подтвердили работу за последние 5 минут')
    if runs and runs.get('error',0):warnings.append('Сегодня были ошибки источников: см. журнал source_runs')
    if records['signals_issued'] is None:warnings.append('Журнал сигналов не инициализирован')
    return {'date':local_day.isoformat(),'timezone':zone,'generated_at_utc':now.isoformat(timespec='seconds'),
      'database_connected':True,'data_window_utc':{'start':start,'end_exclusive':end},
      'counts':records,'source_job_runs_today':runs,'source_jobs_latest':jobs,
      'services':services,'tracked_models':total_models,'live_certified_models':certified_models,
      'open_signals':open_signals,'warnings':warnings,
      'definitions':{'raw_source_versions':'Сохранённые версии внешних снимков, не количество прочитанных статей',
      'candles_ingested':'Новые свечи, внесённые в БД в этот день (включая архивные)',
      'signals_issued':'Сигналы, реально записанные в отдельный журнал',
      'paper_forecasts_created':'Виртуальные прогнозы без реальных денег'}}


def offline(day=None,zone='Asia/Makassar'):
    today=dt.date.fromisoformat(day) if day else dt.datetime.now(ZoneInfo(zone)).date()
    return {'date':today.isoformat(),'timezone':zone,'database_connected':False,
      'counts':{},'source_job_runs_today':None,'source_jobs_latest':[], 'services':[],
      'tracked_models':None,'live_certified_models':None,'open_signals':None,
      'warnings':['База ORACLE недоступна: объём анализа и сигналы НЕИЗВЕСТНЫ']}


def get(db=DEFAULT_DB,day=None,zone='Asia/Makassar',now=None):
    db=Path(db)
    if not db.is_file():return offline(day,zone)
    try:
        with sqlite3.connect(f'file:{db.resolve().as_posix()}?mode=ro',uri=True,timeout=3) as c:
            c.execute('PRAGMA query_only=ON')
            return snapshot(c,day,zone,now)
    except (sqlite3.Error,OSError) as exc:
        result=offline(day,zone)
        result['warnings']=[f'Не удалось прочитать базу: {type(exc).__name__}']
        return result


def report_text(s):
    if not s.get('database_connected'):
        return f"ORACLE — {s['date']} ({s['timezone']})\nБаза не подключена. Реальные объёмы и сигналы неизвестны."
    n=s['counts']
    def val(key):return str(n[key]) if n[key] is not None else 'нет таблицы'
    lines=[f"ORACLE — {s['date']} ({s['timezone']})",
       f"Сохранённые версии источников: {val('raw_source_versions')}",
       f"Уникальные объекты источников: {val('unique_provider_subject_pairs')}",
       f"Добавлено свечей (включая историю): {val('candles_ingested')}",
       f"Добавлено фактов в память: {val('knowledge_facts_saved')}",
       f"Экспериментов: {val('experiments_performed')}",
       f"Виртуальных прогнозов: {val('paper_forecasts_created')}",
       f"Подтверждённых сигналов в журнале: {val('signals_issued')}",
       f"Проверено результатов сигналов: {val('signals_resolved')}",
       'Результативность НЕ оценивается только количеством записей.']
    if s['source_job_runs_today']:
        lines.append('Источник задач (успех / ошибка / пропуск): '+ ' / '.join(str(s['source_job_runs_today'].get(k,0)) for k in ('ok','error','skip')))
    for h in s['services']:
        lines.append(f"Процесс {h['service']}: {'есть свежий heartbeat' if h['heartbeat_fresh'] else 'не подтверждён'}")
    if s['warnings']:
        lines.append('Предупреждения: '+ '; '.join(s['warnings']))
    return '\n'.join(lines)


def main():
    p=argparse.ArgumentParser(description='Сводка реальной активности ORACLE из SQLite')
    p.add_argument('--db',default=str(DEFAULT_DB))
    p.add_argument('--date',default=None,help='YYYY-MM-DD, default today in Bali')
    p.add_argument('--tz',default='Asia/Makassar')
    p.add_argument('--json',action='store_true')
    a=p.parse_args()
    try: data=get(a.db,a.date,a.tz)
    except (ValueError,KeyError) as exc:raise SystemExit(f'Invalid date/timezone: {exc}')
    print(json.dumps(data,ensure_ascii=False,indent=2) if a.json else report_text(data))
    if not data['database_connected']:raise SystemExit(2)

if __name__=='__main__':main()
