#!/usr/bin/env python3
"""Fonbet line reader. Plain HTTP like a regular visitor, no login.
Poll politely: caller is a systemd timer every 15 min, max 20 bets/day
enforced in value engine, not here."""
import gzip, json, re, time, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      "Accept": "application/json", "Accept-Encoding": "gzip",
      "Referer": "https://www.fonbet.ru/"}
HOSTS = ["https://line03.bkfon-resource.ru",
         "https://line04.bkfon-resource.ru",
         "https://line05.bkfon-resource.ru"]

def get_line():
    last_err = None
    for host in HOSTS:
        url = host + "/line/default/getGamesIn.default.json.gz"
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                return json.loads(data.decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise RuntimeError("fonbet all hosts failed: %r" % last_err)

FOOTBALL_KIND = 1

def extract_h2h(events=None):
    line = events or get_line()
    out = []
    for ev in line.get("events", []):
        if ev.get("kind") != "football" or ev.get("level") != 0:
            continue
        start = ev.get("startTime")
        if not start:
            continue
        i = 0
        print("event", ev.get("id"), ev.get("name"), "teams:", ev.get("teamHome"), ev.get("teamAway"),
              "packs:", len(ev.get("packs", [])), file=__import__("sys").stderr)
        i += 1
        if i > 5:
            break
    return out
