---
name: ict-liquidity-analysis
description: >
  Use this skill for any task involving ICT-style (Inner Circle Trader) chart analysis on futures instruments, 
  specifically MNQ (Micro E-mini Nasdaq) and MES (Micro E-mini S&P 500). Triggers include: detecting External 
  Liquidity levels (highs/lows of a session/day/week), detecting Internal Liquidity / FVG / GAP (Fair Value Gap 
  across 3 consecutive candles), detecting SMT Divergence (correlation break between MNQ and MES), generating 
  LONG/SHORT signals based on liquidity logic, building dashboards or alerts for these concepts, or any mention 
  of "נזילות", "SMT", "FVG", "גאפ", "נזילות חיצונית", "נזילות פנימית", "שבירת קורלציה", "external liquidity", 
  "internal liquidity", "fair value gap". Always use this skill when the user asks to analyze, detect, or alert 
  on these patterns for MNQ/MES or any correlated futures pair.
---
 
# ICT Liquidity Analysis Skill
## (Based on "המטרה הסודית של האלגוריתם" — ICT Method for MNQ & MES)
 
---
 
## Core Concept: How the Algorithm Moves Price
 
The algorithm's job every day is to **reach the nearest liquidity**, and after reaching it — reverse toward the next nearest liquidity. Price moves between liquidity zones like a pac-man eating dots.
 
Two types of liquidity:
1. **External Liquidity** (נזילות חיצונית) — the highs and lows of a time period
2. **Internal Liquidity / FVG** (נזילות פנימית / גאפ) — gaps inside 3-candle sequences
 
---
 
## Instruments
 
| Instrument | Full Name | Point Value |
|------------|-----------|-------------|
| `MES=F` | Micro E-mini S&P 500 | $5 per point |
| `MNQ=F` | Micro E-mini Nasdaq-100 | $2 per point |
| `ES=F` | E-mini S&P 500 | $50 per point |
| `NQ=F` | E-mini Nasdaq-100 | $20 per point |
 
Always start with micro versions (MES, MNQ). They move identically to ES/NQ but with smaller contract sizes.
 
---
 
## 1. External Liquidity (נזילות חיצונית)
 
### Definition
The **highest high** or **lowest low** that price reached in a given time period.
 
### Common External Liquidity Levels
| Level | Hebrew | Description |
|-------|--------|-------------|
| PDH | גבוה של אתמול | Previous Day High |
| PDL | נמוך של אתמול | Previous Day Low |
| PWH | גבוה של השבוע הקודם | Previous Week High |
| PWL | נמוך של השבוע הקודם | Previous Week Low |
| HOD | גבוה היום | High of Day (so far) |
| LOD | נמוך היום | Low of Day (so far) |
| NYO | פתיחת ניו יורק | New York Open (09:30 ET) price level |
| PMO | פתיחת פרה-מרקט | Pre-Market Open level |
| TDO | פתיחת יום המסחר | Trading Day Open |
 
### Detection Logic (Python)
```python
def get_external_liquidity(df, timeframe='day'):
    """
    df: OHLCV DataFrame with datetime index
    Returns dict of key external liquidity levels
    """
    levels = {}
    
    if timeframe == 'day':
        # Previous day high/low
        daily = df.resample('D').agg({'high': 'max', 'low': 'min', 'open': 'first', 'close': 'last'})
        daily = daily.dropna()
        if len(daily) >= 2:
            levels['PDH'] = daily['high'].iloc[-2]
            levels['PDL'] = daily['low'].iloc[-2]
            levels['HOD'] = daily['high'].iloc[-1]
            levels['LOD'] = daily['low'].iloc[-1]
            levels['TDO'] = daily['open'].iloc[-1]
    
    elif timeframe == 'week':
        weekly = df.resample('W').agg({'high': 'max', 'low': 'min'})
        weekly = weekly.dropna()
        if len(weekly) >= 2:
            levels['PWH'] = weekly['high'].iloc[-2]
            levels['PWL'] = weekly['low'].iloc[-2]
    
    return levels
 
def find_nearest_liquidity(current_price, levels):
    """Find nearest liquidity level above and below current price"""
    above = {k: v for k, v in levels.items() if v > current_price}
    below = {k: v for k, v in levels.items() if v < current_price}
    
    nearest_above = min(above.items(), key=lambda x: x[1] - current_price) if above else None
    nearest_below = max(below.items(), key=lambda x: x[1]) if below else None  # closest below
    
    return nearest_above, nearest_below
```
 
### Key Rule
After price **sweeps** (touches or crosses) an external liquidity level → expect a **reversal** toward the next liquidity in the opposite direction.
 
---
 
## 2. Internal Liquidity / FVG / GAP (נזילות פנימית)
 
### Definition
A gap created **between the wick of candle 1 and the wick of candle 3** in any 3 consecutive candles.
- FVG exists **only when** the wicks of candle 1 and candle 3 do NOT touch each other
- Candle color does NOT matter — only wick positions
 
### Bullish FVG (גאפ בעליה)
3 candles moving up:
- **Gap zone**: between `candle[0].high` (top wick of candle 1) and `candle[2].low` (bottom wick of candle 3)
- Condition: `candle[2].low > candle[0].high`
 
```
Candle 1    Candle 2    Candle 3
   |           |           |
   |          [=]         [=]
  [=]          |           |
   |        ← GAP zone →
             (between high of C1 and low of C3)
```
 
### Bearish FVG (גאפ בירידה)
3 candles moving down:
- **Gap zone**: between `candle[0].low` (bottom wick of candle 1) and `candle[2].high` (top wick of candle 3)
- Condition: `candle[2].high < candle[0].low`
 
### Detection Logic (Python)
```python
def detect_fvg(df):
    """
    Detect all Fair Value Gaps (Internal Liquidity zones) in OHLCV data.
    Uses wicks (high/low), not body (open/close).
    
    Returns list of dicts: {type, top, bottom, time, candle_index, filled}
    """
    fvgs = []
    
    for i in range(1, len(df) - 1):
        c1 = df.iloc[i - 1]  # candle 1
        c3 = df.iloc[i + 1]  # candle 3
        
        # Bullish FVG: low of candle 3 is ABOVE high of candle 1
        if c3['low'] > c1['high']:
            fvgs.append({
                'type': 'bullish',
                'top': c3['low'],       # upper boundary of gap
                'bottom': c1['high'],   # lower boundary of gap
                'time': df.index[i],    # time of middle candle
                'candle_index': i,
                'filled': False
            })
        
        # Bearish FVG: high of candle 3 is BELOW low of candle 1
        elif c3['high'] < c1['low']:
            fvgs.append({
                'type': 'bearish',
                'top': c1['low'],       # upper boundary of gap
                'bottom': c3['high'],   # lower boundary of gap
                'time': df.index[i],
                'candle_index': i,
                'filled': False
            })
    
    return fvgs
 
def check_fvg_filled(fvgs, df):
    """
    Mark FVGs as filled when price returns into the gap zone.
    A FVG is 'filled' when price trades inside [bottom, top] after formation.
    """
    for fvg in fvgs:
        if fvg['filled']:
            continue
        # Check all candles after the FVG formed
        subsequent = df[df.index > fvg['time']]
        for _, candle in subsequent.iterrows():
            if candle['low'] <= fvg['top'] and candle['high'] >= fvg['bottom']:
                fvg['filled'] = True
                break
    return fvgs
 
def get_active_fvgs(df):
    """Return only unfilled (active) FVGs — these are the live liquidity targets"""
    all_fvgs = detect_fvg(df)
    checked = check_fvg_filled(all_fvgs, df)
    return [f for f in checked if not f['filled']]
```
 
### Key Rules
- Price tends to **return to fill FVGs** — treat them as magnets
- An unfilled bullish FVG below current price = **support target**
- An unfilled bearish FVG above current price = **resistance target**
- Once filled → FVG is consumed (no longer relevant)
 
---
 
## 3. SMT Divergence — שבירת קורלציה (Correlation Break)
 
### Definition
MNQ and MES are **highly correlated** (they move together almost always). An SMT occurs when:
- One instrument **sweeps** (crosses) an external liquidity level
- The other instrument **does NOT** reach that same level
 
This divergence = the algorithm is **reversing direction**.
 
### SMT Bullish (לעליות) → Signal: LONG
- MNQ sweeps **below** external liquidity (makes a new low)
- MES does NOT make a new low (holds above its corresponding level)
- → Price is about to go UP → look for LONG entry
 
### SMT Bearish (לירידות) → Signal: SHORT
- MNQ sweeps **above** external liquidity (makes a new high)  
- MES does NOT make a new high (fails to reach its corresponding level)
- → Price is about to go DOWN → look for SHORT entry
 
### Detection Logic (Python)
```python
def detect_smt(mnq_df, mes_df, lookback=5, tolerance=0.001):
    """
    Detect SMT Divergence between MNQ and MES.
    
    lookback: number of recent candles to compare
    tolerance: % tolerance for "just touching" vs "sweeping"
    
    Returns: list of SMT signals {type, time, mnq_level, mes_level, direction}
    """
    signals = []
    
    # Align both dataframes to same timestamps
    common_idx = mnq_df.index.intersection(mes_df.index)
    mnq = mnq_df.loc[common_idx]
    mes = mes_df.loc[common_idx]
    
    for i in range(lookback, len(common_idx)):
        window_mnq = mnq.iloc[i - lookback:i]
        window_mes = mes.iloc[i - lookback:i]
        
        current_time = common_idx[i]
        
        # Reference lows/highs for the lookback window (external liquidity)
        mnq_ref_low = window_mnq['low'].min()
        mes_ref_low = window_mes['low'].min()
        mnq_ref_high = window_mnq['high'].max()
        mes_ref_high = window_mes['high'].max()
        
        current_mnq_low = mnq.iloc[i]['low']
        current_mes_low = mes.iloc[i]['low']
        current_mnq_high = mnq.iloc[i]['high']
        current_mes_high = mes.iloc[i]['high']
        
        # SMT Bullish: MNQ sweeps below its low, MES does NOT
        mnq_swept_low = current_mnq_low < mnq_ref_low * (1 - tolerance)
        mes_held_low = current_mes_low > mes_ref_low * (1 - tolerance)
        
        if mnq_swept_low and mes_held_low:
            signals.append({
                'type': 'bullish_smt',
                'direction': 'LONG',
                'time': current_time,
                'mnq_swept_to': current_mnq_low,
                'mes_held_at': current_mes_low,
                'description': 'MNQ swept external low, MES held → expect reversal UP'
            })
        
        # SMT Bearish: MNQ sweeps above its high, MES does NOT
        mnq_swept_high = current_mnq_high > mnq_ref_high * (1 + tolerance)
        mes_held_high = current_mes_high < mes_ref_high * (1 + tolerance)
        
        if mnq_swept_high and mes_held_high:
            signals.append({
                'type': 'bearish_smt',
                'direction': 'SHORT',
                'time': current_time,
                'mnq_swept_to': current_mnq_high,
                'mes_held_at': current_mes_high,
                'description': 'MNQ swept external high, MES held → expect reversal DOWN'
            })
    
    return signals
```
 
### SMT Notes
- SMT is most reliable at **major external liquidity** (PDH, PDL, PWH, PWL) — not random highs/lows
- Always check SMT on the **same timeframe** for both instruments
- Best timeframes for SMT: 5m, 15m, 30m
- SMT at session opens (NY open 09:30 ET, London open) are highest quality
 
---
 
## 4. Complete Analysis Pipeline
 
```python
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
 
def run_ict_analysis(timeframe='30m', lookback_days=5):
    """
    Full ICT analysis pipeline for MNQ + MES.
    Returns structured analysis report.
    """
    # Fetch data
    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    
    mnq = yf.download('MNQ=F', start=start, end=end, interval=timeframe)
    mes = yf.download('MES=F', start=start, end=end, interval=timeframe)
    
    results = {
        'timestamp': datetime.now().isoformat(),
        'mnq': {},
        'mes': {},
        'smt_signals': [],
        'bias': None
    }
    
    # External Liquidity
    for ticker, df, key in [('MNQ', mnq, 'mnq'), ('MES', mes, 'mes')]:
        levels = get_external_liquidity(df, 'day')
        levels.update(get_external_liquidity(df, 'week'))
        current_price = df['close'].iloc[-1]
        nearest_above, nearest_below = find_nearest_liquidity(current_price, levels)
        
        results[key] = {
            'current_price': current_price,
            'external_levels': levels,
            'nearest_target_above': nearest_above,
            'nearest_target_below': nearest_below,
            'active_fvgs': get_active_fvgs(df)
        }
    
    # SMT Detection
    smt_signals = detect_smt(mnq, mes)
    results['smt_signals'] = smt_signals
    
    # Overall bias from most recent SMT
    if smt_signals:
        latest = smt_signals[-1]
        results['bias'] = latest['direction']
        results['bias_reason'] = latest['description']
    
    return results
 
def format_analysis_report(results):
    """Human-readable report"""
    lines = []
    lines.append(f"=== ICT Analysis Report ===")
    lines.append(f"Time: {results['timestamp']}")
    lines.append(f"Bias: {results.get('bias', 'NEUTRAL')} — {results.get('bias_reason', '')}")
    lines.append("")
    
    for ticker in ['mnq', 'mes']:
        d = results[ticker]
        lines.append(f"--- {ticker.upper()} ---")
        lines.append(f"  Price: {d['current_price']:.2f}")
        lines.append(f"  Nearest target above: {d['nearest_target_above']}")
        lines.append(f"  Nearest target below: {d['nearest_target_below']}")
        lines.append(f"  Active FVGs: {len(d['active_fvgs'])}")
        for fvg in d['active_fvgs'][-3:]:  # show last 3
            lines.append(f"    [{fvg['type'].upper()}] {fvg['bottom']:.2f} – {fvg['top']:.2f} @ {fvg['time']}")
    
    lines.append("")
    lines.append(f"--- SMT Signals ({len(results['smt_signals'])} found) ---")
    for sig in results['smt_signals'][-5:]:
        lines.append(f"  [{sig['direction']}] {sig['time']} — {sig['description']}")
    
    return '\n'.join(lines)
```
 
---
 
## 5. Telegram Alert Integration
 
```python
import requests
 
def send_telegram_alert(bot_token, chat_id, signal):
    """Send ICT signal alert to Telegram"""
    emoji = '🟢' if signal['direction'] == 'LONG' else '🔴'
    
    msg = f"""
{emoji} *ICT Signal — {signal['direction']}*
🕐 {signal['time']}
📊 Type: {signal['type'].upper()}
📈 MNQ: {signal.get('mnq_swept_to', 'N/A'):.2f}
📉 MES: {signal.get('mes_held_at', 'N/A'):.2f}
💡 {signal['description']}
"""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    requests.post(url, json={
        'chat_id': chat_id,
        'text': msg,
        'parse_mode': 'Markdown'
    })
```
 
---
 
## 6. Key Rules Summary
 
| Concept | Hebrew | Rule |
|---------|--------|------|
| External Liquidity | נזילות חיצונית | Algorithm MUST visit nearest level |
| After sweep | אחרי הגעה לנזילות | Expect reversal to next level |
| FVG (bullish) | גאפ עולה | `C3.low > C1.high` → gap is support |
| FVG (bearish) | גאפ יורד | `C3.high < C1.low` → gap is resistance |
| FVG filled | גאפ מולא | Price traded inside zone → consumed |
| SMT bullish | SMT לעליות | MNQ breaks low, MES holds → LONG |
| SMT bearish | SMT לירידות | MNQ breaks high, MES holds → SHORT |
| Color irrelevant | צבע לא משנה | FVG detection uses wicks only |
 
---
 
## 7. Session Times (ET / Israel Time)
 
| Session | ET | Israel (UTC+3) |
|---------|-----|----------------|
| Pre-Market Open | 04:00 | 11:00 |
| NY Open | 09:30 | 16:30 |
| NY Close | 16:00 | 23:00 |
| Overnight session | 18:00–09:30 | 01:00–16:30 |
 
Best SMT windows: **NY Open (09:30–11:00 ET)** and **London Open (03:00–05:00 ET)**
 
---
 
## 8. Reference Files
 
- `references/smt-examples.md` — visual examples of SMT setups
- `references/fvg-edge-cases.md` — edge cases for FVG detection (mixed color candles, doji wicks)
- `references/session-levels.md` — how to calculate NYO, PMO, TDO levels from intraday data
