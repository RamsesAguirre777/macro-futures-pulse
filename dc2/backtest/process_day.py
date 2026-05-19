"""dc2/backtest/process_day.py — Per-day computation for backtest mode."""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

import numpy as np
import pandas as pd

from dc2.backtest.analyzer import (
    _clasificar_vela,
    _estructura_vs_bp,
    _zona_en_vela,
    compute_clv_tercio,
    analyze_regular_session,
    compute_extra_result_fields,
)
from dc2.constants import N_VELAS_BB, TIMEZONE
from dc2.data_clients import BPCalculator, SkipFilter, TargetsCalculator
from dc2.indicators import (
    _get_prev_day_hint,
    compute_badge_long,
    compute_caution_note,
    compute_ema_conviction_block,
    compute_open_zone,
    compute_signals_3_9,
    direction_from_dist,
    precio_en_rango,
)
from dc2.premium_detector import PremiumDetector
from dc2.utils import (
    _bars_list_to_mi_dataframe,
    _compute_bb_from_closes,
    _filter_bars_before,
    _filter_bars_upto_inclusive,
)

logger = logging.getLogger(__name__)

_SI_VALUES  = {"sí", "si", "yes", "true", "1", "s"}
_NO_VALUES  = {"no", "false", "0", "n"}


def _to_bool(v) -> Optional[bool]:
    """Coerce any truthy/falsy value (including Spanish 'Sí'/'No') to bool or None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in _SI_VALUES:
        return True
    if s in _NO_VALUES:
        return False
    return None  # unknown string → don't crash DuckDB


# ── Time helpers ─────────────────────────────────────────────────────────────

def _to_et(ts) -> datetime:
    """Normalize any timestamp to tz-aware ET datetime."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(TIMEZONE).to_pydatetime()


def _cutoff(d: date, hour: int, minute: int) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=TIMEZONE)


def _hhmm_to_minutes(s: str) -> Optional[int]:
    """Convert 'HH:MM' string → total minutes since midnight."""
    if not s:
        return None
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _get_bb_raw(
    bars: list, cutoff: datetime
) -> tuple[Optional[float], Optional[float]]:
    """BB(20,2) of closes up to cutoff (inclusive)."""
    filtered = _filter_bars_upto_inclusive(bars, cutoff)
    return _compute_bb_from_closes([b["c"] for b in filtered])


def _bars_on_day_rth(bars_1m, d: date) -> list:
    """Filter 1m bars to RTH session [09:30, 16:00) on date d.

    Acepta lista de dicts o DataFrame indexado por timestamp.
    """
    c930 = _cutoff(d, 9, 30)
    c1600 = _cutoff(d, 16, 0)
    if isinstance(bars_1m, pd.DataFrame):
        end = pd.Timestamp(c1600) - pd.Timedelta(nanoseconds=1)
        df = bars_1m.loc[pd.Timestamp(c930) : end]
        out = df.reset_index()[["t", "o", "h", "l", "c", "v"]].to_dict("records")
        return out
    out = [b for b in bars_1m if c930 <= _to_et(b["t"]) < c1600]
    return sorted(out, key=lambda x: x["t"])


def _open_930_from_bars(bars_1m, d: date) -> Optional[float]:
    """Return open of the first 1m bar at 09:30 on date d."""
    c930 = _cutoff(d, 9, 30)
    c931 = _cutoff(d, 9, 31)
    if isinstance(bars_1m, pd.DataFrame):
        df = bars_1m.loc[pd.Timestamp(c930) : pd.Timestamp(c931) - pd.Timedelta(nanoseconds=1)]
        if df.empty:
            return None
        return float(df.iloc[0]["o"])
    for b in sorted(bars_1m, key=lambda x: x["t"]):
        if c930 <= _to_et(b["t"]) < c931:
            return float(b["o"])
    return None


def _prev_daily_bars(daily_bars, d: date) -> list:
    """Daily bars strictly before date d, sorted ascending."""
    if isinstance(daily_bars, pd.DataFrame):
        day_start = pd.Timestamp(_cutoff(d, 0, 0))
        df = daily_bars.loc[: day_start - pd.Timedelta(nanoseconds=1)]
        if df.empty:
            return []
        out = df.reset_index()[["t", "o", "h", "l", "c", "v"]].to_dict("records")
        return out
    return sorted(
        [b for b in daily_bars if _to_et(b["t"]).date() < d],
        key=lambda x: x["t"],
    )


def _bars_to_ms(bars: list) -> list:
    """
    Convert bar list so b['t'] is integer milliseconds since epoch.
    Required because dc2.backtest.analyzer helpers use pd.Timestamp(ts, unit='ms').
    """
    out = []
    for b in bars:
        ts = b["t"]
        if isinstance(ts, pd.Timestamp):
            ts_ms = int(ts.value // 1_000_000)
        else:
            ts_ms = int(pd.Timestamp(ts).value // 1_000_000)
        out.append({**b, "t": ts_ms})
    return out


def _zona_price_levels(h: float, l: float) -> dict:
    """Compute resistencia / correccion / soporte levels for a candle."""
    rng = h - l
    return {
        "resistencia": round(h - rng * 0.33, 4),
        "correccion":  round((h + l) / 2, 4),
        "soporte":     round(l + rng * 0.33, 4),
    }


# ── Main per-day function ─────────────────────────────────────────────────────

def process_day_bt(
    ticker: str,
    fecha: date,
    all_tf: dict,
    daily_bars,
) -> tuple[dict, dict]:
    """
    Full backtest analysis for one ticker/day.
    Returns (dash_dict, res_dict) — no 'ticker'/'fecha' keys (added by caller).
    """
    from dc2.caution_utils import (
        build_tfs_rth,
        compute_caution_cambio_1v3,
        compute_caution_real_930,
    )

    # ── Time cutoffs ──────────────────────────────────────────────────────
    cutoff_928  = _cutoff(fecha, 9, 28)
    cutoff_930  = _cutoff(fecha, 9, 30)

    # ── Daily bar lookups ─────────────────────────────────────────────────
    prev_bars = _prev_daily_bars(daily_bars, fecha)
    if len(prev_bars) < 2:
        logger.warning("%s %s: insuficientes barras diarias (%d)", ticker, fecha, len(prev_bars))
        _dash_e: dict[str, Any] = {"skip": True, "skip_reason": "sin_daily_bars"}
        _res_e: dict[str, Any] = {"skip": True, "razon_skip": "sin_daily_bars"}
        return _dash_e, _res_e

    d1 = prev_bars[-1]
    d2 = prev_bars[-2]
    d3 = prev_bars[-3] if len(prev_bars) >= 3 else None

    prev_date        = _to_et(d1["t"]).date()
    cutoff_prev_1600 = _cutoff(prev_date, 16, 0)

    prev_day_high    = float(d1["h"])
    prev_day_low     = float(d1["l"])
    prev_day_close   = float(d1["c"])
    prev_day_open    = float(d1["o"])
    prev_prev_close  = float(d2["c"])
    prev_prev_high   = float(d2["h"])
    prev_prev_low    = float(d2["l"])

    prev_clv = compute_clv_tercio(prev_day_high, prev_day_low, prev_day_close)

    rango_d1 = round(prev_day_high - prev_day_low, 4)
    rango_d2 = round(float(d2["h"]) - float(d2["l"]), 4)
    rango_d3 = round(float(d3["h"]) - float(d3["l"]), 4) if d3 else None

    prev_day_change = (
        round((prev_day_close - prev_prev_close) / prev_prev_close * 100, 4)
        if prev_prev_close else 0.0
    )

    # ── Premarket price (last 1m close before 09:28 on fecha) ─────────────
    bars_1m_all = all_tf.get("1m", [])
    open_930 = _open_930_from_bars(bars_1m_all, fecha)
    if isinstance(bars_1m_all, pd.DataFrame):
        day_start = pd.Timestamp(_cutoff(fecha, 0, 0))
        end = pd.Timestamp(cutoff_928) - pd.Timedelta(nanoseconds=1)
        df_pre = bars_1m_all.loc[day_start:end]
        bars_1m_pre_day = df_pre.reset_index()[["t", "o", "h", "l", "c", "v"]].to_dict("records")
    else:
        bars_1m_pre_day = [
            b for b in _filter_bars_before(bars_1m_all, cutoff_928)
            if _to_et(b["t"]).date() == fecha
        ]
    precio_premarket = float(bars_1m_pre_day[-1]["c"]) if bars_1m_pre_day else None

    if precio_premarket is None:
        logger.warning("%s %s: sin precio premarket", ticker, fecha)
        _dash_e: dict[str, Any] = {"skip": True, "skip_reason": "sin_premarket_price"}
        _res_e: dict[str, Any] = {"skip": True, "razon_skip": "sin_premarket_price"}
        return _dash_e, _res_e

    # ── Gap ───────────────────────────────────────────────────────────────
    gap_mag = (
        round((precio_premarket - prev_day_close) / prev_day_close * 100, 4)
        if prev_day_close else 0.0
    )
    if gap_mag > 0.5:
        gap_tipo = "GAP_UP"
    elif gap_mag < -0.5:
        gap_tipo = "GAP_DOWN"
    else:
        gap_tipo = "FLAT"

    # ── BP ────────────────────────────────────────────────────────────────
    bp_raw = BPCalculator.calculate_bp(all_tf, cutoff_time=cutoff_928)
    if bp_raw is None:
        _e = {"skip": True, "skip_reason": "sin_bp"}
        return dict(_e), dict(_e)
    bp = round(float(bp_raw), 4)

    # ── Ranges / Targets ──────────────────────────────────────────────────
    last3_daily = prev_bars[-3:]
    rango_raw   = round(np.mean([float(b["h"]) - float(b["l"]) for b in last3_daily]), 4)
    avg_3d      = rango_raw
    rango_usado = round(rango_raw * 0.7 if rango_raw > 5 else rango_raw, 4)

    # ── Caution nota (Milton 9:28 premarket definition) ───────────────────
    caution_nota = compute_caution_note(
        all_tf,
        precio_premarket,
        cutoff_prev_1600,
        ticker=ticker,
    )
    n_cautiones = (
        caution_nota.count("BBT") + caution_nota.count("BBB")
    ) if caution_nota else 0

    # ── Signals 3/9 ───────────────────────────────────────────────────────
    signals_str = compute_signals_3_9(all_tf, cutoff_928)

    # ── Targets ───────────────────────────────────────────────────────────
    tgt = TargetsCalculator.calculate_targets(
        bp,
        rango_usado,
        caution_note=caution_nota,
        gap_type=gap_tipo,
    )
    int_pos  = round(tgt["int_pos"], 4)
    int_neg  = round(tgt["int_neg"], 4)
    max_pos  = round(tgt["max_pos"], 4)
    max_neg  = round(tgt["max_neg"], 4)
    int_dist = float(tgt.get("int_dist", 0.0))

    # ── Direction premarket (for EC block and prev_day_hint) ──────────────
    dist_pm       = precio_premarket - bp
    direction_pm  = direction_from_dist(dist_pm, int_dist=int_dist)

    # ── Badge long ────────────────────────────────────────────────────────
    badge_long = round(
        compute_badge_long(
            all_tf,
            cutoff_time=cutoff_928,
            now_time=cutoff_928,
            precio=precio_premarket,
        ),
        2,
    )

    # ── Rango PM (precio in premarket range) ──────────────────────────────
    bars_pm_4am = [
        b for b in bars_1m_pre_day if _to_et(b["t"]).hour >= 4
    ]
    pm_high = max((float(b["h"]) for b in bars_pm_4am), default=None) if bars_pm_4am else None
    pm_low  = min((float(b["l"]) for b in bars_pm_4am), default=None) if bars_pm_4am else None

    rango_pm_d = (
        precio_en_rango(precio_premarket, pm_high, pm_low)
        if pm_high is not None and pm_low is not None
        else {"pct": 0.5, "label": "sin_datos"}
    )
    rango_pd_d = precio_en_rango(precio_premarket, prev_day_high, prev_day_low)

    # ── EMA conviction block (solo si hay open 9:30; sin open no hay EC operativa)
    if open_930 is None:
        ec = {
            "bb_expande_premarket": None,
            "ema20_pre": None,
            "ema50_pre": None,
            "ema200_pre": None,
            "ema_align": None,
            "ema_sr": None,
            "ec_score": None,
            "ec_accion": "SIN_OPEN",
            "ec_size": None,
        }
    else:
        ec = compute_ema_conviction_block(
            bars_dict=all_tf,
            cutoff_et=cutoff_928,
            cutoff_prev_1600=cutoff_prev_1600,
            precio_ref=precio_premarket,
            direction=direction_pm,
            gap_type=gap_tipo,
            gap_pct=gap_mag,
            prev_day_change_pct=prev_day_change,
            caution_note=caution_nota,
            caution_1v3=None,
            ticker=ticker,
        )

    # ── BB raw values ─────────────────────────────────────────────────────
    pre_mkt_bbt_1h,      pre_mkt_bbb_1h      = _get_bb_raw(all_tf.get("1h", []), cutoff_928)
    market_close_bbt_1h, market_close_bbb_1h = _get_bb_raw(all_tf.get("1h", []), cutoff_prev_1600)
    bbt_5m,   bbb_5m   = _get_bb_raw(all_tf.get("5m", []), cutoff_928)
    bbt_clf_5m, bbb_clf_5m = _get_bb_raw(all_tf.get("5m", []), cutoff_prev_1600)

    # ── Skip filter ───────────────────────────────────────────────────────
    signals_for_skip = f"{caution_nota or ''} {gap_tipo}"
    skip_flag   = SkipFilter.apply_skip_filters(
        ticker, prev_day_change, gap_mag, signals_for_skip
    )
    skip_reason = "universal_skip_filter" if skip_flag else None

    # ── Prev day hint ─────────────────────────────────────────────────────
    prev_day_hint = _get_prev_day_hint(ticker, prev_day_change, direction_pm)

    # ── Classify prev_day candle ──────────────────────────────────────────
    estructura_prev_day = _clasificar_vela(
        high=prev_day_high,   low=prev_day_low,
        close=prev_day_close, open_p=prev_day_open,
        prev_high=prev_prev_high, prev_low=prev_prev_low,
    )

    # ── Classify premarket candle (4am-9:28 vs prev_day) ─────────────────
    pm_open_price = float(bars_pm_4am[0]["o"]) if bars_pm_4am else None
    if pm_open_price and pm_high and pm_low:
        estructura_premarket = _clasificar_vela(
            high=pm_high,        low=pm_low,
            close=precio_premarket, open_p=pm_open_price,
            prev_high=prev_day_high, prev_low=prev_day_low,
        )
    else:
        estructura_premarket = None

    # ── Premarket candle levels ───────────────────────────────────────────
    zona_pre_levels: dict = {}
    if pm_high and pm_low:
        zona_pre_levels = _zona_price_levels(pm_high, pm_low)

    # ── Prev day candle levels ────────────────────────────────────────────
    zona_d1_levels = _zona_price_levels(prev_day_high, prev_day_low)

    # ── RTH bars for this day ─────────────────────────────────────────────
    bars_rth = _bars_on_day_rth(bars_1m_all, fecha)
    bars_rth_ms = _bars_to_ms(bars_rth)  # analyzer needs ms timestamps

    # CLV del OR (primer bloque 5min: 09:30–09:34) construido desde 1m.
    or_clv = None
    if bars_rth_ms and len(bars_rth_ms) >= 5:
        first5 = bars_rth_ms[:5]
        or_high = max(b["h"] for b in first5)
        or_low = min(b["l"] for b in first5)
        or_close = float(first5[-1]["c"])
        or_clv = compute_clv_tercio(or_high, or_low, or_close)

    # ── Direction at open (9:30) ──────────────────────────────────────────
    if open_930 is not None:
        dist_open  = round(open_930 - bp, 4)
        direccion  = direction_from_dist(dist_open, int_dist=int_dist)
        dist_pct_open = round(dist_open / bp * 100, 4) if bp else None
    else:
        dist_open  = None
        direccion  = "ZONA_MUERTA"
        dist_pct_open = None

    # ── open_zone ─────────────────────────────────────────────────────────
    if open_930 is not None:
        open_zone, open_pct_rango = compute_open_zone(
            open_930, bp, int_pos, int_neg, direccion, int_dist=int_dist
        )
        open_pct_rango = round(float(open_pct_rango), 4) if open_pct_rango is not None else None
    else:
        open_zone    = None
        open_pct_rango = None

    # ── Estructura vs BP ──────────────────────────────────────────────────
    estructura_prev_day_vs_bp    = _estructura_vs_bp(estructura_prev_day, direccion)
    estructura_premarket_vs_bp   = (
        _estructura_vs_bp(estructura_premarket, direccion)
        if estructura_premarket else None
    )

    # ── Zona en vela (premarket price position) ───────────────────────────
    zona_pre: dict = {}
    if pm_open_price and pm_high and pm_low:
        zona_pre = _zona_en_vela(precio_premarket, pm_open_price, pm_high, pm_low, precio_premarket)

    # ── Zona en vela (open_930 position in prev_day candle) ───────────────
    zona_d1: dict = {}
    if open_930 is not None:
        zona_d1 = _zona_en_vela(open_930, prev_day_open, prev_day_high, prev_day_low, prev_day_close)

    # ── caution_real_930 & caution_cambio_1v3 ────────────────────────────
    caution_real_930   = None
    caution_cambio_1v3 = None
    if open_930 is not None:
        try:
            tfs_df = {
                tf: _bars_list_to_mi_dataframe(all_tf.get(tf, []))
                for tf in ("1m", "5m", "15m", "30m", "1h", "1d")
            }
            tfs_rth_d = build_tfs_rth(tfs_df, ticker)
            caution_real_930 = compute_caution_real_930(
                tfs_df, fecha, open_930, ticker, tfs_rth=tfs_rth_d
            )
            caution_cambio_1v3 = compute_caution_cambio_1v3(
                caution_nota, caution_real_930, ticker, tfs_df
            )
        except Exception as exc:
            logger.warning("%s %s: caution_real_930 error: %s", ticker, fecha, exc)

    # ── Analyze regular session ───────────────────────────────────────────
    anal: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    if bars_rth_ms and open_930 is not None:
        try:
            anal = analyze_regular_session(
                bars_rth=bars_rth_ms,
                bp=bp,
                int_pos=int_pos,
                int_neg=int_neg,
                max_pos=max_pos,
                max_neg=max_neg,
                open_930_preserve=open_930,
            )
            extra = compute_extra_result_fields(
                bars_rth=bars_rth_ms,
                anal=anal,
                direccion=direccion,
                bp=bp,
                int_pos=int_pos,
                int_neg=int_neg,
                max_pos=max_pos,
                max_neg=max_neg,
            )
        except Exception as exc:
            logger.warning("%s %s: analyze_session error: %s", ticker, fecha, exc)

    # ── Convert HH:MM minute strings → integer ────────────────────────────
    minuto_toco_int      = _hhmm_to_minutes(anal.get("minuto_toco_int", ""))
    minuto_toco_max      = _hhmm_to_minutes(anal.get("minuto_toco_max", ""))
    minuto_int_contrario = _hhmm_to_minutes(extra.get("minuto_int_contrario", ""))

    # ── c_int / c_max ratio ───────────────────────────────────────────────
    c_int = anal.get("c_int") if anal.get("c_int") != "" else None
    c_max = anal.get("c_max") if anal.get("c_max") != "" else None
    c_int_c_max_ratio = (
        round(float(c_int) / float(c_max), 4)
        if c_int and c_max and float(c_max) != 0
        else None
    )

    # ── Premium evaluation ────────────────────────────────────────────────
    td_premium = {
        "ticker":            ticker,
        "direction":         direccion,
        "badge_long":        badge_long,
        "signals_3_9":       signals_str,
        "caution_note":      caution_nota,
        "gap_type":          gap_tipo,
        "gap_pct":           gap_mag,
        "prev_day_change":   prev_day_change,
        "dist":              dist_open or 0.0,
        "open_zone":         open_zone,
        "caution_cambio_1v3": caution_cambio_1v3,
    }
    try:
        premium_ev    = PremiumDetector.evaluate(ticker, td_premium)
        premium_label = premium_ev.get("tier", "STANDARD")
        premium_score = float(premium_ev.get("score", 0.0) or 0.0)
    except Exception:
        premium_label = "STANDARD"
        premium_score = 0.0

    # ── Coerce booleans (dc2 funcs may return 'Sí'/'No' strings) ─────────
    skip_flag = _to_bool(skip_flag)
    bb_expande_premarket = _to_bool(ec.get("bb_expande_premarket"))
    toco_int = _to_bool(anal.get("toco_int"))
    toco_max = _to_bool(anal.get("toco_max"))
    toco_bp_antes_int = _to_bool(anal.get("toco_bp_antes_int"))
    cerro_bp = _to_bool(anal.get("cerro_bp"))
    int_contrario_tocado = _to_bool(extra.get("int_contrario_tocado"))
    segundo_toque_int = _to_bool(extra.get("segundo_toque_int"))

    ec_score = ec.get("ec_score")
    ec_accion = ec.get("ec_accion")
    ec_size = ec.get("ec_size")

    # ── Build dash dict ───────────────────────────────────────────────────
    dash: dict[str, Any] = {
        "gap_tipo":               gap_tipo,
        "gap_mag":                gap_mag,
        "prev_day_high":          prev_day_high,
        "prev_day_low":           prev_day_low,
        "prev_day_close":         prev_day_close,
        "prev_day_change":        prev_day_change,
        "prev_day_open":          prev_day_open,
        "pm_high":                 pm_high,
        "pm_low":                  pm_low,
        "prev_prev_close":        prev_prev_close,
        "prev_prev_high":         prev_prev_high,
        "prev_prev_low":          prev_prev_low,
        "rango_d1":               rango_d1,
        "rango_d2":               rango_d2,
        "rango_d3":               rango_d3,
        "avg_3d":                 avg_3d,
        "rango_raw":              rango_raw,
        "rango_usado":            rango_usado,
        "bp":                     bp,
        "int_pos":                int_pos,
        "int_neg":                int_neg,
        "max_pos":                max_pos,
        "max_neg":                max_neg,
        "pre_mkt_bbt_1h":         pre_mkt_bbt_1h,
        "pre_mkt_bbb_1h":         pre_mkt_bbb_1h,
        "market_close_bbt_1h":    market_close_bbt_1h,
        "market_close_bbb_1h":    market_close_bbb_1h,
        "bbt_5m":                 bbt_5m,
        "bbb_5m":                 bbb_5m,
        "bbt_clf_5m":             bbt_clf_5m,
        "bbb_clf_5m":             bbb_clf_5m,
        "bb_expande_premarket":   bb_expande_premarket,
        "ema20_pre":              ec.get("ema20_pre"),
        "ema50_pre":              ec.get("ema50_pre"),
        "ema200_pre":             ec.get("ema200_pre"),
        "ema_align":              ec.get("ema_align"),
        "ema_sr":                 ec.get("ema_sr"),
        "ec_score":               ec_score,
        "ec_accion":              ec_accion,
        "ec_size":                ec_size,
        "badge_long":             badge_long,
        "rango_pm":               rango_pm_d.get("label"),
        "rango_pm_pct":           rango_pm_d.get("pct"),
        "rango_prev_day":         rango_pd_d.get("label"),
        "rango_prev_day_pct":     rango_pd_d.get("pct"),
        "signals_3_9":            signals_str,
        "caution_nota":           caution_nota,
        "n_cautiones":            n_cautiones,
        "skip":                   skip_flag,
        "skip_reason":            skip_reason,
        "prev_day_hint":          prev_day_hint,
        "premium_label":          premium_label,
        "premium_score":          premium_score,
    }

    # ── Build res dict ────────────────────────────────────────────────────
    res_skip   = skip_flag or (open_930 is None)
    res_reason = skip_reason or ("sin_open_930" if open_930 is None else None)

    res: dict[str, Any] = {
        "bp":                           bp,
        "open_930":                     open_930,
        "dist":                         dist_open,
        "dist_pct":                     dist_pct_open,
        "direccion":                    direccion,
        "open_zone":                    open_zone,
        "open_pct_en_rango":            open_pct_rango,
        "prev_clv":                     prev_clv,
        "or_clv":                       or_clv,
        "estructura_prev_day":          estructura_prev_day,
        "estructura_vs_bp":             estructura_prev_day_vs_bp,
        "estructura_premarket":         estructura_premarket,
        "estructura_premarket_vs_bp":   estructura_premarket_vs_bp,
        "pct_en_vela_pre":              zona_pre.get("pct"),
        "zona_precio_en_vela_pre":      zona_pre.get("zona"),
        "pct_en_vela_d1":               zona_d1.get("pct"),
        "zona_precio_en_vela_d1":       zona_d1.get("zona"),
        "resistencia_pre":              zona_pre_levels.get("resistencia"),
        "correccion_pre":               zona_pre_levels.get("correccion"),
        "soporte_pre":                  zona_pre_levels.get("soporte"),
        "resistencia_d1":               zona_d1_levels.get("resistencia"),
        "correccion_d1":                zona_d1_levels.get("correccion"),
        "soporte_d1":                   zona_d1_levels.get("soporte"),
        "c_int":                        c_int,
        "c_max":                        c_max,
        "c_int_c_max_ratio":            c_int_c_max_ratio,
        "day_change":                   anal.get("day_change"),
        "day_high":                     anal.get("day_high"),
        "day_low":                      anal.get("day_low"),
        "toco_int":                     toco_int,
        "toco_int_dir":                 anal.get("toco_int_dir"),
        "minuto_toco_int":              minuto_toco_int,
        "toco_max":                     toco_max,
        "toco_max_dir":                 anal.get("toco_max_dir"),
        "minuto_toco_max":              minuto_toco_max,
        "toco_bp_antes_int":            toco_bp_antes_int,
        "primer_movimiento":            anal.get("primer_movimiento"),
        "cerro_bp":                     cerro_bp,
        "int_contrario_tocado":         int_contrario_tocado,
        "minuto_int_contrario":         minuto_int_contrario,
        "precio_tras_bp_min":           extra.get("precio_tras_bp_min"),
        "precio_tras_bp_max":           extra.get("precio_tras_bp_max"),
        "tramo_bp_a_int_minutos":       extra.get("tramo_bp_a_int_minutos"),
        "rebote_post_int_cents":        extra.get("rebote_post_int_cents"),
        "retrace_antes_int":          extra.get("retrace_antes_int"),
        "segundo_toque_int":            segundo_toque_int,
        "caution_real_930":             caution_real_930,
        "caution_cambio_1v3":           caution_cambio_1v3,
        "skip":                         res_skip,
        "razon_skip":                   res_reason,
    }

    return dash, res
