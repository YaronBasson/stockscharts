# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Real-time automated trading alert system monitoring MNQ (Micro E-mini Nasdaq-100) and MES (Micro E-mini S&P 500) micro futures for ICT (Inner Circle Trading) signals: SMT divergences, Fair Value Gaps (FVG), and external liquidity levels. Includes a Flask web dashboard (`web_app.py`) with interactive Plotly charts.

## Running

```bash
# Web dashboard (primary interface)
python web_app.py          # opens http://localhost:8080

# CLI agent only
pip install yfinance pandas requests colorama schedule
python ict_smt_agent.py                        # live mode
python ict_smt_agent.py --date 2026-03-31      # date mode: cutoff 15:00 IL
python ict_smt_agent.py --simulate "2026-03-31 15:30"  # explicit cutoff
```

Stop with Ctrl+C. No build step or tests exist.

## Secrets / Environment

All credentials live in `.env` (gitignored). Copy `.env.example` or create manually:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TWELVEDATA_API_KEY=...
POLYGON_API_KEY=...
```

`web_app.py` loads `.env` with a manual `utf-8-sig` parser (handles Windows BOM) before importing `ict_smt_agent`.

## Operating Modes (CLI)

**Live mode** (default): scans every `SCAN_INTERVAL_SECONDS` with real-time data.

**Date mode** (`--date YYYY-MM-DD`): fetches historical data, cuts off at **15:00 Israel time** on that date, runs a single analysis. Telegram alerts are suppressed.

**Simulate mode** (`--simulate "YYYY-MM-DD HH:MM"`): same as date mode but with an explicit cutoff time (Israel time).

## Web Dashboard Features

- Dual Plotly candlestick charts (MNQ + MES) with synchronized zoom/pan
- FVG rectangles drawn from gap formation candle (c3) to last candle — disappear when filled
- SMT alert box: Regular, Hidden, Fill types with ref label (e.g. "swing high @15:30")
- Fill SMT: highlights triggering FVG with bright border + shows gap range in alert box
- Recommendation panel with trade plan table: Entry, TP1–TP4 (named levels + FVG edges), Stop (named level), Stop (configurable % from `.env`)
- Quarters table (Q1–Q4 H/L for today)
- Levels table (PDH/PDL, TDO, TWO, HOD/LOD, Q1–Q4 H/L, NYO)
- Manual Telegram alert button (bypasses dedup, works in historical mode)
- Auto-pause: scanning stops 23:00–14:00 IL (NYSE closed); manual ⏸/▶ button override
- Step buttons (◄/►) to move through historical data by one timeframe interval
- Glossary modal for ICT acronyms
- Chart lock/unlock: locked = all movements sync; unlocked = only modebar buttons sync
- Scroll zoom automatically switches both charts to pan (move) mode

## Quarter System (Israel Time)

| Quarter | Hours | Notes |
|---|---|---|
| Q1 | 01:00–07:00 | Overnight / Asia |
| Q2 | 07:00–13:00 | London session |
| Q3 | 13:00–19:00 | NY Open (16:30 IL = 09:30 ET) is here |
| Q4 | 19:00–00:00 | NY afternoon / close |

Gap: **00:00–01:00** — no quarters defined.

## Configuration (top of `ict_smt_agent.py`)

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | `""` | Read from `.env` |
| `SCAN_INTERVAL_SECONDS` | `60` | Scan frequency |
| `TIMEFRAME` | `"15m"` | Candle interval |
| `LOOKBACK_DAYS` | `3` | Display window (days) |
| `SWING_LOOKBACK_DAYS` | `7` | Data window for swing detection |
| `SMT_LOOKBACK_CANDLES` | `10` | Rolling window fallback for SMT |
| `SMT_TOLERANCE_PCT` | `0.0015` | Sweep tolerance (0.15%) |
| `SWING_STRENGTH` | `2` | Candles each side for swing high/low detection |
| `DATA_SOURCE` | `"yfinance"` | `"yfinance"` or `"twelvedata"` |
| `TWELVEDATA_API_KEY` | `""` | Read from `.env` |
| `STOP_LOSS_PCT` | `10` | Stop loss percentage for trade plan (set in `.env`) |

## Architecture

Two main files + one template:

### `ict_smt_agent.py` (~700+ lines)
**Data pipeline:**
1. `fetch_data()` — downloads OHLCV via yfinance or Twelve Data, converts UTC → Israel time; caches for `CACHE_TTL=55s`
2. `get_external_levels(df, ref_time)` — PDH/PDL, TDO, TWO, HOD/LOD, PWH/PWL, NYO, Q1–Q4 H/L
3. `detect_fvg()` — active (unfilled) Fair Value Gaps; each has `time` (c2) and `start_time` (c3, for chart drawing)
4. `detect_smt()` — Regular SMT: uses `find_nearest_liquidity()` for swing high/low reference (falls back to rolling window); signals include `ref_mnq_label` / `ref_mes_label` (e.g. "swing high @15:30" or "rolling high @14:45")
5. `detect_hidden_smt()` — same but uses candle bodies (open/close), not wicks
6. `detect_fill_smt()` — divergence in how each instrument interacts with its FVG zone
7. `compute_recommendation()` — weighted score → LONG/SHORT/WAIT with strength

**Key helpers:**
- `find_nearest_liquidity(df, swing_strength)` — finds most recent swing high/low to the left of current candle
- `send_telegram()` — returns `(bool, str)` tuple; reads token live from `os.getenv()`
- `get_quarter(dt)`, `quarter_range_str()`, `quarter_end_dt()` — quarter time helpers

**State:** `last_smt_signal`, `last_fvg_alert` — dedup dicts; reset on restart.

### `web_app.py`
- Flask app on `127.0.0.1:8080`, `debug=False`
- `GET /api/scan` — full analysis; returns candles, levels, signals, recommendation, `stop_loss_pct`
  - Pause guard: returns `{paused, reason, resume_at}` during 23:00–14:00 IL in live mode
- `POST /api/pause` — toggle/set pause state; body `{action: "pause"|"resume"|"toggle"|"auto"}`
- `POST /api/alert` — manual Telegram send from `_last_scan_ctx`
- Auto-pause constants: `AUTO_PAUSE_START=23`, `AUTO_PAUSE_END=14`
- Twelve Data credit tracking: `TD_CREDITS_PER_SCAN=9`, `TD_DAILY_LIMIT=800`
- `_build_trade_plan_str(action, mnq_levels, mnq_fvgs, mes_fvgs)` — builds TP1–TP4 candidate pool from named levels + FVG edges, picks stop (nearest named level on opposite side), returns Telegram HTML string including `🛑 Stop (level)` and `⛔ Stop (X%)`

### `templates/index.html`
- Bootstrap 5 + Plotly.js
- Chart sync: `chartsLocked` flag; modebar buttons (zoom/pan/reset) always mirror; drag/scroll only syncs when locked; re-locking aligns MES to MNQ's view
- `switchToPanMode()` — called after scroll zoom to auto-switch both charts to pan mode
- FVG shapes: `xref:'x'`, `x0=start_time`, `x1=lastCandleT` — not full-width
- Hidden SMT: diagonal orange line on both charts connecting reference body level → current body level (shows divergence: one line goes down, other stays flat/up)
- Fill SMT highlighted FVG: brighter fill + dotted border on the triggering instrument's chart
