"""
ICT SMT Web Dashboard — Flask backend
Run:
    pip install flask
    python web_app.py
Then open http://localhost:5000
"""

from flask import Flask, render_template, jsonify, request
from datetime import datetime, timedelta
import pandas as pd
import time
from collections import deque

import os
from pathlib import Path

def _load_env():
    """Load .env file — try python-dotenv first, then manual parse as fallback."""
    env_path = Path(__file__).parent / '.env'
    if not env_path.exists():
        return
    # Try python-dotenv
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=True)
    except ImportError:
        pass
    # Manual fallback — handles BOM, CRLF, quoted values
    try:
        for line in env_path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                os.environ[key] = value
    except Exception:
        pass

_load_env()

from ict_smt_agent import (
    fetch_data, get_external_levels, detect_fvg, detect_smt,
    detect_hidden_smt, detect_fill_smt, compute_recommendation,
    get_quarter, quarter_range_str, quarter_end_dt, auto_timeframe,
    nearest_liquidity, send_telegram,
    ISRAEL_TZ, QUARTERS, TIMEFRAME, LOOKBACK_DAYS, SWING_LOOKBACK_DAYS, TIMEFRAME_MAX_DAYS,
    DATA_SOURCE, TWELVEDATA_API_KEY, WEIGHTS, STOP_LOSS_PCT,
)

# ── Web SMT deduplication (keyed by signal type + candle time) ─
_last_web_smt: dict = {}

# ── Recommendation alert deduplication ────────────────────────
_last_rec_alert: dict = {}

# ── Last scan context (for manual Telegram trigger) ───────────
_last_scan_ctx: dict = {}

# ── Pause state ───────────────────────────────────────────────
# Auto-pause: NYSE closes ~23:00 IL; resume at 14:00 IL.
# Manual override: user button toggles _pause_manual (True/False).
# None means "follow auto logic".
AUTO_PAUSE_START = 23   # hour IL — pause from here
AUTO_PAUSE_END   = 14   # hour IL — resume at this hour

_pause_manual: bool | None = None   # None = auto, True/False = manual override

def _auto_paused() -> bool:
    h = datetime.now(ISRAEL_TZ).hour
    return h >= AUTO_PAUSE_START or h < AUTO_PAUSE_END

def _is_paused() -> bool:
    if _pause_manual is not None:
        return _pause_manual
    return _auto_paused()

def _pause_reason() -> str:
    if _pause_manual is True:
        return "manual"
    if _pause_manual is None and _auto_paused():
        return "auto"
    return ""

ALLOWED_SOURCES = {"yfinance", "twelvedata"}

# ── Twelve Data credit tracking ───────────────────────────────
# Each full scan (MNQ + MES) costs ~9 credits.
# Free plan: 8 credits/minute, 800 credits/day.
TD_CREDITS_PER_SCAN = 9
TD_MINUTE_LIMIT     = 8
TD_DAILY_LIMIT      = 800
TD_SAFE_INTERVAL_S  = 300   # 5 min between auto-scans — conservative daily budget

_td_minute_log: deque = deque()   # timestamps of each scan (for rolling 60s window)
_td_day_credits: int  = 0
_td_day_start: float  = time.time()

def _td_record_scan():
    """Record a Twelve Data scan and update credit counters."""
    global _td_day_credits, _td_day_start
    now = time.time()
    # Reset daily counter at midnight
    day_start_dt = datetime.now(ISRAEL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_ts = day_start_dt.timestamp()
    if _td_day_start < day_start_ts:
        _td_day_credits = 0
        _td_day_start   = day_start_ts
    _td_day_credits += TD_CREDITS_PER_SCAN
    _td_minute_log.append(now)

def _td_stats() -> dict:
    """Return current Twelve Data usage stats."""
    now = time.time()
    # Purge entries older than 60s
    while _td_minute_log and _td_minute_log[0] < now - 60:
        _td_minute_log.popleft()
    minute_credits = len(_td_minute_log) * TD_CREDITS_PER_SCAN
    return {
        "day_used":       _td_day_credits,
        "day_limit":      TD_DAILY_LIMIT,
        "day_remaining":  max(0, TD_DAILY_LIMIT - _td_day_credits),
        "minute_used":    minute_credits,
        "minute_limit":   TD_MINUTE_LIMIT,
        "safe_interval":  TD_SAFE_INTERVAL_S,
        "scans_remaining": max(0, (TD_DAILY_LIMIT - _td_day_credits) // TD_CREDITS_PER_SCAN),
    }

ALLOWED_TF = {"15m", "1h", "4h", "1d"}

def resample_4h(df):
    """Resample a 1h DataFrame to 4h candles."""
    return (
        df.resample("4h", closed="left", label="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )

app = Flask(__name__)


# ──────────────────────────────────────────────
#  SERIALIZATION HELPERS
# ──────────────────────────────────────────────

def df_to_candles(df):
    return [
        {
            "t": ts.isoformat(),
            "o": round(float(r["open"]),  2),
            "h": round(float(r["high"]),  2),
            "l": round(float(r["low"]),   2),
            "c": round(float(r["close"]), 2),
        }
        for ts, r in df.iterrows()
    ]


def fvg_to_dict(f):
    return {
        "type":       f["type"],
        "bottom":     f["bottom"],
        "top":        f["top"],
        "time":       f["time"].isoformat(),
        "start_time": f["start_time"].isoformat(),
    }


def smt_to_dict(s):
    return {
        "type":          s["type"],
        "direction":     "LONG" if s["direction"].startswith("LONG") else "SHORT",
        "time":          s["time"].isoformat(),
        "detail":        s["detail"],
        "mnq_val":       round(s["mnq_val"], 2),
        "mes_val":       round(s["mes_val"], 2),
        "ref_mnq":       round(s["ref_mnq"], 2),
        "ref_mes":       round(s["ref_mes"], 2),
        "ref_mnq_label": s.get("ref_mnq_label", "ref"),
        "ref_mes_label": s.get("ref_mes_label", "ref"),
    }


def hidden_smt_to_dict(s):
    return {
        "type":         s["type"],
        "direction":    s["direction"],
        "time":         s["time"].isoformat(),
        "detail":       s["detail"],
        "mnq_val":      round(s["mnq_val"], 2),
        "mes_val":      round(s["mes_val"], 2),
        "ref_mnq":      round(s["ref_mnq"], 2),
        "ref_mes":      round(s["ref_mes"], 2),
        "ref_mnq_time": s["ref_mnq_time"].isoformat(),
        "ref_mes_time": s["ref_mes_time"].isoformat(),
    }


def fill_smt_to_dict(s):
    return {
        "subtype":        s["type"],
        "direction":      s["direction"],
        "time":           s["time"].isoformat(),
        "detail":         s["detail"],
        "fvg_instrument":  s.get("fvg_instrument"),
        "fvg_bottom":      s.get("fvg_bottom"),
        "fvg_top":         s.get("fvg_top"),
        "fvg_type":        s.get("fvg_type"),
        "fvg_start_time":  s["fvg_start_time"].isoformat() if s.get("fvg_start_time") else None,
    }


# ──────────────────────────────────────────────
#  ROUTES
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


def _build_trade_plan_str(action: str, mnq_levels: dict, mnq_fvgs: list, mes_fvgs: list) -> str:
    """Build a Telegram-formatted trade plan (entry / TP1-4 / stop) for LONG or SHORT."""
    import html as html_lib
    if action not in ("LONG", "SHORT"):
        return ""

    current = mnq_levels.get("CURRENT", 0)
    if not current:
        return ""

    is_long = action == "LONG"

    # Candidate levels: named price levels + FVG edges
    candidates = [(k, float(v), "level") for k, v in mnq_levels.items() if k != "CURRENT"]
    for f in mnq_fvgs:
        dir_tag = "▲ GAP" if f["type"] == "bullish" else "▼ GAP"
        candidates.append((f"{dir_tag} MNQ top", float(f["top"]),    "fvg"))
        candidates.append((f"{dir_tag} MNQ btm", float(f["bottom"]), "fvg"))
    for f in mes_fvgs:
        dir_tag = "▲ GAP" if f["type"] == "bullish" else "▼ GAP"
        candidates.append((f"{dir_tag} MES top", float(f["top"]),    "fvg"))
        candidates.append((f"{dir_tag} MES btm", float(f["bottom"]), "fvg"))

    # Stop: nearest named level on the opposite side
    stop_pool = [(k, v, t) for k, v, t in candidates if (is_long and v < current) or (not is_long and v > current)]
    stop_pool.sort(key=lambda x: x[1], reverse=is_long)
    stop = next((x for x in stop_pool if x[2] == "level"), stop_pool[0] if stop_pool else None)
    stop_dist = abs(stop[1] - current) if stop else None

    # Targets: up to 4, nearest first, in trade direction
    tp_pool = [(k, v, t) for k, v, t in candidates if (is_long and v > current) or (not is_long and v < current)]
    tp_pool.sort(key=lambda x: x[1], reverse=not is_long)
    tps = tp_pool[:4]

    arrow = "▲" if is_long else "▼"
    entry_emoji = "🟢" if is_long else "🔴"
    lines = [f"\n{entry_emoji} <b>Trade Plan (MNQ)</b>",
             f"  Entry:  <b>{current:.2f}</b>  (current)"]

    for i, (k, v, t) in enumerate(tps, 1):
        dist = abs(v - current)
        rr   = f"  R/R 1:{dist/stop_dist:.1f}" if stop_dist else ""
        pts  = f"+{dist:.2f}" if is_long else f"-{dist:.2f}"
        lines.append(f"  {arrow} TP{i}:  <b>{v:.2f}</b>  ({pts} pts)  {html_lib.escape(k)}{rr}")

    if stop:
        dist = abs(stop[1] - current)
        pts  = f"-{dist:.2f}" if is_long else f"+{dist:.2f}"
        lines.append(f"  🛑 Stop (level): <b>{stop[1]:.2f}</b>  ({pts} pts)  {html_lib.escape(stop[0])}")

    # Percentage-based stop loss
    pct_stop_price = current * (1 - STOP_LOSS_PCT / 100) if is_long else current * (1 + STOP_LOSS_PCT / 100)
    pct_dist       = abs(pct_stop_price - current)
    pct_pts        = f"-{pct_dist:.2f}" if is_long else f"+{pct_dist:.2f}"
    lines.append(f"  ⛔ Stop ({STOP_LOSS_PCT:.4g}%): <b>{pct_stop_price:.2f}</b>  ({pct_pts} pts)")

    return "\n".join(lines)


def _send_web_smt_alerts(smt_sigs, mnq_levels, mes_levels, ref_time, rec=None, mnq_fvgs=None, mes_fvgs=None):
    """Send Telegram for any SMT signals not already alerted this candle."""
    global _last_web_smt
    import html as html_lib

    for sig in smt_sigs:
        sig_key = f"{sig['type']}_{sig['time'].strftime('%Y%m%d%H%M')}"
        if _last_web_smt.get(sig_key):
            continue
        _last_web_smt[sig_key] = True

        is_long = sig["direction"].startswith("LONG")
        emoji   = "🟢" if is_long else "🔴"

        q_info = get_quarter(ref_time)
        q_tag  = ""
        if q_info:
            q_num, qs, qe = q_info
            q_end = quarter_end_dt(ref_time, qe)
            q_tag = f"  |  Q{q_num} till {q_end.strftime('%H:%M')}"

        rec_str = ""
        if rec:
            r_action   = rec.get("action", "WAIT")
            strength   = rec.get("strength", "")
            score      = rec.get("score", 0)
            reasons    = rec.get("reasons", [])
            rec_emoji  = "🟢" if r_action == "LONG" else ("🔴" if r_action == "SHORT" else "⏸")
            rec_str    = f"\n{rec_emoji} <b>Recommendation: {r_action} ({strength})</b>  score: {score:+.3f}"
            if reasons:
                rec_str += "\n📋 " + "\n📋 ".join(html_lib.escape(r) for r in reasons)

        plan_str = _build_trade_plan_str(
            (rec or {}).get("action", "WAIT"),
            mnq_levels, mnq_fvgs or [], mes_fvgs or []
        )

        tg_text = (
            f"{emoji} <b>SMT Signal — {sig['direction']} [WEB]</b>\n"
            f"⏰ {sig['time'].strftime('%d/%m/%Y %H:%M')} (Israel)\n"
            f"📊 MNQ: <b>{sig['mnq_val']:.2f}</b>  (ref: {sig['ref_mnq']:.2f})\n"
            f"📊 MES: <b>{sig['mes_val']:.2f}</b>  (ref: {sig['ref_mes']:.2f})\n"
            f"💡 {html_lib.escape(sig['detail'])}\n"
            f"⚙️ Timeframe: {TIMEFRAME}{q_tag}"
            f"{rec_str}"
            f"{plan_str}"
        )
        send_telegram(tg_text)





@app.route("/api/pause", methods=["POST"])
def api_pause():
    """Toggle or set scan pause state. Body: {"action": "pause"|"resume"|"toggle"|"auto"}"""
    global _pause_manual
    action = (request.get_json(silent=True) or {}).get("action", "toggle")
    if action == "auto":
        _pause_manual = None          # revert to auto logic
    elif action == "pause":
        _pause_manual = True
    elif action == "resume":
        _pause_manual = False
    else:  # toggle
        _pause_manual = not _is_paused()
    return jsonify({
        "paused":  _is_paused(),
        "reason":  _pause_reason(),
        "resume_at": f"{AUTO_PAUSE_END:02d}:00",
    })


@app.route("/api/alert", methods=["POST"])
def api_alert():
    """Manually send Telegram alert for the last scan (bypasses dedup and is_hist guard)."""
    import html as html_lib
    ctx = _last_scan_ctx
    if not ctx:
        return jsonify({"error": "No scan data yet — run a scan first"}), 400

    smt_sigs    = ctx["smt_sigs"]
    hidden_smts = ctx["hidden_smts"]
    fill_smts   = ctx["fill_smts"]
    mnq_levels  = ctx["mnq_levels"]
    mes_levels  = ctx["mes_levels"]
    mnq_fvgs    = ctx.get("mnq_fvgs", [])
    mes_fvgs    = ctx.get("mes_fvgs", [])
    ref_time    = ctx["ref_time"]
    rec         = ctx["rec"]

    all_signals = smt_sigs + hidden_smts + fill_smts
    action      = (rec or {}).get("action", "WAIT")

    if not all_signals and action == "WAIT":
        return jsonify({"sent": False, "reason": "No signals and no LONG/SHORT recommendation"}), 200

    # ── Recommendation summary ────────────────────────────────
    rec_str = ""
    if rec and action != "WAIT":
        strength = rec.get("strength", "")
        score    = rec.get("score", 0)
        reasons  = rec.get("reasons", [])
        rec_emoji = "🟢" if action == "LONG" else "🔴"
        rec_str  = f"\n{rec_emoji} <b>Recommendation: {action} ({strength})</b>  score: {score:+.3f}"
        if reasons:
            rec_str += "\n📋 " + "\n📋 ".join(html_lib.escape(r) for r in reasons)

    # ── Trade plan ────────────────────────────────────────────
    plan_str = _build_trade_plan_str(action, mnq_levels, mnq_fvgs, mes_fvgs)

    q_info = get_quarter(ref_time)
    q_tag  = ""
    if q_info:
        q_num, s, e = q_info
        q_end = quarter_end_dt(ref_time, e)
        q_tag = f"  |  Q{q_num} till {q_end.strftime('%H:%M')}"

    errors = []
    sent   = 0

    if all_signals:
        for sig in all_signals:
            direction = sig.get("direction", "")
            emoji     = "🟢" if "LONG" in direction else "🔴"
            sig_type  = sig.get("type", "smt")
            kind      = ("Hidden SMT" if "hidden" in sig_type
                         else "Fill SMT"   if "fill"   in sig_type
                         else "SMT")
            ref_mnq = sig.get("ref_mnq")
            ref_mes = sig.get("ref_mes")
            vals_str = ""
            if ref_mnq is not None:
                vals_str = (f"\n📊 MNQ: <b>{sig['mnq_val']:.2f}</b>  (ref: {ref_mnq:.2f})"
                            f"\n📊 MES: <b>{sig['mes_val']:.2f}</b>  (ref: {ref_mes:.2f})")
            tg_text = (
                f"{emoji} <b>{kind} — {direction} [MANUAL]</b>\n"
                f"⏰ {ref_time.strftime('%d/%m/%Y %H:%M')} (Israel)\n"
                f"{vals_str}\n"
                f"💡 {html_lib.escape(sig.get('detail',''))}\n"
                f"⚙️ Timeframe: {TIMEFRAME}{q_tag}"
                f"{rec_str}"
                f"{plan_str}"
            )
            ok, reason = send_telegram(tg_text)
            if ok:
                sent += 1
            else:
                errors.append(reason)
    else:
        emoji   = "🟢" if action == "LONG" else "🔴"
        tg_text = (
            f"{emoji} <b>Recommendation: {action} [MANUAL]</b>\n"
            f"⏰ {ref_time.strftime('%d/%m/%Y %H:%M')} (Israel)\n"
            f"⚙️ Timeframe: {TIMEFRAME}{q_tag}"
            f"{rec_str}"
            f"{plan_str}"
        )
        ok, reason = send_telegram(tg_text)
        if ok:
            sent = 1
        else:
            errors.append(reason)

    if sent == 0:
        return jsonify({"sent": False, "reason": errors[0] if errors else "Unknown error"}), 200

    return jsonify({"sent": True, "count": sent})


@app.route("/api/scan")
def api_scan():
    # Honour pause state only in live mode (date= param means historical)
    if not request.args.get("date") and _is_paused():
        return jsonify({
            "paused":    True,
            "reason":    _pause_reason(),
            "resume_at": f"{AUTO_PAUSE_END:02d}:00 IL",
        })

    date_str    = request.args.get("date", "")
    hour        = int(request.args.get("hour", 15))
    minute      = int(request.args.get("minute", 0))
    ui_tf       = request.args.get("tf", "").strip()
    ui_source   = request.args.get("source", DATA_SOURCE).strip()
    if ui_source not in ALLOWED_SOURCES:
        ui_source = DATA_SOURCE
    if ui_source == "twelvedata" and not TWELVEDATA_API_KEY:
        return jsonify({"error": "Twelve Data API key not configured — set TWELVEDATA_API_KEY in ict_smt_agent.py"}), 400

    try:
        if date_str:
            d        = datetime.strptime(date_str, "%Y-%m-%d")
            ref_time = d.replace(hour=hour, minute=minute, second=0, tzinfo=ISRAEL_TZ)
        else:
            ref_time = datetime.now(ISRAEL_TZ)
    except ValueError:
        return jsonify({"error": "Invalid date — use YYYY-MM-DD"}), 400

    if ui_tf and ui_tf not in ALLOWED_TF:
        return jsonify({"error": f"Invalid tf — choose from {sorted(ALLOWED_TF)}"}), 400

    is_hist  = bool(date_str)
    days_ago = max(0, (datetime.now(ISRAEL_TZ) - ref_time).days)

    # 4h is fetched as 1h then resampled; otherwise use requested or auto
    if ui_tf == "4h":
        fetch_tf = "1h"
    elif ui_tf:
        fetch_tf = ui_tf
    else:
        fetch_tf = auto_timeframe(days_ago)

    # respect yfinance limits: if chosen tf can't reach this far, fall back
    max_days = TIMEFRAME_MAX_DAYS.get(fetch_tf, 9999)
    if days_ago > max_days:
        fetch_tf = auto_timeframe(days_ago)
        ui_tf    = fetch_tf   # reflect fallback in response

    # Historical: cut at end of the requested day. Live: let fetch_data use
    # datetime.now() so yfinance gets a clean naive local timestamp (avoids
    # timezone-stripping bugs that shift the start window).
    fetch_end = ref_time.replace(hour=23, minute=59) if is_hist else None
    mnq_df = fetch_data("MNQ=F", fetch_tf, SWING_LOOKBACK_DAYS, end_dt=fetch_end, source=ui_source)
    mes_df = fetch_data("MES=F", fetch_tf, SWING_LOOKBACK_DAYS, end_dt=fetch_end, source=ui_source)

    if ui_tf == "4h":
        mnq_df = resample_4h(mnq_df)
        mes_df = resample_4h(mes_df)

    display_tf = "4h" if ui_tf == "4h" else fetch_tf

    if is_hist:
        if not mnq_df.empty: mnq_df = mnq_df[mnq_df.index <= ref_time]
        if not mes_df.empty: mes_df = mes_df[mes_df.index <= ref_time]

    if mnq_df.empty or mes_df.empty:
        return jsonify({"error": "No data available — market may be closed or date out of range"}), 503

    mnq_levels  = get_external_levels(mnq_df, ref_time=ref_time)
    mes_levels  = get_external_levels(mes_df, ref_time=ref_time)
    mnq_fvgs    = detect_fvg(mnq_df)
    mes_fvgs    = detect_fvg(mes_df)
    smt_sigs    = detect_smt(mnq_df, mes_df)
    hidden_smts = detect_hidden_smt(mnq_df, mes_df)
    fill_smts   = detect_fill_smt(mnq_df, mes_df, mnq_fvgs, mes_fvgs)

    recommendation = compute_recommendation(
        mnq_df, mes_df, mnq_levels, mes_levels,
        mnq_fvgs, mes_fvgs,
        smt_sigs, hidden_smts, fill_smts, ref_time,
    )

    # ── Store context for manual alert trigger ───────────────────
    global _last_scan_ctx
    _last_scan_ctx = {
        "smt_sigs":    smt_sigs,
        "hidden_smts": hidden_smts,
        "fill_smts":   fill_smts,
        "mnq_levels":  mnq_levels,
        "mes_levels":  mes_levels,
        "mnq_fvgs":    mnq_fvgs,
        "mes_fvgs":    mes_fvgs,
        "ref_time":    ref_time,
        "rec":         recommendation,
    }

    # ── Telegram: SMT alerts only, with recommendation context ──
    if not is_hist and smt_sigs:
        _send_web_smt_alerts(smt_sigs, mnq_levels, mes_levels, ref_time, rec=recommendation, mnq_fvgs=mnq_fvgs, mes_fvgs=mes_fvgs)

    # ── Current quarter ───────────────────────
    q_info  = get_quarter(ref_time)
    quarter = None
    if q_info:
        q_num, s, e = q_info
        q_end_dt    = quarter_end_dt(ref_time, e)
        remaining   = q_end_dt - ref_time
        quarter = {
            "num":           q_num,
            "start":         s,
            "end":           e,
            "range":         quarter_range_str(s, e),
            "remaining_min": int(remaining.total_seconds() // 60),
        }

    # ── Quarter H/L for today ─────────────────
    quarters_today = []
    for q_num, s, e in QUARTERS:
        q_start      = ref_time.replace(hour=s, minute=0, second=0, microsecond=0)
        q_end_dt_val = quarter_end_dt(ref_time, e)

        if q_start > ref_time:
            quarters_today.append({
                "num": q_num, "range": quarter_range_str(s, e), "future": True,
            })
            continue

        # For completed quarters use the quarter boundary; for the current/in-progress
        # quarter use the full quarter end — the df only contains data up to ref_time
        # so no over-fetching occurs, and we don't risk excluding the latest candle.
        actual_end = q_end_dt_val

        def q_hl(df, _qs=q_start, _ae=actual_end):
            sub = df[(df.index >= _qs) & (df.index <= _ae)]
            if sub.empty:
                return None, None
            return round(float(sub["high"].max()), 2), round(float(sub["low"].min()), 2)

        mh, ml = q_hl(mnq_df)
        sh, sl = q_hl(mes_df)

        quarters_today.append({
            "num":     q_num,
            "range":   quarter_range_str(s, e),
            "future":  False,
            "current": bool(q_info and q_info[0] == q_num),
            "mnq_h":   mh, "mnq_l": ml,
            "mes_h":   sh, "mes_l": sl,
        })

    last_candle = mnq_df.index[-1].isoformat() if not mnq_df.empty else None

    td_info = None
    if ui_source == "twelvedata":
        _td_record_scan()
        td_info = _td_stats()

    # Trim candles to LOOKBACK_DAYS for display (swing detection already ran on full 7-day data)
    display_cutoff = ref_time - timedelta(days=LOOKBACK_DAYS)
    mnq_display = mnq_df[mnq_df.index >= display_cutoff]
    mes_display = mes_df[mes_df.index >= display_cutoff]

    skip = {"CURRENT"}
    return jsonify({
        "ref_time":     ref_time.isoformat(),
        "last_candle":  last_candle,
        "timeframe":    display_tf,
        "stop_loss_pct": STOP_LOSS_PCT,
        "source":       ui_source,
        "twelvedata":   td_info,
        "is_historical": is_hist,
        "quarter":      quarter,
        "quarters_today": quarters_today,
        "mnq": {
            "ticker":  "MNQ",
            "current": mnq_levels.get("CURRENT", 0),
            "candles": df_to_candles(mnq_display),
            "levels":  {k: v for k, v in mnq_levels.items() if k not in skip},
            "fvgs":    [fvg_to_dict(f) for f in mnq_fvgs],
        },
        "mes": {
            "ticker":  "MES",
            "current": mes_levels.get("CURRENT", 0),
            "candles": df_to_candles(mes_display),
            "levels":  {k: v for k, v in mes_levels.items() if k not in skip},
            "fvgs":    [fvg_to_dict(f) for f in mes_fvgs],
        },
        "smt_signals":    [smt_to_dict(s) for s in smt_sigs],
        "hidden_smts":    [hidden_smt_to_dict(s) for s in hidden_smts],
        "fill_smts":      [fill_smt_to_dict(s) for s in fill_smts],
        "recommendation": recommendation,
    })


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8080, use_reloader=False)
