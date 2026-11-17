#!/usr/bin/env python3
"""ORACLE separated core prototype. No prediction models, no autonomous gambling/trading."""
import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

UTC = dt.timezone.utc

def iso(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp must be ISO8601 string')
    x = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if x.tzinfo is None:
        raise ValueError('Timezone required')
    return x.astimezone(UTC)

def now():
    return dt.datetime.now(UTC)

def db_open(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS signals (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, issued_at TEXT NOT NULL,
        horizon_at TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', outcome TEXT, resolved_at TEXT);
      CREATE TABLE IF NOT EXISTS outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT NOT NULL,
        notification_type TEXT NOT NULL, text TEXT NOT NULL,
        delivered_at TEXT, UNIQUE(signal_id,notification_type));
    ''')
    conn.commit()
    return conn

def validate(candidate, approved, clock=None):
    clock = clock or now()
    if not isinstance(candidate, dict):
        raise ValueError('Candidate must be JSON object')
    if candidate.get('kind') not in ('sport', 'crypto'):
        raise ValueError('Unsupported kind')
    required = ('id', 'issued_at', 'horizon_at', 'model_id', 'title', 'probability', 'probability_lower', 'sources', 'risks')
    for key in required:
        if key not in candidate:
            raise ValueError('Missing '+key)
    event = candidate['id']
    if not isinstance(event, str) or not (4 <= len(event) <= 120):
        raise ValueError('Invalid id')
    issued = iso(candidate['issued_at']); horizon = iso(candidate['horizon_at'])
    if issued > clock + dt.timedelta(minutes=2):
        raise ValueError('Issued in future')
    if issued < clock - dt.timedelta(hours=3):
        raise ValueError('Stale prediction: must be registered shortly after issuance')
    if horizon <= clock:
        raise ValueError('The event horizon is already past')
    approved_model = approved.get(candidate['model_id'])
    if not isinstance(approved_model, dict) or not approved_model.get('approved'):
        raise ValueError('Model not independently approved: delivery forbidden')
    if approved_model.get('kind') != candidate['kind']:
        raise ValueError('Model kind mismatch')
    # Verification evidence is required, not manufactured by the delivery system.
    p = float(candidate['probability']); low = float(candidate['probability_lower'])
    if not (0 < low <= p < 1):
        raise ValueError('Invalid probability interval')
    sources = candidate['sources']
    if not isinstance(sources, list) or len(sources) < 2:
        raise ValueError('Require at least two evidence references')
    distinct = set()
    for s in sources:
        if not isinstance(s, dict) or not isinstance(s.get('url'), str) or not s['url'].startswith('https://'):
            raise ValueError('Evidence URLs must be HTTPS')
        observed = iso(s['published_at'])
        if observed > issued:
            raise ValueError('Future evidence / lookahead detected')
        distinct.add(s.get('publisher_id',''))
    if len(distinct) < 2 or '' in distinct:
        raise ValueError('Require evidence from distinct identified publishers')
    if not isinstance(candidate['risks'], list) or not candidate['risks']:
        raise ValueError('Explicit risks required')
    if candidate['kind'] == 'sport':
        odds = float(candidate['odds'])
        if not (1.7 <= odds <= 100):
            raise ValueError('Odds outside configured bounds')
        if low * odds - 1 <= 0.03:
            raise ValueError('Conservative edge below +3%')
        if not candidate.get('selection') or not candidate.get('bookmaker'):
            raise ValueError('Missing selection/bookmaker')
        if not candidate.get('odds_checked_at'):
            raise ValueError('Missing quote timestamp')
        if (clock - iso(candidate['odds_checked_at'])) > dt.timedelta(minutes=5):
            raise ValueError('Bookmaker quote is too old')
        if iso(candidate['odds_checked_at']) > clock+dt.timedelta(seconds=30):
            raise ValueError('Quote from future')
    else:
        entry = float(candidate['entry_usd']); target = float(candidate['target_usd'])
        if not (entry > 0 and target > entry):
            raise ValueError('Crypto target must exceed actual observed entry')
        if not candidate.get('coin_id'):
            raise ValueError('Missing coin ID')
        if not candidate.get('entry_checked_at'):
            raise ValueError('Missing entry quote timestamp')
        if clock - iso(candidate['entry_checked_at']) > dt.timedelta(minutes=5):
            raise ValueError('Entry quote too old')
        if iso(candidate['entry_checked_at']) > clock+dt.timedelta(seconds=30):
            raise ValueError('Future entry quote')
    return True

def message(signal):
    p=round(float(signal['probability'])*100,1); l=round(float(signal['probability_lower'])*100,1)
    title=str(signal['title'])[:180]
    why='; '.join(str(s.get('reason','источник'))[:100] for s in signal['sources'][:4])
    risks='; '.join(str(r)[:90] for r in signal['risks'][:4])
    if signal['kind']=='sport':
        odds=float(signal['odds']); ev=round((float(signal['probability_lower'])*odds-1)*100,1)
        body=(f'🏟 ORACLE: {title}\nИсход: {signal["selection"]}\n'
              f'Линия: {odds:.2f} ({signal["bookmaker"]})\n'
              f'Вероятность модели: {p}% (нижняя граница {l}%)\n'
              f'Консервативный EV: {ev:+.1f}%')
    else:
        growth=(float(signal['target_usd'])/float(signal['entry_usd'])-1)*100
        body=(f'🪙 ORACLE: {signal["coin_id"]}\n'
              f'Цена фиксации: ${float(signal["entry_usd"]):.6g}\n'
              f'Цель: ${float(signal["target_usd"]):.6g} ({growth:+.1f}%)\n'
              f'Вероятность модели: {p}% (нижняя граница {l}%)')
    return (body+f'\nПроверить к: {signal["horizon_at"]}\n'
            f'Аргументы: {why}\nРиски: {risks}\n'
            f'Модель: {signal["model_id"]}\nID: {signal["id"]}\n'
            '⚠️ Прогноз для виртуального тестирования, не гарантия дохода.')[:4000]

def add_signal(conn, candidate, approved, clock=None):
    validate(candidate, approved, clock)
    data = json.dumps(candidate, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    hashed=hashlib.sha256(data.encode()).hexdigest()
    with conn:
        conn.execute('INSERT INTO signals(id,kind,issued_at,horizon_at,payload,sha256) VALUES (?,?,?,?,?,?)',
            (candidate['id'],candidate['kind'],candidate['issued_at'],candidate['horizon_at'],data,hashed))
        conn.execute('INSERT INTO outbox(signal_id,notification_type,text) VALUES (?,?,?)',
            (candidate['id'],'forecast',message(candidate)))
    return hashed

def resolve(conn, result, clock=None):
    clock=clock or now()
    record=conn.execute('SELECT * FROM signals WHERE id=?',(result.get('id'),)).fetchone()
    if record is None:
        raise ValueError('Unknown signal')
    if record['status']!='open':
        raise ValueError('Already resolved')
    if clock < iso(record['horizon_at']):
        raise ValueError('Not yet at predetermined horizon')
    original=json.loads(record['payload'])
    if original['kind']=='sport':
        if result.get('outcome') not in ('win','loss','push','void','unverifiable'):
            raise ValueError('Invalid sports settlement')
        label=str(result['outcome'])
        pnl = (float(original['odds']) -1 if label=='win' else -1 if label=='loss' else 0) if label!='unverifiable' else None
        text=(f'🏁 ORACLE: {original["title"]}\nПрогноз: {original["selection"]} при {original["odds"]}\n'
              f'Результат: {label}\nВиртуальный результат на 1 ед.: {pnl if pnl is not None else "не проверено"}')
    else:
        if result.get('outcome')=='unverifiable':
            label='unverifiable'
            text=f'⌛ ORACLE: {original["coin_id"]} — контрольную цену не удалось достоверно получить. Не засчитываю результат.'
        else:
            price=float(result['price_usd'])
            if price <= 0:
                raise ValueError('Invalid price')
            # Explicit fixed-horizon settlement, not 'touched target at any point'
            label='win' if price >= float(original['target_usd']) else 'loss'
            growth=(price/float(original['entry_usd'])-1)*100
            text=(f'🪙 ORACLE: {original["coin_id"]} — проверка в установленный срок\n'
                  f'Было ${original["entry_usd"]} → цель ${original["target_usd"]}\n'
                  f'Фактическая цена: ${price:.6g} ({growth:+.2f}%)\nЦель: {"достигнута" if label=="win" else "не достигнута"}')
    if not isinstance(result.get('source_url'),str) or not result['source_url'].startswith('https://'):
        raise ValueError('Missing result source URL')
    with conn:
        conn.execute("UPDATE signals SET status=?,outcome=?,resolved_at=? WHERE id=? AND status='open'",
                     (label,json.dumps(result,sort_keys=True),clock.isoformat(),record['id']))
        conn.execute('INSERT INTO outbox(signal_id,notification_type,text) VALUES (?,?,?)',
                     (record['id'],'result',(text+'\nИсточник результата: '+result['source_url'])[:4000]))
    return label

def dispatch(conn, enabled=False):
    entries=conn.execute('SELECT * FROM outbox WHERE delivered_at IS NULL ORDER BY id LIMIT 20').fetchall()
    if not entries:
        print('No pending notifications');return 0
    if not enabled:
        for x in entries:
            print(f'PENDING {x["id"]}: {x["text"]}\n')
        print('Dry-run only. Set ORACLE_TELEGRAM_ENABLED=1 to enable delivery.')
        return 0
    token=os.environ.get('NASTYA_BOT_TOKEN','')
    chat=os.environ.get('NASTYA_CHAT_ID','')
    if not token or not chat:
        raise RuntimeError('NASTYA_BOT_TOKEN and NASTYA_CHAT_ID required; never put them in code')
    done=0
    for x in entries:
        data=json.dumps({'chat_id':chat,'text':x['text'],'disable_web_page_preview':True}).encode()
        req=urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage', data=data, headers={'Content-Type':'application/json'},method='POST')
        try:
            with urllib.request.urlopen(req,timeout=15) as reply:
                answer=json.loads(reply.read())
            if not answer.get('ok'):
                raise RuntimeError('Telegram did not confirm acceptance')
            with conn:
                conn.execute('UPDATE outbox SET delivered_at=? WHERE id=? AND delivered_at IS NULL',(now().isoformat(),x['id']))
            done+=1
        except Exception as e:
            # Network timeout after a Telegram accept can cause a duplicate after retry.
            # Record failure locally without printing credential-bearing endpoint.
            print(f'Delivery failed for outbox {x["id"]}: {type(e).__name__}',file=sys.stderr)
            break
    return done

def main():
    parser=argparse.ArgumentParser(description='Independent ORACLE alert/outcome service (no prediction engine bundled)')
    parser.add_argument('--db',default=os.environ.get('ORACLE_DB','oracle.sqlite3'))
    parser.add_argument('--models',default='approved_models.json')
    subs=parser.add_subparsers(dest='action',required=True)
    subs.add_parser('init');subs.add_parser('status');subs.add_parser('dispatch')
    for name in ('submit','resolve'):
        p=subs.add_parser(name);p.add_argument('json_file')
    args=parser.parse_args();con=db_open(args.db)
    if args.action=='init': print('Initialized',args.db)
    elif args.action=='status':
        stats=[dict(x) for x in con.execute('SELECT kind,status,count(*) AS n FROM signals GROUP BY kind,status')]
        pending=con.execute('SELECT count(*) FROM outbox WHERE delivered_at IS NULL').fetchone()[0]
        print(json.dumps({'signals':stats,'pending_notifications':pending},ensure_ascii=False))
    elif args.action=='submit':
        registry=json.loads(Path(args.models).read_text(encoding='utf8'))
        obj=json.loads(Path(args.json_file).read_text(encoding='utf8'))
        print('Committed immutable forecast hash:', add_signal(con,obj,registry))
    elif args.action=='resolve':
        obj=json.loads(Path(args.json_file).read_text(encoding='utf8'))
        print('Recorded settlement:',resolve(con,obj))
    elif args.action=='dispatch': print('Delivered:',dispatch(con,os.environ.get('ORACLE_TELEGRAM_ENABLED')=='1'))

if __name__=='__main__':
    main()
