"""Métricas: EMAs mensuales/diarias, BP 20D, volumen USDT, 1h/15m."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

LOOKBACK_BP_DAYS = 20


def stub_intraday_for_solo_1d(last_close: float, *, n: int = 150) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Velas sintéticas alcistas suaves para que pasen checks intradía en modo debug."""
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=n, freq="h")
    ramp = np.linspace(last_close * 0.98, last_close * 1.02, n)
    h1 = pd.DataFrame(
        {"open": ramp, "high": ramp * 1.001, "low": ramp * 0.999, "close": ramp, "volume": 1.0},
        index=idx,
    )
    idx15 = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=n * 4, freq="15min")
    r15 = np.linspace(last_close * 0.98, last_close * 1.02, len(idx15))
    m15 = pd.DataFrame(
        {
            "open": r15,
            "high": r15 * 1.001,
            "low": r15 * 0.999,
            "close": r15,
            "volume": 1.0,
        },
        index=idx15,
    )
    return h1, m15


MIN_MONTHLY_BARS = 20


@dataclass
class CryptoRow:
    symbol: str
    close_1d: float
    ema3_1d: float
    ema9_1d: float
    ema20_1d: float
    ema3_monthly: float
    ema9_monthly: float
    ema20_monthly: float
    monthly_ok: bool
    resistencia_1d: float  # max high 20d
    soporte_1d: float  # min low 20d
    bp_long: float
    bp_short: float
    volume_usdt_24h: float
    avg_volume_usdt_20d: float
    close_1h: float
    ema20_1h: float
    ema9_15m: float
    ema20_15m: float
    prev_close_1d: float  # para gap en open_signal


def _quote_volume_series(df: pd.DataFrame) -> pd.Series:
    return (df["close"].astype(float) * df["volume"].astype(float)).fillna(0.0)


def compute_crypto_row(
    symbol: str,
    dfs: dict[str, pd.DataFrame],
    *,
    solo_1d: bool = False,
) -> CryptoRow | None:
    """
    ``dfs`` debe incluir ``1d``; ``1h`` y ``15m`` con índice UTC salvo ``solo_1d``
    (relleno sintético solo para métricas; los filtros intradía se omiten en filters).
    """
    d1 = dfs.get("1d")
    h1 = dfs.get("1h")
    m15 = dfs.get("15m")
    if d1 is None or d1.empty or len(d1) < 30:
        return None
    if solo_1d and (h1 is None or h1.empty or m15 is None or m15.empty):
        lc = float(d1["close"].astype(float).iloc[-1])
        h1, m15 = stub_intraday_for_solo_1d(lc)
    if h1 is None or h1.empty or m15 is None or m15.empty:
        return None

    close = d1["close"].astype(float)
    high = d1["high"].astype(float)
    low = d1["low"].astype(float)
    if close.isna().all():
        return None

    cl = float(close.iloc[-1])
    prev_cl = float(close.iloc[-2]) if len(close) >= 2 else cl

    ema3_1d = float(close.ewm(span=3, adjust=False).mean().iloc[-1])
    ema9_1d = float(close.ewm(span=9, adjust=False).mean().iloc[-1])
    ema20_1d = float(close.ewm(span=20, adjust=False).mean().iloc[-1])

    monthly_close = close.resample("ME").last().dropna()
    monthly_ok = len(monthly_close) >= MIN_MONTHLY_BARS
    if monthly_ok:
        ema3_m = monthly_close.ewm(span=3, adjust=False).mean()
        ema9_m = monthly_close.ewm(span=9, adjust=False).mean()
        ema20_m = monthly_close.ewm(span=20, adjust=False).mean()
        ema3_monthly = float(ema3_m.iloc[-1])
        ema9_monthly = float(ema9_m.iloc[-1])
        ema20_monthly = float(ema20_m.iloc[-1])
        if any(np.isnan(x) for x in (ema3_monthly, ema9_monthly, ema20_monthly)):
            monthly_ok = False
    else:
        ema3_monthly = float("nan")
        ema9_monthly = float("nan")
        ema20_monthly = float("nan")

    # Excluye el día actual: resistencia/soporte sobre los 20D previos al cierre de hoy
    tail = d1.iloc[-LOOKBACK_BP_DAYS - 1 : -1]
    resistencia_1d = float(tail["high"].astype(float).max())
    soporte_1d = float(tail["low"].astype(float).min())

    qv = _quote_volume_series(d1)
    volume_usdt_24h = float(qv.iloc[-1]) if len(qv) else float("nan")
    if len(qv) >= 22:
        avg_volume_usdt_20d = float(qv.iloc[-21:-1].mean())
    elif len(qv) >= 2:
        avg_volume_usdt_20d = float(qv.iloc[:-1].tail(20).mean())
    else:
        avg_volume_usdt_20d = float("nan")

    c1h = h1["close"].astype(float)
    if c1h.empty or c1h.isna().all():
        return None
    close_1h = float(c1h.iloc[-1])
    ema20_1h = float(c1h.ewm(span=20, adjust=False).mean().iloc[-1])

    c15 = m15["close"].astype(float)
    if c15.empty or len(c15) < 20:
        return None
    ema9_15m = float(c15.ewm(span=9, adjust=False).mean().iloc[-1])
    ema20_15m = float(c15.ewm(span=20, adjust=False).mean().iloc[-1])

    return CryptoRow(
        symbol=symbol,
        close_1d=cl,
        ema3_1d=ema3_1d,
        ema9_1d=ema9_1d,
        ema20_1d=ema20_1d,
        ema3_monthly=ema3_monthly,
        ema9_monthly=ema9_monthly,
        ema20_monthly=ema20_monthly,
        monthly_ok=monthly_ok,
        resistencia_1d=resistencia_1d,
        soporte_1d=soporte_1d,
        bp_long=resistencia_1d,
        bp_short=soporte_1d,
        volume_usdt_24h=volume_usdt_24h,
        avg_volume_usdt_20d=avg_volume_usdt_20d,
        close_1h=close_1h,
        ema20_1h=ema20_1h,
        ema9_15m=ema9_15m,
        ema20_15m=ema20_15m,
        prev_close_1d=prev_cl,
    )


def strength_score_long(r: CryptoRow) -> float:
    """Score para INDECISO: distancia relativa por encima de resistencia/BP."""
    if r.resistencia_1d <= 0:
        return 0.0
    return (r.close_1d - r.resistencia_1d) / r.resistencia_1d


def strength_score_short(r: CryptoRow) -> float:
    if r.soporte_1d <= 0:
        return 0.0
    return (r.soporte_1d - r.close_1d) / r.soporte_1d
