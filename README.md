# ICT SMT Live Agent — MNQ + MES

Real-time automated trading alert system monitoring MNQ (Micro E-mini Nasdaq-100) and MES (Micro E-mini S&P 500) micro futures for ICT (Inner Circle Trading) signals.

## Signals Detected

- **Regular SMT Divergence** — when MNQ sweeps an external level (wick) while MES holds (or vice versa), indicating a potential reversal
- **Hidden SMT Divergence** — same as regular but uses candle bodies instead of wicks; indicates institutional absorption
- **Fill SMT** — one instrument enters or passes through a Fair Value Gap while the other does not
- **Fair Value Gaps (FVG)** — active unfilled gaps; alerts when price enters a gap zone
- **External Liquidity Levels** — PDH/PDL, PWH/PWL, HOD/LOD, TDO, TWO, NYO, and per-quarter H/L per instrument

## Requirements

```bash
pip install yfinance pandas requests colorama schedule flask
```

Python 3.9+ required (uses `zoneinfo`).

## Usage

### Terminal Agent

```bash
# Live mode — scans every 60 seconds
python ict_smt_agent.py

# Date mode — replay Q3 (13:00–19:00) for a specific date
python ict_smt_agent.py --date 2026-03-31

# Simulate — single snapshot at an explicit Israel time
python ict_smt_agent.py --simulate "2026-03-31 15:30"

# Date/simulate mode with Telegram alerts (prefixed [TEST])
python ict_smt_agent.py --date 2026-03-31 --notify
```

Stop with `Ctrl+C`.

### Web Dashboard

```bash
python web_app.py
# Open http://127.0.0.1:8080
```

Run alongside the terminal agent for alerts:
```bash
python ict_smt_agent.py   # terminal — Telegram alerts
python web_app.py         # web UI — Plotly charts + tables
```

## Operating Modes

| Mode | Flag | Cutoff | Telegram |
|---|---|---|---|
| Live | _(none)_ | real-time | enabled |
| Date | `--date YYYY-MM-DD` | replays 13:00–18:00 IL | suppressed |
| Simulate | `--simulate "YYYY-MM-DD HH:MM"` | given datetime (IL) | suppressed |
| `--notify` | added to date/simulate | — | sends `[TEST]` alerts |

**Typical workflow:** run `--date` at ~15:30 Israel time to review the Q3 setup before the NY Open (16:30 IL / 09:30 ET).

## Quarter System (Israel Time)

The trading day is split into 4 quarters. All times are Israel time:

| Quarter | Hours | Session | AMDX color |
|---|---|---|---|
| Q1 | 01:00–07:00 | Overnight / Asia | Gray |
| Q2 | 07:00–13:00 | London | Red |
| Q3 | 13:00–19:00 | NY Open (16:30 IL = 09:30 ET) | Green |
| Q4 | 19:00–00:00 | NY afternoon / close | Blue |

Gap: **00:00–01:00** — no quarter defined.

Each scan shows:
- A `TODAY'S QUARTERS` table with H/L for each completed or in-progress quarter
- Quarter H/L levels (`Q1H`, `Q1L` … `Q4H`, `Q4L`) sorted by price alongside other levels
- Current quarter, its time range, and time remaining
- SMT section labelled with the quarter-end target time (e.g. "targets valid till 19:00")

## Level Abbreviations

| Level | Description |
|---|---|
| PDH / PDL | Previous Day High / Low |
| HOD / LOD | High of Day / Low of Day (so far) |
| TDO | True Day Open — open of first candle at Q2 start (07:00 IL / London open) |
| TWO | True Week Open — open of first candle of Tuesday |
| PWH / PWL | Previous Week High / Low |
| NYO | NY Open (09:30 ET = 16:30 IL) |
| Q1H/L – Q4H/L | Quarter High / Low |

> **ICT bias rules:** price above TDO/TWO = Premium zone → favor SHORT. Price below = Discount zone → favor LONG.

## Bias Summary

Each scan prints a bias summary showing:
- Whether price is **above or below TDO** (daily bias) and **above or below TWO** (weekly bias) for both MNQ and MES
- An overall directional bias when both instruments agree

## Recommendation Engine

Each scan computes a trade recommendation combining four factors:

| Factor | Weight (default) | Description |
|---|---|---|
| Liquidity | 25% | External level sweeps + FVG interaction |
| SMT | 25% | Regular (×1.0) + Hidden (×0.7) + Fill (×0.5) signals; decayed over 30 min |
| TDO/TWO | 25% | Premium/discount bias from True Day Open and True Week Open |
| Quarter | multiplier | Q3=×1.0, Q2=×0.8, Q4=×0.6, Q1=×0.4; bonus near NY Open or quarter boundaries |

Each factor scores −1.0 to +1.0. The quarter factor is a **multiplier** (0.4–1.3) applied to the weighted average of the other three.

Output:
- **LONG / SHORT / WAIT** — direction or no-trade
- **STRONG** (|score| ≥ 0.75) or **MODERATE** (|score| ≥ 0.50)
- Score breakdown and reasons list shown in terminal and web dashboard

Weights can be tuned in `ict_smt_agent.py` → `WEIGHTS` dict. Telegram alerts include the recommendation when an SMT signal fires.

## Configuration

Edit the constants at the top of `ict_smt_agent.py`:

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `""` | Telegram bot token (leave empty to skip) |
| `TELEGRAM_CHAT_ID` | `""` | Telegram chat/user ID |
| `SCAN_INTERVAL_SECONDS` | `60` | Scan frequency in seconds |
| `TIMEFRAME` | `"15m"` | Candle interval: `1m`, `5m`, `15m`, `30m`, `1h` |
| `LOOKBACK_DAYS` | `2` | Days of historical data to fetch |
| `SMT_LOOKBACK_CANDLES` | `10` | Rolling window for SMT divergence detection |
| `DATA_SOURCE` | `"yfinance"` | Data source: `yfinance` or `twelvedata` |
| `TWELVEDATA_API_KEY` | `""` | Twelve Data API key (free plan: 800 credits/day) |
| `WEIGHTS` | all `0.25` | Recommendation engine factor weights |
| `RECOMMEND_STRONG` | `0.75` | Score threshold for STRONG signal |
| `RECOMMEND_MODERATE` | `0.50` | Score threshold for MODERATE signal |

## Data Sources

| Source | Key required | CME Futures | Notes |
|---|---|---|---|
| `yfinance` | No | ✅ | Free; 15-min delay on intraday data |
| `twelvedata` | Yes | ✅ | 800 credits/day free; 9 credits/scan; 5-min auto-refresh |

## Data Availability (yfinance limits)

| Timeframe | Max lookback | Notes |
|---|---|---|
| `1m` | 7 days | |
| `5m`, `15m`, `30m` | 60 days | default config |
| `1h` | 730 days (~2 years) | |
| `1d` and above | unlimited | |

When using `--date` or `--simulate`, the agent **automatically selects the finest available timeframe** for the requested date — no manual config change needed. Example: `--date` 90 days ago with `TIMEFRAME="15m"` will auto-switch to `1h`. The chosen interval is shown in the startup banner.

## Telegram Alerts

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to receive alerts for:
- SMT divergence signals (with nearest liquidity target, current quarter, and recommendation)
- Price entering an active FVG zone

Alerts are **not sent** in date or simulate mode (unless `--notify` is passed).

## How It Works

1. Fetches OHLCV data for `MNQ=F` and `MES=F` via yfinance or Twelve Data
2. Filters data to the reference cutoff time (live = now; date mode = replays Q3 hourly)
3. Computes external liquidity levels: daily, weekly, NY Open, and per-quarter H/L
4. Detects active Fair Value Gaps
5. Detects Regular SMT, Hidden SMT, and Fill SMT divergences
6. Scores each factor (liquidity, SMT, TDO/TWO bias) and computes a weighted recommendation
7. Prints a color-coded dashboard — including the recommendation — and sends Telegram alerts for new signals (live only)
8. Deduplicates alerts — same SMT signal won't re-alert within the same candle timestamp; FVG alerts fire once per gap entry

All times displayed in **Israel time** (`Asia/Jerusalem`). No data is persisted — state resets on restart.

## Signal Logic

**Bullish Regular SMT:** MNQ makes a new low below the lookback window (wick) while MES holds above its equivalent low → potential long setup. Also fires if MES sweeps low while MNQ holds.

**Bearish Regular SMT:** MNQ makes a new high above the lookback window (wick) while MES fails to reach its equivalent high → potential short setup. Also fires if MES sweeps high while MNQ fails.

**Hidden SMT:** Same logic but uses candle bodies (open/close range) rather than wicks. Indicates institutional absorption — body didn't confirm the wick sweep.

**Fill SMT:** One instrument enters or passes through an active Fair Value Gap while the other does not — structural divergence in how each instrument relates to its own gap.

**FVG (Bullish):** Three-candle pattern where `candle3.low > candle1.high` — gap between those two levels.

**FVG (Bearish):** Three-candle pattern where `candle3.high < candle1.low` — gap between those two levels.

## Web Dashboard Features

- Side-by-side Plotly.js candlestick charts (MNQ | MES)
- Quarter shading with AMDX colors (A=Gray, M=Red, D=Green, X=Blue)
- Level lines color-coded by type with right-anchored labels (always visible when zoomed)
- FVG zones as semi-transparent rectangles
- SMT signal annotations on chart
- **Recommendation panel** — action badge (LONG/SHORT/WAIT), strength, score, factor breakdown bars, reasons list
- Locked chart sync — zoom/pan mirrors between both charts proportionally
- Zoom preserved across live auto-refreshes
- Scale selector: 15m / 1H / 4H / 1D
- Data source toggle: YFinance / 12Data
- Twelve Data credit counter in status bar (day_used / 800 · scans remaining)
- Tables: Quarters H/L, Levels with SMT markers, SMT signals, Active FVGs
