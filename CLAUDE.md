# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Real-time automated trading alert system monitoring MNQ (Micro E-mini Nasdaq-100) and MES (Micro E-mini S&P 500) micro futures for ICT (Inner Circle Trading) signals: SMT divergences, Fair Value Gaps (FVG), and external liquidity levels.

## Running the Agent

```bash
pip install yfinance pandas requests colorama schedule
python ict_smt_agent.py                        # live mode
python ict_smt_agent.py --date 2026-03-31      # date mode: 2 days, cutoff 15:00 IL
python ict_smt_agent.py --simulate "2026-03-31 15:30"  # legacy simulate
```

Stop with Ctrl+C. No build step or tests exist.

## Operating Modes

**Live mode** (default): scans every `SCAN_INTERVAL_SECONDS` with real-time data.

**Date mode** (`--date YYYY-MM-DD`): fetches 2 days of historical data including the given date, cuts off at **15:00 Israel time** on that date, runs a single analysis. Telegram alerts are suppressed. Designed for reviewing the Q3 setup before the NY Open.

**Simulate mode** (`--simulate "YYYY-MM-DD HH:MM"`): same as date mode but with an explicit cutoff time (Israel time).

## Quarter System (Israel Time)

The trading day is divided into 4 quarters. All times are Israel time:

| Quarter | Hours | Notes |
|---|---|---|
| Q1 | 01:00–07:00 | Overnight / Asia |
| Q2 | 07:00–13:00 | London session |
| Q3 | 13:00–19:00 | NY Open (16:30 IL = 09:30 ET) is here |
| Q4 | 19:00–00:00 | NY afternoon / close |

Gap: **00:00–01:00** — no quarters defined.

**Typical usage:** view the agent at ~15:30 IL (Q3). Date mode cuts data at 15:00 IL so all signals and levels reflect what was visible before the NY Open. Signals shown in Q3 are framed as targets valid until 19:00 IL.

Each scan displays:
- A `TODAY'S QUARTERS` table with H/L per completed or in-progress quarter
- Quarter H/L levels (`Q1H`, `Q1L`, … `Q4H`, `Q4L`) in the sorted levels list
- Current quarter, its time range, and minutes remaining
- SMT section labelled with the quarter-end target time

## Configuration (top of `ict_smt_agent.py`)

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | `""` | Optional Telegram alerts |
| `SCAN_INTERVAL_SECONDS` | `60` | Scan frequency |
| `TIMEFRAME` | `"15m"` | Candle interval (`1m`, `5m`, `15m`, `30m`, `1h`) |
| `LOOKBACK_DAYS` | `2` | Historical data window |
| `SMT_LOOKBACK_CANDLES` | `10` | Rolling window for divergence detection |
| `SMT_TOLERANCE_PCT` | `0.0015` | Sweep detection tolerance (0.15%) |

## Architecture

Single file (`ict_smt_agent.py`, ~600 lines), no separate modules:

**Data pipeline:**
1. `fetch_data()` — downloads OHLCV via yfinance, converts UTC → Israel time
2. `get_external_levels(df, ref_time)` — computes PDH/PDL, TDO, TWO, HOD/LOD, PWH/PWL, NYO, and Q1–Q4 H/L for each index; `ref_time` controls the cutoff (defaults to `datetime.now()`)
   - **TDO** = Today's Open (daily open of the current day)
   - **TWO** = This Week's Open (weekly open of the current week)
3. `detect_fvg()` — finds active (unfilled) Fair Value Gaps: bullish when `c3.low > c1.high`, bearish when `c3.high < c1.low`
4. `detect_smt()` — core signal: compares MNQ vs MES against external levels; bullish when MNQ sweeps below a level while MES holds, bearish for the inverse
5. `run_scan(sim_time, date_mode)` — orchestrates the full analysis loop, deduplicates alerts, outputs to terminal + optional Telegram; Telegram is suppressed in date mode

**Quarter helpers:**
- `get_quarter(dt)` — returns `(q_num, start_h, end_h)` for the given Israel-time datetime, or `None` if in the 00:00–01:00 gap
- `quarter_range_str(s, e)` — formats a quarter range as `"HH:00–HH:00"`
- `quarter_end_dt(ref_dt, end_h)` — returns a timezone-aware datetime for the end of a quarter

**State (module-level dicts):**
- `last_smt_signal` — deduplicates SMT alerts within 30 minutes
- `last_fvg_alert` — tracks alerted FVGs by gap ID to suppress repeats

**Key design choices:**
- Single-threaded; network delays block the schedule loop
- No persistence — all state resets on restart
- yfinance is the sole data source (no API key, no fallback)
- Mixed Hebrew/English output in terminal and Telegram messages
- Timezones: `Asia/Jerusalem` for display, `America/New_York` for NYO reference
