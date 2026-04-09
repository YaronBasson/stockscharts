"""
╔══════════════════════════════════════════════════════════════════╗
║           ICT SMT LIVE AGENT — MNQ + MES                        ║
║   External Liquidity | FVG Detection | SMT Divergence Alerts    ║
╚══════════════════════════════════════════════════════════════════╝

Install dependencies:
    pip install yfinance pandas requests colorama schedule

Run:
    python ict_smt_agent.py                          # live mode
    python ict_smt_agent.py --date 2026-03-31        # date mode (cutoff 15:00 IL)
    python ict_smt_agent.py --simulate "2026-03-31 15:30"  # legacy simulate
"""

import yfinance as yf
import pandas as pd
import requests
import time
import os
import schedule
import argparse
import html as html_lib
from datetime import datetime, timedelta
from colorama import init, Fore, Back, Style
try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).parent / '.env', override=True)
except ImportError:
    pass  # python-dotenv not installed; rely on environment variables only
from zoneinfo import ZoneInfo

init(autoreset=True)

# ── DataFrame-level cache ──────────────────────────────────────
# Caches (ticker, interval) → (DataFrame, fetch_timestamp).
# Reuses data if last fetch was within CACHE_TTL seconds.
# yfinance 1.x uses curl_cffi internally — do NOT pass a requests
# session or requests_cache session; let yfinance manage its own.
_DF_CACHE: dict = {}
_CACHE_TTL = 55   # seconds — just under the 60 s scan interval

# ─────────────────────────────────────────────
#  CONFIG — edit these
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
TIMEFRAME             = os.getenv("TIMEFRAME", "15m")
LOOKBACK_DAYS         = int(os.getenv("LOOKBACK_DAYS", "3"))
SWING_LOOKBACK_DAYS   = int(os.getenv("SWING_LOOKBACK_DAYS", "7"))
SMT_LOOKBACK_CANDLES  = int(os.getenv("SMT_LOOKBACK_CANDLES", "10"))
SMT_TOLERANCE_PCT     = float(os.getenv("SMT_TOLERANCE_PCT", "0.0015"))
SWING_STRENGTH        = int(os.getenv("SWING_STRENGTH", "2"))

# ── Data source ───────────────────────────────────────────────
# "yfinance"    — free, no key, works on normal trading days
# "twelvedata"  — requires TWELVEDATA_API_KEY; 800 credits/day on free plan
DATA_SOURCE        = os.getenv("DATA_SOURCE", "yfinance")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")

# ── Recommendation engine ─────────────────────────────────────
# Each factor scores -1.0 (SHORT) → 0 (neutral) → +1.0 (LONG).
# Quarters factor acts as a confidence multiplier (0.4–1.3), not a direction.
WEIGHTS = {
    "liquidity": 0.25,
    "smt":       0.25,
    "quarters":  0.25,   # multiplier — user will tune
    "tdo_two":   0.25,
}
QUARTER_CONFIDENCE = {1: 0.4, 2: 0.8, 3: 1.0, 4: 0.6}
SMT_DECAY_MINUTES  = 30    # signals older than this are ignored
RECOMMEND_STRONG   = 0.75  # |score| threshold for STRONG signal
RECOMMEND_MODERATE = 0.50  # |score| threshold for MODERATE signal

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
ET_TZ     = ZoneInfo("America/New_York")

# yfinance max lookback per interval (days)
TIMEFRAME_MAX_DAYS = {
    "1m":  7,
    "2m":  60, "5m":  60, "15m": 60, "30m": 60, "90m": 60,
    "1h":  730, "60m": 730,
    "1d":  9999, "5d": 9999, "1wk": 9999, "1mo": 9999,
}

def auto_timeframe(days_ago: int) -> str:
    """Return the finest yfinance interval that covers days_ago days."""
    if days_ago <= TIMEFRAME_MAX_DAYS.get(TIMEFRAME, 60):
        return TIMEFRAME
    if days_ago <= 60:
        return "15m"
    if days_ago <= 730:
        return "1h"
    return "1d"


# ─────────────────────────────────────────────
#  QUARTER DEFINITIONS  (Israel time)
#  Q1: 01:00–07:00 | Q2: 07:00–13:00
#  Q3: 13:00–19:00 | Q4: 19:00–00:00
#  Gap: 00:00–01:00 (no trading)
# ─────────────────────────────────────────────
QUARTERS = [
    (1,  1,  7),   # Q1: 01:00–07:00
    (2,  7, 13),   # Q2: 07:00–13:00
    (3, 13, 19),   # Q3: 13:00–19:00
    (4, 19, 24),   # Q4: 19:00–00:00
]

# ─────────────────────────────────────────────
#  STATE  (deduplication / cooldown)
# ─────────────────────────────────────────────
last_smt_signal   = {"type": None, "time": None}
last_fvg_alert    = {"mnq": set(), "mes": set()}
scan_count        = 0


# ══════════════════════════════════════════════
#  QUARTER HELPERS
# ══════════════════════════════════════════════

def get_quarter(dt):
    """Return (q_num, start_h, end_h) for dt, or None if outside trading hours (00:00–01:00)."""
    h = dt.hour
    for q_num, s, e in QUARTERS:
        if s <= h < (24 if e == 24 else e):
            return q_num, s, e
    return None


def quarter_range_str(s, e):
    end_str = "00:00" if e == 24 else f"{e:02d}:00"
    return f"{s:02d}:00–{end_str}"


def quarter_end_dt(ref_dt, end_h):
    """Return timezone-aware datetime for end of a quarter on the same date as ref_dt."""
    if end_h == 24:
        return (ref_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(days=1))
    return ref_dt.replace(hour=end_h, minute=0, second=0, microsecond=0)


# ══════════════════════════════════════════════
#  DATA FETCHING
# ══════════════════════════════════════════════


def fetch_data_twelvedata(ticker: str, interval: str, days: int, end_dt=None) -> pd.DataFrame:
    """Fetch OHLCV bars from Twelve Data REST API."""
    api_key = os.environ.get("TWELVEDATA_API_KEY", TWELVEDATA_API_KEY)
    TD_TICKER = {
        "MNQ=F": "MNQ",   # Twelve Data CME futures symbol
        "MES=F": "MES",
    }
    TD_INTERVAL = {
        "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
        "1h": "1h",   "4h": "4h",   "1d":  "1day",
    }
    td_ticker   = TD_TICKER.get(ticker, ticker.replace("=F", ""))
    td_interval = TD_INTERVAL.get(interval, "15min")

    if end_dt:
        end_str   = end_dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        start_str = (end_dt.replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        end_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        start_str = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    resp = requests.get("https://api.twelvedata.com/time_series", params={
        "symbol":     td_ticker,
        "exchange":   "CME",
        "type":       "futures",
        "interval":   td_interval,
        "start_date": start_str,
        "end_date":   end_str,
        "outputsize": 5000,
        "timezone":   "UTC",
        "apikey":     api_key,
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") == "error" or "values" not in data:
        raise ValueError(f"{data.get('message', 'Twelve Data returned no values')} "
                         f"[code={data.get('code','?')} symbol={td_ticker}]")

    df = pd.DataFrame(data["values"])
    df.index = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert(ISRAEL_TZ)
    df.index.name = None
    df = df.rename(columns={"open":"open","high":"high","low":"low","close":"close","volume":"volume"})
    df = df[["open","high","low","close","volume"]].apply(pd.to_numeric, errors="coerce").dropna()
    df = df.sort_index()  # Twelve Data returns newest-first

    if end_dt:
        df = df[df.index <= end_dt]
    return df



def fetch_data(ticker: str, interval: str, days: int, end_dt=None,
               source: str = None) -> pd.DataFrame:
    """Fetch OHLCV data.
    source: 'yfinance' | 'twelvedata'
    Defaults to DATA_SOURCE config. On failure, falls back to yfinance.
    Cache is shared across all sources to avoid redundant API calls.
    """
    src = source or DATA_SOURCE

    # ── Shared df-level cache (live calls only) ───────────────
    # Prevents the second ticker fetch (MES after MNQ) from hitting the
    # API again within the same scan cycle — critical for rate-limited sources.
    cache_key = (ticker, interval, src)
    if end_dt is None:
        cached = _DF_CACHE.get(cache_key)
        if cached and (time.time() - cached[1]) < _CACHE_TTL:
            return cached[0].copy()

    def _store(df):
        if end_dt is None and not df.empty:
            _DF_CACHE[cache_key] = (df.copy(), time.time())
        return df

    if src == "twelvedata":
        try:
            return _store(fetch_data_twelvedata(ticker, interval, days, end_dt))
        except Exception as e:
            print(f"{Fore.YELLOW}[WARN] Twelve Data fetch failed ({type(e).__name__}: {e}) — falling back to yfinance")

    # ── yfinance path ─────────────────────────────────────────
    try:
        if end_dt:
            end   = end_dt.replace(tzinfo=None) + timedelta(days=1)
            start = end_dt.replace(tzinfo=None) - timedelta(days=days)
        else:
            end   = datetime.now() + timedelta(days=1)
            start = datetime.now() - timedelta(days=days)

        # yfinance 1.x uses curl_cffi — do NOT pass a session object
        tk = yf.Ticker(ticker)
        df = tk.history(
            start=start,
            end=end,
            interval=interval,
            auto_adjust=True,
            prepost=False,
        )
        if df.empty:
            return pd.DataFrame()

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)

        if df.index.tzinfo is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(ISRAEL_TZ)

        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        return _store(df)

    except Exception as e:
        print(f"{Fore.RED}[ERROR] fetch_data({ticker}): {type(e).__name__}: {e}")
        return pd.DataFrame()


# ══════════════════════════════════════════════
#  EXTERNAL LIQUIDITY LEVELS
# ══════════════════════════════════════════════

def get_external_levels(df: pd.DataFrame, ref_time=None) -> dict:
    """
    Compute key external liquidity levels:
    PDH, PDL, TDO, HOD, LOD, PWH, PWL, NYO, Q1H/L … Q4H/L, CURRENT
    ref_time: timezone-aware datetime to use as "now" (defaults to current Israel time).
    """
    if df.empty or len(df) < 2:
        return {}

    if ref_time is None:
        ref_time = datetime.now(ISRAEL_TZ)

    # ── Daily levels ──────────────────────────
    daily = df.resample("1D").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low",  "min"),
        close=("close", "last")
    ).dropna()

    levels = {}

    if len(daily) >= 2:
        levels["PDH"] = round(float(daily["high"].iloc[-2]), 2)
        levels["PDL"] = round(float(daily["low"].iloc[-2]),  2)

    if len(daily) >= 1:
        levels["HOD"] = round(float(daily["high"].iloc[-1]), 2)
        levels["LOD"] = round(float(daily["low"].iloc[-1]),  2)

    # TDO = first candle of Q2 (London session = 07:00 IL) per ICT Quarters Theory.
    # This is the algorithm's true daily reference price — not the midnight open.
    tdo_start = ref_time.replace(hour=7, minute=0, second=0, microsecond=0)
    tdo_candles = df[df.index >= tdo_start]
    if not tdo_candles.empty:
        levels["TDO"] = round(float(tdo_candles["open"].iloc[0]), 2)

    # ── Weekly levels ─────────────────────────
    weekly = df.resample("1W").agg(
        high=("high", "max"),
        low=("low",   "min"),
    ).dropna()

    if len(weekly) >= 2:
        levels["PWH"] = round(float(weekly["high"].iloc[-2]), 2)
        levels["PWL"] = round(float(weekly["low"].iloc[-2]),  2)

    # TWO = first candle of Tuesday (Q2 of week) per ICT Quarters Theory.
    # Tuesday is the algorithm's true weekly reference price.
    ref_weekday = ref_time.weekday()   # 0=Mon, 1=Tue, …, 6=Sun
    days_since_tuesday = (ref_weekday - 1) % 7
    tuesday = (ref_time - timedelta(days=days_since_tuesday)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    tue_candles = df[df.index.date == tuesday.date()]
    if not tue_candles.empty:
        levels["TWO"] = round(float(tue_candles["open"].iloc[0]), 2)

    # ── NY Open level (16:30 Israel = 09:30 ET) ──
    nyo_time = ref_time.replace(hour=16, minute=30, second=0, microsecond=0)
    nyo_candle = df[df.index >= nyo_time]
    if not nyo_candle.empty:
        levels["NYO"] = round(float(nyo_candle["open"].iloc[0]), 2)

    # ── Quarter H/L levels (today only, completed or in-progress quarters) ──
    for q_num, s, e in QUARTERS:
        q_start = ref_time.replace(hour=s, minute=0, second=0, microsecond=0)
        q_end   = quarter_end_dt(ref_time, e)

        # Skip quarters that haven't started yet relative to ref_time
        if q_start > ref_time:
            continue

        # For an in-progress quarter, only include candles up to ref_time
        actual_end = min(q_end, ref_time + timedelta(minutes=15))
        q_df = df[(df.index >= q_start) & (df.index < actual_end)]
        if not q_df.empty:
            levels[f"Q{q_num}H"] = round(float(q_df["high"].max()), 2)
            levels[f"Q{q_num}L"] = round(float(q_df["low"].min()),  2)

    # ── Current price ─────────────────────────
    levels["CURRENT"] = round(float(df["close"].iloc[-1]), 2)

    return levels


def nearest_liquidity(price: float, levels: dict) -> tuple:
    """Return (nearest level above, nearest level below) as (name, value) tuples."""
    skip = {"CURRENT"}
    above = {k: v for k, v in levels.items() if v > price and k not in skip}
    below = {k: v for k, v in levels.items() if v < price and k not in skip}

    nearest_above = min(above.items(), key=lambda x: x[1] - price) if above else None
    nearest_below = max(below.items(), key=lambda x: x[1])         if below else None
    return nearest_above, nearest_below


# ══════════════════════════════════════════════
#  FVG DETECTION
# ══════════════════════════════════════════════

def detect_fvg(df: pd.DataFrame) -> list:
    """
    Detect all Fair Value Gaps (Internal Liquidity).
    Bullish FVG : candle3.low  > candle1.high  → gap = [c1.high, c3.low]
    Bearish FVG : candle3.high < candle1.low   → gap = [c3.high, c1.low]
    Returns list of active (unfilled) FVGs.
    """
    if len(df) < 3:
        return []

    fvgs = []
    for i in range(1, len(df) - 1):
        c1 = df.iloc[i - 1]
        c3 = df.iloc[i + 1]
        gap = None

        if float(c3["low"]) > float(c1["high"]):
            gap = {
                "type":   "bullish",
                "bottom": round(float(c1["high"]), 2),
                "top":    round(float(c3["low"]),  2),
                "time":   df.index[i],
                "filled": False
            }
        elif float(c3["high"]) < float(c1["low"]):
            gap = {
                "type":   "bearish",
                "bottom": round(float(c3["high"]), 2),
                "top":    round(float(c1["low"]),  2),
                "time":   df.index[i],
                "filled": False
            }

        if gap:
            for _, candle in df[df.index > df.index[i + 1]].iterrows():
                if float(candle["low"]) <= gap["top"] and float(candle["high"]) >= gap["bottom"]:
                    gap["filled"] = True
                    break
            fvgs.append(gap)

    return [f for f in fvgs if not f["filled"]]


# ══════════════════════════════════════════════
#  SMT DIVERGENCE
# ══════════════════════════════════════════════

def find_nearest_liquidity(df: pd.DataFrame, swing_strength: int = SWING_STRENGTH):
    """
    Find the most recent swing low and swing high to the left of the current candle.
    A swing low at index i: low[i] is the minimum of low[i-n .. i+n] (n = swing_strength).
    A swing high at index i: high[i] is the maximum of high[i-n .. i+n].
    Searches from right to left (most recent first).
    Returns (swing_low_price, swing_low_time, swing_high_price, swing_high_time).
    Any component is None if no qualifying swing is found.
    """
    n = swing_strength
    length = len(df)
    if length < 2 * n + 1:
        return None, None, None, None

    lows  = df["low"].values
    highs = df["high"].values
    times = df.index

    swing_low_price  = None
    swing_low_time   = None
    swing_high_price = None
    swing_high_time  = None

    # rightmost usable index: need n candles to the right for confirmation
    for i in range(length - n - 1, n - 1, -1):
        if swing_low_price is None:
            window_low = lows[i - n: i + n + 1]
            if lows[i] == window_low.min():
                swing_low_price = float(lows[i])
                swing_low_time  = times[i]

        if swing_high_price is None:
            window_high = highs[i - n: i + n + 1]
            if highs[i] == window_high.max():
                swing_high_price = float(highs[i])
                swing_high_time  = times[i]

        if swing_low_price is not None and swing_high_price is not None:
            break

    return swing_low_price, swing_low_time, swing_high_price, swing_high_time


def detect_smt(mnq: pd.DataFrame, mes: pd.DataFrame,
               lookback: int = SMT_LOOKBACK_CANDLES,
               tol: float = SMT_TOLERANCE_PCT) -> list:
    """
    Compare the most recent candles of MNQ vs MES.
    Detect when EITHER index sweeps an external level the other does NOT — both directions.
      Bullish: MNQ sweeps low & MES holds  —OR—  MES sweeps low & MNQ holds
      Bearish: MNQ sweeps high & MES fails —OR—  MES sweeps high & MNQ fails
    Returns list of signals: {type, direction, time, detail}
    """
    if len(mnq) < lookback + 1 or len(mes) < lookback + 1:
        return []

    signals = []

    common = mnq.index.intersection(mes.index)
    if len(common) < lookback + 1:
        return []

    mnq_a = mnq.loc[common]
    mes_a = mes.loc[common]

    cur_mnq  = mnq_a.iloc[-1]
    cur_mes  = mes_a.iloc[-1]
    cur_time = common[-1]

    cur_mnq_low  = float(cur_mnq["low"])
    cur_mnq_high = float(cur_mnq["high"])
    cur_mes_low  = float(cur_mes["low"])
    cur_mes_high = float(cur_mes["high"])

    # Find nearest swing liquidity: try 3 days first, extend to full history if needed
    three_days_ago = cur_time - timedelta(days=3)
    mnq_recent = mnq_a[mnq_a.index >= three_days_ago].iloc[:-1]
    mes_recent = mes_a[mes_a.index >= three_days_ago].iloc[:-1]
    mnq_sl, mnq_sl_t, mnq_sh, mnq_sh_t = find_nearest_liquidity(mnq_recent)
    mes_sl, mes_sl_t, mes_sh, mes_sh_t = find_nearest_liquidity(mes_recent)
    # Extend to full available data (up to SWING_LOOKBACK_DAYS) if either side missing
    if mnq_sl is None or mnq_sh is None:
        sl2, st2, sh2, sht2 = find_nearest_liquidity(mnq_a.iloc[:-1])
        if mnq_sl is None: mnq_sl, mnq_sl_t = sl2, st2
        if mnq_sh is None: mnq_sh, mnq_sh_t = sh2, sht2
    if mes_sl is None or mes_sh is None:
        sl2, st2, sh2, sht2 = find_nearest_liquidity(mes_a.iloc[:-1])
        if mes_sl is None: mes_sl, mes_sl_t = sl2, st2
        if mes_sh is None: mes_sh, mes_sh_t = sh2, sht2

    # Fallback to rolling window if no swing found
    win_mnq = mnq_a.iloc[-(lookback + 1):-1]
    win_mes = mes_a.iloc[-(lookback + 1):-1]
    ref_mnq_low  = mnq_sl if mnq_sl is not None else float(win_mnq["low"].min())
    ref_mnq_high = mnq_sh if mnq_sh is not None else float(win_mnq["high"].max())
    ref_mes_low  = mes_sl if mes_sl is not None else float(win_mes["low"].min())
    ref_mes_high = mes_sh if mes_sh is not None else float(win_mes["high"].max())

    def _fmt_t(t):
        if t is None:
            return "?"
        if t.date() == cur_time.date():
            return t.strftime("%H:%M")
        return t.strftime("%d/%m %H:%M")

    mnq_sl_label = f"swing low @{_fmt_t(mnq_sl_t)}" if mnq_sl is not None else "rolling low"
    mnq_sh_label = f"swing high @{_fmt_t(mnq_sh_t)}" if mnq_sh is not None else "rolling high"
    mes_sl_label = f"swing low @{_fmt_t(mes_sl_t)}" if mes_sl is not None else "rolling low"
    mes_sh_label = f"swing high @{_fmt_t(mes_sh_t)}" if mes_sh is not None else "rolling high"

    # ICT definition: one index makes ANY new extreme beyond the reference;
    # the other stays within the reference.

    # ── Bullish SMT: MNQ sweeps LOW, MES holds ──
    if cur_mnq_low < ref_mnq_low and cur_mes_low >= ref_mes_low:
        signals.append({
            "type":          "bullish_smt",
            "direction":     "LONG 🟢",
            "time":          cur_time,
            "mnq_val":       cur_mnq_low,
            "mes_val":       cur_mes_low,
            "ref_mnq":       ref_mnq_low,
            "ref_mes":       ref_mes_low,
            "ref_mnq_label": mnq_sl_label,
            "ref_mes_label": mes_sl_label,
            "detail":        f"MNQ swept {mnq_sl_label} ({cur_mnq_low:.2f} < {ref_mnq_low:.2f}), "
                             f"MES held ({cur_mes_low:.2f} >= {ref_mes_low:.2f})"
        })

    # ── Bullish SMT: MES sweeps LOW, MNQ holds ──
    if cur_mes_low < ref_mes_low and cur_mnq_low >= ref_mnq_low:
        signals.append({
            "type":          "bullish_smt",
            "direction":     "LONG 🟢",
            "time":          cur_time,
            "mnq_val":       cur_mnq_low,
            "mes_val":       cur_mes_low,
            "ref_mnq":       ref_mnq_low,
            "ref_mes":       ref_mes_low,
            "ref_mnq_label": mnq_sl_label,
            "ref_mes_label": mes_sl_label,
            "detail":        f"MES swept {mes_sl_label} ({cur_mes_low:.2f} < {ref_mes_low:.2f}), "
                             f"MNQ held ({cur_mnq_low:.2f} >= {ref_mnq_low:.2f})"
        })

    # ── Bearish SMT: MNQ sweeps HIGH, MES does not reach ──
    if cur_mnq_high > ref_mnq_high and cur_mes_high <= ref_mes_high:
        signals.append({
            "type":          "bearish_smt",
            "direction":     "SHORT 🔴",
            "time":          cur_time,
            "mnq_val":       cur_mnq_high,
            "mes_val":       cur_mes_high,
            "ref_mnq":       ref_mnq_high,
            "ref_mes":       ref_mes_high,
            "ref_mnq_label": mnq_sh_label,
            "ref_mes_label": mes_sh_label,
            "detail":        f"MNQ swept {mnq_sh_label} ({cur_mnq_high:.2f} > {ref_mnq_high:.2f}), "
                             f"MES did not reach ({cur_mes_high:.2f} <= {ref_mes_high:.2f})"
        })

    # ── Bearish SMT: MES sweeps HIGH, MNQ does not reach ──
    if cur_mes_high > ref_mes_high and cur_mnq_high <= ref_mnq_high:
        signals.append({
            "type":          "bearish_smt",
            "direction":     "SHORT 🔴",
            "time":          cur_time,
            "mnq_val":       cur_mnq_high,
            "mes_val":       cur_mes_high,
            "ref_mnq":       ref_mnq_high,
            "ref_mes":       ref_mes_high,
            "ref_mnq_label": mnq_sh_label,
            "ref_mes_label": mes_sh_label,
            "detail":        f"MES swept {mes_sh_label} ({cur_mes_high:.2f} > {ref_mes_high:.2f}), "
                             f"MNQ did not reach ({cur_mnq_high:.2f} <= {ref_mnq_high:.2f})"
        })

    return signals


def detect_hidden_smt(mnq: pd.DataFrame, mes: pd.DataFrame,
                      lookback: int = SMT_LOOKBACK_CANDLES) -> list:
    """
    Hidden SMT: same as regular but uses candle BODIES (open/close) — wicks ignored.
    Per ICT skill: body closes beyond reference = hidden divergence.
    """
    if len(mnq) < lookback + 1 or len(mes) < lookback + 1:
        return []

    common = mnq.index.intersection(mes.index)
    if len(common) < lookback + 1:
        return []

    mnq_a = mnq.loc[common]
    mes_a = mes.loc[common]
    win_mnq = mnq_a.iloc[-(lookback + 1):-1]
    win_mes = mes_a.iloc[-(lookback + 1):-1]
    cur_mnq = mnq_a.iloc[-1]
    cur_mes = mes_a.iloc[-1]
    cur_time = common[-1]

    def _fmt(t):
        if t.date() == cur_time.date():
            return t.strftime("%H:%M")
        return t.strftime("%d/%m %H:%M")

    def _body_low_candle(win):
        body_lows = win[["open", "close"]].min(axis=1)
        idx = body_lows.idxmin()
        return float(body_lows.min()), _fmt(idx), idx

    def _body_high_candle(win):
        body_highs = win[["open", "close"]].max(axis=1)
        idx = body_highs.idxmax()
        return float(body_highs.max()), _fmt(idx), idx

    ref_mnq_low,  mnq_low_t,  mnq_low_ts  = _body_low_candle(win_mnq)
    ref_mes_low,  mes_low_t,  mes_low_ts  = _body_low_candle(win_mes)
    ref_mnq_high, mnq_high_t, mnq_high_ts = _body_high_candle(win_mnq)
    ref_mes_high, mes_high_t, mes_high_ts = _body_high_candle(win_mes)

    cur_mnq_body_low  = min(float(cur_mnq["open"]), float(cur_mnq["close"]))
    cur_mes_body_low  = min(float(cur_mes["open"]), float(cur_mes["close"]))
    cur_mnq_body_high = max(float(cur_mnq["open"]), float(cur_mnq["close"]))
    cur_mes_body_high = max(float(cur_mes["open"]), float(cur_mes["close"]))

    signals = []

    if cur_mnq_body_low < ref_mnq_low and cur_mes_body_low >= ref_mes_low:
        signals.append({
            "type": "hidden_bullish_smt", "direction": "LONG 🟢",
            "time": cur_time, "mnq_val": cur_mnq_body_low,
            "mes_val": cur_mes_body_low, "ref_mnq": ref_mnq_low, "ref_mes": ref_mes_low,
            "ref_mnq_time": mnq_low_ts, "ref_mes_time": mes_low_ts,
            "detail": f"Hidden SMT: MNQ body broke low ({cur_mnq_body_low:.2f} < body low {ref_mnq_low:.2f} @{mnq_low_t}), MES body held"
        })
    if cur_mes_body_low < ref_mes_low and cur_mnq_body_low >= ref_mnq_low:
        signals.append({
            "type": "hidden_bullish_smt", "direction": "LONG 🟢",
            "time": cur_time, "mnq_val": cur_mnq_body_low,
            "mes_val": cur_mes_body_low, "ref_mnq": ref_mnq_low, "ref_mes": ref_mes_low,
            "ref_mnq_time": mnq_low_ts, "ref_mes_time": mes_low_ts,
            "detail": f"Hidden SMT: MES body broke low ({cur_mes_body_low:.2f} < body low {ref_mes_low:.2f} @{mes_low_t}), MNQ body held"
        })
    if cur_mnq_body_high > ref_mnq_high and cur_mes_body_high <= ref_mes_high:
        signals.append({
            "type": "hidden_bearish_smt", "direction": "SHORT 🔴",
            "time": cur_time, "mnq_val": cur_mnq_body_high,
            "mes_val": cur_mes_body_high, "ref_mnq": ref_mnq_high, "ref_mes": ref_mes_high,
            "ref_mnq_time": mnq_high_ts, "ref_mes_time": mes_high_ts,
            "detail": f"Hidden SMT: MNQ body broke high ({cur_mnq_body_high:.2f} > body high {ref_mnq_high:.2f} @{mnq_high_t}), MES body held"
        })
    if cur_mes_body_high > ref_mes_high and cur_mnq_body_high <= ref_mnq_high:
        signals.append({
            "type": "hidden_bearish_smt", "direction": "SHORT 🔴",
            "time": cur_time, "mnq_val": cur_mnq_body_high,
            "mes_val": cur_mes_body_high, "ref_mnq": ref_mnq_high, "ref_mes": ref_mes_high,
            "ref_mnq_time": mnq_high_ts, "ref_mes_time": mes_high_ts,
            "detail": f"Hidden SMT: MES body broke high ({cur_mes_body_high:.2f} > body high {ref_mes_high:.2f} @{mes_high_t}), MNQ body held"
        })

    return signals


def detect_fill_smt(mnq: pd.DataFrame, mes: pd.DataFrame,
                    mnq_fvgs: list, mes_fvgs: list) -> list:
    """
    Fill SMT: divergence in how each instrument interacts with its FVG zone.
    Type 1 — one instrument dips into FVG, the other doesn't touch it.
    Type 2 — one instrument enters FVG, the other passes completely through.
    """
    if mnq.empty or mes.empty:
        return []

    signals = []
    last_mnq = mnq.iloc[-1]
    last_mes = mes.iloc[-1]
    cur_time = mnq.index[-1]

    def _fvg_interaction(last_candle, fvg):
        low, high = float(last_candle["low"]), float(last_candle["high"])
        in_fvg  = low <= fvg["top"] and high >= fvg["bottom"]
        thru    = low < fvg["bottom"]
        return in_fvg, thru

    for fvg in mnq_fvgs:
        mnq_in, mnq_thru = _fvg_interaction(last_mnq, fvg)
        mes_in, mes_thru = _fvg_interaction(last_mes, fvg)
        direction = "LONG 🟢" if fvg["type"] == "bullish" else "SHORT 🔴"

        if mnq_in and not mes_in:
            signals.append({
                "type": "fill_smt_type1", "direction": direction, "time": cur_time,
                "detail": f"Fill SMT Type1: MNQ entered {fvg['type']} FVG, MES did not touch → {direction.split()[0]}"
            })
        elif mnq_in and mes_thru:
            signals.append({
                "type": "fill_smt_type2", "direction": direction, "time": cur_time,
                "detail": f"Fill SMT Type2: MNQ in FVG, MES passed through → {direction.split()[0]}"
            })

    for fvg in mes_fvgs:
        mnq_in, mnq_thru = _fvg_interaction(last_mnq, fvg)
        mes_in, mes_thru = _fvg_interaction(last_mes, fvg)
        direction = "LONG 🟢" if fvg["type"] == "bullish" else "SHORT 🔴"

        if mes_in and not mnq_in:
            signals.append({
                "type": "fill_smt_type1", "direction": direction, "time": cur_time,
                "detail": f"Fill SMT Type1: MES entered {fvg['type']} FVG, MNQ did not touch → {direction.split()[0]}"
            })
        elif mes_in and mnq_thru:
            signals.append({
                "type": "fill_smt_type2", "direction": direction, "time": cur_time,
                "detail": f"Fill SMT Type2: MES in FVG, MNQ passed through → {direction.split()[0]}"
            })

    return signals


# ══════════════════════════════════════════════
#  RECOMMENDATION ENGINE
# ══════════════════════════════════════════════

def score_liquidity(mnq_df, mnq_levels, mnq_fvgs,
                    mes_df, mes_levels, mes_fvgs) -> float:
    """
    Score liquidity conditions: external sweeps + FVG zone presence.
    Returns -1.0 (bearish) → 0 (neutral) → +1.0 (bullish).
    """
    def _score_single(df, levels, fvgs):
        if df.empty or not levels:
            return 0.0, []
        cur = levels.get("CURRENT", 0)
        recent = df.iloc[-SMT_LOOKBACK_CANDLES:]
        recent_low  = float(recent["low"].min())
        recent_high = float(recent["high"].max())
        sub = []
        reasons = []

        for name in ("PDL", "PWL", "LOD"):
            lvl = levels.get(name)
            if lvl and recent_low < lvl and cur > lvl:
                sub.append(+1.0)
                reasons.append(f"{name} swept (bullish)")

        for name in ("PDH", "PWH", "HOD"):
            lvl = levels.get(name)
            if lvl and recent_high > lvl and cur < lvl:
                sub.append(-1.0)
                reasons.append(f"{name} swept (bearish)")

        for fvg in fvgs:
            if fvg["bottom"] <= cur <= fvg["top"]:
                v = +0.5 if fvg["type"] == "bullish" else -0.5
                sub.append(v)
                reasons.append(f"Inside {fvg['type']} FVG")

        score = max(-1.0, min(1.0, sum(sub) / len(sub))) if sub else 0.0
        return score, reasons

    s_mnq, r_mnq = _score_single(mnq_df, mnq_levels, mnq_fvgs)
    s_mes, r_mes = _score_single(mes_df, mes_levels, mes_fvgs)
    score = (s_mnq + s_mes) / 2
    reasons = list(dict.fromkeys(r_mnq + r_mes))   # deduplicate
    return score, reasons


def score_smt(regular_smts, hidden_smts, fill_smts, ref_time) -> tuple:
    """
    Score all SMT signals, weighted by type, with time decay.
    Regular=1.0, Hidden=0.7, Fill=0.5. Signals older than SMT_DECAY_MINUTES ignored.
    Returns (score: float, reasons: list).
    """
    TYPE_WEIGHT = {
        "bullish_smt": +1.0, "bearish_smt": -1.0,
        "hidden_bullish_smt": +0.7, "hidden_bearish_smt": -0.7,
        "fill_smt_type1": None, "fill_smt_type2": None,
    }
    TYPE_LABEL = {
        "bullish_smt": "Regular", "bearish_smt": "Regular",
        "hidden_bullish_smt": "Hidden", "hidden_bearish_smt": "Hidden",
        "fill_smt_type1": "Fill Type1", "fill_smt_type2": "Fill Type2",
    }
    FILL_BASE = 0.5

    entries = []
    for sig in regular_smts + hidden_smts:
        w = TYPE_WEIGHT.get(sig["type"], 0)
        if w is not None:
            lbl = TYPE_LABEL.get(sig["type"], "SMT")
            entries.append((sig["time"], w, lbl))

    for sig in fill_smts:
        direction = +1.0 if sig["direction"].startswith("LONG") else -1.0
        lbl = TYPE_LABEL.get(sig["type"], "Fill")
        entries.append((sig["time"], direction * FILL_BASE, lbl))

    if not entries:
        return 0.0, []

    total_w = weighted_sum = 0.0
    reasons = []
    for sig_time, value, lbl in entries:
        age_min = (ref_time - sig_time).total_seconds() / 60
        if age_min > SMT_DECAY_MINUTES:
            continue
        decay = max(0.0, 1.0 - age_min / SMT_DECAY_MINUTES)
        total_w += abs(value) * decay
        weighted_sum += value * decay
        direction_str = "LONG" if value > 0 else "SHORT"
        reasons.append(f"{lbl} SMT {direction_str} @{sig_time.strftime('%H:%M')} (decay {decay:.0%})")

    if total_w == 0:
        return 0.0, ["SMT signals expired (> 30 min old)"]
    score = max(-1.0, min(1.0, weighted_sum / total_w))
    return score, reasons


def score_quarters(ref_time) -> float:
    """
    Returns a confidence multiplier (0.4–1.3) based on the current quarter
    and proximity to high-probability time windows.
    """
    q_info = get_quarter(ref_time)
    if not q_info:
        return 0.4

    q_num, s, e = q_info
    mult = QUARTER_CONFIDENCE[q_num]

    # Bonus: within 30 min of NY Open (16:30 IL) — highest quality window
    nyo = ref_time.replace(hour=16, minute=30, second=0, microsecond=0)
    if abs((ref_time - nyo).total_seconds()) <= 1800:
        mult = min(1.3, mult * 1.2)

    # Bonus: first 30 min after quarter open
    q_start = ref_time.replace(hour=s, minute=0, second=0, microsecond=0)
    if 0 <= (ref_time - q_start).total_seconds() <= 1800:
        mult = min(1.3, mult * 1.1)

    # Penalty: last 15 min of a quarter
    q_end = quarter_end_dt(ref_time, e)
    if 0 <= (q_end - ref_time).total_seconds() <= 900:
        mult *= 0.8

    return round(mult, 2)


def score_tdo_two(current, tdo, two) -> tuple:
    """
    Score based on TDO (daily bias) and TWO (weekly bias).
    Returns (score: float, reasons: list).
    Below TDO/TWO = discount = LONG (+). Above = premium = SHORT (-).
    """
    sub = []
    reasons = []

    if tdo:
        daily_dir = +1.0 if current < tdo else -1.0
        dist = abs(current - tdo) / tdo
        conf = min(1.0, 0.5 + dist * 50)
        sub.append(daily_dir * conf)
        label = "Discount" if daily_dir > 0 else "Premium"
        reasons.append(f"Daily {label} vs TDO ({tdo:.2f})")

    if two:
        weekly_dir = +1.0 if current < two else -1.0
        dist = abs(current - two) / two
        conf = min(1.0, 0.5 + dist * 50)
        sub.append(weekly_dir * conf)
        label = "Discount" if weekly_dir > 0 else "Premium"
        reasons.append(f"Weekly {label} vs TWO ({two:.2f})")

    if not sub:
        return 0.0, []

    # Conflict between daily and weekly → neutral
    if len(sub) == 2 and sub[0] * sub[1] < 0:
        return 0.0, ["Conflict: daily and weekly bias oppose each other"]

    score = max(-1.0, min(1.0, sum(sub) / len(sub)))
    return score, reasons


def compute_recommendation(mnq_df, mes_df, mnq_levels, mes_levels,
                            mnq_fvgs, mes_fvgs,
                            regular_smts, hidden_smts, fill_smts,
                            ref_time) -> dict:
    """
    Combine all scoring factors into a single trade recommendation.
    """
    current = mnq_levels.get("CURRENT", 0)
    tdo     = mnq_levels.get("TDO", 0)
    two     = mnq_levels.get("TWO", 0)

    s_liq, r_liq   = score_liquidity(mnq_df, mnq_levels, mnq_fvgs,
                                     mes_df, mes_levels, mes_fvgs)
    s_smt, r_smt   = score_smt(regular_smts, hidden_smts, fill_smts, ref_time)
    q_mult         = score_quarters(ref_time)
    s_bias, r_bias = score_tdo_two(current, tdo, two)

    # Normalize weights (exclude quarters — it's a multiplier)
    w = WEIGHTS
    denom = w["liquidity"] + w["smt"] + w["tdo_two"]
    raw   = (s_liq * w["liquidity"] + s_smt * w["smt"] + s_bias * w["tdo_two"]) / denom
    final = max(-1.0, min(1.0, raw * q_mult))

    if   final >=  RECOMMEND_STRONG:   action, strength = "LONG",  "STRONG"
    elif final >=  RECOMMEND_MODERATE: action, strength = "LONG",  "MODERATE"
    elif final <= -RECOMMEND_STRONG:   action, strength = "SHORT", "STRONG"
    elif final <= -RECOMMEND_MODERATE: action, strength = "SHORT", "MODERATE"
    else:                              action, strength = "WAIT",  ""

    reasons = []
    if abs(s_liq) >= 0.3: reasons.extend(r_liq)
    reasons.extend(r_smt)   # always show SMT breakdown
    if abs(s_bias) >= 0.3: reasons.extend(r_bias)

    return {
        "action":       action,
        "strength":     strength,
        "score":        round(final, 3),
        "raw_score":    round(raw, 3),
        "quarter_mult": q_mult,
        "scores": {
            "liquidity": round(s_liq, 3),
            "smt":       round(s_smt, 3),
            "tdo_two":   round(s_bias, 3),
        },
        "weights":  {k: v for k, v in w.items()},
        "reasons":  reasons,
    }


# ══════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════

def send_telegram(message: str) -> tuple[bool, str]:
    """Send alert to Telegram. Returns (success, reason)."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN") or TELEGRAM_BOT_TOKEN
    chat_id = os.getenv("TELEGRAM_CHAT_ID")   or TELEGRAM_CHAT_ID
    if not token:
        return False, "TELEGRAM_BOT_TOKEN not set in .env"
    if not chat_id:
        return False, "TELEGRAM_CHAT_ID not set in .env"
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={
            "chat_id":    chat_id,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)
        if not r.ok:
            err = f"HTTP {r.status_code}: {r.text}"
            print(f"{Fore.RED}[TELEGRAM ERROR] {err}{Style.RESET_ALL}")
            return False, err
        return True, "ok"
    except Exception as e:
        print(f"{Fore.RED}[TELEGRAM ERROR] {e}{Style.RESET_ALL}")
        return False, str(e)


# ══════════════════════════════════════════════
#  DISPLAY
# ══════════════════════════════════════════════

def print_header(ref_time, mode_label):
    now_str = ref_time.strftime("%d/%m/%Y %H:%M:%S")
    q_info  = get_quarter(ref_time)

    print(f"\n{Back.BLUE}{Fore.WHITE}{'═'*65}{Style.RESET_ALL}")
    print(f"{Back.BLUE}{Fore.WHITE}  ICT SMT AGENT  |  MNQ + MES  |  {now_str}  {Style.RESET_ALL}")
    print(f"{Back.BLUE}{Fore.WHITE}{'═'*65}{Style.RESET_ALL}")

    if q_info:
        q_num, s, e = q_info
        q_end_dt = quarter_end_dt(ref_time, e)
        remaining = q_end_dt - ref_time
        rem_h, rem_rem = divmod(int(remaining.total_seconds()), 3600)
        rem_m = rem_rem // 60
        rng   = quarter_range_str(s, e)
        q_color = [Fore.CYAN, Fore.YELLOW, Fore.GREEN, Fore.MAGENTA][q_num - 1]
        print(f"  {q_color}Q{q_num}  {rng}  |  {rem_h}h {rem_m:02d}m remaining in quarter{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}Signals valid for rest of Q{q_num} (till {rng.split('–')[1]}){Style.RESET_ALL}")
    else:
        print(f"  {Fore.YELLOW}Outside trading hours (00:00–01:00){Style.RESET_ALL}")

    print(f"  {Fore.WHITE}{mode_label}{Style.RESET_ALL}\n")


def print_side_by_side(mnq_levels: dict, mes_levels: dict,
                       mnq_df: pd.DataFrame, mes_df: pd.DataFrame,
                       mnq_fvgs: list, mes_fvgs: list, ref_time):
    """
    Print MNQ and MES levels side by side.
    Rule: build every plain string first, pad it with :<W, THEN wrap in color.
    Never embed ANSI codes inside a string before padding — invisible chars
    break Python's field-width calculation.
    """
    mnq_cur   = mnq_levels.get("CURRENT", 0)
    mes_cur   = mes_levels.get("CURRENT", 0)
    skip      = {"CURRENT"}
    W         = 42          # visible chars per column
    SEP       = " │ "
    TOTAL     = W * 2 + len(SEP)
    LINE      = f"{Fore.WHITE}{'─' * TOTAL}{Style.RESET_ALL}"
    q_colors  = [Fore.CYAN, Fore.YELLOW, Fore.GREEN, Fore.MAGENTA]
    current_q = get_quarter(ref_time)

    # ── Price header ──────────────────────────────────────────────────────
    print(f"\n{LINE}")
    hdr_l = f"  MNQ  current: {mnq_cur:>10.2f}"
    hdr_r = f"  MES  current: {mes_cur:>10.2f}"
    # plain strings → pad → color
    print(f"{Fore.CYAN}{hdr_l:<{W}}{Style.RESET_ALL}"
          f"{SEP}"
          f"{Fore.YELLOW}{hdr_r}{Style.RESET_ALL}")

    # ── Quarter summary (full-width rows, no ─│─ split) ──────────────────
    print(LINE)
    print(f"{Fore.WHITE}  QUARTERS (Israel time){Style.RESET_ALL}")
    print(LINE)

    for q_num, s, e in QUARTERS:
        q_start      = ref_time.replace(hour=s, minute=0, second=0, microsecond=0)
        q_end_dt_val = quarter_end_dt(ref_time, e)
        rng          = quarter_range_str(s, e)
        color        = q_colors[q_num - 1]
        is_current   = current_q and current_q[0] == q_num
        now_mark     = " ►" if is_current else "  "   # plain chars, no ANSI

        if q_start > ref_time:
            plain = f"  Q{q_num}  {rng}   (future)"
            print(f"{color}{plain}{Style.RESET_ALL}")
            continue

        actual_end = min(q_end_dt_val, ref_time + timedelta(minutes=15))

        def q_hl(df, _q_start=q_start, _actual_end=actual_end):
            qdf = df[(df.index >= _q_start) & (df.index < _actual_end)]
            if qdf.empty:
                return "       —", "       —"
            return f"{float(qdf['high'].max()):>9.2f}", f"{float(qdf['low'].min()):>9.2f}"

        mnq_h, mnq_l = q_hl(mnq_df)
        mes_h, mes_l = q_hl(mes_df)

        # All plain text — pad works correctly
        plain = (f"  Q{q_num}  {rng}{now_mark}"
                 f"   MNQ  H:{mnq_h}  L:{mnq_l}"
                 f"    MES  H:{mes_h}  L:{mes_l}")
        print(f"{color}{plain}{Style.RESET_ALL}")

    # ── Level rows (two columns, MNQ │ MES) ──────────────────────────────
    print(LINE)
    col_hdr = f"  {'Level':<6}  {'Value':>9}  {'Dist':>9}"
    # plain string padded → then color applied around it
    print(f"{Fore.CYAN}{col_hdr:<{W}}{Style.RESET_ALL}"
          f"{SEP}"
          f"{Fore.YELLOW}{col_hdr}{Style.RESET_ALL}")
    print(LINE)

    all_names = sorted(
        {k for k in (set(mnq_levels) | set(mes_levels)) if k not in skip},
        key=lambda n: mnq_levels.get(n, mes_levels.get(n, 0)),
        reverse=True
    )

    # Insert CURRENT marker between levels above/below MNQ price
    rows = []
    cur_inserted = False
    for name in all_names:
        ref = mnq_levels.get(name, mes_levels.get(name, 0))
        if not cur_inserted and ref < mnq_cur:
            rows.append(None)
            cur_inserted = True
        rows.append(name)
    if not cur_inserted:
        rows.append(None)

    def level_row(name, val, cur):
        """Return (plain_str, color). plain_str has no ANSI codes."""
        if val is None:
            return f"  {'—':<6}  {'':>9}  {'':>10}", Fore.WHITE
        dist  = val - cur
        arrow = "↑" if dist > 0 else "↓"
        color = Fore.GREEN if dist > 0 else Fore.RED
        mk    = " ◄" if abs(dist) < 0.5 else "  "
        plain = f"  {arrow} {name:<6}  {val:9.2f}  ({dist:+8.2f}){mk}"
        return plain, color

    for row in rows:
        if row is None:
            cur_l = f"  ── CURRENT ──       {mnq_cur:9.2f}"
            cur_r = f"  ── CURRENT ──       {mes_cur:9.2f}"
            print(f"{Fore.WHITE}{cur_l:<{W}}{Style.RESET_ALL}"
                  f"{SEP}"
                  f"{Fore.WHITE}{cur_r}{Style.RESET_ALL}")
            continue

        mnq_val = mnq_levels.get(row)
        mes_val = mes_levels.get(row)

        mnq_s, mnq_c = level_row(row, mnq_val, mnq_cur)
        mes_s, mes_c = level_row(row, mes_val, mes_cur)

        # ◄ SMT when same level is above price for one instrument, below for the other
        smt_flag = (mnq_val is not None and mes_val is not None
                    and (mnq_val > mnq_cur) != (mes_val > mes_cur))
        smt_tag  = (f" {Back.YELLOW}{Fore.BLACK}◄ SMT{Style.RESET_ALL}"
                    if smt_flag else "")

        # Plain strings padded first, then wrapped in color
        print(f"{mnq_c}{mnq_s:<{W}}{Style.RESET_ALL}"
              f"{SEP}"
              f"{mes_c}{mes_s}{Style.RESET_ALL}{smt_tag}")

    # ── FVGs ─────────────────────────────────────────────────────────────
    def fvg_rows(fvgs, cur):
        out = []
        for fvg in fvgs[-4:]:
            sym    = "▲" if fvg["type"] == "bullish" else "▼"
            fc     = Fore.GREEN if fvg["type"] == "bullish" else Fore.RED
            inside = fvg["bottom"] <= cur <= fvg["top"]
            tag    = " IN" if inside else "   "
            plain  = f"  {sym} {fvg['bottom']:.2f}–{fvg['top']:.2f} {fvg['time'].strftime('%H:%M')}{tag}"
            out.append((plain, fc, inside))
        return out

    mnq_fvg_rows = fvg_rows(mnq_fvgs, mnq_cur)
    mes_fvg_rows = fvg_rows(mes_fvgs, mes_cur)
    n_fvg = max(len(mnq_fvg_rows), len(mes_fvg_rows))

    if n_fvg:
        print(LINE)
        fhdr_l = f"  FVGs  MNQ ({len(mnq_fvgs)} active)"
        fhdr_r = f"  FVGs  MES ({len(mes_fvgs)} active)"
        print(f"{Fore.MAGENTA}{fhdr_l:<{W}}{Style.RESET_ALL}"
              f"{SEP}"
              f"{Fore.MAGENTA}{fhdr_r}{Style.RESET_ALL}")
        for i in range(n_fvg):
            ls, lc, l_in = mnq_fvg_rows[i] if i < len(mnq_fvg_rows) else ("", Fore.WHITE, False)
            rs, rc, r_in = mes_fvg_rows[i] if i < len(mes_fvg_rows) else ("", Fore.WHITE, False)
            in_tag = (f" {Back.YELLOW}{Fore.BLACK}INSIDE{Style.RESET_ALL}"
                      if l_in or r_in else "")
            print(f"{lc}{ls:<{W}}{Style.RESET_ALL}{SEP}{rc}{rs}{Style.RESET_ALL}{in_tag}")

    print(LINE)
    # ── Legend ───────────────────────────────────────────────────────────
    dim = Fore.WHITE + Style.DIM if hasattr(Style, "DIM") else Fore.WHITE
    print(f"{dim}  PDH/PDL  Prev Day High/Low   │  HOD/LOD  Today High/Low (so far)"
          f"   │  TDO  Today Open   │  TWO  This Week Open{Style.RESET_ALL}")
    print(f"{dim}  PWH/PWL  Prev Week High/Low  │  NYO  NY Open 09:30ET=16:30IL"
          f"       │  QxH/QxL  Quarter x High/Low{Style.RESET_ALL}")
    print()


def print_smt_signal(sig: dict):
    is_long = sig["direction"].startswith("LONG")
    bg      = Back.GREEN if is_long else Back.RED
    emoji   = "🟢 LONG"  if is_long else "🔴 SHORT"

    print(f"\n{bg}{Fore.WHITE}{'█'*65}{Style.RESET_ALL}")
    print(f"{bg}{Fore.WHITE}  ⚡ SMT SIGNAL — {emoji}  {Style.RESET_ALL}")
    print(f"{bg}{Fore.WHITE}{'█'*65}{Style.RESET_ALL}")
    print(f"  {Fore.WHITE}⏰ Time:   {sig['time'].strftime('%d/%m/%Y %H:%M')}")
    print(f"  📊 MNQ:   {sig['mnq_val']:.2f}  (ref: {sig['ref_mnq']:.2f})")
    print(f"  📊 MES:   {sig['mes_val']:.2f}  (ref: {sig['ref_mes']:.2f})")
    print(f"  💡 {sig['detail']}\n")


def print_no_signal():
    print(f"  {Fore.WHITE}SMT: {Fore.YELLOW}No correlation break detected{Style.RESET_ALL}")
    print(f"  {Fore.WHITE}Bias: {Fore.WHITE}Neutral — monitoring...{Style.RESET_ALL}\n")


# ══════════════════════════════════════════════
#  MAIN SCAN LOOP
# ══════════════════════════════════════════════

def run_scan(sim_time=None, date_mode=False, notify=False, timeframe=None):
    global scan_count, last_smt_signal
    scan_count += 1

    tf = timeframe or TIMEFRAME

    # Determine reference time
    if sim_time:
        ref_time = sim_time
    else:
        ref_time = datetime.now(ISRAEL_TZ)

    # Clear screen only in live mode — in date/simulate mode let output scroll
    if not sim_time and not date_mode:
        os.system("cls" if os.name == "nt" else "clear")

    if date_mode:
        mode_label = (f"⏪ Q3 REPLAY: {ref_time.strftime('%d/%m/%Y')}  |  "
                      f"{ref_time.strftime('%H:%M')} IL  |  #{scan_count}  |  TF: {tf}")
    elif sim_time:
        mode_label = (f"⏪ SIMULATE: {ref_time.strftime('%d/%m/%Y %H:%M')}  |  "
                      f"Scan #{scan_count}  |  TF: {tf}")
    else:
        mode_label = (f"LIVE  |  Scan #{scan_count}  |  Timeframe: {tf}  |  "
                      f"SMT window: {SMT_LOOKBACK_CANDLES} candles")

    print_header(ref_time, mode_label)

    # ── Fetch ──────────────────────────────────
    print(f"  {Fore.CYAN}Loading data...{Style.RESET_ALL}", end="\r")

    # For any historical mode: fetch full day so the filter below can trim precisely
    fetch_end = ref_time.replace(hour=23, minute=59) if sim_time else ref_time
    mnq_df = fetch_data("MNQ=F", tf, SWING_LOOKBACK_DAYS, end_dt=fetch_end)
    mes_df = fetch_data("MES=F", tf, SWING_LOOKBACK_DAYS, end_dt=fetch_end)

    # Always filter to ref_time (works for simulate, date mode, and live)
    if not mnq_df.empty:
        mnq_df = mnq_df[mnq_df.index <= ref_time]
    if not mes_df.empty:
        mes_df = mes_df[mes_df.index <= ref_time]

    if mnq_df.empty or mes_df.empty:
        print(f"  {Fore.RED}⚠️  Could not load data — check connection, retrying in {SCAN_INTERVAL_SECONDS}s{Style.RESET_ALL}")
        return

    # ── Levels & FVGs ─────────────────────────
    mnq_levels = get_external_levels(mnq_df, ref_time=ref_time)
    mes_levels = get_external_levels(mes_df, ref_time=ref_time)
    mnq_fvgs   = detect_fvg(mnq_df)
    mes_fvgs   = detect_fvg(mes_df)

    print_side_by_side(mnq_levels, mes_levels, mnq_df, mes_df,
                       mnq_fvgs, mes_fvgs, ref_time)

    # ── SMT ───────────────────────────────────
    q_info = get_quarter(ref_time)
    q_end_str = ""
    if q_info:
        _, _, e = q_info
        q_end_str = "00:00" if e == 24 else f"{e:02d}:00"

    print(f"{Fore.WHITE}  SMT DIVERGENCE ANALYSIS"
          + (f"  — targets valid till {q_end_str}" if q_end_str else "")
          + f"{Style.RESET_ALL}")

    smt_signals  = detect_smt(mnq_df, mes_df)
    hidden_smts  = detect_hidden_smt(mnq_df, mes_df)
    fill_smts    = detect_fill_smt(mnq_df, mes_df, mnq_fvgs, mes_fvgs)

    if smt_signals:
        for sig in smt_signals:
            print_smt_signal(sig)

            sig_key = (sig["type"], sig["time"].strftime("%Y%m%d%H%M"))
            if last_smt_signal["type"] != sig_key:
                last_smt_signal["type"] = sig_key

                cur_mnq = mnq_levels.get("CURRENT", 0)
                cur_mes = mes_levels.get("CURRENT", 0)
                tgt_mnq_above, tgt_mnq_below = nearest_liquidity(cur_mnq, mnq_levels)
                tgt_mes_above, tgt_mes_below = nearest_liquidity(cur_mes, mes_levels)

                is_long = sig["direction"].startswith("LONG")
                tgt_mnq = tgt_mnq_above if is_long else tgt_mnq_below
                tgt_mes = tgt_mes_above if is_long else tgt_mes_below

                tg_msg  = f"\n🎯 <b>Target MNQ:</b> {tgt_mnq[0]} @ {tgt_mnq[1]}" if tgt_mnq else ""
                tg_msg2 = f"\n🎯 <b>Target MES:</b> {tgt_mes[0]} @ {tgt_mes[1]}" if tgt_mes else ""

                q_tag    = f"  |  Q{q_info[0]} till {q_end_str}" if q_info else ""
                test_tag = "\n🧪 <b>[TEST]</b> sent from date/simulate mode" if notify and (date_mode or sim_time) else ""
                emoji = "🟢" if is_long else "🔴"
                rec_now = compute_recommendation(
                    mnq_df, mes_df, mnq_levels, mes_levels,
                    mnq_fvgs, mes_fvgs,
                    smt_signals, hidden_smts, fill_smts, ref_time,
                )
                rec_act = rec_now["action"]
                rec_str = rec_now["strength"]
                rec_scr = rec_now["score"]
                rec_tag = (f"\n📈 <b>Recommendation: {rec_act}"
                           + (f" ({rec_str})" if rec_str else "")
                           + f"</b>  score: {rec_scr:+.3f}")
                tg_text = (
                    f"{emoji} <b>{'[TEST] ' if notify and (date_mode or sim_time) else ''}SMT Signal — {sig['direction']}</b>\n"
                    f"⏰ {sig['time'].strftime('%d/%m/%Y %H:%M')} (Israel)\n"
                    f"📊 MNQ: <b>{sig['mnq_val']:.2f}</b>  (ref: {sig['ref_mnq']:.2f})\n"
                    f"📊 MES: <b>{sig['mes_val']:.2f}</b>  (ref: {sig['ref_mes']:.2f})\n"
                    f"💡 {html_lib.escape(sig['detail'])}"
                    f"{tg_msg}{tg_msg2}\n"
                    f"⚙️ Timeframe: {tf}{q_tag}"
                    f"{rec_tag}{test_tag}"
                )
                if not date_mode or notify:
                    send_telegram(tg_text)
                    print(f"  {Fore.GREEN}✅ Telegram alert sent{' [TEST]' if notify and date_mode else ''}{Style.RESET_ALL}")
    else:
        print_no_signal()

    # ── FVG alerts (new ones) ─────────────────
    for ticker, fvgs, df in [("MNQ", mnq_fvgs, mnq_df), ("MES", mes_fvgs, mes_df)]:
        for fvg in fvgs:
            fvg_id = f"{fvg['type']}_{fvg['bottom']}_{fvg['top']}"
            cur    = float(df["close"].iloc[-1])
            inside = fvg["bottom"] <= cur <= fvg["top"]

            if inside and fvg_id not in last_fvg_alert[ticker.lower()]:
                last_fvg_alert[ticker.lower()].add(fvg_id)
                fc = "🟩" if fvg["type"] == "bullish" else "🟥"
                tg_text = (
                    f"{fc} <b>FVG — Price inside gap!</b>\n"
                    f"📌 {ticker}  |  {fvg['type'].upper()}\n"
                    f"📐 Zone: {fvg['bottom']:.2f} – {fvg['top']:.2f}\n"
                    f"💰 Current price: {cur:.2f}\n"
                    f"⏰ {ref_time.strftime('%d/%m/%Y %H:%M')} (Israel)"
                )
                if not date_mode or notify:
                    send_telegram(tg_text)
                print(f"  {Fore.MAGENTA}⚡ FVG alert: {ticker} price inside {fvg['type']} gap "
                      f"[{fvg['bottom']:.2f}–{fvg['top']:.2f}]{Style.RESET_ALL}")

    # ── Summary bias ──────────────────────────
    print(f"\n{Fore.WHITE}{'─'*65}")
    q_lbl = f"Q{q_info[0]} — {quarter_range_str(q_info[1], q_info[2])}" if q_info else "Outside trading hours"
    print(f"  SUMMARY & BIAS  [{q_lbl}]{Style.RESET_ALL}")
    print(f"{Fore.WHITE}{'─'*65}{Style.RESET_ALL}")

    mnq_cur = mnq_levels.get("CURRENT", 0)
    mes_cur = mes_levels.get("CURRENT", 0)
    mnq_tdo = mnq_levels.get("TDO", 0)
    mes_tdo = mes_levels.get("TDO", 0)
    mnq_two = mnq_levels.get("TWO", 0)
    mes_two = mes_levels.get("TWO", 0)

    mnq_vs_tdo = "above TDO ↑" if mnq_cur > mnq_tdo else "below TDO ↓"
    mes_vs_tdo = "above TDO ↑" if mes_cur > mes_tdo else "below TDO ↓"
    mnq_vs_two = "above TWO ↑" if mnq_cur > mnq_two else "below TWO ↓"
    mes_vs_two = "above TWO ↑" if mes_cur > mes_two else "below TWO ↓"

    mnq_tdo_color = Fore.GREEN if mnq_cur > mnq_tdo else Fore.RED
    mes_tdo_color = Fore.GREEN if mes_cur > mes_tdo else Fore.RED
    mnq_two_color = Fore.GREEN if mnq_cur > mnq_two else Fore.RED
    mes_two_color = Fore.GREEN if mes_cur > mes_two else Fore.RED

    print(f"  MNQ {mnq_cur:.2f}  {mnq_tdo_color}{mnq_vs_tdo}{Style.RESET_ALL}  "
          f"{mnq_two_color}{mnq_vs_two}{Style.RESET_ALL}  "
          f"|  MES {mes_cur:.2f}  {mes_tdo_color}{mes_vs_tdo}{Style.RESET_ALL}  "
          f"{mes_two_color}{mes_vs_two}{Style.RESET_ALL}")

    overall = "Neutral ⚪"
    if mnq_cur > mnq_tdo and mes_cur > mes_tdo:
        overall = f"{Fore.GREEN}BULLISH 🟢 — both above TDO{Style.RESET_ALL}"
    elif mnq_cur < mnq_tdo and mes_cur < mes_tdo:
        overall = f"{Fore.RED}BEARISH 🔴 — both below TDO{Style.RESET_ALL}"

    w_overall = ""
    if mnq_two and mes_two:
        if mnq_cur > mnq_two and mes_cur > mes_two:
            w_overall = f"  {Fore.GREEN}(weekly BULLISH — both above TWO){Style.RESET_ALL}"
        elif mnq_cur < mnq_two and mes_cur < mes_two:
            w_overall = f"  {Fore.RED}(weekly BEARISH — both below TWO){Style.RESET_ALL}"

    print(f"  Overall bias: {overall}{w_overall}")

    # ── Recommendation ────────────────────────
    rec = compute_recommendation(
        mnq_df, mes_df, mnq_levels, mes_levels,
        mnq_fvgs, mes_fvgs,
        smt_signals, hidden_smts, fill_smts, ref_time,
    )
    print(f"\n{Fore.WHITE}{'─'*65}")
    print(f"  RECOMMENDATION{Style.RESET_ALL}")
    print(f"{Fore.WHITE}{'─'*65}{Style.RESET_ALL}")
    action   = rec["action"]
    strength = rec["strength"]
    score    = rec["score"]
    act_color = Fore.GREEN if action == "LONG" else Fore.RED if action == "SHORT" else Fore.YELLOW
    str_color = Fore.GREEN if strength == "STRONG" else Fore.YELLOW if strength == "MODERATE" else Fore.WHITE
    print(f"  {act_color}{action}{Style.RESET_ALL}"
          + (f"  {str_color}{strength}{Style.RESET_ALL}" if strength else "")
          + f"  score: {score:+.3f}"
          + f"  (raw: {rec['raw_score']:+.3f} × {rec['quarter_mult']:.2f}Q)")
    s = rec["scores"]
    w = rec["weights"]
    print(f"  factors:  "
          f"liq {s['liquidity']:+.2f}×{w['liquidity']}  "
          f"smt {s['smt']:+.2f}×{w['smt']}  "
          f"tdo/two {s['tdo_two']:+.2f}×{w['tdo_two']}")
    for reason in rec["reasons"]:
        print(f"  {Fore.CYAN}{reason}{Style.RESET_ALL}")

    if date_mode:
        q_rem = get_quarter(ref_time)
        end_str = ("00:00" if q_rem[2] == 24 else f"{q_rem[2]:02d}:00") if q_rem else "19:00"
        print(f"\n  {Fore.YELLOW}[Q3 REPLAY] Data up to {ref_time.strftime('%H:%M')} IL  |  "
              f"Signals: {ref_time.strftime('%H:%M')}–{end_str}{Style.RESET_ALL}\n")
    elif sim_time:
        pass  # single-shot simulate — no footer needed
    else:
        print(f"\n  {Fore.WHITE}Next scan in {SCAN_INTERVAL_SECONDS}s...  "
              f"(Ctrl+C to stop){Style.RESET_ALL}\n")


# ══════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    parser = argparse.ArgumentParser(
        description="ICT SMT Live Agent — MNQ + MES",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python ict_smt_agent.py                               # live mode\n"
            "  python ict_smt_agent.py --date 2026-03-31             # date mode, no Telegram\n"
            "  python ict_smt_agent.py --date 2026-03-31 --notify    # date mode + [TEST] Telegram\n"
            "  python ict_smt_agent.py --simulate \"2026-03-31 15:30\" --notify\n"
        )
    )
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD",
        help="Date mode: fetch 2 days including this date, analyse data up to 15:00 Israel time."
    )
    parser.add_argument(
        "--simulate", metavar="DATETIME",
        help='Legacy simulate: "YYYY-MM-DD HH:MM" (Israel time).'
    )
    parser.add_argument(
        "--notify", action="store_true",
        help="Send Telegram alerts even in date/simulate mode (messages are prefixed with [TEST])."
    )
    args = parser.parse_args()

    base_date = None   # date mode: the calendar date to replay
    sim_time  = None   # simulate mode: single explicit cutoff
    date_mode = False

    if args.date:
        try:
            d = datetime.strptime(args.date, "%Y-%m-%d")
            base_date = d.replace(hour=0, minute=0, second=0, tzinfo=ISRAEL_TZ)
            date_mode = True
        except ValueError:
            print(f"{Fore.RED}Invalid format. Use: --date YYYY-MM-DD{Style.RESET_ALL}")
            sys.exit(1)

    elif args.simulate:
        try:
            sim_time = datetime.strptime(args.simulate, "%Y-%m-%d %H:%M").replace(tzinfo=ISRAEL_TZ)
        except ValueError:
            print(f"{Fore.RED}Invalid format. Use: --simulate \"YYYY-MM-DD HH:MM\"{Style.RESET_ALL}")
            sys.exit(1)

    # Auto-select finest timeframe supported for the requested date
    ref_for_tf = base_date or sim_time
    if ref_for_tf:
        days_ago     = (datetime.now(ISRAEL_TZ) - ref_for_tf).days
        effective_tf = auto_timeframe(days_ago)
    else:
        effective_tf = TIMEFRAME

    if effective_tf != TIMEFRAME:
        tf_note = f" (auto — {TIMEFRAME} limited to {TIMEFRAME_MAX_DAYS.get(TIMEFRAME, 60)}d)"
    else:
        tf_note = ""

    if date_mode:
        mode_str = f"{Fore.YELLOW}⏪ Q3 REPLAY — {base_date.strftime('%d/%m/%Y')}  13:00→19:00 IL{Style.RESET_ALL}"
    elif sim_time:
        mode_str = f"{Fore.YELLOW}⏪ SIMULATE — {sim_time.strftime('%d/%m/%Y %H:%M')}{Style.RESET_ALL}"
    else:
        mode_str = "Live"

    print(f"""
{Fore.CYAN}╔══════════════════════════════════════════════════════════════╗
║          ICT SMT LIVE AGENT — MNQ + MES                      ║
║  External Liquidity | FVG | SMT Divergence                   ║
╚══════════════════════════════════════════════════════════════╝{Style.RESET_ALL}

  Timeframe  : {effective_tf}{tf_note}
  Scan every : {SCAN_INTERVAL_SECONDS}s
  Telegram   : {'✅ configured' if TELEGRAM_BOT_TOKEN else '⚠️  not configured'}
  Notify     : {'✅ [TEST] alerts enabled' if args.notify else 'suppressed in date/simulate mode'}
  Mode       : {mode_str}

  Quarters (Israel time):
    Q1  01:00–07:00  |  Q2  07:00–13:00
    Q3  13:00–19:00  |  Q4  19:00–00:00

  מתחיל...
""")
    time.sleep(1)

    if date_mode:
        # ── Q3 replay: scan every hour from 13:00 to 18:00 ──
        checkpoints = [base_date.replace(hour=h) for h in range(13, 19)]
        for i, cp in enumerate(checkpoints):
            run_scan(sim_time=cp, date_mode=True, notify=args.notify, timeframe=effective_tf)
            if i < len(checkpoints) - 1:
                nxt = cp + timedelta(hours=1)
                print(f"\n  {Fore.CYAN}── {cp.strftime('%H:%M')} done · next checkpoint "
                      f"{nxt.strftime('%H:%M')} in 3s ──{Style.RESET_ALL}")
                time.sleep(3)
        print(f"\n{Fore.GREEN}  ✅ Q3 replay complete for "
              f"{base_date.strftime('%d/%m/%Y')}{Style.RESET_ALL}\n")

    elif sim_time:
        # ── Single snapshot ──
        run_scan(sim_time=sim_time, date_mode=False, notify=args.notify, timeframe=effective_tf)

    else:
        # ── Live mode: replay past Q3 hours, then scan continuously ──
        now    = datetime.now(ISRAEL_TZ)
        q_info = get_quarter(now)
        if q_info and q_info[0] == 3 and now.hour > 13:
            today_base   = now.replace(hour=0, minute=0, second=0, microsecond=0)
            past_hours   = [today_base.replace(hour=h) for h in range(13, now.hour)]
            print(f"  {Fore.YELLOW}Replaying {len(past_hours)} past Q3 hour(s) before going live...{Style.RESET_ALL}\n")
            time.sleep(1)
            for cp in past_hours:
                run_scan(sim_time=cp, timeframe=TIMEFRAME)
                time.sleep(1)

        run_scan(timeframe=TIMEFRAME)
        schedule.every(SCAN_INTERVAL_SECONDS).seconds.do(lambda: run_scan(timeframe=TIMEFRAME))
        while True:
            schedule.run_pending()
            time.sleep(1)
