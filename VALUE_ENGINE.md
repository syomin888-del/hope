# VALUE-ENGINE-1

Paper recommendations only; no bookmaker execution or real-money stake.
Run: python3 value_engine.py [--date YYYY-MM-DD] [--no-notify] [--db PATH].
Tests: python3 -m pytest -q tests/test_value_engine.py (python3-pytest).

Complete h2h markets are grouped by identical outcome sets. Decimal odds
are averaged across distinct captured bookmakers, then inverse averages
are normalized to sum to one. Each quote is evaluated with EV=p*odds-1.
Inclusive filters: EV >= 3%, decimal odds 1.7 through 2.5. Stake is
1000 * 0.25 * max(0, EV/(odds-1)), rounded to cents. The bank is a fixed
paper sizing reference, not a funded account or cumulative exposure cap.

SQLite value_bets stores event, outcome, source, market, odds, p, ev,
stake, timestamp and paper=1. Unique event/market/outcome/source keys
avoid repeat recommendations. Pending Russian Telegram messages retry on
subsequent scans; an ambiguous network timeout may cause duplicate delivery.
Credentials: NASTYA_BOT_TOKEN/NASTYA_CHAT_ID or /opt/agent/tgtoken and tgchat.
Do not put credentials into git.

Timer: oracle-value-engine.timer, every 30 minutes at :00:45/:30:45 UTC,
ordered after the existing capture service. Install units from systemd/.
Logs: journalctl -u oracle-value-engine.service.

The existing dataport lacked get_upcoming_odds; this change adds ESPN
legacy and current moneyline parsing. Missing markets (including soccer
draw quotes) are omitted, not synthesized. ESPN typically exposes one
bookmaker, so positive consensus edges may be absent. Live validation:
16 quoted upcoming events, 0 qualifying bets, 0 feed failures. Telegram
delivery/retry is tested with mocked HTTP; no fabricated live bet is sent.
