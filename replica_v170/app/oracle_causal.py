#!/usr/bin/env python3
"""ORACLE causal inference *candidate* engine, not a causal or price predictor.

Only verified, time-stamped exposure edges may generate company-specific paths.
No company exposure is inferred from a headline alone. Human/evidence confirmation
is needed before assigning even a qualitative direction. No trade execution.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import re
import sqlite3
from typing import Mapping, Sequence

UTC=dt.timezone.utc

# Rule dictionaries are qualitative scenario maps, not causal proofs.
RULES={
 "resource_discovery":{"category":"supply_potential","lag":"years","mechanism":"possible future supply increase; mine economics, permits and funding unknown", "producer":"conditional_positive","commodity":"conditional_negative","consumer":"conditional_positive"},
 "production_start":{"category":"supply_realized","lag":"months","mechanism":"physical supply may increase; scale and market absorption required", "producer":"conditional_positive","commodity":"conditional_negative","consumer":"conditional_positive"},
 "production_shutdown":{"category":"supply_disruption","lag":"days_to_quarters","mechanism":"physical supply may decrease; substitution and inventories matter", "producer":"conditional_negative","commodity":"conditional_positive","consumer":"conditional_negative"},
 "export_restriction":{"category":"trade_disruption","lag":"days_to_months","mechanism":"trade flow changes; local and global prices can diverge", "exporter":"conditional_negative","substitute":"conditional_positive","importer":"conditional_negative"},
 "rate_increase":{"category":"macro_tightening","lag":"months_to_years","mechanism":"higher discount rates and borrowing costs; currency effects vary", "rate_sensitive_debtor":"conditional_negative","deposit_margins":"ambiguous"},
 "rate_decrease":{"category":"macro_easing","lag":"months_to_years","mechanism":"lower discount rates; may be offset by worsening economic demand", "rate_sensitive_debtor":"ambiguous","deposit_margins":"ambiguous"},
 "new_factory":{"category":"capacity_expansion","lag":"quarters_to_years","mechanism":"capacity and capex may alter supply; utilization, yields and demand unknown", "factory_owner":"ambiguous","equipment_supplier":"conditional_positive","competitor":"conditional_negative"},
 "contract_award":{"category":"revenue_potential","lag":"quarters","mechanism":"new backlog can affect revenue if delivery and margin are proven", "supplier":"conditional_positive","competitor":"ambiguous"},
 "cyber_attack":{"category":"operational_risk","lag":"days_to_months","mechanism":"downtime, customer churn and remediation spending", "affected_company":"conditional_negative","security_vendor":"ambiguous"},
 "drug_approval":{"category":"regulatory_catalyst","lag":"quarters_to_years","mechanism":"addressable revenue depends on launch and reimbursement", "drug_owner":"conditional_positive","competitor":"ambiguous"},
}
PHRASES={
 "resource_discovery":[r"\bdiscover(?:ed|y|s)?\b.{0,50}\b(deposit|reserve|mine|ore|resource)\b",r"\b(mineral|copper|lithium|gold)\b.{0,30}\b(discover|deposit)\b",r"обнаружил.{0,30}(месторожд|залеж)",r"нашл.{0,40}(месторожд|залеж)"],
 "production_start":[r"\b(start|begin|launch|commenc)(?:\w*)\b.{0,35}\b(production|output|mining)\b",r"\bproduction (?:begins|started|commences)\b",r"запустил.{0,35}производств",r"начал.{0,35}добыч"],
 "production_shutdown":[r"\b(shutdown|shuts down|halts production|mine closure)\b",r"остановил.{0,20}(производств|добыч)"],
 "export_restriction":[r"\b(export ban|export restriction|export tariff)\b",r"запрет.{0,20}экспорт"],
 "rate_increase":[r"\b(raises|hikes|increases)\b.{0,35}\b(interest rates|policy rate)\b",r"повысил.{0,20}(ставк|процент)"],
 "rate_decrease":[r"\b(cuts|lowers|reduces)\b.{0,35}\b(interest rates|policy rate)\b",r"снизил.{0,20}(ставк|процент)"],
 "new_factory":[r"\b(opens|opened|builds|inaugurates)\b.{0,40}\b(factory|plant|fab)\b",r"открыл.{0,40}(завод|фабрик)"],
 "contract_award":[r"\b(wins|awarded|secures)\b.{0,40}\b(contract|tender|order)\b",r"выиграл.{0,30}(тендер|контракт)"],
 "cyber_attack":[r"\b(ransomware|cyberattack|cyber attack)\b",r"кибератак|взлом"],
 "drug_approval":[r"\b(fda approves|regulator approves)\b.{0,60}\b(drug|therapy|vaccine)\b",r"одобрил.{0,30}(препарат|лекарств)"],
}

def utc(value:str)->dt.datetime:
    t=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if t.tzinfo is None:raise ValueError('time must be timezone-aware')
    return t.astimezone(UTC)

def classify(headline:str)->list[str]:
    if not isinstance(headline,str) or not headline.strip():return []
    return [kind for kind,patterns in PHRASES.items() if any(re.search(p,headline,re.I|re.U) for p in patterns)]

def open_db(filename:str):
    c=sqlite3.connect(filename,timeout=15)
    c.execute('PRAGMA busy_timeout=15000')
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS ci_events(id TEXT PRIMARY KEY,first_seen_at TEXT NOT NULL,
      published_at TEXT,headline TEXT NOT NULL,url TEXT NOT NULL,source TEXT NOT NULL,
      event_type TEXT NOT NULL,subject TEXT,raw_hash TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ci_exposures(subject TEXT NOT NULL,ticker TEXT NOT NULL,role TEXT NOT NULL,
      evidence_url TEXT NOT NULL,evidence_first_seen_at TEXT NOT NULL,
      verified_at TEXT NOT NULL,verified_by TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,
      PRIMARY KEY(subject,ticker,role,evidence_url));
    CREATE TABLE IF NOT EXISTS ci_paths(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,
      ticker TEXT NOT NULL,role TEXT NOT NULL,qualitative_impact TEXT NOT NULL,
      lag TEXT NOT NULL,mechanism TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'HYPOTHESIS',
      created_at TEXT NOT NULL, UNIQUE(event_id,ticker,role));
    ''')
    c.commit();return c

def register_exposure(c,*,subject:str,ticker:str,role:str,evidence_url:str,
                      evidence_first_seen_at:str,verified_at:str,verified_by:str):
    if not evidence_url.startswith('https://') or not verified_by.strip():raise ValueError('verified evidence and reviewer required')
    if utc(verified_at)<utc(evidence_first_seen_at):raise ValueError('verified before evidence existed')
    c.execute('INSERT OR IGNORE INTO ci_exposures VALUES (?,?,?,?,?,?,?,1)',
             (subject.strip().lower(),ticker.strip().upper(),role,evidence_url,evidence_first_seen_at,verified_at,verified_by))
    c.commit()

def ingest_event(c,*,headline:str,url:str,source:str,first_seen_at:str,published_at:str|None=None,subject:str|None=None):
    if not url.startswith('https://') or not source or not headline:raise ValueError('provenance required')
    seen=utc(first_seen_at)
    if published_at and utc(published_at)>seen+dt.timedelta(minutes=10):raise ValueError('publication after observation time')
    subject=(subject or '').strip().lower()
    ids=[]
    for kind in classify(headline):
        # Not independent news if same URL repeated.
        event_id=hashlib.sha256((url+'|'+kind).encode()).hexdigest()[:24]
        c.execute('''INSERT OR IGNORE INTO ci_events
           VALUES(?,?,?,?,?,?,?,?,?)''',(event_id,seen.isoformat(),published_at,headline,url,source,kind,
                 subject or None,hashlib.sha256(headline.encode()).hexdigest()))
        ids.append(event_id)
    c.commit();return ids

def build_paths(c,event_id:str,asof:str):
    cutoff=utc(asof)
    row=c.execute('SELECT first_seen_at,event_type,subject FROM ci_events WHERE id=?',(event_id,)).fetchone()
    if row is None:raise KeyError(event_id)
    first,kind,subject=row
    if utc(first)>cutoff:raise ValueError('future event leakage')
    if not subject:return []
    rule=RULES[kind]
    exposures=c.execute('''SELECT ticker,role FROM ci_exposures WHERE subject=? AND active=1
      AND verified_at<=? AND evidence_first_seen_at<=?''',(subject,cutoff.isoformat(),cutoff.isoformat())).fetchall()
    created=[]
    for ticker,role in exposures:
        impact=rule.get(role)
        if impact is None:continue
        key=hashlib.sha256(f'{event_id}|{ticker}|{role}'.encode()).hexdigest()[:24]
        c.execute('''INSERT OR IGNORE INTO ci_paths
           (id,event_id,ticker,role,qualitative_impact,lag,mechanism,state,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)''',(key,event_id,ticker,role,impact,rule['lag'],rule['mechanism'],
                                           'UNVALIDATED_RELATIONSHIP_HYPOTHESIS',cutoff.isoformat()))
        created.append({'ticker':ticker,'role':role,'scenario':impact,'lag':rule['lag'],
                        'state':'UNVALIDATED_RELATIONSHIP_HYPOTHESIS','path_id':key})
    c.commit();return created

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('headline');args=p.parse_args()
    print(json.dumps({'event_types':classify(args.headline),'notice':'Economic scenarios only, not price forecast.'},ensure_ascii=False))
