from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from dc2.constants import N_VELAS_BB, TIMEZONE

import logging

logger = logging.getLogger(__name__)


def _bars_df_to_records(df: pd.DataFrame) -> list[dict]:
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
    """Filter bars with ts strictly less than cutoff_time."""
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


def _compute_bb_from_closes(closes: list) -> tuple[float | None, float | None]:
    """Returns (bbt, bbb) using EMA-BB(20,2) — exponential mean and std (ewm, adjust=False)."""
    if len(closes) < N_VELAS_BB:
        return None, None
    close = pd.Series([float(x) for x in closes[-N_VELAS_BB:]], dtype=float)
    mid = float(close.ewm(span=N_VELAS_BB, adjust=False).mean().iloc[-1])
    std = float(close.ewm(span=N_VELAS_BB, adjust=False).std().iloc[-1])
    if np.isnan(mid) or np.isnan(std):
        return None, None
    return mid + 2 * std, mid - 2 * std
