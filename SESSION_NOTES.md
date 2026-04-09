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
