#!/usr/bin/env python3
"""HOPE dataport: unified multi-provider sports data layer (DATAPORT-1).

Providers: TheSportsDB free key (123, docs trial key) and ESPN public
scoreboard endpoints. api-football cannot be auto-registered without
e-mail confirmation, so it is recorded as unavailable in
/opt/agent/apikeys.json. Covers football (soccer) plus NBA, NFL, NHL
and MLB. Every HTTP response is cached on disk with a 6h TTL and each
provider is rate limited. Stdlib only, no network on import.
"""
import datetime as dt
import hashlib
import json
import pathlib
import re
import time
import unicodedata
import urllib.request
import urllib.parse

BASE = pathlib.Path("/opt/ORACLE_EVA")
CACHE = BASE / "dataport_cache"
KEYS_FILE = pathlib.Path("/opt/agent/apikeys.json")
TTL_SECONDS = 6 * 3600
MAX_HTTP_BYTES = 2_000_000
TSDB_KEY = "123"
TSDB_SPORT = {"football": "Soccer", "nba": "Basketball", "nfl": "American Football", "nhl": "Ice Hockey", "mlb": "Baseball"}
SPORT_NAME = {"football": "soccer", "nba": "basketball", "nfl": "american football", "nhl": "ice hockey", "mlb": "baseball"}
ESPN_SPORT = {"football": "soccer/{league}", "nba": "basketball/nba", "nfl": "football/nfl", "nhl": "hockey/nhl", "mlb": "baseball/mlb"}
FOOTBALL_LEAGUES = {"en.1": "eng.1", "en.2": "eng.2", "de.1": "ger.1", "de.2": "ger.2", "es.1": "esp.1", "fr.1": "fra.1", "it.1": "ita.1", "nl.1": "ned.1", "pt.1": "por.1", "tr.1": "tur.1"}
TSDB_LEAGUE_NAMES = {
    "en.1": {"english premier league"}, "en.2": {"english league championship", "english championship"},
    "de.1": {"german bundesliga"}, "de.2": {"german 2 bundesliga", "german bundesliga 2"},
    "es.1": {"spanish la liga"}, "fr.1": {"french ligue 1"}, "it.1": {"italian serie a"},
    "nl.1": {"dutch eredivisie"}, "pt.1": {"portuguese primeira liga"}, "tr.1": {"turkish super lig"}}
TSDB_OTHER_LEAGUES = {"nba": {"nba"}, "nfl": {"nfl"}, "nhl": {"nhl"}, "mlb": {"mlb"}}
RATE_SECONDS = {"thesportsdb": 2.2, "espn": 1.2}
UTC = dt.timezone.utc
TEAM_ALIASES = {
    "man utd": "manchester united", "man united": "manchester united",
    "man city": "manchester city", "spurs": "tottenham",
    "tottenham hotspur": "tottenham", "wolves": "wolverhampton",
    "wolverhampton wanderers": "wolverhampton", "nottm forest": "nottingham forest",
    "inter milan": "inter", "internazionale milano": "inter",
    "atletico de madrid": "atletico madrid", "stade rennais": "rennes",
    "olympique lyonnais": "lyon", "olympique de marseille": "marseille",
    "bayer 04 leverkusen": "bayer leverkusen", "juventus fc": "juventus"}


def _now():
    return dt.datetime.now(UTC)


def _clock(s):
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d.astimezone(UTC) if d.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(x for x in s if not unicodedata.combining(x))
    words = re.findall(r"[a-z0-9]+", s.casefold())
    while words and words[-1] in ("fc", "afc"):
        words.pop()
    n = " ".join(words)
    return TEAM_ALIASES.get(n, n)


def _check_date(date):
    if not isinstance(date, str) or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", date):
        raise ValueError("BAD_DATE")
    return date


def _check_args(sport, league, date):
    if sport not in TSDB_SPORT:
        raise ValueError("BAD_SPORT")
    _check_date(date)
    if sport == "football" and league not in FOOTBALL_LEAGUES:
        raise ValueError("BAD_LEAGUE")
    if sport != "football" and league not in ESPN_SPORT:
        raise ValueError("BAD_LEAGUE")


def _rate(provider):
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = CACHE / "_rate.json"
    try:
        last = json.loads(path.read_text())
    except (OSError, ValueError):
        last = {}
    prev = float(last.get(provider, 0.0))
    wait = RATE_SECONDS[provider] - (time.time() - prev)
    if wait > 0:
        time.sleep(min(wait, 15.0))
    last[provider] = time.time()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(last))
    tmp.replace(path)


def _http(url, provider):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.port not in (None, 443) or u.username or u.password or u.fragment:
        raise RuntimeError("UNSAFE_PUBLIC_URL")
    if u.hostname not in ("www.thesportsdb.com", "site.api.espn.com"):
        raise RuntimeError("HOST_DENIED")
    _rate(provider)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "HOPE-EVA-DATAPORT/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        if r.status != 200:
            raise RuntimeError("HTTP_" + str(r.status))
        return r.read(MAX_HTTP_BYTES + 1)


def _fetch(url, provider):
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = CACHE / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json")
    try:
        doc = json.loads(path.read_text())
        if _now().timestamp() - float(doc.get("fetched_at", 0)) < TTL_SECONDS:
            return doc.get("payload")
    except (OSError, ValueError, TypeError):
        pass
    payload = json.loads(_http(url, provider))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"url": url, "fetched_at": _now().timestamp(), "payload": payload}, default=str))
    tmp.replace(path)
    return payload


def _tsdb_url(sport, date):
    return ("https://www.thesportsdb.com/api/v1/json/" + TSDB_KEY +
            "/eventsday.php?d=" + date + "&s=" + urllib.parse.quote(TSDB_SPORT[sport]))


def _espn_url(sport, league, date):
    path = ESPN_SPORT[sport].format(league=FOOTBALL_LEAGUES.get(league, league))
    return ("https://site.api.espn.com/apis/site/v2/sports/" + path +
            "/scoreboard?dates=" + date.replace("-", "") + "&limit=150")


def get_upcoming_odds(sport, league, date):
    """Return complete real ESPN h2h markets; omit missing odds."""
    _check_args(sport, league, date)
    result = []
    for e in _fetch(_espn_url(sport, league, date), "espn").get("events", []):
        start = _clock(e.get("date"))
        if not start or start <= _now():
            continue
        books = []
        for c in e.get("competitions", []):
            if c.get("status", {}).get("type", {}).get("state") != "pre":
                continue
            names = {t.get("homeAway"): t.get("team", {}).get("displayName") for t in c.get("competitors", [])}
            for q in c.get("odds", []):
                try:
                    outcomes = []
                    for side in ["home", "away"] + (["draw"] if sport == "football" else []):
                        field = "drawOdds" if side == "draw" else side + "TeamOdds"
                        raw = q.get(field, {}).get("moneyLine")
                        if raw is None:
                            raw = q["moneyline"][side]["close"]["odds"]
                        line = 100.0 if str(raw).upper() == "EVEN" else float(raw)
                        name = "Draw" if side == "draw" else names[side]
                        if not line or not name:
                            raise ValueError("Missing quote")
                        price = 1 + (line / 100 if line > 0 else 100 / abs(line))
                        outcomes.append({"name": name, "price": price})
                    provider = q.get("provider", {})
                    books.append({"key": str(provider.get("id") or provider.get("name") or "espn"), "markets": [{"key": "h2h", "outcomes": outcomes}]})
                except (KeyError, TypeError, ValueError):
                    continue
        if books:
            result.append({"id": str(e["id"]), "event": e["name"], "start_time": start.isoformat(), "sport": sport, "league": league, "bookmakers": books})
    return result
