from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

import pandas as pd
import pytz

from dc2.data_clients import BPCalculator, TargetsCalculator
from dc2.indicators import (
    compute_badge_long,
    compute_open_zone,
    compute_signals_3_9,
    conviction_score,
    direction_from_dist,
    ema_alignment,
)
from dc2.utils import _filter_bars_before

from dc2.macro_futures.config import FUTURES, VIX_SYMBOL
from dc2.macro_futures.data_client import FuturesDataClient

logger = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")
_UTC = pytz.UTC

ZONA_MAP: dict[str, str] = {
    "normal_t1": "T1",
    "normal_t2": "T2",
    "normal_t3": "T3",
    "ext_int_max": "EXT",
    "doble_dir_zone": "ZM",
}


def _macro_row_nd(
    now: datetime,
    *,
    price: float | None,
    session_open: datetime,
    session_high: float | None = None,
    session_low: float | None = None,
    prev_session_high: float | None = None,
    prev_session_low: float | None = None,
) -> dict[str, Any]:
    """Fila macro cuando no hay BP o datos insuficientes (misma forma que el dict con bp=None)."""
    return {
        "price": None if price is None else round(float(price), 4),
        "bp": None,
        "session_open": session_open.strftime("%Y-%m-%d %H:%M ET"),
        "session_high": session_high,
        "session_low": session_low,
        "prev_session_high": prev_session_high,
        "prev_session_low": prev_session_low,
        "int_pos": None,
        "int_neg": None,
        "max_pos": None,
        "max_neg": None,
        "direction": "N/D",
        "zona": "N/D",
        "dist_overnight": None,
        "dist_overnight_pct": None,
        "badge": None,
        "signals_3_9": "",
        "ema_align": "N/D",
        "ec_score": None,
        "ec_accion": "N/D",
        "gap_tipo": None,
        "gap_mag": None,
        "timestamp": now.isoformat(),
    }


def _fetch_vix_dict() -> dict[str, Any]:
    import yfinance as yf

    try:
        vix_data = yf.download(
            VIX_SYMBOL,
            period="5d",
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
    except Exception as e:
        logger.warning("VIX: descarga falló: %s", e)
        return {"spot": None, "delta": None, "dir": "N/D", "nivel": "N/D"}

    if vix_data is None or getattr(vix_data, "empty", False):
        return {"spot": None, "delta": None, "dir": "N/D", "nivel": "N/D"}

    close = vix_data["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = pd.Series(close, dtype=float).dropna()
    if close.empty:
        return {"spot": None, "delta": None, "dir": "N/D", "nivel": "N/D"}

    vix_spot = float(close.iloc[-1])
    vix_prev = float(close.iloc[-2]) if len(close) >= 2 else vix_spot
    vix_delta = round(vix_spot - vix_prev, 2)
    vix_dir = (
        "subiendo"
        if vix_delta > 0.5
        else ("bajando" if vix_delta < -0.5 else "lateral")
    )
    vix_nivel = (
        "panico"
        if vix_spot > 30
        else (
            "elevado"
            if vix_spot > 20
            else ("neutro" if vix_spot > 15 else "compresion")
        )
    )
    return {"spot": vix_spot, "delta": vix_delta, "dir": vix_dir, "nivel": vix_nivel}


def _get_session_open(symbol: str, now: datetime) -> datetime:
    """
    ES / NQ / YM (CME Globex): sesión abre a las 18:00 ET (domingo abre la semana;
        viernes cierra ~17:00). No hay apertura sábado 18:00 ET: si el día
        candidato cae en sábado o domingo, se retrocede hasta un día hábil.

    BTC (Binance): sesión = 00:00 UTC diario (equivale a ~20:00 ET en EDT).
    """
    if now.tzinfo is None:
        now_et = _ET.localize(now)
    else:
        now_et = now.astimezone(_ET)

    if symbol == "BTC":
        now_utc = now_et.astimezone(_UTC)
        d_utc = now_utc.date()
        session_open_utc = _UTC.localize(datetime.combine(d_utc, time(0, 0, 0)))
        return session_open_utc.astimezone(_ET)

    d = now_et.date()
    today_1800 = _ET.localize(datetime.combine(d, time(18, 0, 0)))
    if now_et >= today_1800:
        candidate = d
    else:
        candidate = d - timedelta(days=1)
    while candidate.weekday() == 5:  # solo sábado
        candidate -= timedelta(days=1)
    return _ET.localize(datetime.combine(candidate, time(18, 0, 0)))


def _ec_accion(ec_score: float) -> str:
    if ec_score < 0:
        return "BLOQUEADO"
    if ec_score >= 7.0:
        return "PREMIUM"
    if ec_score >= 5.0:
        return "OPERABLE"
    return "SKIP"


def _ema20_50_200_1h(
    bars_1h: list[dict], cutoff_time: datetime
) -> tuple[float, float, float, float]:
    filt = _filter_bars_before(bars_1h, cutoff_time)
    if len(filt) < 3:
        c = float(filt[-1]["c"]) if filt else 0.0
        return c, c, c, c
    closes = pd.Series([float(b["c"]) for b in filt], dtype=float)
    e20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
    e50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1])
    e200 = float(closes.ewm(span=200, adjust=False).mean().iloc[-1])
    return float(filt[-1]["c"]), e20, e50, e200


def _gap_and_prev_day(
    bars_1d: list[dict], cutoff_time: datetime
) -> tuple[str, float, float]:
    daily = _filter_bars_before(bars_1d, cutoff_time)
    if len(daily) < 2:
        return "FLAT", 0.0, 0.0
    open_hoy = float(daily[-1]["o"])
    close_ayer = float(daily[-2]["c"])
    if close_ayer == 0:
        return "FLAT", 0.0, 0.0
    gap_pct = (open_hoy - close_ayer) / close_ayer * 100.0
    gap_mag = abs(gap_pct)
    gap_tipo = "GAP_UP" if gap_pct > 0.05 else ("GAP_DOWN" if gap_pct < -0.05 else "FLAT")

    prev_day_pct = 0.0
    if len(daily) >= 3:
        c_ayer = float(daily[-2]["c"])
        c_ante = float(daily[-3]["c"])
        if c_ante:
            prev_day_pct = (c_ayer - c_ante) / c_ante * 100.0

    return gap_tipo, gap_mag, prev_day_pct


def _cutoff_bp_to_datetime(cutoff_bp: pd.Timestamp) -> datetime:
    dt = cutoff_bp.to_pydatetime()
    if dt.tzinfo is None:
        return _ET.localize(dt)
    return dt.astimezone(_ET)


def _session_1m_high_low(
    bars_1m: list[dict],
    session_open: datetime,
) -> tuple[float | None, float | None, float | None, float | None]:
    """
    session: t >= session_open
    prev_session: session_open - 24h <= t < session_open
    """
    so = pd.Timestamp(session_open)
    if so.tzinfo is None:
        so = so.tz_localize(_ET)
    else:
        so = so.tz_convert(_ET)
    prev_start = so - pd.Timedelta(hours=24)

    session_bars: list[dict] = []
    prev_bars: list[dict] = []
    for b in bars_1m:
        t = b["t"]
        if not isinstance(t, pd.Timestamp):
            t = pd.Timestamp(t)
        if t.tzinfo is None:
            t = t.tz_localize(_ET)
        else:
            t = t.tz_convert(_ET)
        if t >= so:
            session_bars.append(b)
        elif prev_start <= t < so:
            prev_bars.append(b)

    if not session_bars:
        session_high, session_low = None, None
    else:
        session_high = max(float(x["h"]) for x in session_bars)
        session_low = min(float(x["l"]) for x in session_bars)

    if not prev_bars:
        prev_session_high, prev_session_low = None, None
    else:
        prev_session_high = max(float(x["h"]) for x in prev_bars)
        prev_session_low = min(float(x["l"]) for x in prev_bars)

    return session_high, session_low, prev_session_high, prev_session_low


def run_macro_analysis(lookback_days: int = 5) -> dict[str, Any]:
    """
    BP congelado: BPCalculator con cutoff_bp = session_open + 1 min (snapshot al nacer la sesión).
    Badge / signals_3_9 / ema_alignment usan now (estado actual).

    Retorno: ``{"futures": {símbolo: fila}, "vix": {spot, delta, dir, nivel}}``.
    """
    client = FuturesDataClient()
    all_bars = client.fetch_all_bars(lookback_days=lookback_days)
    now = datetime.now(_ET)

    out: dict[str, Any] = {}

    for symbol in FUTURES:
        bars_dict: dict[str, list] = all_bars.get(symbol, {})
        bars_1m = bars_dict.get("1m") or []
        if not bars_1m:
            if symbol in ("TLT", "GLD"):
                so = _get_session_open(symbol, now)
                out[symbol] = _macro_row_nd(
                    now,
                    price=None,
                    session_open=so,
                )
            else:
                logger.warning("[%s] Sin barras 1m — omitido", symbol)
            continue

        precio_actual = float(bars_1m[-1]["c"])

        session_open = _get_session_open(symbol, now)
        session_high, session_low, prev_session_high, prev_session_low = (
            _session_1m_high_low(bars_1m, session_open)
        )

        cutoff_bp = pd.Timestamp(session_open + timedelta(minutes=1))
        if cutoff_bp.tz is None:
            cutoff_bp = cutoff_bp.tz_localize(_ET)
        else:
            cutoff_bp = cutoff_bp.tz_convert(_ET)

        bp = BPCalculator.calculate_bp(bars_dict, cutoff_time=cutoff_bp)
        cutoff_bp_dt = _cutoff_bp_to_datetime(cutoff_bp)

        if bp is None:
            logger.warning(
                "[%s] BP=None — sin barras en TFs al cutoff_bp %s",
                symbol,
                cutoff_bp_dt.strftime("%Y-%m-%d %H:%M ET"),
            )
            out[symbol] = _macro_row_nd(
                now,
                price=precio_actual,
                session_open=session_open,
                session_high=session_high,
                session_low=session_low,
                prev_session_high=prev_session_high,
                prev_session_low=prev_session_low,
            )
            continue

        try:
            bp_f = float(bp)

            daily_for_targets = _filter_bars_before(
                bars_dict.get("1d", []), cutoff_bp_dt
            )
            range_3d = TargetsCalculator.calculate_range_3d(
                symbol, daily_for_targets
            )
            targets = TargetsCalculator.calculate_targets(bp_f, range_3d)

            dist = precio_actual - bp_f
            direction = direction_from_dist(
                dist, float(targets.get("int_dist", 0.0))
            )

            int_dist_val = float(targets.get("int_dist", 0.0))
            open_bars = [b for b in bars_1m if b["t"] >= session_open]
            session_open_price = (
                float(open_bars[0]["o"]) if open_bars else precio_actual
            )
            open_zone, _open_pct = compute_open_zone(
                open_930=session_open_price,
                bp=bp_f,
                int_pos=float(targets["int_pos"]),
                int_neg=float(targets["int_neg"]),
                direction=direction,
                int_dist=int_dist_val,
            )
            zona_label = ZONA_MAP.get(open_zone or "", "??")

            dist_overnight = round(dist, 4)
            dist_overnight_pct = (
                round((precio_actual - bp_f) / bp_f * 100, 3) if bp_f else 0.0
            )

            badge = compute_badge_long(
                bars_dict,
                cutoff_time=cutoff_bp_dt,
                now_time=now,
                precio=precio_actual,
            )
            signals_3_9 = compute_signals_3_9(bars_dict, now)

            _, e20, e50, e200 = _ema20_50_200_1h(bars_dict.get("1h", []), now)
            ema_align = ema_alignment(precio_actual, e20, e50, e200)

            gap_tipo, gap_mag, prev_day_pct = _gap_and_prev_day(
                bars_dict.get("1d", []), cutoff_bp_dt
            )

            ec = conviction_score(
                direction,
                gap_tipo,
                gap_mag,
                prev_day_pct,
                ema_align,
                ticker=symbol,
            )

            out[symbol] = {
                "price": round(precio_actual, 4),
                "bp": round(bp_f, 4),
                "session_open": session_open.strftime("%Y-%m-%d %H:%M ET"),
                "session_high": session_high,
                "session_low": session_low,
                "prev_session_high": prev_session_high,
                "prev_session_low": prev_session_low,
                "int_pos": round(float(targets["int_pos"]), 4),
                "int_neg": round(float(targets["int_neg"]), 4),
                "max_pos": round(float(targets["max_pos"]), 4),
                "max_neg": round(float(targets["max_neg"]), 4),
                "direction": direction,
                "zona": zona_label,
                "dist_overnight": dist_overnight,
                "dist_overnight_pct": dist_overnight_pct,
                "badge": round(float(badge), 2),
                "signals_3_9": signals_3_9,
                "ema_align": ema_align,
                "ec_score": float(ec),
                "ec_accion": _ec_accion(ec),
                "gap_tipo": gap_tipo if symbol != "BTC" else None,
                "gap_mag": round(gap_mag, 3) if symbol != "BTC" else None,
                "timestamp": now.isoformat(),
            }
        except Exception as e:
            if symbol in ("TLT", "GLD"):
                logger.warning("[%s] error en pipeline macro: %s", symbol, e)
                out[symbol] = _macro_row_nd(
                    now,
                    price=precio_actual,
                    session_open=session_open,
                    session_high=session_high,
                    session_low=session_low,
                    prev_session_high=prev_session_high,
                    prev_session_low=prev_session_low,
                )
            else:
                raise

    vix = _fetch_vix_dict()
    return {"futures": out, "vix": vix}
