from __future__ import annotations

"""
mode_live.py — BP en tiempo real vía yfinance.

Rangos reducidos al 50% (intraday = extensiones menores que premarket/open).
Válido de 9:30 ET hasta cierre postmarket.
"""

import logging
from datetime import datetime

import pandas as pd
import yfinance as yf

from dc2.constants import TIMEZONE
from dc2.data_clients import BPCalculator, TargetsCalculator
from dc2.indicators import direction_from_dist
from dc2.utils import _compute_bb_from_closes
from dc2.print_dashboard import print_live_ticker

logger = logging.getLogger(__name__)

_LIVE_RANGE_SCALE = 0.5  # rangos al 50%: intraday mueve menos que premarket

# TF → (period, interval) para yfinance
_TF_YF = {
    "1h":  ("30d", "1h"),
    "15m": ("5d",  "15m"),
    "5m":  ("2d",  "5m"),
    "1m":  ("1d",  "1m"),
}


def _download_bars(ticker: str, period: str, interval: str) -> list[dict]:
    """Descarga yfinance y convierte a lista de barras OHLC."""
    try:
        df = yf.download(ticker, period=period, interval=interval,
                         progress=False, auto_adjust=True)
    except Exception as exc:
        logger.warning("[LIVE] %s %s/%s error: %s", ticker, interval, period, exc)
        return []

    if df is None or df.empty:
        return []

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    out = []
    for ts, row in df.iterrows():
        try:
            if hasattr(ts, "tz_convert"):
                ts_et = ts.tz_convert(TIMEZONE)
            elif hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                ts_et = pd.Timestamp(ts).tz_convert(TIMEZONE)
            else:
                ts_et = pd.Timestamp(ts).tz_localize("UTC").tz_convert(TIMEZONE)
            out.append({
                "t": ts_et,
                "o": float(row["Open"]),
                "c": float(row["Close"]),
                "h": float(row["High"]),
                "l": float(row["Low"]),
            })
        except Exception:
            continue
    return out


def run_mode_live(ticker: str) -> None:
    """Calcula BP + targets live para ticker y printea a stdout."""
    logger.info("[LIVE] %s — descargando barras yfinance", ticker)

    bars_dict: dict[str, list[dict]] = {}
    for tf_key, (period, interval) in _TF_YF.items():
        bars = _download_bars(ticker, period, interval)
        bars_dict[tf_key] = bars
        logger.debug("[LIVE] %s %s: %d barras", ticker, tf_key, len(bars))

    # ── BP ───────────────────────────────────────────────────────
    bp = BPCalculator.calculate_bp(bars_dict)
    if bp is None:
        print(f"❌ {ticker}: BP=None — sin datos suficientes en yfinance")
        return

    # ── range_3d → range_live ────────────────────────────────────
    bars_1d = _download_bars(ticker, "10d", "1d")
    cutoff_now = datetime.now(TIMEZONE)
    range_3d = TargetsCalculator.calculate_range_3d(
        ticker,
        bars_1d,
        bars_1h=bars_dict.get("1h", []),
        cutoff_time=cutoff_now,
    )
    range_live = range_3d * _LIVE_RANGE_SCALE
    targets = TargetsCalculator.calculate_targets(bp, range_live)

    # ── Precio actual ────────────────────────────────────────────
    bars_1m = bars_dict.get("1m", [])
    precio = float(bars_1m[-1]["c"]) if bars_1m else None
    dist = (precio - bp) if precio is not None else None
    direction = direction_from_dist(dist) if dist is not None else "N/D"

    # ── Open 9:30 (primer bar RTH) ───────────────────────────────
    bars_rth = [
        b for b in bars_1m
        if b["t"].hour > 9 or (b["t"].hour == 9 and b["t"].minute >= 30)
    ]
    open_930 = float(bars_rth[0]["o"]) if bars_rth else None

    # ── Session High / Low (barras RTH 1m) ───────────────────────
    session_high = max((float(b["h"]) for b in bars_rth), default=None)
    session_low  = min((float(b["l"]) for b in bars_rth), default=None)

    # ── BB 5M actual (últimas 20 barras) ─────────────────────────
    bars_5m = bars_dict.get("5m", [])
    bbt_5m, bbb_5m = _compute_bb_from_closes([b["c"] for b in bars_5m])

    # ── BB 1H actual ─────────────────────────────────────────────
    bars_1h = bars_dict.get("1h", [])
    bbt_1h, bbb_1h = _compute_bb_from_closes([b["c"] for b in bars_1h])

    # ── EMA3 / EMA9 en 1H ────────────────────────────────────────
    ema3_1h: float | None = None
    ema9_1h: float | None = None
    if len(bars_1h) >= 9:
        _cl_1h = pd.Series([float(b["c"]) for b in bars_1h[-20:]], dtype=float)
        ema3_1h = float(_cl_1h.ewm(span=3, adjust=False).mean().iloc[-1])
        ema9_1h = float(_cl_1h.ewm(span=9, adjust=False).mean().iloc[-1])

    # ── EMA20 / EMA50 / EMA200 en 5M ─────────────────────────────
    ema20_5m: float | None = None
    ema50_5m: float | None = None
    ema200_5m: float | None = None
    if len(bars_5m) >= 20:
        _cl_5m = pd.Series([float(b["c"]) for b in bars_5m], dtype=float)
        ema20_5m  = float(_cl_5m.ewm(span=20,  adjust=False).mean().iloc[-1])
        ema50_5m  = float(_cl_5m.ewm(span=50,  adjust=False).mean().iloc[-1]) if len(bars_5m) >= 50  else None
        ema200_5m = float(_cl_5m.ewm(span=200, adjust=False).mean().iloc[-1]) if len(bars_5m) >= 200 else None

    td = {
        "bp":          bp,
        "int_pos":     targets["int_pos"],
        "int_neg":     targets["int_neg"],
        "max_pos":     targets["max_pos"],
        "max_neg":     targets["max_neg"],
        "direction":   direction,
        "dist":        dist,
        "open_930":    open_930,
        "session_high": session_high,
        "session_low":  session_low,
        "ema3_1h":     ema3_1h,
        "ema9_1h":     ema9_1h,
        "ema20_5m":    ema20_5m,
        "ema50_5m":    ema50_5m,
        "ema200_5m":   ema200_5m,
        "bbt_5m":      bbt_5m,
        "bbb_5m":      bbb_5m,
        "bbt_1h":      bbt_1h,
        "bbb_1h":      bbb_1h,
        "range_live":  range_live,
    }

    print_live_ticker(ticker, td, precio, cutoff_now)
