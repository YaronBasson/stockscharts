# ICT SMT Agent — Session Notes

---

## SESSION 1 — Completed: 2026-04-03

### Overview
Major refactor of `ict_smt_agent.py` covering quarter-based analysis, dual operating
modes, side-by-side display, and SMT detection fixes.

### Changes Made

#### 1. Quarter System
The trading day is split into 4 quarters (Israel time). No trading defined for 00:00–01:00.

| Quarter | Hours | Session context |
|---|---|---|
| Q1 | 01:00–07:00 | Overnight / Asia |
| Q2 | 07:00–13:00 | London |
| Q3 | 13:00–19:00 | NY Open at 16:30 IL (09:30 ET) |
| Q4 | 19:00–00:00 | NY afternoon / close |

- Every scan shows the current quarter, its time range, and minutes remaining
- Quarter H/L levels (`Q1H`, `Q1L` … `Q4H`, `Q4L`) computed and shown in levels display
- SMT section labelled with the quarter-end target time (e.g. "targets valid till 19:00")

#### 2. Operating Modes

**Live mode** (`python ict_smt_agent.py`):
- Scans every `SCAN_INTERVAL_SECONDS` in real time
- If started during Q3 (13:00–19:00), first replays completed Q3 hours before going live
- Screen clears between live scans (dashboard style)

**Date mode** (`--date YYYY-MM-DD`):
- Replays all 6 Q3 hourly checkpoints: 13:00 → 14:00 → 15:00 → 16:00 → 17:00 → 18:00
- 3-second pause between checkpoints
- Telegram alerts suppressed by default (use `--notify` to enable `[TEST]` alerts)

**Simulate mode** (`--simulate "YYYY-MM-DD HH:MM"`):
- Single snapshot at the given Israel time

**`--notify` flag**: Sends Telegram alerts prefixed with `[TEST]` in date/simulate mode

#### 3. Auto Timeframe Selection

| Lookback | Auto-selected interval |
|---|---|
| Within configured TIMEFRAME limit | configured TIMEFRAME |
| ≤ 60 days | `15m` |
| ≤ 730 days (~2 years) | `1h` |
| > 730 days | `1d` |

#### 4. SMT Detection Fix
**Bug:** `SMT_TOLERANCE_PCT = 0.0015` caused tiny sweeps (e.g. 0.5 pt) to be missed.
**Fix:** Removed tolerance. Uses strict `<` / `>=` comparison. All 4 symmetric cases covered:

| Case | Sweeper | Holder | Signal |
|---|---|---|---|
| Low sweep | MNQ new low | MES holds | LONG |
| Low sweep | MES new low | MNQ holds | LONG |
| High sweep | MNQ new high | MES holds | SHORT |
| High sweep | MES new high | MNQ holds | SHORT |

#### 5. Side-by-Side Display (`print_side_by_side`)
MNQ and MES shown side by side. `◄ SMT` marker on levels where divergence exists.
**Key rule:** Pad plain strings first (`:<W`), apply ANSI color after — mixing breaks alignment.

#### 6. Level Abbreviations
PDH/PDL, HOD/LOD, TDO, PWH/PWL, NYO, Q1H/L–Q4H/L

#### 7. Fair Value Gap (FVG)
- Bullish ▲: `c3.low > c1.high` — gap = `[c1.high, c3.low]`
- Bearish ▼: `c3.high < c1.low` — gap = `[c3.high, c1.low]`
- Active = not yet filled. `◄ IN` when current price is inside.

---

## SESSION 2 — Completed: 2026-04-03

### Overview
Built a parallel Flask web application (`web_app.py` + `templates/index.html`) providing
a graphical dashboard for the same ICT SMT analysis engine.

---

### Phase 1 — Flask Backend (`web_app.py`)

**File created.** Reuses all logic from `ict_smt_agent.py` via imports — no duplicated logic.

Routes:
- `GET /` — serves the dashboard HTML
- `GET /api/scan?date=YYYY-MM-DD&hour=HH&tf=TF` — returns JSON

JSON response fields:
```
ref_time, last_candle, timeframe, is_historical, quarter, quarters_today,
mnq { ticker, current, candles, levels, fvgs },
mes { ticker, current, candles, levels, fvgs },
smt_signals [ { type, direction, time, detail, mnq_val, mes_val, ref_mnq, ref_mes } ]
```

Serialization helpers: `df_to_candles()`, `fvg_to_dict()`, `smt_to_dict()`

**Timeframe selector (Phase 1b):**
- Accepted `?tf=` param: `15m`, `1h`, `4h`, `1d`
- `4h` not native in yfinance — fetched as `1h` and resampled via `resample_4h()`
- If chosen tf can't reach the requested historical date, auto-falls back silently

**Quarter H/L fix:**
- Changed `actual_end` from `min(q_end, ref_time + 15min)` to simply `q_end`
- The df only contains data up to ref_time, so no over-fetching occurs
- Used `<=` instead of `<` to include boundary candles

**Data freshness fix:**
- Live mode: `fetch_end=None` so `fetch_data` uses `datetime.now()` (avoids tz-stripping bug)
- Changed from `yf.download()` to `yf.Ticker.history()` to bypass module-level cache
- End window pushed `+1 day` (yfinance end is exclusive — without this the current candle is clipped)
- Note: yfinance free tier has inherent **15-minute delay** on intraday data — unavoidable

**Staleness indicator:**
- `last_candle` returned in API response
- Status bar shows "Last candle: HH:MM IL (Xm ago)" — turns red if > 30 minutes old

---

### Phase 2 — Frontend (`templates/index.html`)

Single-page Bootstrap 5 dark-theme dashboard with Plotly.js charts.

#### Charts
- Two side-by-side Plotly.js **candlestick charts** (MNQ | MES)
- Green candles = up, red = down
- `scrollZoom:true`, modebar with pan/zoom/reset
- Right margin `r:90` to accommodate level labels

#### Quarter Shading
- AMDX model colors:
  - Q1 A — Light gray `#b0b0b0`
  - Q2 M — Light red `#ef9a9a`
  - Q3 D — Light green `#a5d6a7`
  - Q4 X — Light blue `#90caf9`
- Background fill + colored left boundary line per quarter, per day
- Labels `Q1 A`, `Q2 M`, `Q3 D`, `Q4 X` shown in chart annotation area
- **Timezone fix:** shape timestamps include `+03:00` offset extracted from candle data —
  without this, Plotly reads bare timestamps as UTC, shifting boxes 3 hours right

#### X-axis Ticks
- Custom `tickvals` at quarter boundaries: 01:00, 07:00, 13:00, 19:00 for each day
- Labels show `Q1\n01:00`, `Q2\n07:00`, etc. — date (MM/DD) shown on first tick of each day
- Replaces Plotly's default 6-hour grid (00:00, 06:00, 12:00, 18:00)

#### Level Lines
- Dashed horizontal lines color-coded by type:
  - PDH/PDL → orange `#f0883e`
  - PWH/PWL → purple `#bc8cff`
  - HOD/LOD → amber `#ffa657`
  - TDO → gray `#8b949e`
  - NYO → blue `#58a6ff`
  - QxH/L → green `#55d980`
- Labels anchored at `xref:'paper', x:1.01` (right edge) — always visible when zoomed

#### FVG Zones
- Semi-transparent rectangles: green (bullish) / red (bearish), full chart width

#### SMT Signal Annotations
- ▲/▼ arrows on chart at signal timestamp

#### Zoom Preservation on Refresh
- Before each `Plotly.react()` call, current x/y ranges are saved via `savedZoom()`
- Restored after the update via `Plotly.relayout()` with `isSyncing=true` to suppress sync ping-pong
- **User's zoom/pan position is preserved across live auto-refreshes**

#### Chart Locking (Sync)
- **Lock button** (🔒 Locked / 🔓 Unlocked) — default: locked
- When locked, any zoom/pan on either chart mirrors to the other:
  - X-axis: exact same time window
  - Y-axis: proportional to price ratio (`mesCurrent / mnqCurrent`) so visual % amplitude matches
- Listeners attached once per div (guard `listenersAttached{}`) — prevents stacking on re-renders
- `isSyncing` flag prevents infinite relay loop between charts

#### Controls
| Control | Function |
|---|---|
| Live / Date mode toggle | Switch between real-time and historical |
| Date picker | Select historical date (date mode) |
| Hour buttons 13–18 | Q3 checkpoints (date mode) |
| Scale: 15m / 1H / 4H / 1D | Change candlestick timeframe; triggers reload |
| 🔒 Lock button | Sync both charts zoom/pan |
| ↺ Refresh | Manual refresh |

#### Tables
- **Quarters table**: Q1–Q4 with MNQ H/L and MES H/L, colored by AMDX
- **Levels table**: all levels for both instruments, `◄ SMT` marker where divergence exists
- **SMT signals table**: direction (▲ LONG / ▼ SHORT), time, detail
- **Active FVGs table**: instrument, type, bottom, top, time

#### Status Bar
- API status (OK / error)
- Last candle: time + age (red if stale > 30 min)
- Countdown to next live refresh

#### Auto-refresh (Live Mode)
- Polls `/api/scan` every 60 seconds
- Countdown shown in status bar
- View preserved across refreshes (zoom, pan)

---

### Files Status

| File | Status |
|---|---|
| `ict_smt_agent.py` | Main agent — refactored; `fetch_data` updated to use `Ticker.history()` |
| `web_app.py` | Flask backend — complete |
| `templates/index.html` | Web dashboard — complete |
| `simulate.py` | Legacy — broken (pending deletion) |
| `CLAUDE.md` | Updated with quarter system and architecture |
| `README.md` | Updated with data availability table and operating modes |
| `SESSION_NOTES.md` | This file |

---

### Known Issues / Pending

- `simulate.py` still exists — broken, should be deleted
- yfinance free tier: 15-minute inherent delay on intraday data (cannot be bypassed)
- FVG detection uses the full lookback window — very old gaps may appear if LOOKBACK_DAYS is large
- Live mode Q3 replay only covers completed full hours (e.g. at 15:45 it replays 13:00 and 14:00, not 15:00)
- Web app does not send Telegram alerts — run terminal agent in parallel for alerts

### Running the Web App

```bash
pip install flask
python web_app.py
# Open http://127.0.0.1:8080
```

Run alongside the terminal agent for alerts:
```bash
python ict_smt_agent.py   # terminal — alerts via Telegram
python web_app.py         # web UI — visualization only
```

---

## SESSION 3 — Completed: 2026-04-03

### Overview
Three additions: completed the Twelve Data credit counter UI, added TWO (This Week's Open) as a new level throughout the stack, and fixed a Twelve Data symbol resolution error.

---

### 1. Twelve Data Credit Counter (UI completion)

**Context:** Backend credit tracking (`_td_record_scan`, `_td_stats`) was already in `web_app.py`. The UI element and render logic were missing.

**Changes — `templates/index.html`:**
- Added `<span id="sb-td"></span>` to the status bar HTML (between `sb-candle` and `sb-next`)
- Added credit counter logic in `render()`:
  - Displays: `12Data: X/800 credits · N scans left`
  - Color: gray → amber (`#ffa657`) at >50% usage → red (`#f85149`) at >80%
  - Clears the element when source is not `twelvedata`

---

### 2. TWO — This Week's Open

New level added across the full stack alongside the existing TDO (Today's Open).

**Definition:** TWO = the open price of the first candle of the current trading week (from weekly resample).

#### `ict_smt_agent.py` — `get_external_levels()`
- Extended weekly resample to include `open=("open", "first")`
- Added `levels["TWO"] = round(float(weekly["open"].iloc[-1]), 2)` (requires `len(weekly) >= 1`)

#### `ict_smt_agent.py` — Bias / Checks section
- Added `mnq_two` / `mes_two` lookups alongside `mnq_tdo` / `mes_tdo`
- Per-instrument print now shows both `above/below TDO ↑↓` and `above/below TWO ↑↓` on one line
- Added a weekly bias line: `(weekly BULLISH — both above TWO)` / `(weekly BEARISH — both below TWO)` appended to the overall bias output

#### `ict_smt_agent.py` — Legend
- Added `TWO  This Week Open` to the printed legend line alongside TDO

#### `templates/index.html`
- CSS: `.lbl-TWO { color: #26c6da; }` (cyan, distinct from TDO gray `#8b949e`)
- `LEVEL_COLOR`: `TWO: '#26c6da'` — renders as a cyan dashed horizontal line on both charts
- Level table and chart annotations pick up TWO automatically (no additional wiring needed)

#### `CLAUDE.md`
- Updated `get_external_levels` description to include TWO with definitions for both TDO and TWO

---

### 3. Twelve Data Symbol Error Fix

**Error seen:** `ValueError: symbol or figi parameter is missing or invalid` — API returning status=error, falling back to yfinance.

**Root cause:** Without a `type` parameter, the Twelve Data API may fail to unambiguously resolve `MNQ`/`MES` as futures contracts (vs. stocks/ETFs with similar tickers).

**Fix — `ict_smt_agent.py` — `fetch_data_twelvedata()`:**
- Added `"type": "futures"` to the request params — explicitly scopes symbol lookup to futures instruments
- Improved error message: now includes API error code and the symbol sent, e.g. `[code=400 symbol=MNQ]`, for easier diagnosis on recurrence

---

### Files Status

| File | Status |
|---|---|
| `ict_smt_agent.py` | TWO level added; Twelve Data `type=futures` fix; bias section updated |
| `web_app.py` | Credit counter backend — complete from session 2 |
| `templates/index.html` | Credit counter UI complete; TWO color added |
| `CLAUDE.md` | Updated with TWO definition |
| `SESSION_NOTES.md` | This file |

### Known Issues / Pending

- `simulate.py` still exists — broken, should be deleted
- yfinance free tier: 15-minute inherent delay on intraday data (cannot be bypassed)
- FVG detection uses the full lookback window — very old gaps may appear if LOOKBACK_DAYS is large
- Web app does not send Telegram alerts — run terminal agent in parallel for alerts
- If Twelve Data symbol error recurs even with `type=futures`, it indicates the API key plan does not include futures data

---

## SESSION 4 — Completed: 2026-04-05

### Overview
Four major additions this session:
1. Removed all dead code (TradingView, Polygon integrations)
2. Fixed TDO and TWO definitions to match ICT skill specifications
3. Added Hidden SMT and Fill SMT detection
4. Built the full recommendation engine combining all analysis factors

---

### 1. Dead Code Removal

**Removed from `ict_smt_agent.py`:**
- `fetch_data_tradingview()` — `tvdatafeed` library was removed from PyPI; never functional
- `fetch_data_polygon()` — Polygon free plan does not include CME futures; never functional
- `TV_TICKER_MAP`, `_BARS_PER_DAY`, `POLYGON_TICKER_MAP`, `POLYGON_INTERVAL_MAP` constants
- `TRADINGVIEW_USERNAME`, `TRADINGVIEW_PASSWORD`, `POLYGON_API_KEY` config variables
- `import numpy as np` — was never used after Polygon removal
- `ref_date_midnight` local variable in `run_scan()` — assigned but never read
- `import os as _os` block that was only used by dead TV code
- `DATA_SOURCE` branching guards for TV/Polygon in `fetch_data()`

**Removed from `web_app.py`:**
- TV/Polygon guards in `api_scan()`
- `from datetime import timedelta` — was unused

**Result:** `ict_smt_agent.py` now has exactly 2 data sources: `yfinance` (default) and `twelvedata`.
`DATA_SOURCE` config variable still exists and controls the default.

---

### 2. TDO and TWO Definition Fixes

**Problem:** The code computed TDO as midnight daily open and TWO as Monday's weekly resample open.
These differed from the ICT skill definitions:
- **TDO (True Day Open)** per ICT skill = open of first candle of Q2 (London session = 07:00 IL)
- **TWO (True Week Open)** per ICT skill = open of first candle of Tuesday (Q2 of the week)

**Fixed in `ict_smt_agent.py` → `get_external_levels()`:**

TDO fix:
```python
tdo_start = ref_time.replace(hour=7, minute=0, second=0, microsecond=0)
tdo_candles = df[df.index >= tdo_start]
if not tdo_candles.empty:
    levels["TDO"] = round(float(tdo_candles["open"].iloc[0]), 2)
```

TWO fix:
```python
ref_weekday = ref_time.weekday()
days_since_tuesday = (ref_weekday - 1) % 7
tuesday = (ref_time - timedelta(days=days_since_tuesday)).replace(
    hour=0, minute=0, second=0, microsecond=0)
tue_candles = df[df.index.date == tuesday.date()]
if not tue_candles.empty:
    levels["TWO"] = round(float(tue_candles["open"].iloc[0]), 2)
```

**ICT Bias Rules (implemented):**
- Price above TDO → Premium zone → favor SHORT
- Price below TDO → Discount zone → favor LONG
- Price above TWO → Weekly Premium → favor SHORT all week
- Price below TWO → Weekly Discount → favor LONG all week

---

### 3. Hidden SMT and Fill SMT Detection

Two new signal types added to `ict_smt_agent.py`.

#### `detect_hidden_smt(mnq, mes, lookback=SMT_LOOKBACK_CANDLES)`
Uses candle **bodies** (open/close range) instead of wicks. A hidden divergence is "confirmed" when the wick swept but the body did not — indicating institutional absorption.

Returns list of dicts: `{ type, direction, time, detail, mnq_val, mes_val }`

Same 4 symmetric cases as regular SMT (MNQ sweeps low / MES holds, MES sweeps low / MNQ holds, MNQ sweeps high / MES holds, MES sweeps high / MNQ holds).

#### `detect_fill_smt(mnq, mes, mnq_fvgs, mes_fvgs)`
Detects FVG-interaction divergence — when one instrument enters/fills a Fair Value Gap while the other does not.

- **Type 1:** One instrument's current price is inside an active FVG; other is not
- **Type 2:** One instrument passes through an FVG zone (gap no longer active); other still has it active

Returns list of dicts: `{ subtype, direction, time, detail }`

---

### 4. Recommendation Engine

New configuration constants added to top of `ict_smt_agent.py`:
```python
WEIGHTS = {
    "liquidity": 0.25,
    "smt":       0.25,
    "quarters":  0.25,
    "tdo_two":   0.25,
}
QUARTER_CONFIDENCE = {1: 0.4, 2: 0.8, 3: 1.0, 4: 0.6}
SMT_DECAY_MINUTES  = 30
RECOMMEND_STRONG   = 0.75
RECOMMEND_MODERATE = 0.50
```

#### Scoring Functions (all return -1.0 to +1.0 unless noted)

**`score_liquidity(mnq_df, mnq_levels, mnq_fvgs, mes_df, mes_levels, mes_fvgs)`**
- Checks if price recently swept external liquidity (PDH/PDL/PWH/PWL)
- Checks if price is inside an active FVG
- Both instruments must confirm for full score; single-instrument = half score
- Returns `(float, reasons_list)`

**`score_smt(regular_smts, hidden_smts, fill_smts, ref_time)`**
- Regular SMT: weight 1.0
- Hidden SMT: weight 0.7 (body-based = less decisive)
- Fill SMT: weight 0.5 (FVG-based = contextual)
- Signals older than `SMT_DECAY_MINUTES` are ignored
- Aggregates direction: LONG = +score, SHORT = -score
- Returns `float`

**`score_quarters(ref_time)`** → returns multiplier `0.4–1.3`
- Uses `QUARTER_CONFIDENCE`: Q1=0.4, Q2=0.8, Q3=1.0, Q4=0.6
- Bonus +0.2 for being within 15 min of NY Open (16:30 IL)
- Bonus +0.1 for being within 10 min of any quarter boundary
- Maximum capped at 1.3

**`score_tdo_two(current, tdo, two)`**
- Both below TDO and TWO → +1.0 (LONG — HIGH confidence)
- Both above TDO and TWO → -1.0 (SHORT — HIGH confidence)
- Conflict (one above, one below) → 0.0 (NEUTRAL — LOW confidence)
- Returns `(float, reasons_list)`

#### `compute_recommendation(mnq_df, mes_df, mnq_levels, mes_levels, mnq_fvgs, mes_fvgs, regular_smts, hidden_smts, fill_smts, ref_time)`

```python
raw_score = (
    s_liq    * w["liquidity"] +
    s_smt    * w["smt"] +
    s_tdo_two * w["tdo_two"]
) / (w["liquidity"] + w["smt"] + w["tdo_two"])

final_score = raw_score * q_mult
```

Returns dict:
```python
{
    "action":       "LONG" | "SHORT" | "WAIT",
    "strength":     "STRONG" | "MODERATE" | "",
    "score":        float,       # final_score (-1 to +1)
    "raw_score":    float,       # before quarter multiplier
    "quarter_mult": float,       # 0.4–1.3
    "scores":       {"liquidity": float, "smt": float, "tdo_two": float},
    "weights":      {"liquidity": float, "smt": float, "tdo_two": float},
    "reasons":      [str, ...],  # human-readable bullets
}
```

Score thresholds:
- `|final| >= RECOMMEND_STRONG (0.75)` → STRONG
- `|final| >= RECOMMEND_MODERATE (0.50)` → MODERATE
- below both → WAIT (no action)

#### Terminal Output (in `run_scan()`)
Printed after the SUMMARY & BIAS section:
```
─────────────────────────────────────────────────────────────────
  RECOMMENDATION
─────────────────────────────────────────────────────────────────
  LONG  STRONG  score: +0.823  (raw: +0.632 × 1.30Q)
  factors:  liq +0.75×0.25  smt +0.50×0.25  tdo/two +1.00×0.25
  ✅ MNQ swept below PDL — potential reversal
  ✅ Both below TDO and TWO — LONG bias confirmed
  ...
```

#### Telegram Alert Enhancement
When an SMT signal triggers a Telegram alert, the recommendation is appended:
```
📈 Recommendation: LONG (STRONG)  score: +0.823
```

---

### 5. Web App + Dashboard Updates

#### `web_app.py`
New imports: `detect_hidden_smt`, `detect_fill_smt`, `compute_recommendation`, `WEIGHTS`

New serializers: `hidden_smt_to_dict()`, `fill_smt_to_dict()`

Added to `api_scan()` after existing detections:
```python
hidden_smts = detect_hidden_smt(mnq_df, mes_df)
fill_smts   = detect_fill_smt(mnq_df, mes_df, mnq_fvgs, mes_fvgs)
recommendation = compute_recommendation(
    mnq_df, mes_df, mnq_levels, mes_levels,
    mnq_fvgs, mes_fvgs,
    smt_sigs, hidden_smts, fill_smts, ref_time,
)
```

New JSON fields in response: `hidden_smts`, `fill_smts`, `recommendation`

#### `templates/index.html` — Recommendation Panel
Full-width card added below the data tables. Shown only after first data load.

Layout (3 columns):
- **Left:** Action badge (LONG / SHORT / WAIT) + strength label + final score
- **Center:** 4 factor progress bars:
  - Liquidity score (-1 to +1) → green/red/orange bar
  - SMT score (-1 to +1) → green/red/orange bar
  - TDO/TWO score (-1 to +1) → green/red/orange bar
  - Quarter multiplier (0–1.3) → green/orange bar
  - Each bar shows weight % label (e.g. `(25%)`)
- **Right:** Analysis reasons list with colored bullets (green=bullish, red=bearish)

Quarter info line: `Q3 · ×1.30 multiplier · 42m remaining`

CSS classes: `.rec-action-long/short/wait`, `.rec-strength-strong/moderate`, `.rec-score-bar`, `.rec-score-fill-pos/neg/neu`

JS function: `renderRecommendation(rec, quarter)` — called from `render(data)`.

---

### Files Status

| File | Status |
|---|---|
| `ict_smt_agent.py` | TDO/TWO fixed; hidden/fill SMT added; recommendation engine complete; terminal output updated |
| `web_app.py` | Imports hidden/fill SMT + recommendation; included in JSON response |
| `templates/index.html` | Recommendation panel UI complete |
| `CLAUDE.md` | Updated with TWO definition (session 3) |
| `README.md` | Fully rewritten (session 3) — reflects current 2-source architecture |
| `SESSION_NOTES.md` | This file |

### Known Issues / Pending

- `simulate.py` still exists — broken, should be deleted
- yfinance free tier: 15-minute inherent delay on intraday data (cannot be bypassed)
- FVG detection uses the full lookback window — very old gaps may appear if LOOKBACK_DAYS is large
- Web app does not send Telegram alerts — run terminal agent in parallel for alerts
- Recommendation weights (`WEIGHTS`) are currently equal (0.25 each); user to adjust after testing
- Hidden SMT and Fill SMT signals are not yet shown in the web dashboard SMT table (only included in the recommendation score)

---

## SESSION 5 — Completed: 2026-04-11

### Overview
Major round of improvements spanning ICT accuracy, security hardening, UX polish, and trade planning:
1. Dynamic nearest liquidity (swing high/low) for SMT reference instead of rolling window
2. SMT signal ref labels with timestamps
3. Security fixes: Polygon key to `.env`, removed debug endpoint, Flask debug off
4. Auto-pause 23:00–14:00 IL with manual override
5. Chart/UI overhaul: FVG visualization, chart sync redesign, scroll-zoom → auto-pan
6. Trade plan table with TP levels and configurable % stop loss
7. Telegram alerts now include full trade plan

---

### 1. Dynamic Nearest Liquidity for SMT Detection

**Problem:** `detect_smt()` was comparing the current candle against the rolling min/max of the last N candles. This missed the ICT concept of "nearest liquidity to the left" — the most recent local swing high/low.

**New function: `find_nearest_liquidity(df, swing_strength=SWING_STRENGTH)`**
- Scans df (excluding current candle) from right to left
- Returns `(swing_low_price, swing_low_time, swing_high_price, swing_high_time)` or `None`
- Swing detection: `low[i] == min(low[i-n:i+n+1])` / `high[i] == max(high[i-n:i+n+1])` where n = `SWING_STRENGTH`
- Minimum data: `2 * SWING_STRENGTH + 2` candles

**`detect_smt()` modification:**
- Calls `find_nearest_liquidity(mnq[:-1])` and `find_nearest_liquidity(mes[:-1])` for reference levels
- Falls back to rolling window (`SMT_LOOKBACK_CANDLES`) if no swing found on either side

**New config constants:**
```python
SWING_STRENGTH = 2          # candles each side for swing detection
SWING_LOOKBACK_DAYS = 7     # data window for swing detection
```

---

### 2. SMT Ref Labels with Timestamps

All SMT signal dicts now include `ref_mnq_label` and `ref_mes_label` fields:
- Swing reference: `"swing low @14:15"` / `"swing high @15:30"`
- Rolling fallback: `"rolling low @14:45"` / `"rolling high @15:00"`

Rolling fallback also tracks the timestamp of the min/max candle:
```python
mnq_rl_low_t  = win_mnq["low"].idxmin()
mnq_rl_high_t = win_mnq["high"].idxmax()
```

Dashboard alert box shows: `swing low @14:15: 23941.50` instead of bare `ref: 23941.50`.

Fill SMT signal dicts also extended with: `fvg_instrument`, `fvg_bottom`, `fvg_top`, `fvg_type`, `fvg_start_time`.

---

### 3. Security Fixes

- **Polygon API key:** Was hardcoded in `py.py` (`KEY='...'`). Replaced with `os.getenv("POLYGON_API_KEY", "")`. Key added to `.env`. Old key rotated (was publicly committed on GitHub).
- **`/api/debug-env` endpoint:** Removed entirely from `web_app.py`.
- **Flask debug mode:** Changed `app.run(debug=True)` → `app.run(debug=False)`.

---

### 4. Auto-Pause 23:00–14:00 IL

**`web_app.py`:**
- Constants: `AUTO_PAUSE_START = 23`, `AUTO_PAUSE_END = 14`
- `_auto_paused()` — returns True if current IL time is in the pause window (live mode only)
- `_is_paused()` — `True` if manual pause active OR auto-paused
- `_pause_reason()` — human-readable reason string
- `_pause_manual` flag — for manual override
- `GET /api/scan` guard: returns `{paused: true, reason, resume_at}` when paused
- `POST /api/pause` endpoint: body `{action: "pause"|"resume"|"toggle"|"auto"}`

**`templates/index.html`:**
- `#paused-banner` div shown when paused; `⏸ Pause` / `▶ Resume` button
- `setPausedUI()`, `handlePaused()`, `togglePause()` JS functions
- `loadData()` checks for `data.paused` before rendering

---

### 5. Chart & UI Overhaul

#### FVG Visualization (3 shapes per gap)
- Each active FVG now draws 3 shapes: fill rect + top edge line + bottom edge line
- Makes even 1-point gaps visible via the edge lines
- Changed from `xref:'paper'` (full chart width) to `xref:'x'` with `x0=start_time` (c3 candle) to `x1=lastCandleT`
- `detect_fvg()` now includes `start_time` (c3 timestamp) in each FVG dict

**Fill SMT highlighted FVG:**
- Triggering FVG drawn with brighter fill + dotted border on the relevant instrument's chart
- Uses `layer:'above'` so it overlays other shapes

#### SMT Alert Box Layout Fix
- Alert box was above the `.tbl-card`, hiding table rows beneath
- Moved `#smt-alert-box` inside the card div — alert and table now share one scrollable container
- `max-height` increased from 48% to 62%

#### Chart Sync Redesign
Complete rewrite of chart sync logic to satisfy 5 requirements:
1. Modebar zoom in/out/reset/pan buttons always mirror to the other chart
2. When locked: all drag/scroll movements sync between charts
3. When pan button pressed on one chart → also pressed on other
4. When unlocked: modebar buttons still mirror, but chart movements don't sync
5. Re-locking aligns MES to MNQ's current view

Key implementation:
- `mirrorToOther()` — always called for modebar button events (both locked and unlocked)
- `syncLockedMovement()` — only called for drag/scroll events when locked
- `chartsLocked` flag; lock button re-aligns MES on lock
- Distinguishes modebar events (`'xaxis.range': [array]`) from drag/scroll events (`'xaxis.range[0]'` string keys)

#### Scroll Zoom → Auto Pan Mode
After any scroll zoom, both charts automatically switch to pan (move) mode:
```javascript
function switchToPanMode() {
    setTimeout(() => {
        isSyncing = false;
        Plotly.relayout('mnq-chart', {dragmode: 'pan'});
        Plotly.relayout('mes-chart', {dragmode: 'pan'});
    }, 0);
}
```
Called from 3 places: Y-axis wheel handler, first-zoom recenter, modebar relayout handler.

**Y-axis scroll on right side of chart:**
- Added `wheel` event listener on the chart div for events not captured by Plotly (right side / empty area)
- `if (e.defaultPrevented) return` — skips if Plotly already handled it (left side price labels)

---

### 6. Trade Plan Table

**`web_app.py` — `_build_trade_plan_str(action, mnq_levels, mnq_fvgs, mes_fvgs)`:**
- Builds candidate pool from named MNQ levels + FVG edges (top/bottom)
- Sorts by distance from current price
- Picks TP1–TP4 (4 nearest in trade direction)
- Picks stop = nearest named level on opposite side
- Returns Telegram HTML string:
  ```
  📌 Entry: 23941.50
  🎯 TP1: PDL (23982.00) — +40.50 pts
  🎯 TP2: TDO (24050.00) — +108.50 pts
  🛑 Stop (level): PWL (23890.00) — -51.50 pts
  ⛔ Stop (10%): 21547.35 — -2394.15 pts
  ```

**`templates/index.html`:**
- Replaced targets column with full-width `#rec-trade-plan` table in recommendation panel
- Rows: Entry, TP1–TP4, Stop (level), Stop (%)
- FVG edges shown in blue; named levels in default color
- R/R calculated per TP: `abs(tp - entry) / abs(stop - entry)`

---

### 7. Configurable Stop Loss Percentage

**`ict_smt_agent.py`:**
```python
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "10"))
```

**`web_app.py`:**
- Imported `STOP_LOSS_PCT` from `ict_smt_agent`
- Included in `/api/scan` response as `"stop_loss_pct"`
- `_build_trade_plan_str()` appends `⛔ Stop (X%)` row

**`templates/index.html`:**
- `data_stop_loss_pct = 10` global, set from `data.stop_loss_pct` in `render()`
- Trade plan table: `⛔ Stop (X%)` row showing `pctStopPrice = isLong ? current*(1-pct/100) : current*(1+pct/100)`

**`.env`:** Added `# STOP_LOSS_PCT=10` comment (uncomment to override).

---

### Files Status

| File | Status |
|---|---|
| `ict_smt_agent.py` | `find_nearest_liquidity()` added; `detect_smt()` uses swing levels; `SWING_STRENGTH`, `SWING_LOOKBACK_DAYS`, `STOP_LOSS_PCT` added; FVG `start_time` field added |
| `web_app.py` | Debug endpoint removed; `debug=False`; pause infrastructure; `_build_trade_plan_str()`; trade plan in Telegram alerts; `stop_loss_pct` in response |
| `templates/index.html` | FVG 3-shape visualization; Fill SMT FVG highlight; chart sync redesign; scroll zoom → auto pan; trade plan table; configurable % stop loss |
| `py.py` | Polygon key moved to `.env` via `os.getenv()` |
| `.env` | Added `POLYGON_API_KEY`; added `# STOP_LOSS_PCT=10` comment |
| `CLAUDE.md` | Fully rewritten to reflect current state |
| `SESSION_NOTES.md` | This file |

### Known Issues / Pending

- `simulate.py` still exists — broken, should be deleted
- yfinance free tier: 15-minute inherent delay on intraday data (cannot be bypassed)
- Recommendation weights (`WEIGHTS`) are equal (0.25 each); user to adjust after live testing
- Web app now sends Telegram alerts (via `/api/alert` and `_send_web_smt_alerts()`); terminal agent no longer required in parallel for alerts

---

## SESSION 6 — Completed: 2026-04-11

### Overview
Small UX fixes and Hidden SMT visual improvement.

### 1. Hidden SMT — Diagonal Line Between Bodies

**Problem:** Hidden SMT reference was drawn as a horizontal dashed orange line at `refLevel` from `refTime` to `lastCandleT`. This didn't show the divergence visually.

**Fix (`templates/index.html` — `buildChart()`):**
Replaced horizontal line with a diagonal line connecting:
- Start: `(ref_mnq_time / ref_mes_time, ref_mnq / ref_mes)` — the reference body level on the reference candle
- End: `(latestHidden.time, mnq_val / mes_val)` — the current candle's body level

Result: on MNQ the line goes **down** (body swept below reference), on MES the line stays **flat or goes up** (body held). The two diagonal lines side-by-side show the divergence clearly.

Added small `●` dot annotations at both endpoints; label shows current body value at the right end.

### 2. Date Mode — Charts Not Updating on Date/Hour Change

**Problem:** Two event handlers were missing `loadData()` calls:
1. Hour buttons (13:00–18:00) — updated `selectedHour` but never fetched data
2. Date picker — no `change` listener at all; user had to click "Scan" manually

**Fix (`templates/index.html`):**
- Added `loadData()` at the end of the `.btn-hour` click handler
- Added `document.getElementById('date-input').addEventListener('change', () => { loadData(); })`

Note: step arrows (◄/►) and keyboard ←/→ already called `loadData()` via `stepTime()` — those were not broken.

### Files Status

| File | Status |
|---|---|
| `templates/index.html` | Hidden SMT diagonal line; date picker and hour buttons now auto-load |

---

## SESSION 7 — Completed: 2026-04-11

### Overview
Five fixes and improvements this session:
1. Hidden SMT detection extended to use swing body lookback (was limited to 10-candle rolling window)
2. Chart Y-axis sync bug fix — other chart was resetting on X-only scroll
3. New cross-instrument structural divergence scoring factor (`score_mnq_divergence`)
4. Weekend gap removed from charts using Plotly `rangebreaks`
5. `LOOKBACK_DAYS` now counts real trading days, not calendar days

---

### 1. Hidden SMT — Extended Lookback via Swing Body Levels

**Problem:** `detect_hidden_smt()` used a fixed 10-candle rolling window (`SMT_LOOKBACK_CANDLES`) to find the reference body low/high. At 15m intervals, a reference candle at 01:00 is 44 candles before 12:00 — completely outside the window. Signals were missed.

**Example missed:** May 14 2025 — 01:00 body low was the reference; at 12:00 MES body dropped below it while MNQ held. No signal fired.

**New function: `find_nearest_body_liquidity(df, swing_strength=SWING_STRENGTH)`** (`ict_smt_agent.py`)
- Identical structure to `find_nearest_liquidity()` but uses body extremes: `min/max(open, close)` per candle instead of `low/high`
- Scans right-to-left, returns `(body_swing_low_price, body_swing_low_time, body_swing_high_price, body_swing_high_time)`

**`detect_hidden_smt()` changes:**
- Now mirrors `detect_smt()` architecture: scans last 3 days for nearest swing body high/low via `find_nearest_body_liquidity()`
- Falls back to full history if 3-day window yields `None`
- Falls back to 10-candle rolling window body extremes only as last resort
- Reference timestamps (`ref_mnq_time`, `ref_mes_time`) now correctly point to the swing body candle, so diagonal lines draw from the right origin

---

### 2. Chart Y-Axis Sync Bug Fix

**Problem:** In `syncLockedMovement()`, when doing a pure X scroll/zoom (no Y change in the relayout event), the `else` branch set `yaxis.autorange: true` on the other chart, resetting it to show all data (full zoom-out). Looked like a "reset".

**Fix (`templates/index.html`):**
Removed the `else { update['yaxis.autorange'] = true; }` branch. When `y0` is undefined (pure X movement), the other chart's Y axis is now left completely untouched.

---

### 3. Cross-Instrument Structural Divergence Score

**New function: `score_mnq_divergence(mnq_levels, mes_levels)`** (`ict_smt_agent.py`)

Detects when one instrument has ≥2 structural indicators in one direction that the other instrument does NOT confirm:

| Diverging instrument | Conditions (≥2) | Other holds | Signal | Trade on |
|---|---|---|---|---|
| MNQ | below PDL/PWL/LOD + below TDO | MES < 2 indicators | LONG +1.0 | MES |
| MES | below PDL/PWL/LOD + below TDO | MNQ < 2 indicators | LONG +1.0 | MNQ |
| MES | above PDH/PWH/HOD + above TDO | MNQ < 2 indicators | SHORT -1.0 | MNQ |
| MNQ | above PDH/PWH/HOD + above TDO | MES < 2 indicators | SHORT -1.0 | MES |

Returns `(score: float, reasons: list)`. Reason text explicitly names which instrument to trade (e.g. `"MES divergence SHORT: above PDH + above TDO, MNQ held → sell MNQ"`).

**`WEIGHTS` updated:**
```python
WEIGHTS = {
    "liquidity":      0.25,
    "smt":            0.25,
    "quarters":       0.25,
    "tdo_two":        0.25,
    "mnq_divergence": 0.25,   # new
}
```
Denominator in `compute_recommendation()` is now exactly 1.0 (`w["liquidity"] + w["smt"] + w["tdo_two"] + w["mnq_divergence"]`).

**Terminal output:**
```
factors:  liq +0.00×0.25  smt +1.00×0.25  tdo/two -0.76×0.25  mnq_div +1.00×0.25
```

**Web dashboard:** New "MNQ Div" bar added to recommendation panel (5th factor bar). Quarter × bar moved to its own full-width row below the 2×2 grid.

---

### 4. Weekend Gap Removed from Charts

**Problem:** Plotly `xaxis type:'date'` rendered calendar time including weekends, leaving empty space between Friday's last candle and Monday's first candle.

**Fix (`templates/index.html` — `buildChart()`):**
```javascript
xaxis: {
  ...
  rangebreaks: [
    { bounds: ['sat', 'mon'] },   // skip Sat–Sun gap
  ],
}
```
Plotly hides the Saturday–Sunday range visually while keeping all shape/annotation datetime values intact.

---

### 5. Display Window: Calendar Days → Trading Days

**Problem:** `LOOKBACK_DAYS=3` on a Monday subtracted 3 calendar days, giving Monday + (weekend = no data) + Friday — only 2 real trading days visible.

**Fix (`web_app.py`):**
```python
trading_dates = sorted(mnq_df.index.normalize().unique())
if len(trading_dates) >= LOOKBACK_DAYS:
    display_cutoff = trading_dates[-LOOKBACK_DAYS].to_pydatetime().replace(tzinfo=ISRAEL_TZ)
```
Counts distinct dates that actually have candle data — only real trading days appear. `LOOKBACK_DAYS=3` on Monday now shows Mon + Fri + Thu. Also handles holidays automatically.

---

### Files Status

| File | Status |
|---|---|
| `ict_smt_agent.py` | `find_nearest_body_liquidity()` added; `detect_hidden_smt()` uses swing body lookup; `score_mnq_divergence()` added; `WEIGHTS` updated with `mnq_divergence`; `compute_recommendation()` wired; terminal factors line updated |
| `web_app.py` | Display cutoff now counts trading days instead of calendar days |
| `templates/index.html` | Y-axis sync bug fixed; `rangebreaks` weekend gap removed; MNQ Div factor bar added to recommendation panel |
