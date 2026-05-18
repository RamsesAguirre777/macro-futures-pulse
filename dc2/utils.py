"""Utilidades portadas de data_collector.py — misma lógica, imports a dc2.constants."""
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from dc2.constants import N_VELAS_BB, TIMEZONE

import logging

logger = logging.getLogger(__name__)


def _bars_list_to_mi_dataframe(bars: list) -> pd.DataFrame:
    """Convierte barras Alpaca (dict con t,o,h,l,c,v) al formato esperado por mi_backtesting_nvda."""
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(
        [
            {
                "timestamp": b["t"],
                "open": b["o"],
                "high": b["h"],
                "low": b["l"],
                "close": b["c"],
                "volume": b["v"],
            }
            for b in bars
        ]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(TIMEZONE)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(TIMEZONE)
    df["date_et"] = df["timestamp"].dt.date
    return df

def _get_igual_baj_rate(ticker: str) -> str:
    """INT rate de igual BAJ por ticker para warnings del dashboard.
    TODO: calibrar con datos reales cuando se tengan parquets de SQQQ/TQQQ/TSLL/NVDL.
    Los tickers legacy (SPY/MSFT/AMZN/GLD) conservan sus valores calibrados.
    """
    rates = {"SPY": "65.8", "MSFT": "65.7", "AMZN": "67.1", "GLD": "63.2"}
    return rates.get(ticker, "~66")

def _bars_df_to_records(df: pd.DataFrame) -> list[dict]:
    """DF(index=t) -> list[dict] con llave 't'."""
    if df is None or df.empty:
        return []
    if df.index.name != "t":
        df = df.copy()
        df.index.name = "t"
    out = df.reset_index()
    if "t" not in out.columns:
        return []
    cols = [c for c in ["t", "o", "h", "l", "c", "v"] if c in out.columns]
    return out[cols].to_dict("records")


def _filter_bars_before(bars, cutoff_time: datetime) -> list:
    """Filtra barras con ts estrictamente menor a cutoff_time.

    Acepta lista de dicts o DataFrame indexado por timestamp.
    """
    if isinstance(bars, pd.DataFrame):
        cutoff = pd.Timestamp(cutoff_time)
        if cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize(TIMEZONE)
        else:
            cutoff = cutoff.tz_convert(TIMEZONE)
        sliced = bars.loc[: cutoff - pd.Timedelta(nanoseconds=1)]
        return _bars_df_to_records(sliced)
    out = []
    for b in bars:
        ts = b["t"]
        if ts.tzinfo is None:
            ts = pd.Timestamp(ts).tz_localize(TIMEZONE)
        if ts < cutoff_time:
            out.append(b)
    out.sort(key=lambda x: (
        pd.Timestamp(x["t"]).tz_convert(TIMEZONE)
        if pd.Timestamp(x["t"]).tzinfo
        else pd.Timestamp(x["t"]).tz_localize(TIMEZONE)
    ))
    return out

def _filter_bars_upto_inclusive(bars, cutoff_time: datetime) -> list:
    """Filtra barras con ts <= cutoff_time (incluye el corte 16:00).

    Acepta lista de dicts o DataFrame indexado por timestamp.
    """
    if isinstance(bars, pd.DataFrame):
        cutoff = pd.Timestamp(cutoff_time)
        if cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize(TIMEZONE)
        else:
            cutoff = cutoff.tz_convert(TIMEZONE)
        sliced = bars.loc[:cutoff]
        return _bars_df_to_records(sliced)
    out = []
    for b in bars:
        ts = b["t"]
        if ts.tzinfo is None:
            ts = pd.Timestamp(ts).tz_localize(TIMEZONE)
        if ts <= cutoff_time:
            out.append(b)
    out.sort(key=lambda x: (
        pd.Timestamp(x["t"]).tz_convert(TIMEZONE)
        if pd.Timestamp(x["t"]).tzinfo
        else pd.Timestamp(x["t"]).tz_localize(TIMEZONE)
    ))
    return out

def _test_cutoff_from_1m(bars_dict: dict) -> tuple[datetime, str]:
    """
    Modo test: cutoff = última barra 1m disponible (no 09:28 fijo).
    _filter_bars_before usa ts < cutoff; devolvemos cutoff = última_ts + 1s
    para incluir esa barra. Retorna (cutoff_para_bp, etiqueta legible ET).
    """
    bars_1m = bars_dict.get("1m", [])
    if not bars_1m:
        now = datetime.now(TIMEZONE).replace(second=0, microsecond=0)
        return now + timedelta(seconds=1), now.strftime("%Y-%m-%d %H:%M ET")
    sorted_bars = sorted(bars_1m, key=lambda x: x["t"])
    ts = pd.Timestamp(sorted_bars[-1]["t"])
    if ts.tzinfo is None:
        ts = ts.tz_localize(TIMEZONE)
    else:
        ts = ts.tz_convert(TIMEZONE)
    ts = ts.replace(second=0, microsecond=0)
    cutoff_bp = (ts + pd.Timedelta(seconds=1)).to_pydatetime()
    label = ts.strftime("%Y-%m-%d %H:%M ET")
    return cutoff_bp, label

def _validate_premarket_bars(
    ticker: str,
    bars_1m: list,
    cutoff_et: datetime,
    min_bars: int = 5,
) -> bool:
    """
    Verifica que haya al menos min_bars velas de 1m
    antes de cutoff_et. Si no, loguea advertencia y
    retorna False para que el ticker sea skipped.
    """
    filtered = _filter_bars_before(bars_1m, cutoff_et)
    if len(filtered) < min_bars:
        logger.warning(
            "%s: solo %d barras 1m premarket disponibles (mín %d). "
            "BP de este ticker puede ser impreciso.",
            ticker,
            len(filtered),
            min_bars,
        )
        return len(filtered) >= 2  # mínimo absoluto para BP
    return True

def _compute_bb_from_closes(closes: list) -> tuple[float | None, float | None]:
    """Devuelve (bbt, bbb) con EMA-BB(20,2) — media y std exponenciales (ewm, adjust=False)."""
    if len(closes) < N_VELAS_BB:
        return None, None
    close = pd.Series([float(x) for x in closes[-N_VELAS_BB:]], dtype=float)
    mid = float(close.ewm(span=N_VELAS_BB, adjust=False).mean().iloc[-1])
    std = float(close.ewm(span=N_VELAS_BB, adjust=False).std().iloc[-1])
    if np.isnan(mid) or np.isnan(std):
        return None, None
    return mid + 2 * std, mid - 2 * std

def _ensure_outputs_dir() -> None:
    Path("outputs").mkdir(parents=True, exist_ok=True)

def _signals_display(ticker_data: dict) -> str:
    return ticker_data.get("signals_3_9") or ticker_data.get("signals", "")

