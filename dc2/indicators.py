from __future__ import annotations

from datetime import datetime

import pandas as pd

from dc2.constants import N_VELAS_BB, ZM_THRESHOLD_PCT
from dc2.utils import _filter_bars_before

import logging

logger = logging.getLogger(__name__)


def compute_badge_long(
    bars_dict: dict,
    cutoff_time: datetime,
    now_time: datetime,
    precio: float | None = None,
) -> float:
    """
    Momentum score 0-100. UP + DOWN = 100 per check.

    Checks 1-2 use cutoff_time (session snapshot).
    Checks 3-8 use now_time (current state).
    """
    up = 0.0

    # CHECK 1 — EMA9/20 on 1m (snapshot) — 25 pts
    bars_1m = bars_dict.get("1m", [])
    if bars_1m:
        filtered = _filter_bars_before(bars_1m, cutoff_time)
        if len(filtered) >= 20:
            closes = pd.Series([float(b["c"]) for b in filtered], dtype=float)
            ema9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
            ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
            if ema9 > ema20:
                up += 25.0

    # CHECK 2 — price > EMA20 on 15m (snapshot) — 25 pts
    bars_15m = bars_dict.get("15m", [])
    if bars_15m and precio is not None:
        filtered = _filter_bars_before(bars_15m, cutoff_time)
        if len(filtered) >= 20:
            closes = pd.Series([float(b["c"]) for b in filtered], dtype=float)
            ema20_15m = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
            if precio > ema20_15m:
                up += 25.0

    # CHECK 3 — EMA9/20 on 1m (current) — 25 pts
    if bars_1m:
        filtered = _filter_bars_before(bars_1m, now_time)
        if len(filtered) >= 20:
            closes = pd.Series([float(b["c"]) for b in filtered], dtype=float)
            ema9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
            ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
            if ema9 > ema20:
                up += 25.0

    # CHECKS 4-8 — gap or candle color per TF (current) — 5 pts each
    GAP_THRESHOLD = 0.0005
    for tf in ("1d", "1h", "30m", "15m", "5m"):
        bars = bars_dict.get(tf, [])
        if not bars:
            continue
        filtered = _filter_bars_before(bars, now_time)
        if len(filtered) < 2:
            continue
        last = filtered[-1]
        prev = filtered[-2]
        open_p = float(last["o"])
        close_p = float(last["c"])
        prev_close = float(prev["c"])
        if prev_close == 0:
            continue
        gap_pct = (open_p - prev_close) / prev_close
        if gap_pct > GAP_THRESHOLD:
            up += 5
        elif gap_pct >= -GAP_THRESHOLD and close_p > open_p:
            up += 5

    return float(up)


def compute_signals_3_9(bars_dict: dict, cutoff_time: datetime) -> str:
    """EMA3/EMA9 crossover signal per TF (15m, 30m, 1h)."""
    parts = []
    for tf in ["15m", "30m", "1h"]:
        if tf not in bars_dict:
            continue
        bars_tf = bars_dict[tf]
        if isinstance(bars_tf, pd.DataFrame):
            if bars_tf.empty:
                continue
        else:
            if not bars_tf:
                continue
        filtered = _filter_bars_before(bars_tf, cutoff_time)
        if len(filtered) < 9:
            continue
        tail20 = filtered[-N_VELAS_BB:]
        closes = pd.Series([float(b["c"]) for b in tail20], dtype=float)
        ema3 = float(closes.ewm(span=3, adjust=False).mean().iloc[-1])
        ema9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
        signal = "up" if ema3 > ema9 else "down"
        parts.append(f"3/9 {signal} {tf}")
    return ", ".join(parts) if parts else ""


def ema_alignment(
    open_p: float,
    e20: float,
    e50: float,
    e200: float,
    pct: float = 0.001,
) -> str:
    """
    Classifies price position relative to EMA20/50/200.
    Returns: 'sobre_3' | 'bajo_3' | 'entre' | 'at_any'
    pct: AT tolerance (0.1% of price)
    """
    def zone(o, e):
        if not e or e == 0:
            return "unknown"
        d = (o - e) / e
        return "above" if d > pct else ("below" if d < -pct else "at")

    zones = [zone(open_p, e20), zone(open_p, e50), zone(open_p, e200)]
    if "at" in zones:
        return "at_any"
    if all(z == "above" for z in zones):
        return "sobre_3"
    if all(z == "below" for z in zones):
        return "bajo_3"
    return "entre"


def direction_from_dist(dist: float, int_dist: float = 0.0) -> str:
    """
    Direction from open vs BP.
    Dead zone = ZM_THRESHOLD_PCT * int_dist (~11% of int_dist); fallback ±$0.25.
    """
    threshold = (int_dist * ZM_THRESHOLD_PCT) if int_dist > 0 else 0.25
    if dist > threshold:
        return "ALCISTA"
    if dist < -threshold:
        return "BAJISTA"
    return "ZONA_MUERTA"


def compute_open_zone(
    open_930: float,
    bp: float,
    int_pos: float | None,
    int_neg: float | None,
    direction: str,
    int_dist: float = 0.0,
) -> tuple[str | None, float | None]:
    """
    Zone where price opened relative to BP→INT range.
    Returns (zone_label, pct_in_range). pct > 1.0 means ext_int_max.
    """
    threshold = (int_dist * ZM_THRESHOLD_PCT) if int_dist > 0 else 0.25
    dist_abs = abs(open_930 - bp)
    if dist_abs < threshold:
        return "doble_dir_zone", 0.0

    if direction == "ALCISTA":
        if int_pos is None:
            return None, None
        rango = int_pos - bp
        if rango <= 0:
            return "doble_dir_zone", 0.0
        pct = (open_930 - bp) / rango
    elif direction == "BAJISTA":
        if int_neg is None:
            return None, None
        rango = bp - int_neg
        if rango <= 0:
            return "doble_dir_zone", 0.0
        pct = (bp - open_930) / rango
    else:
        return "doble_dir_zone", 0.0

    if pct <= 0:
        return "doble_dir_zone", round(pct, 4)
    if pct <= 0.333:
        zone = "normal_t1"
    elif pct <= 0.667:
        zone = "normal_t2"
    elif pct <= 1.0:
        zone = "normal_t3"
    else:
        zone = "ext_int_max"
    return zone, round(pct, 4)


def conviction_score(
    dir_ph: str,
    gap_tipo: str,
    gap_mag: float,
    pdc: float,
    ema_align: str,
    bb_expande: bool | None = None,
    n_cautiones: int = 0,
    caution_1v3: str | None = None,
    ticker: str = "",
    open_930: float | None = None,
    int_pos: float | None = None,
    int_neg: float | None = None,
) -> float:
    """
    Conviction score EC: 0-10 scale.
    Base 5.0 + EMA alignment + GAP + PrevDay + bb_expande.

    score >= 7  → PREMIUM
    5-6         → OPERABLE
    <= 4        → SKIP
    Returns -1.0 if price opened beyond INT (BLOQUEADO).

    Note: per-ticker calibration weights are not included in this public release.
    The structural scoring logic is preserved; override this function with your
    own calibrated weights after backtesting on your own data.
    """
    score = 5.0

    # EMA alignment (±2.0 pts)
    if dir_ph == "BAJISTA":
        if ema_align == "bajo_3":
            score += 2.0
        elif ema_align == "sobre_3":
            score -= 1.0
        elif ema_align in ("entre", "at_any"):
            score -= 0.5
    elif dir_ph == "ALCISTA":
        if ema_align == "sobre_3":
            score += 2.0
        elif ema_align == "bajo_3":
            score -= 1.0
        elif ema_align in ("entre", "at_any"):
            score -= 0.5

    # GAP type and magnitude (±2.0 pts)
    mag = abs(float(gap_mag or 0))
    if dir_ph == "BAJISTA":
        if gap_tipo == "GAP_DOWN":
            score += 2.0 if mag > 3 else (1.5 if mag > 1 else 0.5)
        elif gap_tipo == "GAP_UP":
            score += 1.5 if mag > 3 else (-0.5 if mag > 1 else 0.0)
    elif dir_ph == "ALCISTA":
        if gap_tipo == "GAP_UP":
            score += 1.5 if mag > 3 else (1.0 if mag > 1 else 0.5)
        elif gap_tipo == "GAP_DOWN":
            score += -1.5 if mag > 3 else (-0.5 if mag > 1 else 0.5)

    # Prev day change (±2.0 pts)
    pdc_f = float(pdc or 0)
    if dir_ph == "BAJISTA":
        if pdc_f < -3:
            score += 1.5
        elif pdc_f < -1:
            score += 2.0
        elif pdc_f < 0:
            score += 1.0
        elif pdc_f < 1:
            score += 0.5
        elif pdc_f < 3:
            score -= 0.5
        else:
            score -= 1.5
    elif dir_ph == "ALCISTA":
        if pdc_f > 5:
            score += 2.0
        elif pdc_f > 2:
            score += 1.5
        elif pdc_f > 0:
            score += 0.5
        elif pdc_f > -1:
            score += 0.0
        elif pdc_f > -3:
            score -= 0.5
        else:
            score -= 1.5

    # BB expansion bonus (+0.5)
    if bb_expande is True:
        score += 0.5

    # Caution count (±1.5 pts)
    if dir_ph in ("ALCISTA", "BAJISTA"):
        if n_cautiones == 0:
            score += 1.5
        elif n_cautiones == 4 and dir_ph == "BAJISTA":
            score += 1.5
        elif n_cautiones == 4 and dir_ph == "ALCISTA":
            score -= 0.5

    # Bloqueado: price already past INT at open
    if open_930 is not None:
        if dir_ph == "ALCISTA" and int_pos is not None and open_930 > int_pos:
            return -1.0
        if dir_ph == "BAJISTA" and int_neg is not None and open_930 < int_neg:
            return -1.0
        if dir_ph == "ALCISTA" and int_pos is not None:
            if (int_pos - open_930) < 0.30:
                return -1.0
        if dir_ph == "BAJISTA" and int_neg is not None:
            if (open_930 - int_neg) < 0.30:
                return -1.0

    return round(max(0.0, min(10.0, score)), 1)
