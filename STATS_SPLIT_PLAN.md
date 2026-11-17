# STATS-SPLIT: Verified vs Simulated Statistics Separation Plan

Status: analysis only - no code changed.
Date: 2026-10-31
Scope: HOPE UI /opt/ORACLE_EVA/hope_ui/v0_1 and backend evidence-grade separation in hope_ops/releases/v3_0_paper_finality and v4_0_result_bridge.

---

## 1. How the backend separates P&L by evidence grade

The backend distinguishes two evidence classes by inspecting the v55_paper.source column.

### 1.1 Source tags (the discriminator)

- hope_ops/releases/v3_0_paper_finality/finality.py:19  SOURCE_PREFIX=HOPE_V3_AS_RECORDED_ODDS_NOT_BOOKMAKER_VERIFIED
- hope_ops/releases/v4_0_result_bridge/bridge.py:19  SOURCE=HOPE_V4_INDEPENDENT_SCORE_AS_RECORDED_ODDS_NOT_BOOKMAKER_VERIFIED

Rows whose source starts with HOPE_V3_AS_RECORDED_ODDS_NOT_BOOKMAKER_VERIFIED are simulated / unverified-odds settlements. All other settled rows (e.g. EVA_R8_ESPN_FOOTBALLDATA_FINAL_EXACT_QUOTE, v4_games_FINAL) are pre-existing settlements.

### 1.2 Separation logic in finality.py

hope_ops/releases/v3_0_paper_finality/finality.py:252-283 (classify_all):
- Line 258: pool=SIMULATED_UNVERIFIED_ODDS if source startswith SOURCE_PREFIX else PREEXISTING_SETTLEMENT
- Lines 259-260: accumulate rows, stake, net per pool.
- Lines 261-274: category labels FINISHED_AS_RECORDED_UNVERIFIED_ODDS vs FINISHED_PREEXISTING.
- Line 282: returns pnl_separated_by_quote_evidence (per-pool rows, stake_usd, net_usd, roi_pct).
- Line 283: explicit WARNING that native all-paper P&L mixes V3 simulations and that separated P&L must be used.

### 1.3 Separation logic in bridge.py

hope_ops/releases/v4_0_result_bridge/bridge.py:421-444 (classify):
- Lines 425-431: per-row classification into V4_RECONCILED_SIMULATION, V3_AS_RECORDED_SIMULATION, PRE_V3_SETTLEMENT, with per-kind count/stake/net.
- Lines 436-441: same split for portfolio rows.
- Line 444: returns pnl_separated keyed by kind.

### 1.4 Current DB reality (read-only snapshot)

v55_paper settled rows by pool:
- PREEXISTING_SETTLEMENT: 39 rows, stake 390.0, net +31.2
- SIMULATED_UNVERIFIED_ODDS: 363 rows, stake 3630.0, net -637.9

v56_portfolio settled rows by pool:
- PREEXISTING_SETTLEMENT: 2 rows, stake 20.0, net -20.0
- SIMULATED_UNVERIFIED_ODDS: 1 row, stake 10.0, net -10.0

So the backend has the data needed to separate the two classes; the UI does not use it.

---

## 2. What the HOPE UI currently shows

### 2.1 Backend endpoint /api/settlements

hope_ui/v0_1/hope_server.py:149-169 (ReadOnlyData.settlements):
- Lines 156-158: perf = COUNT(*), SUM(profit), SUM(stake) over all v55_paper rows with status IN (WIN,LOSS,PUSH) - no source filter, so it mixes PREEXISTING and SIMULATED.
- Lines 159-162: pp = same aggregate over v56_portfolio joined to v55_paper - also unfiltered by source.
- Line 168: returns paper_settled and portfolio_settled as single mixed aggregates; the note (line 169) only says ROI is over confirmed WIN/LOSS/PUSH and excludes PENDING/archive - it does not mention the simulated/unverified class.
- The endpoint does not expose pnl_separated_by_quote_evidence or pnl_separated from the backend classifiers.

### 2.2 Frontend rendering

hope_ui/v0_1/app.js:
- Line 22 (perf): renders a single panel with Рассчитанных, P&L $, ROI from one aggregate object.
- Line 23 (renderSettlements): calls perf(g,Paper - рассчитанные исходы,d.paper_settled) and perf(g,Портфель - рассчитанные исходы,d.portfolio_settled) - one mixed number each, no split by evidence grade.
- Line 23 notice text: Статусы PENDING и исторический архив исключены из P&L и ROI. Отсутствие результатов не означает прибыль. - no mention that simulated/unverified-odds settlements are included.
- Line 2 (titles.settlements): Подтверждённые исходы и честная статистика расчёта - implies verified, but the numbers include simulated rows.
- renderPaper (line 19) and renderPortfolio (line 20) show status but not source, so a user cannot tell a simulated settlement from a pre-existing one in the tables.

### 2.3 Net effect

The UI currently presents one blended P&L/ROI for paper and portfolio that mixes:
- PREEXISTING_SETTLEMENT (verified/legacy exact-quote settlements), and
- SIMULATED_UNVERIFIED_ODDS (V3/V4 as-recorded, not bookmaker-verified).

Given the snapshot, the blended paper P&L is dominated by the simulated class (363 of 402 settled rows, net -637.9 vs +31.2), so the displayed ROI is effectively a simulation result presented as честная статистика.

---

## 3. Plan to display verified and simulated statistics separately

No code changes in this task. Proposed changes for a follow-up task:

### 3.1 Backend (hope_ui/v0_1/hope_server.py)

1. In settlements() (lines 149-169), add source-aware aggregates:
   - paper_settled_verified: same aggregate but AND (source IS NULL OR source NOT LIKE HOPE_V3_AS_RECORDED_ODDS_NOT_BOOKMAKER_VERIFIED%).
   - paper_settled_simulated: same aggregate but AND source LIKE HOPE_V3_AS_RECORDED_ODDS_NOT_BOOKMAKER_VERIFIED%.
   - Mirror both for v56_portfolio (portfolio_settled_verified, portfolio_settled_simulated).
2. Keep the existing blended paper_settled / portfolio_settled for backward compatibility, but mark them as blended in a new field or add a blended: true flag.
3. Add a pnl_separated block matching the backend classifier shape (rows, stake_usd, net_usd, roi_pct) so the UI and CLI agree.
4. Update the note string to state explicitly that SIMULATED_UNVERIFIED_ODDS is shown separately and is not bookmaker-verified.
5. Optionally include source in /api/paper and /api/portfolio row payloads (currently paper() at lines 117-124 and portfolio() at lines 126-134 omit source).

### 3.2 Frontend (hope_ui/v0_1/app.js)

1. In renderSettlements (line 23), replace the two mixed perf panels with four panels:
   - Paper - подтверждённые (PREEXISTING_SETTLEMENT)
   - Paper - симуляция (SIMULATED_UNVERIFIED_ODDS)
   - Портфель - подтверждённые
   - Портфель - симуляция
   Each using the corresponding new field from 3.1.
2. Add a clear notice: verified vs simulated, and that simulated odds are not bookmaker-verified.
3. In renderPaper (line 19) and renderPortfolio (line 20), add a source/evidence column (or a derived evidence_class badge) so each row is labeled.
4. Update titles.settlements (line 2) to reflect the split.

### 3.3 Tests / verification

1. Read-only SQL check that verified + simulated == blended for both paper and portfolio.
2. Confirm /api/settlements JSON contains all new fields and no existing field is removed.
3. Confirm UI renders four panels and that the sum of the two paper panels equals the old blended number.
4. Confirm no writes: hope_server.py remains read-only (PRAGMA query_only=ON, line 57).

### 3.4 Rollout

1. Back up hope_server.py and app.js to hope_ui/v07_backups/ (existing convention).
2. Apply backend change first, verify JSON via curl on localhost.
3. Apply frontend change, verify rendering.
4. Restart the HOPE UI service only after both are verified.

---

## 4. File/line reference summary

| Concern | File | Lines |
|---|---|---|
| V3 source prefix | hope_ops/releases/v3_0_paper_finality/finality.py | 19 |
| V4 source tag | hope_ops/releases/v4_0_result_bridge/bridge.py | 19 |
| V3 pool split | hope_ops/releases/v3_0_paper_finality/finality.py | 252-283 (esp. 258, 282, 283) |
| V4 pool split | hope_ops/releases/v4_0_result_bridge/bridge.py | 421-444 (esp. 425-431, 436-441, 444) |
| UI settlements endpoint (mixed) | hope_ui/v0_1/hope_server.py | 149-169 (esp. 156-162, 168-169) |
| UI paper/portfolio rows (no source) | hope_ui/v0_1/hope_server.py | 117-134 |
| UI perf panel (single mixed) | hope_ui/v0_1/app.js | 22 |
| UI settlements render (mixed) | hope_ui/v0_1/app.js | 23 |
| UI paper/portfolio tables (no source) | hope_ui/v0_1/app.js | 19-20 |
| UI settlements title | hope_ui/v0_1/app.js | 2 |
