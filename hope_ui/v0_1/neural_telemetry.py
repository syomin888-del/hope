"""HOPE Neural Observatory v0.3: bounded read-only log and process telemetry.

Never loads or imports historic Oracle code, never shells out, never opens DB for writes,
never treats a lingering journal STARTED event as evidence of a running process.
"""
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

UTC = dt.timezone.utc
SECTORS = (
    ('ingest', 'Данные и коэффициенты', '#81d9ff'),
    ('sports', 'Спортивная аналитика', '#8dbeff'),
    ('evidence', 'Факты и исследования', '#b8aeff'),
    ('portfolio', 'Портфель и риск', '#64ddc5'),
    ('settlement', 'Результаты и сверка', '#f0b68f'),
    ('crypto', 'Криптовалюты', '#cf9cff'),
    ('stocks', 'Фондовый рынок', '#e4c483'),
)
CORE_TASKS = (
    'archive_ingest', 'original_sports_hypotheses', 'original_sports_finals',
    'fonbet_snapshot', 'original_v7_cycle',
)
# The V4 task list is historical and may expand. Only tasks actually seen in logs are counted.
EVA_RUNNER_HASH = '91eec11a00fba3bccf2da4f88bd5265a5a7b4d175c1ee8e58420ca8b33bdd412'
NAME_RE = re.compile(r'^[a-z][a-z0-9_]{0,80}$')
FOLDER_RE = re.compile(r'^\d{8}T\d{12}Z$')
MAX_EVENTS = 260


def utc_now():
    return dt.datetime.now(UTC)


def parse_time(value):
    try:
        if not isinstance(value, str): return None
        x = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return x.astimezone(UTC) if x.tzinfo else None
    except (ValueError, OverflowError): return None


def iso(x):
    return x.isoformat(timespec='seconds') if x else None


def safe_name(x):
    return str(x)[:81] if isinstance(x, str) and NAME_RE.fullmatch(x) else None


def sector_for(name):
    name = name or ''
    if 'settle' in name or 'final' in name or name.endswith('_results'): return 'settlement'
    if 'portfolio' in name or 'risk' in name: return 'portfolio'
    if 'crypto' in name or 'binance' in name or 'token' in name: return 'crypto'
    if 'stock' in name or 'worldbank' in name or 'gleif' in name or 'cftc' in name: return 'stocks'
    if any(x in name for x in ('news','research','discovery','library','book','licensed','crossref','openalex','evidence','video')): return 'evidence'
    if any(x in name for x in ('fonbet','odds','quote','source','feed','archive','ingest')): return 'ingest'
    if any(x in name for x in ('sports','v7','v5','paper','openliga','model','hypotheses')): return 'sports'
    return 'evidence'


def display_action(name):
    pretty = {
        'archive_ingest':'Проверка исторического архива',
        'original_sports_hypotheses':'Формирование спортивных гипотез',
        'original_sports_finals':'Поиск подтверждённых исходов',
        'fonbet_snapshot':'Считывание событий и коэффициентов Fonbet',
        'original_v7_cycle':'Расчёт моделей V7 и paper-портфеля',
        'multi_sport_odds_hub':'Обход источников OddsLink / MoviOdds',
        'sports_brain':'Работа спортивного анализатора',
        'sports_paper_settle':'Проверка исходов paper-прогнозов',
        'v5_paper_settlement_6h':'Расчёт WIN / LOSS / PUSH',
        'sports_settle':'Сверка завершённых спортивных событий',
        'sports_propose':'Генерация гипотез событий',
        'news_claims':'Анализ новостных утверждений',
        'research_discovery':'Поиск новых исследовательских материалов',
        'stocks':'Исследование фондовых данных',
        'open_binance_spot':'Получение данных Binance Spot',
        'v5_evidence_search':'Проверка доказательств спортивных прогнозов',
        'fonbet_bridge':'Связывание рынков Fonbet с событиями',
    }
    return pretty.get(name, 'Исполнение '+name.replace('_',' '))


def bounded_json(path, max_bytes=128_000):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size>max_bytes:
            return None
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError): return None


def safe_lines(path, max_bytes=1_048_576, max_lines=1000):
    try:
        if path.is_symlink() or not path.is_file(): return []
        if path.stat().st_size>max_bytes: return []
        return path.read_text(encoding='utf-8').splitlines()[-max_lines:]
    except (OSError, UnicodeError): return []


def boot_ts(proc_root):
    try:
        for line in (proc_root/'stat').read_text().splitlines():
            if line.startswith('btime '):return float(line.split()[1])
    except (OSError, ValueError, IndexError): pass
    return None


def proc_start(pid_dir, boot):
    if boot is None:return None
    try:
        stat=(pid_dir/'stat').read_text()
        # Field 2 (comm) can contain parentheses; field 22 is index 19 after comm.
        remain=stat.rsplit(')',1)[1].strip().split()
        ticks=int(remain[19])
        return dt.datetime.fromtimestamp(boot+ticks/os.sysconf('SC_CLK_TCK'), UTC)
    except (OSError, ValueError, IndexError, OverflowError): return None


def verified_processes(proc_root=Path('/proc'), now=None, verify_hash=EVA_RUNNER_HASH):
    """Require an EVA-only runner signature inside each candidate's mount namespace.

    Do not inspect or return unrelated process commands; exact, bounded script allowlist.
    Lack of /proc permission returns no positives; a stale start event stays UNKNOWN.
    """
    now=now or utc_now()
    matches=[]
    boot=boot_ts(proc_root)
    expected_scripts={'/runner/v4_slot_v175.py', '/runner/probe.py', '/app/sports_free.py',
                      '/app/oracle_v5.py', '/app/oracle_fonbet_full.py'}
    try:dirs=(p for p in proc_root.iterdir() if p.name.isdigit())
    except OSError:return matches
    for pid_dir in dirs:
        try:
            raw=(pid_dir/'cmdline').read_bytes()[:4096]
            argv=[x.decode('utf-8','replace') for x in raw.split(b'\x00') if x]
            script=next((arg for arg in argv if arg in expected_scripts),None)
            if not script:continue
            # This file is mounted in HOPE EVA's container; a production process
            # must not be classified as HOPE merely because it has the same argv.
            runner=pid_dir/'root/runner/v4_slot_v175.py'
            if not runner.is_file() or hashlib.sha256(runner.read_bytes()).hexdigest()!=verify_hash:continue
            job=None
            if script=='/runner/v4_slot_v175.py':
                i=argv.index(script);job=safe_name(argv[i+1]) if i+1<len(argv) else None
            elif script=='/app/oracle_fonbet_full.py':job='fonbet_snapshot'
            elif script=='/app/oracle_v5.py':job='original_v7_cycle'
            elif script=='/app/sports_free.py':
                i=argv.index(script)
                arg=argv[i+1] if i+1<len(argv) else ''
                job={'ingest':'archive_ingest','propose':'original_sports_hypotheses','settle':'original_sports_finals'}.get(arg)
            elif script=='/runner/probe.py':job='core_runner'
            if not job:continue
            start=proc_start(pid_dir,boot)
            matches.append({'job':job,'pid':int(pid_dir.name),'started_at':iso(start),
                            'elapsed_seconds':max(0,int((now-start).total_seconds())) if start else None,
                            'sector':sector_for(job),'state':'RUNNING','action':display_action(job),
                            'observability':'PROCESS_VERIFIED'})
        except (OSError,ValueError,PermissionError):continue
    return sorted(matches,key=lambda x:(x['job'],x['pid']))[:25]


def v4_started_at(folder):
    if not FOLDER_RE.fullmatch(folder.name):return None
    try:return dt.datetime.strptime(folder.name,'%Y%m%dT%H%M%S%fZ').replace(tzinfo=UTC)
    except ValueError:return None


def v4_state(result, stdout_file):
    state=str(result.get('state') or 'UNKNOWN')
    code=result.get('exit_code')
    if state.startswith('TIMEOUT') or code==124:return 'TIMEOUT','Процесс превысил лимит времени'
    if code!=0:return 'FAILED','Задание завершилось с ошибкой'
    body=safe_lines(stdout_file,65536,80)
    # V4 original result appears as one JSON line or as the last JSON object.
    for line in reversed(body):
        try:obj=json.loads(line)
        except ValueError:continue
        if isinstance(obj,dict) and obj.get('task')==result.get('job'):
            detail=str(obj.get('state','OK')).upper()
            if detail=='NOT_DUE':return 'SKIPPED','Не наступило время выполнения'
            if detail=='DEGRADED':return 'DEGRADED','Исполнено с ограничениями'
            return 'COMPLETE','Завершено; результат алгоритма: '+detail[:30]
    return 'COMPLETE','Исполнитель завершился с кодом 0; эффект не подтверждён'


def read_recent(base,now=None,days=2,proc_root=Path('/proc')):
    base=Path(base);now=now or utc_now();cutoff=now-dt.timedelta(days=days)
    logs=base/'logs/v175';events=[];pending=[]
    core=logs/'core'
    if core.is_dir() and not core.is_symlink():
        for path in sorted(core.glob('run_*/events.jsonl'),reverse=True)[:90]:
            started={}
            for line in safe_lines(path,1_048_576,200):
                try:data=json.loads(line)
                except ValueError:continue
                kind=data.get('kind');at=parse_time(data.get('at'))
                if at is None or at<cutoff or at>now+dt.timedelta(minutes=2):continue
                job=safe_name(data.get('job'))
                if kind=='TASK_STARTED' and job:
                    started[job]=at
                    events.append({'id':'core:'+path.parent.name+':'+job+':start','at':iso(at),
                                   'job':job,'sector':sector_for(job),'state':'STARTED',
                                   'description':display_action(job),'lane':'CORE'})
                elif kind=='TASK_FINISHED' and job:
                    started.pop(job,None)
                    st='COMPLETE' if data.get('ok') else 'FAILED'
                    events.append({'id':'core:'+path.parent.name+':'+job+':finish','at':iso(at),
                                   'started_at':iso(at-dt.timedelta(seconds=float(data.get('duration_seconds') or 0))),
                                   'duration_seconds':data.get('duration_seconds'),
                                   'job':job,'sector':sector_for(job),'state':st,
                                   'description':display_action(job),'detail':str(data.get('state',''))[:80], 'lane':'CORE'})
            for job,at in started.items():
                pending.append({'job':job,'at':iso(at),'lane':'CORE'})
    v4root=logs/'v4'
    if v4root.is_dir() and not v4root.is_symlink():
        for folder in sorted(v4root.iterdir(),reverse=True)[:700] if v4root.exists() else []:
            if folder.is_symlink() or not folder.is_dir():continue
            start=v4_started_at(folder)
            if not start or start<cutoff or start>now+dt.timedelta(minutes=2):continue
            result=bounded_json(folder/'result.json')
            if not isinstance(result,dict):
                pending.append({'job':None,'at':iso(start),'lane':'V4','folder':folder.name})
                continue
            job=safe_name(result.get('job'))
            if not job:continue
            done=parse_time(result.get('at'))
            if done is None:done=start
            st,detail=v4_state(result,folder/'stdout.txt')
            events.append({'id':'v4:'+folder.name+':'+job,'at':iso(done),'started_at':iso(start),
                'duration_seconds':max(0,round((done-start).total_seconds(),1)),
                'job':job,'sector':sector_for(job),'state':st,
                'description':display_action(job),'detail':detail,'lane':'V4'})
    processes=verified_processes(proc_root,now)
    # The process itself is the proof of active execution. A recent journal
    # STARTED event with no process proof is only UNKNOWN, never RUNNING.
    active_keys={p['job'] for p in processes}
    unknown=[]
    for p in pending:
        if p.get('job') in active_keys:continue
        at=parse_time(p['at'])
        if at and (now-at).total_seconds()<600:
            unknown.append({'job':p.get('job'),'sector':sector_for(p.get('job')),
                'started_at':p['at'],'elapsed_seconds':int((now-at).total_seconds()),
                'state':'UNKNOWN','lane':p['lane'],
                'action':'Процесс не подтверждён; проверяется актуальность журнала'})
    events.sort(key=lambda x:x['at'],reverse=True)
    recent24=[x for x in events if parse_time(x['at'])>=now-dt.timedelta(hours=24)]
    completions=[x for x in recent24 if x['state'] in ('COMPLETE','FAILED','TIMEOUT','DEGRADED','SKIPPED')]
    summary={
        'completed_24h':sum(x['state']=='COMPLETE' for x in completions),
        'failed_24h':sum(x['state'] in ('FAILED','TIMEOUT','DEGRADED') for x in completions),
        'skipped_24h':sum(x['state']=='SKIPPED' for x in completions),
        'observed_24h':len(completions),
        'running_verified':len(processes),
    }
    return {'product':'HOPE','version':'0.3.0','at_utc':iso(now),'read_only':True,
        'source':'EVA_PROCESS_AND_LOG_EVIDENCE','status':'RUNNING' if processes else 'NO_VERIFIED_ACTIVE_PROCESS',
        'active':processes,'unconfirmed':unknown[:8], 'history':events[:MAX_EVENTS],
        'sectors':[{'id':x[0],'title':x[1],'color':x[2]} for x in SECTORS],
        'summary':summary,'limitations':[
            'Внутренний этап старой модели показывается только при наличии явных данных в журналах.',
            'Без подтверждения процесса Linux активность не изображается.',
            'Задание EXIT_0 не доказывает точность прогноза или прибыльность.'
        ]}
