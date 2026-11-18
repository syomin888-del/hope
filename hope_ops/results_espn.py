#!/usr/bin/env python3
# ESPN scoreboard provider (stdlib only). No key needed.
import json, re, unicodedata, urllib.request
SOCCER = {"en.1":"eng.1","en.2":"eng.2","de.1":"ger.1","de.2":"ger.2","es.1":"esp.1","fr.1":"fra.1","it.1":"ita.1","nl.1":"ned.1","pt.1":"por.1","tr.1":"tur.1"}
RU = {"angliya":"en.1","germaniya":"de.1","ispaniya":"es.1","italiya":"it.1","niderlandy":"nl.1","portugaliya":"pt.1","turtsiya":"tr.1","frantsiya":"fr.1"}
SOCCER = {"en.1":"eng.1","en.2":"eng.2","de.1":"ger.1","de.2":"ger.2","es.1":"esp.1","fr.1":"fra.1","it.1":"ita.1","nl.1":"ned.1","pt.1":"por.1","tr.1":"tur.1"}
RU = {"angliya":"en.1","germaniya":"de.1","ispaniya":"es.1","italiya":"it.1","niderlandy":"nl.1","portugaliya":"pt.1","turtsiya":"tr.1","frantsiya":"fr.1"}
_CYR = {"а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"yo","ж":"zh","з":"z","и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"ts","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"}

def _translit(s):
    return "".join(_CYR.get(ch, ch) for ch in s.lower())

def norm_name(s):
    s = _translit(str(s or ""))
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()

def espn_league(sport, league):
    sport = str(sport or "").strip().lower()
    league = str(league or "")
    if sport in ("am-football", "american football", "nfl") or "nfl" in league.lower():
        return "football", "nfl"
    if league in SOCCER:
        return "soccer", SOCCER[league]
    low = league.lower()
    for pref, code in RU.items():
        if low.startswith(pref):
            return "soccer", SOCCER[code]
    return None

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ORACLE_EVA/1.0)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def fetch_final(sport, league, date):
    mapped = espn_league(sport, league)
    if mapped is None:
        return []
    espn_sport, espn_lg = mapped
    day = str(date)[:10].replace("-", "")
    url = "https://site.api.espn.com/apis/site/v2/sports/%s/%s/scoreboard?dates=%s&limit=150" % (espn_sport, espn_lg, day)
    try:
        data = _get(url)
    except Exception:
        return []
    out = []
    for ev in data.get("events", []) or []:
        try:
            comp = ev["competitions"][0]
            st = (comp.get("status") or {}).get("type", {})
            completed = bool(st.get("completed")) or st.get("name") in ("FINAL", "FT", "FULL_TIME")
            if not completed:
                continue
            home = away = None
            hs = as_ = None
            for c in comp.get("competitors", []):
                try:
                    sc = int(c.get("score") if c.get("score") is not None else -1)
                except (TypeError, ValueError):
                    sc = -1
                if sc < 0:
                    completed = False
                    break
                if c.get("homeAway") == "home":
                    home, hs = c.get("team", {}).get("displayName"), sc
                else:
                    away, as_ = c.get("team", {}).get("displayName"), sc
            if not completed or home is None or away is None:
                continue
            out.append({"home": home, "away": away, "home_score": hs, "away_score": as_, "status": "FINAL", "espn_id": str(ev.get("id") or ""), "kickoff_utc": comp.get("date") or ev.get("date") or ""})
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out

def _selftest():
    checks = [("football", "it.1", "2026-11-01"), ("football", "Италия. Серия А. Сезон 26/27", "2026-11-01"), ("am-football", "NFL. Регулярный сезон", "2026-11-01")]
    ok = True
    for sport, league, day in checks:
        rows = fetch_final(sport, league, day)
        print("SELFTEST %s | %s | %s -> %d finals" % (sport, league, day, len(rows)))
        if not rows:
            ok = False
    print("SELFTEST_OK" if ok else "SELFTEST_EMPTY")
    return 0 if ok else 1

if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
            continue
    return out

def _selftest():
    checks = [("football", "it.1", "2026-11-01"), ("football", "Италия. Серия А. Сезон 26/27", "2026-11-01"), ("am-football", "NFL. Регулярный сезон", "2026-11-01")]
    ok = True
    for sport, league, day in checks:
        rows = fetch_final(sport, league, day)
        print("SELFTEST %s | %s | %s -> %d finals" % (sport, league, day, len(rows)))
        if not rows:
            ok = False
    print("SELFTEST_OK" if ok else "SELFTEST_EMPTY")
    return 0 if ok else 1

if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
