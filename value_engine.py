#!/usr/bin/env python3
"""Consensus value recommendations; paper bank, never executes wagers."""
import argparse, datetime as dt, fcntl, hashlib, json, math, os, sqlite3
from pathlib import Path
import urllib.request
import dataport
BASE = Path(__file__).resolve().parent
BANK = 1000.0

def devig(odds):
    prices = [float(x) for x in odds]
    if len(prices) < 2 or any(not math.isfinite(x) or x <= 1 for x in prices):
        raise ValueError("Complete decimal market required")
    total = sum(1 / x for x in prices)
    return [(1 / x) / total for x in prices]

def kelly(p, odds, bank=BANK, fraction=0.25):
    if not all(math.isfinite(x) for x in (p, odds, bank, fraction)):
        raise ValueError("Non-finite input")
    if not 0 <= p <= 1 or odds <= 1 or bank < 0 or not 0 <= fraction <= 1:
        raise ValueError("Invalid Kelly input")
    return bank * fraction * max(0.0, (p * odds - 1) / (odds - 1))

def candidates(event):
    start = dt.datetime.fromisoformat(event["start_time"].replace("Z", "+00:00"))
    if start.tzinfo is None or start <= dt.datetime.now(dt.timezone.utc):
        return
    groups = {}
    for book in event.get("bookmakers", []):
        for market in book.get("markets", []):
            if market.get("key") != "h2h":
                continue
            try:
                quotes = {o["name"]: float(o["price"]) for o in market["outcomes"]}
                names = tuple(sorted(quotes))
                if len(names) != len(market["outcomes"]):
                    continue
                devig([quotes[n] for n in names])
                groups.setdefault((market["key"], names), {})[book["key"]] = quotes
            except (KeyError, TypeError, ValueError):
                continue
    for (market, names), books in groups.items():
        averages = [sum(q[n] for q in books.values()) / len(books) for n in names]
        probabilities = devig(averages)
        for name, p in zip(names, probabilities):
            for source, quotes in books.items():
                odds = quotes[name]
                ev = p * odds - 1
                if 1.7 <= odds <= 2.5 and ev >= 0.03 - 1e-12:
                    key = [event.get("sport"), event.get("league"), event["id"], market, name, source]
                    yield dict(bet_key=hashlib.sha256(json.dumps(key).encode()).hexdigest(), event=event["event"], event_id=event["id"], start_time=start.isoformat(), market=market, outcome=name, source=source, odds=odds, p=p, ev=ev, stake=round(kelly(p, odds), 2))

def connect(path):
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS value_bets (
        id INTEGER PRIMARY KEY, bet_key TEXT NOT NULL UNIQUE,
        event TEXT NOT NULL, event_id TEXT NOT NULL, start_time TEXT NOT NULL,
        market TEXT NOT NULL, outcome TEXT NOT NULL, source TEXT NOT NULL,
        odds REAL NOT NULL, p REAL NOT NULL, ev REAL NOT NULL, stake REAL NOT NULL,
        timestamp TEXT NOT NULL, notified_at TEXT, notification_attempts INTEGER NOT NULL DEFAULT 0,
        notification_error TEXT, paper INTEGER NOT NULL DEFAULT 1)""")
    con.commit()
    return con

def save(con, bets):
    count = 0
    for bet in bets:
        row = dict(bet, timestamp=dt.datetime.now(dt.timezone.utc).isoformat())
        fields = list(row)
        with con:
            cursor = con.execute("INSERT OR IGNORE INTO value_bets (" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ")", [row[k] for k in fields])
            count += cursor.rowcount
    return count

def credentials():
    def read(env, file):
        value = os.environ.get(env)
        if value:
            return value
        try:
            return Path(file).read_text().strip()
        except OSError:
            return ""
    return read("NASTYA_BOT_TOKEN", "/opt/agent/tgtoken"), read("NASTYA_CHAT_ID", "/opt/agent/tgchat")

def notify(con):
    rows = con.execute("SELECT * FROM value_bets WHERE notified_at IS NULL ORDER BY id").fetchall()
    if not rows:
        return 0
    token, chat = credentials()
    if not token or not chat:
        raise RuntimeError("Telegram credentials unavailable; pending bets retained")
    done = 0
    for row in rows:
        text = ("📊 Бумажная ставка (не реальная)\n" + row["event"] + "\n" + row["outcome"] + " / " + row["source"] + "\nКоэффициент: {:.2f}\nEV: {:+.2%}\nСтавка: ${:.2f} (банк $1000)").format(row["odds"], row["ev"], row["stake"])
        request = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage", data=json.dumps(dict(chat_id=chat, text=text[:4000])).encode(), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                answer = json.loads(response.read())
            if not answer.get("ok"):
                raise RuntimeError("Telegram rejected notification")
        except Exception as exc:
            with con:
                con.execute("UPDATE value_bets SET notification_attempts=notification_attempts+1, notification_error=? WHERE id=?", (type(exc).__name__, row["id"]))
            print("Telegram delivery failed:", type(exc).__name__, flush=True)
            return done
        with con:
            con.execute("UPDATE value_bets SET notified_at=?, notification_attempts=notification_attempts+1, notification_error=NULL WHERE id=?", (dt.datetime.now(dt.timezone.utc).isoformat(), row["id"]))
        done += 1
    return done

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(BASE / "oracle.sqlite3"))
    parser.add_argument("--date", default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()
    with open(BASE / ".value_engine.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        con = connect(args.db)
        events = added = failures = 0
        targets = [("football", league) for league in dataport.FOOTBALL_LEAGUES] + [(s, s) for s in ("nba", "nfl", "nhl", "mlb")]
        for sport, league in targets:
            try:
                upcoming = dataport.get_upcoming_odds(sport, league, args.date)
                events += len(upcoming)
                for event in upcoming:
                    added += save(con, candidates(event))
            except Exception as exc:
                failures += 1
                print("Feed failed:", sport, league, type(exc).__name__, flush=True)
        delivered = 0 if args.no_notify else notify(con)
        print(json.dumps(dict(events=events, new_bets=added, notified=delivered, feed_failures=failures)), flush=True)
        con.close()
        if failures == len(targets):
            raise SystemExit(1)

if __name__ == "__main__":
    main()
