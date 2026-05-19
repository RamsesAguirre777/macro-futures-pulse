from __future__ import annotations

"""
mode_live_crypto.py — BP en tiempo real vía Binance public REST (klines).

Símbolos Binance: BTCUSDT, ETHUSDT (sin guión).
"""

import logging
from datetime import datetime

import pandas as pd
import requests

from dc2.constants import TIMEZONE
from dc2.data_clients import BPCalculator, TargetsCalculator
from dc2.indicators import direction_from_dist
from dc2.print_dashboard import print_live_crypto_ticker
from dc2.utils import _compute_bb_from_closes

logger = logging.getLogger(__name__)

_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
_CRYPTO_RANGE_SCALE = 1.0

# TF → limit (Binance max per request)
_TF_BINANCE: dict[str, int] = {
    "1h": 720,
    "5m": 576,
    "1m": 1440,
}


def _download_bars_binance(symbol: str, interval: str, limit: int) -> list[dict]:
    """Descarga klines de Binance y convierte a lista de barras OHLC."""
    try:
        resp = requests.get(
            _BINANCE_KLINES_URL,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        logger.warning("[LIVE-CRYPTO] %s %s limit=%s error: %s", symbol, interval, limit, exc)
        return []

    if not rows:
        return []

    out: list[dict] = []
    for row in rows:
        try:
            ts_et = (
                pd.Timestamp(int(row[0]), unit="ms", tz="UTC")
                .tz_convert(TIMEZONE)
            )
            out.append({
                "t": ts_et,
                "o": float(row[1]),
                "c": float(row[4]),
                "h": float(row[2]),
                "l": float(row[3]),
            })
        except Exception:
            continue
    return out


def run_mode_live_crypto(ticker: str) -> None:
    """Calcula BP + targets live para par crypto y printea a stdout."""
    logger.info("[LIVE-CRYPTO] %s — descargando barras Binance", ticker)

    bars_dict: dict[str, list[dict]] = {}
    for tf_key, limit in _TF_BINANCE.items():
        bars = _download_bars_binance(ticker, tf_key, limit)
        bars_dict[tf_key] = bars
        logger.debug("[LIVE-CRYPTO] %s %s: %d barras", ticker, tf_key, len(bars))

    bp = BPCalculator.calculate_bp(bars_dict)
    if bp is None:
        print(f"❌ {ticker}: BP=None — sin datos suficientes en Binance")
        return

    bars_1d = _download_bars_binance(ticker, "1d", 10)
    cutoff_now = datetime.now(TIMEZONE)
    range_3d = TargetsCalculator.calculate_range_3d(
        ticker,
        bars_1d,
        bars_1h=bars_dict.get("1h", []),
        cutoff_time=cutoff_now,
    )
    range_live = range_3d * _CRYPTO_RANGE_SCALE
    targets = TargetsCalculator.calculate_targets(bp, range_live)

    bars_1m = bars_dict.get("1m", [])
    precio = float(bars_1m[-1]["c"]) if bars_1m else None
    dist = (precio - bp) if precio is not None else None
    direction = direction_from_dist(dist) if dist is not None else "N/D"

    bars_1d_today = _download_bars_binance(ticker, "1d", 2)
    open_day = float(bars_1d_today[-1]["o"]) if bars_1d_today else None

    bars_1h = bars_dict.get("1h", [])
    bars_24h = bars_1h[-24:] if len(bars_1h) >= 24 else bars_1h
    high_24h = max((float(b["h"]) for b in bars_24h), default=None)
    low_24h = min((float(b["l"]) for b in bars_24h), default=None)

    price_24h_ago = float(bars_24h[0]["c"]) if bars_24h else None
    change_24h_pct = (
        ((precio - price_24h_ago) / price_24h_ago * 100)
        if (precio and price_24h_ago)
        else None
    )

    bars_5m = bars_dict.get("5m", [])
    bbt_5m, bbb_5m = _compute_bb_from_closes([b["c"] for b in bars_5m])
    bbt_1h, bbb_1h = _compute_bb_from_closes([b["c"] for b in bars_1h])

    ema3_1h: float | None = None
    ema9_1h: float | None = None
    if len(bars_1h) >= 9:
        _cl_1h = pd.Series([float(b["c"]) for b in bars_1h[-20:]], dtype=float)
        ema3_1h = float(_cl_1h.ewm(span=3, adjust=False).mean().iloc[-1])
        ema9_1h = float(_cl_1h.ewm(span=9, adjust=False).mean().iloc[-1])

    ema20_5m: float | None = None
    ema50_5m: float | None = None
    ema200_5m: float | None = None
    if len(bars_5m) >= 20:
        _cl_5m = pd.Series([float(b["c"]) for b in bars_5m], dtype=float)
        ema20_5m = float(_cl_5m.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50_5m = (
            float(_cl_5m.ewm(span=50, adjust=False).mean().iloc[-1])
            if len(bars_5m) >= 50
            else None
        )
        ema200_5m = (
            float(_cl_5m.ewm(span=200, adjust=False).mean().iloc[-1])
            if len(bars_5m) >= 200
            else None
        )

    td = {
        "bp": bp,
        "int_pos": targets["int_pos"],
        "int_neg": targets["int_neg"],
        "max_pos": targets["max_pos"],
        "max_neg": targets["max_neg"],
        "direction": direction,
        "dist": dist,
        "open_day": open_day,
        "high_24h": high_24h,
        "low_24h": low_24h,
        "change_24h_pct": change_24h_pct,
        "ema3_1h": ema3_1h,
        "ema9_1h": ema9_1h,
        "ema20_5m": ema20_5m,
        "ema50_5m": ema50_5m,
        "ema200_5m": ema200_5m,
        "bbt_5m": bbt_5m,
        "bbb_5m": bbb_5m,
        "bbt_1h": bbt_1h,
        "bbb_1h": bbb_1h,
        "range_live": range_3d * _CRYPTO_RANGE_SCALE,
    }

    print_live_crypto_ticker(ticker, td, precio, cutoff_now)
