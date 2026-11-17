import datetime as dt
import json
import pytest
import value_engine as v
import dataport

def test_devig():
    assert v.devig([2, 2]) == pytest.approx([0.5, 0.5])
    odds = [1.9, 3.2, 4.1]
    p = v.devig(odds)
    assert sum(p) == pytest.approx(1)
    assert p[0] / p[1] == pytest.approx(3.2 / 1.9)

@pytest.mark.parametrize("odds", [[], [2], [1, 2], [0, 2], [float("nan"), 2], [float("inf"), 2]])
def test_devig_bad(odds):
    with pytest.raises(ValueError):
        v.devig(odds)

def test_kelly():
    assert v.kelly(0.55, 2) == pytest.approx(25)
    assert v.kelly(0.6, 2.5) == pytest.approx(1000 * 0.25 * 0.5 / 1.5)
    assert v.kelly(0.4, 2) == 0
    assert v.kelly(0.5, 2) == 0

@pytest.mark.parametrize("p,odds", [(-0.1, 2), (1.1, 2), (0.5, 1), (float("nan"), 2)])
def test_kelly_bad(p, odds):
    with pytest.raises(ValueError):
        v.kelly(p, odds)

def event():
    books = []
    for source, home, away in [("a", 2.4, 1.7), ("b", 1.8, 2.1), ("c", 1.8, 2.1)]:
        books.append(dict(key=source, markets=[dict(key="h2h", outcomes=[dict(name="Home", price=home), dict(name="Away", price=away)])]))
    return dict(id="test", event="Home — Away", start_time=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(), bookmakers=books)

def test_candidates_and_dedup(tmp_path):
    bets = list(v.candidates(event()))
    assert bets
    assert all(b["ev"] >= 0.03 and 1.7 <= b["odds"] <= 2.5 for b in bets)
    con = v.connect(tmp_path / "test.sqlite3")
    assert v.save(con, bets) == len(bets)
    assert v.save(con, bets) == 0
    assert con.execute("SELECT count(*) FROM value_bets WHERE paper=1").fetchone()[0] == len(bets)
    con.close()

def test_single_book_no_edge():
    e = event()
    e["bookmakers"] = e["bookmakers"][:1]
    assert list(v.candidates(e)) == []

def test_notify_retry_and_success(tmp_path, monkeypatch):
    con = v.connect(tmp_path / "notify.sqlite3")
    bets = list(v.candidates(event()))
    v.save(con, bets)
    monkeypatch.setattr(v, "credentials", lambda: ("fake-token", "fake-chat"))
    def fail(*args, **kwargs):
        raise TimeoutError("private URL must not be logged")
    monkeypatch.setattr(v.urllib.request, "urlopen", fail)
    assert v.notify(con) == 0
    assert con.execute("SELECT notified_at FROM value_bets").fetchone()[0] is None
    sent = []
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b"{\"ok\": true}"
    def success(request, timeout):
        sent.append(json.loads(request.data)["text"])
        return Reply()
    monkeypatch.setattr(v.urllib.request, "urlopen", success)
    assert v.notify(con) == len(bets)
    assert all("Коэффициент:" in t and "EV:" in t and "Ставка:" in t for t in sent)
    assert v.notify(con) == 0
    con.close()

def test_dataport_quotes(monkeypatch):
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    comp = dict(status=dict(type=dict(state="pre")), competitors=[dict(homeAway="home", team=dict(displayName="Home")), dict(homeAway="away", team=dict(displayName="Away"))], odds=[dict(provider=dict(id="book"), homeTeamOdds=dict(moneyLine=140), awayTeamOdds=dict(moneyLine=-150))])
    payload = dict(events=[dict(id="123", name="Home — Away", date=future, competitions=[comp])])
    monkeypatch.setattr(dataport, "_fetch", lambda *args: payload)
    quotes = dataport.get_upcoming_odds("nba", "nba", future[:10])
    outcomes = quotes[0]["bookmakers"][0]["markets"][0]["outcomes"]
    assert outcomes[0]["price"] == pytest.approx(2.4)
    assert outcomes[1]["price"] == pytest.approx(1 + 100 / 150)
    assert dataport.get_upcoming_odds("football", "en.1", future[:10]) == []
    comp["odds"] = []
    assert dataport.get_upcoming_odds("nba", "nba", future[:10]) == []

def test_dataport_modern_moneyline(monkeypatch):
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    quote = dict(provider=dict(name="DraftKings"), moneyline=dict(home=dict(close=dict(odds="+120")), away=dict(close=dict(odds="-142"))))
    comp = dict(status=dict(type=dict(state="pre")), competitors=[dict(homeAway="home", team=dict(displayName="Home")), dict(homeAway="away", team=dict(displayName="Away"))], odds=[quote])
    monkeypatch.setattr(dataport, "_fetch", lambda *args: dict(events=[dict(id="modern", name="Home — Away", date=future, competitions=[comp])]))
    events = dataport.get_upcoming_odds("nba", "nba", future[:10])
    assert events[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] == pytest.approx(2.2)

def test_started_event_skipped():
    e = event()
    e["start_time"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
    assert list(v.candidates(e)) == []
